"""Atomic child filing and mutation fencing for hierarchical delivery."""

from __future__ import annotations

import inspect
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import fields
from typing import Any

from sqlalchemy import and_, insert, select, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    sessions,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    task_session_attempts,
    tasks,
    workspaces,
)
from src.git.manager import is_valid_git_oid
from src.integration.models import BranchKey
from src.integration.outbox import enqueue_integration_event
from src.integration.ownership import BranchOwnership
from src.integration.parent_completion import ParentCompletion
from src.models import RepoConfig, Task, TaskStatus
from src.task_names import child_task_id, fresh_root_id

_OID = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_BATCH_STATES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)
DefaultHeadResolver = Callable[[RepoConfig, str], Awaitable[str] | str]
BranchMaterializer = Callable[[RepoConfig, str, str], Awaitable[str] | str]
CheckpointVerifier = Callable[[dict, RepoConfig, str], Awaitable[str] | str]
AncestryVerifier = Callable[[RepoConfig, str, str], Awaitable[bool] | bool]


async def materialize_exact_branch(git, checkout: str, branch: str, base_sha: str) -> str:
    """Create *branch* at *base_sha*, refusing any unexpected existing tip."""
    from src.git.manager import GitError, RemoteRefState

    if not _OID.fullmatch(base_sha):
        raise HierarchyError("invalid", "materialization base is not an exact Git OID")
    remote = await git.als_remote_ref(checkout, branch)
    if remote.state is RemoteRefState.ERROR:
        raise GitError(remote.error or "remote branch state is unknown")
    if remote.state is RemoteRefState.PRESENT:
        if remote.oid != base_sha:
            raise HierarchyError(
                "delivery_target_fixed", "branch exists at an unexpected commit"
            )
        return base_sha
    # Retained repositories outlive filing: ls-remote can resolve a newer
    # base without bringing its commit object into the local object store.
    present = await git.arun_git_result(
        ["cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=checkout, lock_held=True
    )
    if present.returncode != 0:
        fetched = await git.arun_git_result(
            ["fetch", "--no-tags", "origin", base_sha], cwd=checkout, lock_held=True
        )
        if fetched.returncode != 0:
            raise GitError(fetched.stderr or "could not fetch pinned materialization base")
    try:
        await git.apush_validated_ref(checkout, base_sha, branch)
    except GitError:
        # A concurrent creator may have won.  Only its exact pinned result is
        # idempotent success; any other result remains a hard refusal.
        raced = await git.als_remote_ref(checkout, branch)
        if raced.state is RemoteRefState.PRESENT and raced.oid == base_sha:
            return base_sha
        raise
    confirmed = await git.als_remote_ref(checkout, branch)
    if confirmed.state is not RemoteRefState.PRESENT or confirmed.oid != base_sha:
        raise HierarchyError("invalid", "remote did not confirm the pinned branch ref")
    return base_sha


#: Which of :func:`resolve_workspace_checkpoint`'s preconditions failed, and
#: whether the agent holding the workspace can do anything about it.  The
#: refusal used to collapse all four into one sentence naming the workspace,
#: which read as a lock problem even when the only missing thing was the
#: task's ``branch_name``.  ``fixable`` distinguishes "fix your git state and
#: close again" from "this is daemon state; escalate".
WORKSPACE_PRECONDITIONS = {
    "no_integration_workspace": (
        "task holds no integration workspace",
        "operator",
    ),
    "workspace_not_owned": (
        "the integration workspace is locked by another task",
        "operator",
    ),
    "repo_mismatch": (
        "task is not bound to the delivery repository",
        "operator",
    ),
    "branch_not_recorded": (
        "task has no branch_name recorded",
        "worker",
    ),
}


def _workspace_precondition(precondition: str, detail: str, **context) -> HierarchyError:
    """Build the refusal for one named ``resolve_workspace_checkpoint`` gate."""
    _summary, fixable = WORKSPACE_PRECONDITIONS[precondition]
    return HierarchyError(
        "dirty",
        detail,
        {"precondition": precondition, "fixable_by": fixable, **context},
    )


async def resolve_workspace_checkpoint(db, git, task: dict, repo: RepoConfig) -> str:
    """Return an owned writer workspace's clean, exactly-pushed current HEAD."""
    workspace = await db.get_workspace_for_task(task["id"])
    if workspace is None:
        raise _workspace_precondition(
            "no_integration_workspace",
            f"task {task['id']} holds no integration workspace",
            task_id=task["id"],
        )
    if workspace.locked_by_task_id != task["id"]:
        raise _workspace_precondition(
            "workspace_not_owned",
            f"integration workspace {workspace.workspace_path} is locked by "
            f"{workspace.locked_by_task_id or 'nobody'}, not {task['id']}",
            task_id=task["id"],
            workspace_id=workspace.id,
            locked_by_task_id=workspace.locked_by_task_id,
        )
    if task["repo_id"] != repo.id:
        raise _workspace_precondition(
            "repo_mismatch",
            f"task {task['id']} is bound to repository {task['repo_id'] or 'none'}, "
            f"but delivery targets {repo.id}",
            task_id=task["id"],
            task_repo_id=task["repo_id"],
            delivery_repo_id=repo.id,
        )
    if not task["branch_name"]:
        raise _workspace_precondition(
            "branch_not_recorded",
            f"task {task['id']} has no branch_name recorded — record the branch "
            f"this workspace is on with `aq task set {task['id']} --branch <branch>`",
            task_id=task["id"],
            remedy=f"aq task set {task['id']} --branch <branch>",
        )
    checkout = workspace.workspace_path
    branch = await git.aget_current_branch(checkout, strict=True)
    if branch != task["branch_name"].removeprefix("refs/heads/"):
        raise HierarchyError("dirty", "workspace is not on the canonical task branch")
    status = await git._arun(["status", "--porcelain"], cwd=checkout)
    if status:
        raise HierarchyError("dirty", "workspace has uncommitted changes")
    actual_head = (await git._arun(["rev-parse", "HEAD"], cwd=checkout)).lower()
    if not is_valid_git_oid(actual_head):
        raise HierarchyError("dirty", "workspace HEAD is not an exact Git OID")
    from src.git.manager import RemoteRefState

    remote = await git.als_remote_ref(checkout, task["branch_name"].removeprefix("refs/heads/"))
    if remote.state is not RemoteRefState.PRESENT or remote.oid != actual_head:
        raise HierarchyError("dirty", "workspace HEAD is not exactly pushed")
    return actual_head


async def resolve_repair_commit_proof(
    git, checkout: str, *, base_sha: str, head_sha: str
) -> dict[str, Any]:
    """Snapshot the exact ordered commits in one proved repair lineage.

    This performs Git I/O only.  Callers bind the immutable snapshot to the
    locked operation/stage/owner identity in their following database
    transaction.
    """
    if not is_valid_git_oid(base_sha) or not is_valid_git_oid(head_sha):
        raise HierarchyError("dirty", "repair lineage is not an exact Git OID pair")
    if base_sha == head_sha:
        commits: list[str] = []
    else:
        if await git.ais_ancestor(checkout, base_sha, head_sha, strict=True) is not True:
            raise HierarchyError("dirty", "repair HEAD does not descend from its bound subject")
        output = await git._arun(
            ["rev-list", "--reverse", f"{base_sha}..{head_sha}"], cwd=checkout
        )
        commits = [value.strip().lower() for value in output.splitlines() if value.strip()]
        if (
            not commits
            or commits[-1] != head_sha
            or len(commits) != len(set(commits))
            or any(not is_valid_git_oid(value) for value in commits)
        ):
            raise HierarchyError("dirty", "repair commit lineage is incomplete or invalid")
    parents_output = await git._arun(
        ["show", "-s", "--format=%P", head_sha], cwd=checkout
    )
    head_parents = [value.strip().lower() for value in parents_output.split() if value.strip()]
    if any(not is_valid_git_oid(value) for value in head_parents):
        raise HierarchyError("dirty", "repair HEAD parent lineage is invalid")
    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commits": commits,
        "head_parents": head_parents,
    }


async def resolve_workspace_repair_proof(
    db, git, task: dict, repo: RepoConfig, *, base_sha: str
) -> dict[str, Any]:
    """Prove a clean pushed writer HEAD and snapshot all commits from its subject."""
    head_sha = await resolve_workspace_checkpoint(db, git, task, repo)
    workspace = await db.get_workspace_for_task(task["id"])
    if workspace is None:
        raise _workspace_precondition(
            "no_integration_workspace",
            f"task {task['id']} holds no integration workspace",
            task_id=task["id"],
        )
    return await resolve_repair_commit_proof(
        git, workspace.workspace_path, base_sha=base_sha, head_sha=head_sha
    )


async def verify_workspace_checkpoint(db, git, task: dict, repo: RepoConfig, head_sha: str) -> str:
    """Prove a parent's actual checkout HEAD is clean and exactly pushed."""
    actual_head = await resolve_workspace_checkpoint(db, git, task, repo)
    if actual_head != head_sha:
        raise HierarchyError("dirty", "caller head does not match workspace HEAD")
    return actual_head


def hierarchy_mode_enabled(project: Any) -> bool:
    """Whether writes for *project* use isolated hierarchical delivery."""
    return getattr(project, "hierarchical_integration_mode", "disabled") in {
        "hierarchy",
        "train",
    }


class HierarchyIntegration:
    """The project-lock writer for checkpoints, origins, and child membership."""

    def __init__(
        self,
        db,
        *,
        default_head_resolver: DefaultHeadResolver | None = None,
        branch_materializer: BranchMaterializer | None = None,
        checkpoint_verifier: CheckpointVerifier | None = None,
        ancestry_verifier: AncestryVerifier | None = None,
        ownership: BranchOwnership | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.default_head_resolver = default_head_resolver
        self.branch_materializer = branch_materializer
        self.checkpoint_verifier = checkpoint_verifier
        self.ancestry_verifier = ancestry_verifier
        self.ownership = ownership or BranchOwnership(db)
        self.parent_completion = ParentCompletion(db, clock=clock)
        self.clock = clock

    async def file_children(
        self, parent_id: str, children: list[dict], expected_generation: int
    ) -> dict:
        if not children:
            raise HierarchyError("invalid", "at least one child is required")
        async with self.db.immediate() as conn:
            parent = await self._task_row(conn, parent_id)
            project, repo = await self._enabled_route(conn, parent)
            await self.db.lock_hierarchy_project(conn, project["id"])
            parent = await self._task_row(conn, parent_id)
            await self._ensure_origin_chain(conn, parent_id, repo)
            checkpoint = await self._locked_checkpoint(conn, parent_id)
            if int(checkpoint["generation"]) != expected_generation:
                raise HierarchyError(
                    "stale_parent",
                    f"expected generation {expected_generation}, found {checkpoint['generation']}",
                )
            base_sha = checkpoint["checkpoint_sha"]
            if not base_sha or not _OID.fullmatch(base_sha):
                raise HierarchyError("invalid", "parent checkpoint does not name an exact commit")
            generation = expected_generation + 1
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == parent_id)
                .where(task_integration_checkpoints.c.generation == expected_generation)
                .values(
                    generation=generation,
                    verified_sha=None,
                    verified_generation=None,
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )

            created: list[dict[str, Any]] = []
            origins: list[dict[str, Any]] = []
            for child in children:
                task_id, capped = await child_task_id(conn, parent_id)
                if capped:
                    raise HierarchyError("invalid", "hierarchical child exceeds the naming cap")
                task = self._build_child(parent, repo.id, task_id, child)
                await self.db.create_task(task, conn=conn)
                await self.db.set_parent(
                    task_id,
                    parent_id,
                    conn=conn,
                    description=child.get("reason"),
                    integration_authorized=True,
                )
                origin = await self._reserve_origin(
                    conn,
                    task_id=task_id,
                    repository_id=repo.id,
                    parent_task_id=parent_id,
                    parent_ref=checkpoint["branch"],
                    base_sha=base_sha,
                    generation=generation,
                )
                await self._insert_checkpoint(
                    conn,
                    task_id=task_id,
                    repository_id=repo.id,
                    branch=origin["branch"],
                    checkpoint_sha=base_sha,
                )
                created.append({"task_id": task_id, "title": task.title})
                origins.append(origin)

        return {"outcome": "filed", "generation": generation, "children": created, "origins": origins}

    async def readiness(self, task_id: str) -> dict:
        return await self.parent_completion.readiness(task_id)

    async def record_disposition(self, child_task_id: str, **values) -> dict:
        return await self.parent_completion.record_disposition(child_task_id, **values)

    async def verify_parent(
        self, task_id: str, generation: int, head_sha: str, evidence_ids: list[str]
    ) -> dict:
        if not _OID.fullmatch(head_sha):
            return {"outcome": "stale_head", "task_id": task_id}
        if self.checkpoint_verifier is None:
            return {"outcome": "stale_head", "task_id": task_id}
        async with self.db._engine.connect() as conn:
            task = await self._task_row(conn, task_id)
            _project, repo = await self._enabled_route(conn, task)
        actual = self.checkpoint_verifier(task, repo, head_sha)
        if inspect.isawaitable(actual):
            actual = await actual
        if actual != head_sha:
            return {"outcome": "stale_head", "task_id": task_id}
        return await self.parent_completion.verify_parent(
            task_id, generation, head_sha, evidence_ids
        )

    async def complete_parent(self, task_id: str, generation: int, head_sha: str) -> dict:
        return await self.parent_completion.complete_parent(task_id, generation, head_sha)

    async def wake_verifier(self, task_id: str, fence) -> dict:
        return await self.parent_completion.wake_verifier(task_id, fence)

    async def checkpoint_leaf_completion(self, task_id: str, head_sha: str) -> dict:
        """Advance only a leaf's live completion head after clean pushed proof.

        The immutable branch origin remains the filing base.  Review and
        promotion consume this live checkpoint as the finished source head.
        """
        if not _OID.fullmatch(head_sha) or self.checkpoint_verifier is None:
            raise HierarchyError("dirty", "leaf completion head is not verifiable")
        async with self.db._engine.connect() as read_conn:
            task = await self._task_row(read_conn, task_id)
            project, repo = await self._enabled_route(read_conn, task)
        verified = self.checkpoint_verifier(task, repo, head_sha)
        if inspect.isawaitable(verified):
            verified = await verified
        if verified != head_sha:
            raise HierarchyError("dirty", "leaf checkpoint verifier returned another head")
        async with self.db.immediate() as conn:
            task = await self._task_row(conn, task_id)
            project, repo = await self._enabled_route(conn, task)
            await self.db.lock_hierarchy_project(conn, project["id"])
            child = (
                await conn.execute(select(tasks.c.id).where(tasks.c.parent_task_id == task_id).limit(1))
            ).first()
            if child is not None:
                raise HierarchyError("invariant_error", "leaf completion has children")
            checkpoint = await self._locked_checkpoint(conn, task_id)
            if checkpoint.get("episode_id") is not None:
                raise HierarchyError("invariant_error", "parent episode cannot close as a leaf")
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .where(task_integration_checkpoints.c.version == checkpoint["version"])
                .values(
                    checkpoint_sha=head_sha,
                    state="working",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
        return {
            "outcome": "checkpointed",
            "task_id": task_id,
            "generation": int(checkpoint["generation"]),
            "head_sha": head_sha,
        }

    async def file_prepared_child_on(
        self,
        conn,
        parent_id: str,
        task: Task,
        *,
        requirements: list[tuple[str, str | None]] | None = None,
        edges: list[tuple[str, str, str | None]] | None = None,
        labels: list[str] | None = None,
        expected_generation: int | None = None,
        routing_policy=None,
        current_parent_head: str | None = None,
    ) -> dict:
        """Insert a validated command-layer task in the caller's transaction."""
        parent = await self._task_row(conn, parent_id)
        project, repo = await self._enabled_route(conn, parent)
        await self.db.lock_hierarchy_project(conn, project["id"])
        await self._ensure_origin_chain(conn, parent_id, repo)
        checkpoint = await self._locked_checkpoint(conn, parent_id)
        if current_parent_head is not None and not is_valid_git_oid(current_parent_head):
            raise HierarchyError("dirty", "filing parent HEAD is not an exact Git OID")
        current_generation = int(checkpoint["generation"])
        if expected_generation is not None and current_generation != expected_generation:
            raise HierarchyError(
                "stale_parent",
                f"expected generation {expected_generation}, found {current_generation}",
            )
        generation = current_generation + 1
        await self._bump_checkpoint(conn, parent_id, current_generation, generation)
        task_id, capped = await child_task_id(conn, parent_id)
        if capped:
            raise HierarchyError("invalid", "hierarchical child exceeds the naming cap")
        task.id = task_id
        task.project_id = project["id"]
        task.parent_task_id = None
        task.repo_id = repo.id
        task.branch_name = f"aq/{task_id}"
        task.status = TaskStatus.DEFINED
        await self.db.create_task(task, conn=conn)
        await self.db.set_parent(
            task_id, parent_id, conn=conn, integration_authorized=True
        )
        await self._write_task_extras(
            conn,
            task_id,
            requirements=requirements,
            edges=edges,
            labels=labels,
        )
        gate_id = await self._maybe_create_routing_gate(conn, task, routing_policy)
        origin = await self._reserve_origin(
            conn,
            task_id=task_id,
            repository_id=repo.id,
            parent_task_id=parent_id,
            parent_ref=checkpoint["branch"],
            base_sha=current_parent_head or checkpoint["checkpoint_sha"],
            generation=generation,
        )
        await self._insert_checkpoint(
            conn,
            task_id=task_id,
            repository_id=repo.id,
            branch=origin["branch"],
            checkpoint_sha=origin["base_sha"],
        )
        return {
            "task_id": task_id,
            "generation": generation,
            "origin": origin,
            "gate_id": gate_id,
        }

    async def file_prepared_children_on(
        self,
        conn,
        parent_id: str,
        child_tasks: list[Task],
        *,
        routing_policy=None,
    ) -> list[dict]:
        """Insert sibling tasks with one parent-generation advance.

        Graph and proposal materialisation use this bulk path rather than
        ``file_prepared_child_on``.  Keep routing admission here as well: a
        profile-less child must receive the same durable routing gate whether
        it was filed individually or as part of a graph/batch.
        """
        if not child_tasks:
            return []
        parent = await self._task_row(conn, parent_id)
        project, repo = await self._enabled_route(conn, parent)
        await self.db.lock_hierarchy_project(conn, project["id"])
        await self._ensure_origin_chain(conn, parent_id, repo)
        checkpoint = await self._locked_checkpoint(conn, parent_id)
        current_generation = int(checkpoint["generation"])
        generation = current_generation + 1
        await self._bump_checkpoint(conn, parent_id, current_generation, generation)
        created: list[dict] = []
        for task in child_tasks:
            task_id, capped = await child_task_id(conn, parent_id)
            if capped:
                raise HierarchyError("invalid", "hierarchical child exceeds the naming cap")
            task.id = task_id
            task.project_id = project["id"]
            task.parent_task_id = None
            task.repo_id = repo.id
            task.branch_name = f"aq/{task_id}"
            task.status = TaskStatus.DEFINED
            await self.db.create_task(task, conn=conn)
            await self.db.set_parent(
                task_id, parent_id, conn=conn, integration_authorized=True
            )
            # ``set_parent`` writes the row.  Preserve that placement on the
            # in-memory task too, because the routing policy evaluates the
            # same task.created fields as ordinary command-layer creation.
            task.parent_task_id = parent_id
            gate_id = await self._maybe_create_routing_gate(conn, task, routing_policy)
            origin = await self._reserve_origin(
                conn,
                task_id=task_id,
                repository_id=repo.id,
                parent_task_id=parent_id,
                parent_ref=checkpoint["branch"],
                base_sha=checkpoint["checkpoint_sha"],
                generation=generation,
            )
            await self._insert_checkpoint(
                conn,
                task_id=task_id,
                repository_id=repo.id,
                branch=origin["branch"],
                checkpoint_sha=origin["base_sha"],
            )
            created.append({"task_id": task_id, "origin": origin, "gate_id": gate_id})
        return created

    async def file_root_on(
        self,
        conn,
        task: Task,
        *,
        requirements: list[tuple[str, str | None]] | None = None,
        edges: list[tuple[str, str, str | None]] | None = None,
        labels: list[str] | None = None,
        routing_policy=None,
    ) -> dict:
        """Insert an enabled-project root and reserve its isolated origin."""
        _project, repo = await self._root_route(conn, task.project_id)
        await self.db.lock_hierarchy_project(conn, task.project_id)
        task.id = await fresh_root_id(conn)
        task.parent_task_id = None
        task.repo_id = repo.id
        # A freshly filed root has no remote task branch yet.  Resolve its
        # immutable origin from the repository default branch, then record
        # the canonical branch identity for materialization.
        task.branch_name = None
        await self.db.create_task(task, conn=conn)
        await self._write_task_extras(
            conn,
            task.id,
            requirements=requirements,
            edges=edges,
            labels=labels,
        )
        gate_id = await self._maybe_create_routing_gate(conn, task, routing_policy)
        await self._ensure_origin_chain(conn, task.id, repo)
        task.branch_name = f"aq/{task.id}"
        await conn.execute(update(tasks).where(tasks.c.id == task.id).values(branch_name=task.branch_name))
        return {"task_id": task.id, "generation": 0, "gate_id": gate_id}

    async def bootstrap_container_collection(self, task_id: str) -> dict:
        """Checkpoint an affirmatively untouched container at its pinned origin.

        A released graph container legitimately has a nonzero ``claim_epoch``:
        the release itself traverses READY -> IN_PROGRESS without assigning a
        worker.  Epoch alone therefore cannot distinguish it from a prior
        writer.  This path instead requires positive evidence that the *current
        task incarnation* has never acquired a session or workspace.
        """
        from src.integration.models import Fence

        async with self.db._engine.connect() as conn:
            task = await self._task_row(conn, task_id)
            project, repo = await self._enabled_route(conn, task)
        target = BranchKey(repository_id=repo.id, branch=f"aq/{task_id}")
        owner = await self.ownership.get_owner(target)
        if owner is None or owner["owner_id"] != task_id or owner["owner_role"] != "worker":
            return {"outcome": "waiting", "task_id": task_id}
        fence = Fence(target=target, owner_id=task_id, token=int(owner["fence_token"]))
        transition = None
        async with self.ownership.mutation_exclusion(fence, expected_role="worker") as conn:
            await self.db.lock_hierarchy_project(conn, project["id"])
            task = await self._task_row(conn, task_id)
            checkpoint = await self._locked_checkpoint(conn, task_id)
            origin = (await conn.execute(select(task_branch_origins).where(
                task_branch_origins.c.task_id == task_id,
                task_branch_origins.c.repository_id == repo.id,
                task_branch_origins.c.retired_at.is_(None),
            ))).mappings().one_or_none()
            children = (await conn.execute(select(tasks.c.id).where(
                tasks.c.parent_task_id == task_id
            ).limit(1))).scalar_one_or_none()
            live = (await conn.execute(select(sessions.c.id).where(
                sessions.c.task_id == task_id,
                sessions.c.state.in_(("starting", "running", "draining")),
            ).limit(1))).scalar_one_or_none()
            workspace = (await conn.execute(select(workspaces.c.id).where(
                workspaces.c.locked_by_task_id == task_id
            ).limit(1))).scalar_one_or_none()
            attempt = (await conn.execute(select(task_session_attempts.c.id).where(
                task_session_attempts.c.task_id == task_id,
                task_session_attempts.c.started_at >= task["created_at"],
            ).limit(1))).scalar_one_or_none()
            manually_paused = (await conn.execute(select(task_metadata.c.value).where(
                task_metadata.c.task_id == task_id,
                task_metadata.c.key == "manual_pause",
            ).limit(1))).scalar_one_or_none()

            reason = None
            if origin is None or not origin["materialized"] or not origin["reserved"]:
                reason = "origin_pending"
            elif children is None:
                reason = "not_a_container"
            elif live is not None or task["assigned_agent_id"] is not None:
                reason = "live_claim"
            elif workspace is not None:
                reason = "workspace_attached"
            elif attempt is not None:
                reason = "current_incarnation_attempt"
            elif manually_paused is not None:
                reason = "manual_pause"
            elif task["branch_name"] != target.branch:
                reason = "branch_identity"
            elif task["status"] not in {"IN_PROGRESS", "PAUSED"}:
                reason = "container_not_released"
            elif task["status"] == "PAUSED" and checkpoint["episode_id"] is None:
                reason = "unexplained_pause"
            if reason is not None:
                return {"outcome": "waiting", "task_id": task_id, "reason": reason}
            head = await self._resolve_head(repo, target.branch)
            if head != origin["base_sha"] or checkpoint["checkpoint_sha"] != head:
                return {"outcome": "waiting", "task_id": task_id, "reason": "origin_changed"}
            operation = await self.parent_completion.reserve_episode_on(
                conn, parent=task, project=project, checkpoint=checkpoint,
                pre_collection_sha=head,
            )
            if checkpoint["episode_id"] is None:
                await conn.execute(update(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == task_id
                ).values(episode_id=operation["episode_id"], state="awaiting_children",
                         version=task_integration_checkpoints.c.version + 1,
                         updated_at=self.clock()))
                transition = await self.db._apply_transition(
                    conn, task_id, TaskStatus.PAUSED, context="integration_parent_suspended",
                    assigned_agent_id=None, _manual_pause_control=True,
                )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
        # Crash recovery re-enters above with the same episode and paused
        # never-run task; transfer grants one fenced daemon collector.
        collector = await self.ownership.transfer(fence, operation["id"], "collector")
        return {"outcome": "checkpointed", "task_id": task_id,
                "operation_id": operation["id"], "fence": collector.model_dump(mode="json")}

    async def checkpoint_parent(
        self,
        task_id: str,
        head_sha: str,
        generation: int,
        *,
        _suspend: bool = False,
        expect_claim_epoch: int | None = None,
    ) -> dict:
        if not _OID.fullmatch(head_sha):
            raise HierarchyError("dirty", "head_sha must be a lowercase 40-character Git OID")
        if self.checkpoint_verifier is None:
            raise HierarchyError("dirty", "workspace checkpoint verifier is unavailable")
        async with self.db._engine.connect() as read_conn:
            task = await self._task_row(read_conn, task_id)
            _project, repo = await self._enabled_route(read_conn, task)
            checkpoint_completion = (
                await read_conn.execute(
                    select(
                        task_integration_checkpoints.c.episode_id,
                        task_integration_checkpoints.c.last_completed_operation_id,
                        task_integration_checkpoints.c.last_completed_verification_id,
                    ).where(task_integration_checkpoints.c.task_id == task_id)
                )
            ).mappings().one_or_none()
            prior = None
            if (
                checkpoint_completion is not None
                and checkpoint_completion["episode_id"] is None
                and checkpoint_completion["last_completed_operation_id"] is not None
                and checkpoint_completion["last_completed_verification_id"] is not None
            ):
                prior = (
                    await read_conn.execute(
                        select(
                            integration_repair_operations.c.id.label("operation_id"),
                            integration_repair_operations.c.episode_id,
                            integration_parent_operation_completions.c.verification_id,
                            integration_parent_verifications.c.head_sha,
                        )
                        .join(
                            integration_parent_operation_completions,
                            integration_parent_operation_completions.c.operation_id
                            == integration_repair_operations.c.id,
                        )
                        .join(
                            integration_parent_verifications,
                            and_(
                                integration_parent_verifications.c.operation_id
                                == integration_parent_operation_completions.c.operation_id,
                                integration_parent_verifications.c.id
                                == integration_parent_operation_completions.c.verification_id,
                                integration_parent_verifications.c.parent_task_id
                                == integration_parent_operation_completions.c.parent_task_id,
                                integration_parent_verifications.c.episode_id
                                == integration_parent_operation_completions.c.episode_id,
                            ),
                        )
                        .where(
                            integration_repair_operations.c.parent_task_id == task_id,
                            integration_repair_operations.c.state == "completed",
                            integration_parent_operation_completions.c.operation_id
                            == checkpoint_completion["last_completed_operation_id"],
                            integration_parent_operation_completions.c.verification_id
                            == checkpoint_completion["last_completed_verification_id"],
                            integration_parent_operation_completions.c.parent_task_id
                            == task_id,
                        )
                    )
                ).mappings().one_or_none()
        verified = self.checkpoint_verifier(task, repo, head_sha)
        if inspect.isawaitable(verified):
            verified = await verified
        if verified != head_sha:
            raise HierarchyError("dirty", "checkpoint verifier returned another head")
        carry_forward = None
        if prior is not None:
            if self.ancestry_verifier is None:
                raise HierarchyError(
                    "stale_head", "receipt carry-forward ancestry verifier is unavailable"
                )
            ancestor = self.ancestry_verifier(repo, prior["head_sha"], head_sha)
            if inspect.isawaitable(ancestor):
                ancestor = await ancestor
            if ancestor is not True:
                raise HierarchyError(
                    "stale_head", "new parent checkpoint does not contain verified aggregate"
                )
            carry_forward = dict(prior)
        async with self.db.immediate() as conn:
            task = await self._task_row(conn, task_id)
            project, repo = await self._enabled_route(conn, task)
            await self.db.lock_hierarchy_project(conn, project["id"])
            await self._ensure_origin_chain(conn, task_id, repo)
            checkpoint = await self._locked_checkpoint(conn, task_id)
            if (
                checkpoint["repository_id"] != repo.id
                or checkpoint["branch"] != task["branch_name"]
            ):
                raise HierarchyError(
                    "delivery_target_fixed", "parent checkpoint branch identity changed"
                )
            if int(checkpoint["generation"]) != generation:
                raise HierarchyError(
                    "stale", f"expected generation {generation}, found {checkpoint['generation']}"
                )
            operation = await self.parent_completion.reserve_episode_on(
                conn,
                parent=task,
                project=project,
                checkpoint=checkpoint,
                pre_collection_sha=head_sha,
                carry_forward=carry_forward,
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .where(task_integration_checkpoints.c.generation == generation)
                .values(
                    checkpoint_sha=head_sha,
                    state="awaiting_children",
                    verified_sha=None,
                    verified_generation=None,
                    episode_id=operation["episode_id"],
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
            transition = None
            if _suspend:
                transition = await self.db._apply_transition(
                    conn,
                    task_id,
                    TaskStatus.PAUSED,
                    context="integration_parent_suspended",
                    assigned_agent_id=None,
                    expect_claim_epoch=expect_claim_epoch,
                    _manual_pause_control=True,
                )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
        return {
            "outcome": "checkpointed",
            "task_id": task_id,
            "generation": generation,
            "head_sha": head_sha,
            "episode_id": operation["episode_id"],
            "operation_id": operation["id"],
        }

    async def checkpoint_and_suspend_parent(
        self,
        task_id: str,
        head_sha: str,
        generation: int,
        *,
        expect_claim_epoch: int,
    ) -> dict:
        """Atomically reserve the collection episode and pause its producer."""
        return await self.checkpoint_parent(
            task_id,
            head_sha,
            generation,
            _suspend=True,
            expect_claim_epoch=expect_claim_epoch,
        )

    async def materialize_origin(self, origin_id: str) -> dict:
        """Create a pending canonical ref only when absent or already exact.

        The pending origin row remains the scanner source.  This operation is
        deliberately idempotent so Task 10 can retry it after any crash.
        """
        if self.branch_materializer is None:
            raise HierarchyError("invalid", "branch materializer is unavailable")
        async with self.db._engine.connect() as read_conn:
            row = (
                await read_conn.execute(
                    select(task_branch_origins).where(task_branch_origins.c.id == origin_id)
                )
            ).mappings().one_or_none()
            if row is None or row["retired_at"] is not None:
                raise HierarchyError("invalid", "pending origin does not exist")
            task = await self._task_row(read_conn, row["task_id"])
            repo = await self._repo_on(read_conn, row["repository_id"])
        if repo is None:
            raise HierarchyError("invalid", "origin repository does not exist")
        branch = f"aq/{task['id']}"
        ownership = await self.ownership.get_owner(
            BranchKey(repository_id=repo.id, branch=branch)
        )
        if ownership is None or ownership["owner_id"] != task["id"]:
            raise HierarchyError("invalid", "origin branch has no task ownership")
        from src.integration.models import Fence

        fence = Fence(
            target=BranchKey(repository_id=repo.id, branch=branch),
            owner_id=task["id"],
            token=int(ownership["fence_token"]),
        )
        transition = None
        async with self.ownership.mutation_exclusion(fence) as conn:
            current = (
                await conn.execute(
                    select(task_branch_origins)
                    .where(task_branch_origins.c.id == origin_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if current is None or current["retired_at"] is not None:
                raise HierarchyError("invalid", "pending origin changed")
            task = await self._task_row(conn, current["task_id"])
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            value = self.branch_materializer(repo, branch, current["base_sha"])
            if inspect.isawaitable(value):
                value = await value
            if value != current["base_sha"]:
                raise HierarchyError("invalid", "materialized ref is not the pinned base")
            if not current["materialized"]:
                result = await conn.execute(
                    update(task_branch_origins)
                    .where(task_branch_origins.c.id == origin_id)
                    .where(task_branch_origins.c.materialized.is_(False))
                    .where(task_branch_origins.c.retired_at.is_(None))
                    .values(materialized=True, materialized_at=self.clock())
                )
                if result.rowcount != 1:
                    raise HierarchyError("stale", "origin changed during materialization")
            if task["status"] == TaskStatus.DEFINED.value and not task["is_blocked"]:
                transition = await self.db._apply_transition(
                    conn,
                    task["id"],
                    TaskStatus.READY,
                    context="origin_materialized",
                )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return {
            "outcome": "materialized",
            "origin_id": origin_id,
            "branch": branch,
            "base_sha": current["base_sha"],
        }

    async def mutate_hierarchy(
        self, task_id: str, mutation: str, arguments: dict
    ) -> dict:
        if mutation != "reparent":
            raise HierarchyError("invalid", f"unsupported hierarchy mutation: {mutation}")
        new_parent_id = arguments.get("parent_id")
        if not new_parent_id:
            raise HierarchyError("invalid", "reparent requires parent_id")
        async with self.db.immediate() as conn:
            child = await self._task_row(conn, task_id)
            project, repo = await self._enabled_route(conn, child)
            await self.db.lock_hierarchy_project(conn, project["id"])
            child = await self._task_row(conn, task_id)
            old_parent_id = child["parent_task_id"]
            if not old_parent_id or old_parent_id == new_parent_id:
                raise HierarchyError("invalid", "reparent requires a different existing parent")
            new_parent = await self._task_row(conn, new_parent_id)
            if new_parent["project_id"] != project["id"]:
                raise HierarchyError("invalid", "new parent belongs to another project")
            await self._assert_reparentable(conn, task_id, repo.id)
            await self._ensure_origin_chain(conn, new_parent_id, repo)
            old_checkpoint = await self._locked_checkpoint(conn, old_parent_id)
            new_checkpoint = await self._locked_checkpoint(conn, new_parent_id)
            expected_old = arguments.get("expected_old_generation")
            expected_new = arguments.get("expected_new_generation")
            if expected_old is None or int(old_checkpoint["generation"]) != int(expected_old):
                raise HierarchyError("stale_parent", "old parent generation changed")
            if expected_new is None or int(new_checkpoint["generation"]) != int(expected_new):
                raise HierarchyError("stale_parent", "new parent generation changed")
            now = self.clock()
            origin = await self._locked_origin(conn, task_id, repo.id)
            await conn.execute(
                update(task_branch_origins)
                .where(task_branch_origins.c.id == origin["id"])
                .where(task_branch_origins.c.materialized.is_(False))
                .values(retired_at=now)
            )
            old_generation = int(expected_old) + 1
            new_generation = int(expected_new) + 1
            for parent_id, expected, generation in (
                (old_parent_id, expected_old, old_generation),
                (new_parent_id, expected_new, new_generation),
            ):
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(task_integration_checkpoints.c.task_id == parent_id)
                    .where(task_integration_checkpoints.c.generation == expected)
                    .values(
                        generation=generation,
                        verified_sha=None,
                        verified_generation=None,
                        version=task_integration_checkpoints.c.version + 1,
                        updated_at=now,
                    )
                )
            await self.db.set_parent(
                task_id, new_parent_id, conn=conn, integration_authorized=True
            )
            await self._reserve_origin(
                conn,
                task_id=task_id,
                repository_id=repo.id,
                parent_task_id=new_parent_id,
                parent_ref=new_checkpoint["branch"],
                base_sha=new_checkpoint["checkpoint_sha"],
                generation=new_generation,
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    checkpoint_sha=new_checkpoint["checkpoint_sha"],
                    verified_sha=None,
                    verified_generation=None,
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=now,
                )
            )
        return {
            "outcome": "updated",
            "task_id": task_id,
            "old_parent_id": old_parent_id,
            "new_parent_id": new_parent_id,
            "old_parent_generation": old_generation,
            "new_parent_generation": new_generation,
        }

    async def _root_route(self, conn, project_id: str) -> tuple[dict, RepoConfig]:
        """The (project, repo) a new root in *project_id* files through."""
        project = (
            await conn.execute(
                select(self._projects_table()).where(self._projects_table().c.id == project_id)
            )
        ).mappings().one_or_none()
        if project is None or project["hierarchical_integration_mode"] not in {
            "hierarchy",
            "train",
        }:
            raise HierarchyError("invalid", "hierarchical integration is not enabled")
        project = dict(project)
        repository_id = project["integration_repository_id"]
        repo = await self._repo_on(conn, repository_id) if repository_id else None
        if repo is None or repo.project_id != project_id:
            raise HierarchyError("invalid", "designated repository is not in the project")
        return project, repo

    async def graph_route(
        self, conn, project_id: str, parent_id: str | None = None
    ) -> tuple[dict, RepoConfig] | None:
        """The route a task graph in *project_id* files through, or ``None``.

        ``None`` means the project is not hierarchical and the graph creator
        may take its legacy path.  An enabled project answers the same
        ``(project, repo)`` that :meth:`file_root_on` (new container) or
        :meth:`file_prepared_children_on` (existing *parent_id*) will use,
        raising the same :class:`HierarchyError` they would — so a dry run
        refuses exactly what the real run refuses, before anything is written.
        """
        mode = await conn.scalar(
            select(self._projects_table().c.hierarchical_integration_mode).where(
                self._projects_table().c.id == project_id
            )
        )
        if mode not in {"hierarchy", "train"}:
            return None
        if parent_id is not None:
            return await self._enabled_route(conn, await self._task_row(conn, parent_id))
        return await self._root_route(conn, project_id)

    async def _enabled_route(self, conn, task: dict) -> tuple[dict, RepoConfig]:
        project = (
            await conn.execute(select(self._projects_table()).where(self._projects_table().c.id == task["project_id"]))
        ).mappings().one()
        project = dict(project)
        if project["hierarchical_integration_mode"] not in {"hierarchy", "train"}:
            raise HierarchyError("invalid", "hierarchical integration is not enabled")
        repository_id = project["integration_repository_id"]
        if not repository_id or task["repo_id"] != repository_id:
            raise HierarchyError("invalid", "task is not bound to the designated repository")
        repo = await self._repo_on(conn, repository_id)
        if repo is None or repo.project_id != project["id"]:
            raise HierarchyError("invalid", "designated repository is not in the project")
        return project, repo

    @staticmethod
    def _projects_table():
        from src.database.tables import projects

        return projects

    async def _repo_on(self, conn, repository_id: str) -> RepoConfig | None:
        from src.database.queries.repo_queries import RepoQueryMixin
        from src.database.tables import repos

        row = (await conn.execute(select(repos).where(repos.c.id == repository_id))).mappings().one_or_none()
        return RepoQueryMixin._row_to_repo(row) if row is not None else None

    async def _task_row(self, conn, task_id: str) -> dict:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one_or_none()
        if row is None:
            raise HierarchyError("invalid", f"task not found: {task_id}")
        return dict(row)

    async def _locked_checkpoint(self, conn, task_id: str) -> dict:
        row = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None:
            raise HierarchyError("invalid", f"task has no integration checkpoint: {task_id}")
        return dict(row)

    async def _locked_origin(self, conn, task_id: str, repository_id: str) -> dict:
        row = (
            await conn.execute(
                select(task_branch_origins)
                .where(task_branch_origins.c.task_id == task_id)
                .where(task_branch_origins.c.repository_id == repository_id)
                .where(task_branch_origins.c.retired_at.is_(None))
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None:
            raise HierarchyError("invalid", "task has no live branch origin")
        return dict(row)

    async def reconcile_unmaterialized_tasks_on(
        self,
        conn,
        *,
        project_id: str,
        repository_id: str,
        origin_generation: int,
    ) -> list[str]:
        """Bind a pristine legacy graph to its designated hierarchy repository.

        This is deliberately caller-transaction-owned: the operational control
        has already taken the project hierarchy lock and generation fence.  It
        first proves that *all* null-repository tasks can be recovered, so an
        ambiguity never leaves a partially rebound graph behind.
        """
        repo = await self._repo_on(conn, repository_id)
        if repo is None or repo.project_id != project_id:
            raise HierarchyError("invalid", "designated repository is not in the project")

        targets = list(
            (
                await conn.execute(
                    select(tasks)
                    .where(tasks.c.project_id == project_id, tasks.c.repo_id.is_(None))
                    .order_by(tasks.c.id)
                )
            ).mappings()
        )
        if not targets:
            return []

        # Include ancestors because reserving a child necessarily reserves its
        # full delivery chain.  No row in that chain may belong to a different
        # repository or be active while this operator-only recovery runs.
        candidates: dict[str, dict] = {}
        # A null-bound parent with a differently-bound descendant is just as
        # ambiguous as a differently-bound ancestor: recovering the parent
        # would otherwise silently change that child's future delivery chain.
        seed_ids: set[str] = set()
        for target in targets:
            seed_ids.update(await self.db.subtree_ids(target["id"], conn=conn))
        for seed_id in sorted(seed_ids):
            row = await self._task_row(conn, seed_id)
            while True:
                if row["project_id"] != project_id:
                    raise HierarchyError("invalid", "task hierarchy crosses the selected project")
                candidates[row["id"]] = row
                parent_id = row["parent_task_id"]
                if not parent_id:
                    break
                row = await self._task_row(conn, parent_id)

        candidate_ids = sorted(candidates)
        active_sessions = set(
            (
                await conn.execute(
                    select(sessions.c.task_id).where(sessions.c.task_id.in_(candidate_ids))
                )
            ).scalars()
        )
        for row in candidates.values():
            if row["repo_id"] not in {None, repository_id}:
                raise HierarchyError("invalid", "task is bound to a different repository")
            if (
                row["assigned_agent_id"] is not None
                or row["id"] in active_sessions
                or row["status"] in {TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value}
            ):
                raise HierarchyError("busy", "task has an active assignment or claim")

        origins = list(
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id.in_(candidate_ids),
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings()
        )
        if origins:
            raise HierarchyError(
                "invalid", "task already has a live branch origin and is not an unmaterialized rollout task"
            )
        checkpoints = list(
            (
                await conn.execute(
                    select(task_integration_checkpoints.c.task_id).where(
                        task_integration_checkpoints.c.task_id.in_(candidate_ids)
                    )
                )
            ).scalars()
        )
        if checkpoints:
            raise HierarchyError("invalid", "task already has an integration checkpoint")

        for task_id in sorted(target["id"] for target in targets):
            await self._ensure_origin_chain(
                conn,
                task_id,
                repo,
                origin_generation=origin_generation,
                root_branch=repo.default_branch,
            )
        return sorted(target["id"] for target in targets)

    async def _ensure_origin_chain(
        self,
        conn,
        task_id: str,
        repo: RepoConfig,
        *,
        origin_generation: int = 0,
        root_branch: str | None = None,
    ) -> None:
        chain: list[dict] = []
        current = await self._task_row(conn, task_id)
        while current is not None:
            chain.append(current)
            parent_id = current["parent_task_id"]
            current = await self._task_row(conn, parent_id) if parent_id else None
        chain.reverse()
        parent_checkpoint: dict | None = None
        for row in chain:
            existing = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == row["id"]
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                parent_checkpoint = dict(existing)
                continue
            branch = f"aq/{row['id']}"
            if parent_checkpoint is None:
                base_sha = await self._resolve_head(
                    repo, root_branch or row["branch_name"] or repo.default_branch
                )
                parent_ref = repo.default_branch
            else:
                base_sha = parent_checkpoint["checkpoint_sha"]
                parent_ref = parent_checkpoint["branch"]
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == row["id"])
                .values(repo_id=repo.id, branch_name=branch, updated_at=self.clock())
            )
            await self._reserve_origin(
                conn,
                task_id=row["id"],
                repository_id=repo.id,
                parent_task_id=row["parent_task_id"],
                parent_ref=parent_ref,
                base_sha=base_sha,
                generation=origin_generation,
            )
            await self._insert_checkpoint(
                conn,
                task_id=row["id"],
                repository_id=repo.id,
                branch=branch,
                checkpoint_sha=base_sha,
            )
            parent_checkpoint = {
                "task_id": row["id"],
                "repository_id": repo.id,
                "branch": branch,
                "generation": 0,
                "checkpoint_sha": base_sha,
            }

    async def _resolve_head(self, repo: RepoConfig, branch: str) -> str:
        if self.default_head_resolver is None:
            raise HierarchyError("invalid", "repository head resolver is unavailable")
        value = self.default_head_resolver(repo, branch)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, str) or not _OID.fullmatch(value):
            raise HierarchyError("invalid", "repository head is not an exact Git OID")
        return value

    async def _reserve_origin(
        self,
        conn,
        *,
        task_id: str,
        repository_id: str,
        parent_task_id: str | None,
        parent_ref: str,
        base_sha: str,
        generation: int,
    ) -> dict:
        if not _OID.fullmatch(base_sha):
            raise HierarchyError("invalid", "branch origin base is not an exact Git OID")
        branch = f"aq/{task_id}"
        if parent_task_id is not None and parent_ref == "main":
            raise HierarchyError("invalid", "child delivery cannot target the default branch")
        origin_id = str(uuid.uuid4())
        now = self.clock()
        values = {
            "id": origin_id,
            "task_id": task_id,
            "repository_id": repository_id,
            "parent_task_id": parent_task_id,
            "parent_repository_id": repository_id if parent_task_id else None,
            "parent_ref": parent_ref,
            "base_sha": base_sha,
            "creation_generation": generation,
            "reserved": True,
            "materialized": False,
            "created_at": now,
        }
        await conn.execute(insert(task_branch_origins).values(**values))
        await self.ownership.acquire(
            BranchKey(repository_id=repository_id, branch=branch),
            task_id,
            "worker",
            conn=conn,
        )
        event_id = f"materialize-{origin_id}"
        await enqueue_integration_event(
            conn,
            event_id=event_id,
            dedup_key=f"integration.branch_materialization:{origin_id}",
            project_id=(await self._task_row(conn, task_id))["project_id"],
            event_type="integration.branch_materialization_pending",
            payload={
                "operation_id": origin_id,
                "origin_id": origin_id,
                "task_id": task_id,
                "repository_id": repository_id,
                "branch": branch,
                "parent_ref": parent_ref,
                "base_sha": base_sha,
            },
            available_at=now,
        )
        return values | {"branch": branch}

    async def _insert_checkpoint(
        self,
        conn,
        *,
        task_id: str,
        repository_id: str,
        branch: str,
        checkpoint_sha: str,
    ) -> None:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id=task_id,
                repository_id=repository_id,
                branch=branch,
                generation=0,
                checkpoint_sha=checkpoint_sha,
                state="working",
                version=0,
                branch_owner_id=task_id,
                updated_at=self.clock(),
            )
        )

    async def _bump_checkpoint(
        self, conn, task_id: str, expected: int, generation: int
    ) -> None:
        result = await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == task_id)
            .where(task_integration_checkpoints.c.generation == expected)
            .values(
                generation=generation,
                verified_sha=None,
                verified_generation=None,
                version=task_integration_checkpoints.c.version + 1,
                updated_at=self.clock(),
            )
        )
        if result.rowcount != 1:
            raise HierarchyError("stale_parent", "parent generation changed")

    async def _write_task_extras(
        self,
        conn,
        task_id: str,
        *,
        requirements: list[tuple[str, str | None]] | None,
        edges: list[tuple[str, str, str | None]] | None,
        labels: list[str] | None,
    ) -> None:
        if requirements:
            await self.db.add_task_workspace_requirements(task_id, requirements, conn=conn)
        for depends_on, dep_type, description in edges or ():
            await self.db.add_dependency(
                task_id,
                depends_on,
                dep_type,
                description=description,
                conn=conn,
            )
        for label in labels or ():
            await self.db.add_task_label(task_id, label, conn=conn)

    async def _maybe_create_routing_gate(self, conn, task: Task, routing_policy) -> str | None:
        if routing_policy is None or not routing_policy(task):
            return None
        gate_id, _created = await self.db.create_gate(
            task.project_id,
            "routing",
            "Route task",
            question="Assign profile + intelligence class (+ workspace if profile needs one).",
            waiter_task_ids=[task.id],
            conn=conn,
        )
        task.is_blocked = True
        return gate_id

    @staticmethod
    def _build_child(parent: dict, repository_id: str, task_id: str, values: dict) -> Task:
        allowed = {field.name for field in fields(Task)} - {
            "id",
            "project_id",
            "repo_id",
            "parent_task_id",
            "branch_name",
            "status",
        }
        unknown = set(values) - allowed - {"reason"}
        if unknown:
            raise HierarchyError("invalid", "unknown child fields: " + ", ".join(sorted(unknown)))
        title = str(values.get("title") or "").strip()
        if not title:
            raise HierarchyError("invalid", "child title is required")
        supplied = {key: value for key, value in values.items() if key in allowed}
        supplied["title"] = title
        supplied.setdefault("description", title)
        return Task(
            id=task_id,
            project_id=parent["project_id"],
            repo_id=repository_id,
            parent_task_id=None,
            branch_name=f"aq/{task_id}",
            status=TaskStatus.DEFINED,
            **supplied,
        )

    async def _assert_reparentable(self, conn, task_id: str, repository_id: str) -> None:
        origin = await self._locked_origin(conn, task_id, repository_id)
        if origin["materialized"]:
            raise HierarchyError("delivery_target_fixed", "branch origin is already materialized")
        task = await self._task_row(conn, task_id)
        if task["status"] not in {TaskStatus.DEFINED.value, TaskStatus.READY.value}:
            raise HierarchyError("delivery_target_fixed", "task already started")
        busy = (
            await conn.execute(
                select(workspaces.c.id).where(workspaces.c.locked_by_task_id == task_id).limit(1)
            )
        ).first()
        live = (
            await conn.execute(
                select(sessions.c.id)
                .where(sessions.c.task_id == task_id)
                .where(sessions.c.state.in_(("starting", "running", "draining")))
                .limit(1)
            )
        ).first()
        delivered = (
            await conn.execute(
                select(task_delivery_receipts.c.id)
                .where(task_delivery_receipts.c.source_task_id == task_id)
                .limit(1)
            )
        ).first()
        sealed = (
            await conn.execute(
                select(integration_batch_members.c.task_id)
                .select_from(
                    integration_batch_members.join(
                        integration_batches,
                        integration_batches.c.id == integration_batch_members.c.batch_id,
                    )
                )
                .where(integration_batch_members.c.task_id == task_id)
                .where(integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_STATES))
                .limit(1)
            )
        ).first()
        if busy or live or delivered:
            raise HierarchyError("delivery_target_fixed", "task has started or delivered work")
        if sealed:
            raise HierarchyError("sealed", "task belongs to an active sealed batch")


__all__ = [
    "HierarchyIntegration",
    "hierarchy_mode_enabled",
    "materialize_exact_branch",
    "verify_workspace_checkpoint",
]
