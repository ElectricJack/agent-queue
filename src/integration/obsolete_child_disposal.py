"""Explicit, fenced ours-merge disposition of a superseded completed child."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

from sqlalchemy import or_, select

from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_promotion_intents,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    tasks,
    workspaces,
)
from src.git.manager import GitError, RemoteRefState
from src.integration.child_delivery import _ProofFailed, _snapshot_identity, _snapshot_on
from src.integration.delivery_observer import prerequisite_observer
from src.integration.delivery_truth import DeliveryState
from src.integration.engine import RootEngineOwnership
from src.integration.lock import BranchLock
from src.integration.ownership import BranchOwnershipError
from src.integration.provenance import GitProvenance
from src.integration.models import BranchKey
from src.integration.obsolete_children import obsolete_child_blocker


class ObsoleteChildDisposal:
    """Keep the parent's tree, but publish real ancestry and an immutable audit."""

    def __init__(self, db, *, authorize=None):
        self.db = db
        self.authorize = authorize

    async def _snapshot(self, conn, task_id):
        snapshot = await _snapshot_on(conn, task_id)
        if (snapshot is None or snapshot["status"] != "COMPLETED"
                or snapshot["mode"] not in ("hierarchy", "train")
                or snapshot["has_children"] or snapshot["object_experiment"]):
            raise _ProofFailed("obsolete disposition requires a completed code leaf in a managed project")
        parent = snapshot["parent"]
        if (parent is None or not parent["branch_name"] or not snapshot["branch"]
                or parent["repo_id"] != snapshot["repository_id"]
                or snapshot["repository_id"] != snapshot["integration_repository_id"]):
            raise _ProofFailed("the child and current parent must bind the integration repository")
        origin = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.repository_id == snapshot["repository_id"],
            task_branch_origins.c.retired_at.is_(None),
        ))).mappings().one_or_none()
        if (origin is None or not origin["reserved"] or not origin["materialized"]
                or origin["branch_name"] != snapshot["branch"]
                or origin["parent_task_id"] != parent["id"]
                or origin["parent_repository_id"] != snapshot["repository_id"]
                or not origin["parent_ref"]
                or origin["parent_ref"].removeprefix("refs/heads/") !=
                parent["branch_name"].removeprefix("refs/heads/")):
            raise _ProofFailed("the reserved source origin does not bind this parent branch")
        repository = (await conn.execute(select(repos).where(
            repos.c.id == snapshot["repository_id"],
        ))).mappings().one_or_none()
        completion = (await conn.execute(select(task_completion_records).where(
            task_completion_records.c.id == snapshot["completion_id"],
        ))).mappings().one_or_none()
        if (repository is None or repository["project_id"] != snapshot["project_id"]
                or completion is None or completion["outcome"] != "pass"
                or completion["branch"] not in (None, snapshot["branch"])):
            raise _ProofFailed("the repository or passing completion binding is missing")
        try:
            commits = json.loads(completion["commits"])
        except (TypeError, ValueError) as exc:
            raise _ProofFailed("the passing completion has invalid source commits") from exc
        if not isinstance(commits, list) or not commits:
            raise _ProofFailed("a code-free child needs the guarded record-noop command")
        if snapshot["head_sha"] and snapshot["head_sha"] != commits[-1]:
            raise _ProofFailed("the checkpoint and completed source disagree")
        snapshot["head_sha"] = commits[-1]
        identity = {
            "task": _snapshot_identity(snapshot), "parent": dict(parent),
            "origin": dict(origin), "repository": dict(repository), "completion": dict(completion),
            "mode": snapshot["mode"], "integration_repository_id": snapshot["integration_repository_id"],
            "parent_checkpoint": snapshot["parent_checkpoint"], "operation": snapshot["operation"],
        }
        parent_project = await conn.scalar(select(tasks.c.project_id).where(
            tasks.c.id == parent["id"],
        ))
        if parent_project != snapshot["project_id"]:
            raise _ProofFailed("the parent no longer belongs to the child's project")
        branch = parent["branch_name"].removeprefix("refs/heads/")
        if (not branch.startswith("aq/")
                or branch == repository["default_branch"].removeprefix("refs/heads/")
                or branch == snapshot["branch"].removeprefix("refs/heads/")):
            raise _ProofFailed("obsolete disposition requires a distinct managed parent branch")
        if snapshot["operation"] is not None:
            raise _ProofFailed("a live parent collection owns this child")
        ids = (task_id, parent["id"])
        branches = (snapshot["branch"].removeprefix("refs/heads/"), branch)
        refs = (*branches, *("refs/heads/" + name for name in branches))
        queries = (
            select(tasks.c.id).where(tasks.c.id.in_(ids), tasks.c.assigned_agent_id.is_not(None)),
            select(sessions.c.id).where(sessions.c.task_id.in_(ids), sessions.c.state != "stopped"),
            select(workspaces.c.id).where(workspaces.c.locked_by_task_id.in_(ids)),
            select(integration_branch_owners.c.owner_id).where(
                integration_branch_owners.c.repository_id == snapshot["repository_id"],
                integration_branch_owners.c.ref.in_(refs),
                integration_branch_owners.c.handoff_state.in_(("attached", "handoff_pending")),
            ),
            select(integration_batches.c.id).where(
                integration_batches.c.repository_id == snapshot["repository_id"],
                integration_batches.c.lifecycle.not_in(("promoted", "aborted", "failed", "empty")),
                or_(integration_batches.c.target_ref.in_(refs),
                    integration_batches.c.target_ref.is_(None)),
            ),
            select(integration_promotion_intents.c.id).where(
                integration_promotion_intents.c.repository_id == snapshot["repository_id"],
                integration_promotion_intents.c.target_branch.in_(refs),
                integration_promotion_intents.c.state.not_in(("committed", "superseded")),
            ),
        )
        for query in queries:
            if await conn.scalar(query.exists().select()):
                raise _ProofFailed("a live writer, workspace, batch or promotion owns these branches")
        return snapshot, identity

    async def _proof(self, observed, snapshot, superseded_by):
        git, store = observed.git, observed.store
        args = dict(task_id=snapshot["task_id"], source_base=snapshot["base_sha"],
                    source_oid=snapshot["head_sha"])
        if not await obsolete_child_blocker(observed, **args):
            raise _ProofFailed("the child does not exclusively change removed parent paths")
        provenance = GitProvenance(git, store, repository_url=observed.repository_url)
        if not await provenance.strict_ancestor(superseded_by, observed.target_oid):
            raise _ProofFailed("superseded-by is not an ancestor of the parent")
        if not await provenance.strict_ancestor(snapshot["base_sha"], superseded_by):
            raise _ProofFailed("superseded-by is not descended from the child's origin base")
        previous = await git.arun_git_result([
            "--no-replace-objects", "rev-parse", "--verify", superseded_by + "^1",
        ], cwd=store)
        if previous.returncode:
            raise _ProofFailed("superseded-by has no first parent proving the removal")
        at_removal = replace(observed, target_oid=superseded_by)
        before_removal = replace(observed, target_oid=previous.stdout.strip())
        if (not await obsolete_child_blocker(at_removal, **args)
                or await obsolete_child_blocker(before_removal, **args)):
            raise _ProofFailed("superseded-by did not remove the last of the child's changed paths")

    async def run(self, request, *, operator_id):
        result = {"task_id": request.task_id, "outcome": "blocked"}
        observer = prerequisite_observer(self.db)
        if observer is None:
            return {**result, "reason": "obsolete disposition requires active Git-first delivery"}
        if not operator_id:
            return {**result, "reason": "obsolete disposition requires an authorized operator"}
        try:
            async with self.db._engine.connect() as conn:
                snapshot, identity = await self._snapshot(conn, request.task_id)
            target = "refs/heads/" + snapshot["parent"]["branch_name"].removeprefix("refs/heads/")
            view = await observer.observe([request.task_id])
            observed = next((item for item in view.snapshots if (
                item.project_id, item.repository_id, item.target_ref
            ) == (snapshot["project_id"], snapshot["repository_id"], target)), None)
            if observed is None or observed.error or not observed.target_oid or not await view.fresh():
                raise _ProofFailed("the current parent could not be observed")
            git, store = observed.git, observed.store
            result.update(project_id=snapshot["project_id"], parent_task_id=snapshot["parent_task_id"],
                          head_sha=snapshot["head_sha"], parent_head_sha=observed.target_oid,
                          superseded_by=request.superseded_by, parent_branch=target)
            async with self.db._engine.connect() as conn:
                verified = (await view.verified_on(conn, [request.task_id])).get(request.task_id)
            if (verified is None or verified.source_oid != snapshot["head_sha"]
                    or verified.state not in (DeliveryState.PENDING, DeliveryState.CONTAINED)):
                raise _ProofFailed("the completed source binding could not be verified")
            if request.expected_head_sha and request.expected_head_sha != snapshot["head_sha"]:
                return {**result, "outcome": "changed", "reason": "the previewed child head changed"}
            # Idempotence uses strict ancestry even with a legacy-equivalence observer.
            if await GitProvenance(git, store, repository_url=observed.repository_url).strict_ancestor(
                snapshot["head_sha"], observed.target_oid,
            ):
                return {**result, "outcome": "nothing_to_redrive",
                        "reason": "the child is already an ancestor of the parent"}
            if (request.expected_parent_head_sha
                    and request.expected_parent_head_sha != observed.target_oid):
                return {**result, "outcome": "changed", "reason": "the previewed parent head changed"}
            await self._proof(observed, snapshot, request.superseded_by)
            if request.dry_run:
                return {**result, "outcome": "would_advance",
                        "reason": "would merge the obsolete child with the parent's unchanged tree"}
            if not request.reason or not request.reason.strip() or not request.expected_head_sha:
                raise _ProofFailed("applying requires exact heads and a nonblank reason")
            if not request.expected_parent_head_sha:
                raise _ProofFailed("applying requires the exact parent head")
            locks = BranchLock(self.db)
            deadline = asyncio.get_running_loop().time() + 30
            async with asyncio.timeout_at(deadline):
                async with RootEngineOwnership(self.db).exclusion(snapshot["repository_id"]) as conn:
                    await self.db.lock_hierarchy_project(conn, snapshot["project_id"])
                    if self.authorize is not None:
                        actor, refusal = await self.authorize()
                        if refusal or actor != operator_id:
                            raise _ProofFailed("operator authority changed before publication")
                    _current, current_identity = await self._snapshot(conn, request.task_id)
                    if current_identity != identity:
                        raise _ProofFailed("the completion or parent binding changed")
                    proof = (await view.verified_on(conn, [request.task_id])).get(request.task_id)
                    if (proof is None or proof.source_oid != snapshot["head_sha"]
                            or proof.state not in (DeliveryState.PENDING, DeliveryState.CONTAINED)):
                        raise _ProofFailed("the retained source identity changed")
                    fences = []
                    holder = "obsolete-disposition:" + uuid.uuid4().hex
                    for branch in sorted((snapshot["branch"], target)):
                        fences.append(await locks.acquire_on(conn, BranchKey(
                            repository_id=snapshot["repository_id"], branch=branch,
                        ), holder, ttl_seconds=30, role="collector"))
                    async with git.arepository_transaction(store):
                        if await git.aget_remote_url(store) != observed.repository_url:
                            raise _ProofFailed("the retained repository destination changed")
                        refs = await git.als_remote_qualified_refs(store, [target,
                            "refs/heads/" + snapshot["branch"].removeprefix("refs/heads/")],
                            repository_url=observed.repository_url)
                        expected = {target: observed.target_oid,
                                    "refs/heads/" + snapshot["branch"].removeprefix("refs/heads/"):
                                    snapshot["head_sha"]}
                        if any(refs.get(ref) is None or refs[ref].state is not RemoteRefState.PRESENT
                               or refs[ref].oid != oid for ref, oid in expected.items()):
                            raise _ProofFailed("a published child or parent head moved")
                        tree = await git.atree_sha(store, observed.target_oid)
                        message = (
                            f"Dispose obsolete child {request.task_id} with unchanged parent tree\n\n"
                            f"{request.reason.strip()}\n\n"
                            f"AQ-Obsolete-Child: {request.task_id}\n"
                            f"AQ-Child-Head: {snapshot['head_sha']}\n"
                            f"AQ-Parent-Head: {observed.target_oid}\n"
                            f"AQ-Superseded-By: {request.superseded_by}\n"
                            f"AQ-Disposition-Actor: {operator_id}\n"
                        )
                        commit = await git.arun_git_result([
                            "--no-replace-objects", *git.resolve_commit_identity().config_args(),
                            "commit-tree", tree, "-p", observed.target_oid,
                            "-p", snapshot["head_sha"], "-m", message,
                        ], cwd=store)
                        if commit.returncode:
                            raise GitError("could not create obsolete disposition merge")
                        merge = commit.stdout.strip()
                        for fence in fences:
                            locks.require_current(await locks.lock_on(conn, fence.target), fence)
                        await git.apush_qualified_ref(
                            store, ref=target, tip_oid=merge, expected_old_oid=observed.target_oid,
                            authority_deadline=deadline,
                        )
                    for fence in fences:
                        await locks.release(fence, conn=conn)
            return {**result, "outcome": "advanced", "merge_head_sha": merge, "tree_sha": tree,
                    "reason": "published an audited unchanged-tree merge of the obsolete child"}
        except (_ProofFailed, GitError, OSError, BranchOwnershipError, TimeoutError) as exc:
            return {**result, "reason": str(exc)}
