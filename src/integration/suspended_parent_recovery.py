"""Consume a producer's durable detach proof into its existing collector episode."""

from __future__ import annotations

import json
import time

from sqlalchemy import insert, select

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    agents,
    events,
    gates,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    repos,
    task_branch_origins,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.git.manager import GitError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, StaleFence
from src.integration.writers import OperationSafety

RECOVERY_EVENT = "integration.parent_collector_recovered"


class SuspendedParentRecovery:
    """Recover only the current, detached producer; never stop or release a writer."""

    def __init__(self, db, hierarchy, *, clock=time.time):
        self.db = db
        self.hierarchy = hierarchy
        self.clock = clock

    async def _facts_on(self, conn, task_id, *, lock=False):
        async def row(table, *conditions):
            query = select(table).where(*conditions)
            if lock:
                query = query.with_for_update()
            value = (await conn.execute(query)).mappings().one_or_none()
            return dict(value) if value is not None else None

        parent = await row(tasks, tasks.c.id == task_id)
        report = {"task_id": task_id, "kind": "suspended_worker"}

        def refuse(reason, outcome="blocked"):
            return {**report, "outcome": outcome, "reason": reason}, None

        if parent is None:
            return refuse("parent does not exist", "not_found")
        report.update(project_id=parent["project_id"], branch=parent["branch_name"])
        project = await row(projects, projects.c.id == parent["project_id"])
        if (
            project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["hierarchical_integration_draining"]
        ):
            return refuse("the project is not actively collecting")
        checkpoint = await row(
            task_integration_checkpoints, task_integration_checkpoints.c.task_id == task_id
        )
        if (
            parent["status"] != "PAUSED"
            or parent["assigned_agent_id"] is not None
            or checkpoint is None
            or checkpoint["state"] != "awaiting_children"
            or not checkpoint["episode_id"]
        ):
            return refuse("the parent is not an unassigned suspended producer awaiting children")
        report["checkpoint"] = checkpoint
        repo = await row(repos, repos.c.id == parent["repo_id"])
        branch = parent["branch_name"]
        if (
            repo is None
            or repo["project_id"] != parent["project_id"]
            or repo["id"] != project["integration_repository_id"]
            or not branch
            or branch.removeprefix("refs/heads/") == repo["default_branch"]
            or checkpoint["repository_id"] != repo["id"]
            or checkpoint["branch"] != branch
        ):
            return refuse("the parent's canonical integration branch identity is inconsistent")
        origin = await row(
            task_branch_origins,
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.repository_id == repo["id"],
            task_branch_origins.c.retired_at.is_(None),
        )
        if (
            origin is None
            or not origin["materialized"]
            or not origin["reserved"]
            or origin["branch_name"] != branch
            or origin["parent_task_id"] != parent["parent_task_id"]
            or origin["created_at"] < parent["created_at"]
        ):
            return refuse("the current parent has no matching materialized branch origin")
        episode = await row(
            integration_parent_episodes,
            integration_parent_episodes.c.id == checkpoint["episode_id"],
        )
        operation = await row(
            integration_repair_operations,
            integration_repair_operations.c.parent_task_id == task_id,
            integration_repair_operations.c.episode_id == checkpoint["episode_id"],
        )
        if (
            episode is None
            or operation is None
            or operation["target_kind"] != "parent"
            or operation["batch_id"] is not None
            or episode["parent_task_id"] != task_id
            or episode["repository_id"] != repo["id"]
            or episode["created_at"] < parent["created_at"]
            or episode["generation"] > checkpoint["generation"]
            or episode["pre_collection_checkpoint_sha"] != checkpoint["checkpoint_sha"]
        ):
            return refuse("the parent has no matching current collection episode and operation")
        report.update(operation_id=operation["id"], episode_id=episode["id"])
        if operation["state"] not in {"active", "escalated"}:
            return refuse("the operation is not collecting; human_required needs its own decision")
        holds = dict(
            (
                await conn.execute(
                    select(task_metadata.c.key, task_metadata.c.value).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key.in_(
                            ("manual_pause", TERMINAL_BLOCKED_META_KEY, "needs_attention")
                        ),
                    )
                )
            ).all()
        )
        if holds:
            return refuse("an operator hold or operational failure requires its own decision")
        gate = await conn.scalar(
            select(gates.c.id)
            .join(task_gates, task_gates.c.gate_id == gates.c.id)
            .where(task_gates.c.task_id == task_id, gates.c.status == "open")
            .limit(1)
        )
        if gate is not None:
            return refuse(f"open gate {gate} requires its own decision")
        # Resolved per call, like ``_cmd_integration_reopen_collection`` does:
        # a module-level binding would capture whatever
        # ``cancelled_collection_recovery.CancelledCollectionRecovery`` names at
        # *our* import time, so a test that substitutes the recovery class while
        # this module loads would leave the substitute bound here for good.
        from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery

        if (
            await CancelledCollectionRecovery._live_holder_on(conn, task_id)
            or await conn.scalar(
                select(agents.c.id).where(agents.c.current_task_id == task_id).limit(1)
            )
            is not None
        ):
            return refuse("a session, agent or workspace still holds the parent")
        stage = await conn.scalar(
            select(integration_repair_stages.c.ordinal)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.repair_task_id.is_not(None),
                integration_repair_stages.c.state.not_in(
                    ("passed", "failed", "expired", "cancelled")
                ),
            )
            .limit(1)
        )
        if (
            stage is not None
            or operation["verifier_task_id"] is not None
            or checkpoint["current_verification_id"] is not None
        ):
            return refuse("a repair writer or verifier already owns this collection phase")
        target = BranchKey(repository_id=repo["id"], branch=branch)
        owner = await row(
            integration_branch_owners,
            integration_branch_owners.c.repository_id == repo["id"],
            integration_branch_owners.c.ref == branch,
        )
        if owner is None:
            return refuse("the parent branch has no ownership record")
        report["owner"] = owner
        if (
            owner["handoff_state"] not in {"reserved", "released"}
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
        ):
            return refuse("the branch still has an attached or unresolved writer")
        if owner["owner_id"] == operation["id"] and owner["owner_role"] == "collector":
            return refuse("the current operation already owns collection", "nothing_to_reopen")
        if (
            owner["owner_id"] != task_id
            or owner["owner_role"] != "worker"
            or not owner["confirmed_workspace_id"]
        ):
            return refuse("the current producer lacks a durable confirmed workspace detach proof")
        blockers = await OperationSafety._ambiguous_writes_on(conn, operation)
        pending = await conn.scalar(
            select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.repository_id == repo["id"],
                integration_promotion_intents.c.target_branch == branch,
                integration_promotion_intents.c.state != "committed",
            )
            .limit(1)
        )
        mutation = await conn.scalar(
            select(integration_candidate_ref_mutations.c.id)
            .where(
                integration_candidate_ref_mutations.c.repository_id == repo["id"],
                integration_candidate_ref_mutations.c.branch == branch,
                integration_candidate_ref_mutations.c.state == "reserved",
            )
            .limit(1)
        )
        if blockers or pending is not None or mutation is not None:
            return refuse("unresolved external writes require reconciliation first", "ambiguous")
        receipts = [
            dict(r)
            for r in (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.parent_operation_id == operation["id"],
                        task_delivery_receipts.c.parent_episode_id == episode["id"],
                    )
                    .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
                )
            ).mappings()
        ]
        head = checkpoint["checkpoint_sha"]
        for receipt in receipts:
            if receipt["repository_id"] != repo["id"] or receipt["target_branch"] != branch:
                return refuse("a receipt belongs to another collection branch")
            if receipt["after_sha"]:
                if receipt["before_sha"] != head:
                    return refuse("collection receipts do not form a contiguous head chain")
                head = receipt["after_sha"]
        report.update(
            receipts=receipts,
            head_sha=head,
            outcome="would_reopen",
            reason="transfer the detached producer to its current episode collector",
        )
        facts = dict(
            parent=parent,
            project=project,
            checkpoint=checkpoint,
            origin=origin,
            episode=episode,
            operation=operation,
            owner=owner,
            receipts=receipts,
        )
        return report, (json.dumps(facts, sort_keys=True, default=str), target)

    async def run(
        self,
        task_id,
        *,
        dry_run=True,
        expected_head_sha=None,
        reason=None,
        operator_id=None,
        automatic=False,
    ):
        async with self.db._engine.connect() as conn:
            diagnosis, facts = await self._facts_on(conn, task_id)
        if diagnosis["outcome"] != "would_reopen":
            return diagnosis
        repo = await self.db.get_repo(facts[1].repository_id)
        try:
            remote_head = await self.hierarchy._resolve_head(repo, facts[1].branch)
        except GitError as exc:
            return {**diagnosis, "outcome": "blocked", "reason": str(exc)}
        if remote_head != diagnosis["head_sha"]:
            return {
                **diagnosis,
                "outcome": "blocked",
                "remote_head_sha": remote_head,
                "reason": "the published parent branch is not at the recorded collection head",
            }
        if dry_run:
            return diagnosis
        if not automatic and (expected_head_sha != remote_head or not reason or not reason.strip()):
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "supply the reported head and an audit reason",
            }
        try:
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, diagnosis["project_id"])
                current, current_facts = await self._facts_on(conn, task_id, lock=True)
                if current_facts != facts:
                    return {
                        **current,
                        "outcome": "changed",
                        "reason": "collection identity changed; repeat the dry run",
                    }
                owner = current["owner"]
                fence = await self.hierarchy.ownership.transfer_detached_on(
                    conn,
                    Fence(target=facts[1], owner_id=owner["owner_id"], token=owner["fence_token"]),
                    current["operation_id"],
                    "collector",
                )
                await conn.execute(
                    insert(events).values(
                        event_type=RECOVERY_EVENT,
                        project_id=current["project_id"],
                        task_id=task_id,
                        timestamp=self.clock(),
                        payload=json.dumps(
                            {
                                "operator_id": operator_id,
                                "reason": reason,
                                "episode_id": current["episode_id"],
                                "operation_id": current["operation_id"],
                                "head_sha": remote_head,
                                "previous_owner": owner,
                                "collector_fence_token": fence.token,
                            }
                        ),
                    )
                )
        except (BranchBusy, StaleFence) as exc:
            return {**diagnosis, "outcome": "changed", "reason": str(exc)}
        return {**diagnosis, "outcome": "reopened", "collector_fence_token": fence.token}
