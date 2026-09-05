"""Atomic child filing and mutation fencing for hierarchical delivery."""

from __future__ import annotations

import inspect
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import fields
from typing import Any

from sqlalchemy import insert, select, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    sessions,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.integration.models import BranchKey
from src.integration.outbox import enqueue_integration_event
from src.integration.ownership import BranchOwnership
from src.models import RepoConfig, Task, TaskStatus
from src.task_names import child_task_id
from src.task_names import fresh_root_id


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


async def verify_workspace_checkpoint(db, git, task: dict, repo: RepoConfig, head_sha: str) -> str:
    """Prove a parent's actual checkout HEAD is clean and exactly pushed."""
    workspace = await db.get_workspace_for_task(task["id"])
    if (
        workspace is None
        or workspace.locked_by_task_id != task["id"]
        or task["repo_id"] != repo.id
        or task["branch_name"] != f"aq/{task['id']}"
    ):
        raise HierarchyError("dirty", "task has no exact owned integration workspace")
    checkout = workspace.workspace_path
    branch = await git.aget_current_branch(checkout, strict=True)
    if branch != task["branch_name"]:
        raise HierarchyError("dirty", "workspace is not on the canonical task branch")
    status = await git._arun(["status", "--porcelain"], cwd=checkout)
    if status:
        raise HierarchyError("dirty", "workspace has uncommitted changes")
    actual_head = (await git._arun(["rev-parse", "HEAD"], cwd=checkout)).lower()
    if actual_head != head_sha:
        raise HierarchyError("dirty", "caller head does not match workspace HEAD")
    from src.git.manager import RemoteRefState

    remote = await git.als_remote_ref(checkout, task["branch_name"])
    if remote.state is not RemoteRefState.PRESENT or remote.oid != actual_head:
        raise HierarchyError("dirty", "workspace HEAD is not exactly pushed")
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
        ownership: BranchOwnership | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.default_head_resolver = default_head_resolver
        self.branch_materializer = branch_materializer
        self.checkpoint_verifier = checkpoint_verifier
        self.ownership = ownership or BranchOwnership(db)
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
    ) -> dict:
        """Insert a validated command-layer task in the caller's transaction."""
        parent = await self._task_row(conn, parent_id)
        project, repo = await self._enabled_route(conn, parent)
        await self.db.lock_hierarchy_project(conn, project["id"])
        await self._ensure_origin_chain(conn, parent_id, repo)
        checkpoint = await self._locked_checkpoint(conn, parent_id)
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
    ) -> list[dict]:
        """Insert sibling tasks with one parent-generation advance."""
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
            created.append({"task_id": task_id, "origin": origin})
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
        from src.database.tables import projects

        project = (
            await conn.execute(select(projects).where(projects.c.id == task.project_id))
        ).mappings().one_or_none()
        if project is None or project["hierarchical_integration_mode"] not in {
            "hierarchy",
            "train",
        }:
            raise HierarchyError("invalid", "hierarchical integration is not enabled")
        repository_id = project["integration_repository_id"]
        repo = await self._repo_on(conn, repository_id) if repository_id else None
        if repo is None or repo.project_id != task.project_id:
            raise HierarchyError("invalid", "designated repository is not in the project")
        await self.db.lock_hierarchy_project(conn, task.project_id)
        task.id = await fresh_root_id(conn)
        task.parent_task_id = None
        task.repo_id = repo.id
        task.branch_name = f"aq/{task.id}"
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
        return {"task_id": task.id, "generation": 0, "gate_id": gate_id}

    async def checkpoint_parent(self, task_id: str, head_sha: str, generation: int) -> dict:
        if not _OID.fullmatch(head_sha):
            raise HierarchyError("dirty", "head_sha must be a lowercase 40-character Git OID")
        if self.checkpoint_verifier is None:
            raise HierarchyError("dirty", "workspace checkpoint verifier is unavailable")
        async with self.db._engine.connect() as read_conn:
            task = await self._task_row(read_conn, task_id)
            _project, repo = await self._enabled_route(read_conn, task)
        verified = self.checkpoint_verifier(task, repo, head_sha)
        if inspect.isawaitable(verified):
            verified = await verified
        if verified != head_sha:
            raise HierarchyError("dirty", "checkpoint verifier returned another head")
        async with self.db.immediate() as conn:
            task = await self._task_row(conn, task_id)
            project, repo = await self._enabled_route(conn, task)
            await self.db.lock_hierarchy_project(conn, project["id"])
            await self._ensure_origin_chain(conn, task_id, repo)
            checkpoint = await self._locked_checkpoint(conn, task_id)
            if int(checkpoint["generation"]) != generation:
                raise HierarchyError(
                    "stale", f"expected generation {generation}, found {checkpoint['generation']}"
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
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
        return {"outcome": "checkpointed", "task_id": task_id, "generation": generation, "head_sha": head_sha}

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

    async def _ensure_origin_chain(self, conn, task_id: str, repo: RepoConfig) -> None:
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
                base_sha = await self._resolve_head(repo, row["branch_name"] or repo.default_branch)
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
                generation=0,
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
        gate_id = await self.db.create_gate(
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
