"""Start fresh aggregate verification after a completed parent's PR advances."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any

from sqlalchemy import insert, select, update

from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.integration_train_queries import (
    _ACTIVE_BATCH_LIFECYCLES,
    _root_delivery_receipt_conditions,
)
from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery
from src.integration.models import BranchKey, Fence, HierarchicalIntegrationPolicy
from src.integration.ownership import BranchOwnership
from src.integration.records import ParentEpisodeRecords
from src.integration.parent_engine import parent_engine_guard
from src.integration.review_evidence import ReviewEvidenceProducer
from src.models import TaskStatus

_OID = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ParentHeadObservation:
    """A configured GitHub read of an open, canonical parent pull request."""

    task_id: str
    source: dict[str, Any]
    head_sha: str
    policy_generation: int


class ParentSourceReverification:
    def __init__(self, db, promotion, *, clock=time.time):
        self.db, self.promotion, self.clock = db, promotion, clock
        self.producer = ReviewEvidenceProducer(db, promotion, clock=clock)

    @parent_engine_guard()
    async def run(self, task_id: str, observation: ParentHeadObservation) -> dict:
        source, head = observation.source, observation.head_sha
        if (
            task_id != observation.task_id
            or source.get("review_kind") != "parent"
            or not isinstance(head, str)
            or not _OID.fullmatch(head)
            or head == source.get("head")
        ):
            raise HierarchyError("invalid", "observation does not name a moved parent head")
        owner_statement = select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.repository_id == source["repository_id"],
            t.integration_branch_owners.c.ref == source["branch"],
        )
        receipt_statement = (
            select(t.task_delivery_receipts)
            .where(
                t.task_delivery_receipts.c.target_task_id == task_id,
                t.task_delivery_receipts.c.repository_id == source["repository_id"],
                t.task_delivery_receipts.c.target_branch == source["branch"],
            )
            .order_by(t.task_delivery_receipts.c.id)
        )
        async with self.db._engine.connect() as conn:
            if await self.producer._pull_request_source_on(
                conn, task_id
            ) != source or not await self.producer._authorization_on(
                conn,
                task_id,
                source,
                observation.policy_generation,
            ):
                raise HierarchyError("stale", "parent source or authorization changed")
            observed_owner = (await conn.execute(owner_statement)).mappings().one_or_none()
            if (
                observed_owner is None
                or observed_owner["handoff_state"] not in {"reserved", "released"}
                or observed_owner["session_id"] is not None
                or observed_owner["workspace_id"] is not None
            ):
                raise HierarchyError("waiting", "parent branch has no detached aggregate owner")
            receipts = (await conn.execute(receipt_statement)).mappings().all()
        # Remote reads precede the short mutation transaction. The transaction
        # rechecks the exact source, receipts and fence, as well as every
        # admission control, before changing the aggregate.
        await CancelledCollectionRecovery(self.db, self.promotion)._prove(
            {
                "project_id": source["project_id"],
                "target": BranchKey(repository_id=source["repository_id"], branch=source["branch"]),
                "expected_tip": head,
                "episode": {"pre_collection_checkpoint_sha": source["head"]},
                "receipts": receipts,
            }
        )
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            parent = (
                (
                    await conn.execute(
                        select(t.tasks)
                        .where(
                            t.tasks.c.id == task_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            project = (
                (
                    await conn.execute(
                        select(t.projects)
                        .where(
                            t.projects.c.id == source["project_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                parent is None
                or project is None
                or project["status"] != "ACTIVE"
                or project["hierarchical_integration_draining"]
                or parent["assigned_agent_id"] is not None
                or await self.producer._pull_request_source_on(conn, task_id) != source
            ):
                raise HierarchyError("stale", "completed parent source changed or is inactive")
            policy = HierarchicalIntegrationPolicy.model_validate(
                project["hierarchical_integration_policy"]
            )
            if not policy.root.repair.source_ci or not await self.producer._authorization_on(
                conn,
                task_id,
                source,
                observation.policy_generation,
            ):
                raise HierarchyError(
                    "unauthorized", "automatic parent reverification is not authorized"
                )
            if await self.db._read_manual_pause(conn, task_id) is not None:
                raise HierarchyError("waiting", "parent has a manual pause")
            checkpoint = dict(
                (
                    await conn.execute(
                        select(t.task_integration_checkpoints)
                        .where(
                            t.task_integration_checkpoints.c.task_id == task_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            operation = (
                (
                    await conn.execute(
                        select(t.integration_repair_operations)
                        .where(
                            t.integration_repair_operations.c.id
                            == checkpoint["last_completed_operation_id"],
                            t.integration_repair_operations.c.parent_task_id == task_id,
                            t.integration_repair_operations.c.episode_id
                            == checkpoint["episode_id"],
                            t.integration_repair_operations.c.state == "completed",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operation is None:
                raise HierarchyError("invalid", "parent has no completed aggregate operation")
            for writer in {task_id, operation["verifier_task_id"]} - {None}:
                if await CancelledCollectionRecovery._live_holder_on(conn, writer):
                    raise HierarchyError("waiting", "a session or workspace still holds the parent")
            repo = (
                (
                    await conn.execute(
                        select(t.repos).where(
                            t.repos.c.id == source["repository_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            if source["branch"].removeprefix("refs/heads/") == repo["default_branch"]:
                raise HierarchyError("invalid", "parent source is the default branch")
            await self._require_undelivered_on(conn, task_id, source, repo)
            owner = (await conn.execute(owner_statement.with_for_update())).mappings().one_or_none()
            if (
                owner is None
                or owner != observed_owner
                or owner["owner_id"]
                not in {task_id, operation["id"], operation["verifier_task_id"]}
            ):
                raise HierarchyError("stale", "parent branch ownership changed during proof")
            if (await conn.execute(receipt_statement)).mappings().all() != receipts:
                raise HierarchyError("stale", "parent receipts changed during proof")
            # The normal reopen guard must see the previous completed
            # episode: it preserves delivered child identities and advances
            # the parent generation without reopening any child.
            transition = await self.db._apply_transition(
                conn,
                task_id,
                TaskStatus.PAUSED,
                force=True,
                context="integration_parent_source_advanced",
                assigned_agent_id=None,
                _manual_pause_control=True,
            )
            completion = ParentEpisodeRecords(self.db, clock=self.clock)
            new_checkpoint = checkpoint | {
                "episode_id": None,
                "generation": source["generation"] + 1,
            }
            replacement = await completion.reserve_episode_on(
                conn,
                parent=dict(parent),
                project=dict(project),
                checkpoint=new_checkpoint,
                pre_collection_sha=head,
                carry_forward={
                    "operation_id": operation["id"],
                    "episode_id": operation["episode_id"],
                    "verification_id": source["verification_id"],
                    "head_sha": source["head"],
                },
            )
            fence = await BranchOwnership(self.db).transfer_detached_on(
                conn,
                Fence(
                    target=BranchKey(
                        repository_id=source["repository_id"], branch=source["branch"]
                    ),
                    owner_id=owner["owner_id"],
                    token=owner["fence_token"],
                ),
                replacement["id"],
                "collector",
            )
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(
                    t.task_integration_checkpoints.c.task_id == task_id,
                )
                .values(
                    episode_id=replacement["episode_id"],
                    generation=new_checkpoint["generation"],
                    checkpoint_sha=head,
                    verified_sha=None,
                    verified_generation=None,
                    current_verification_id=None,
                    state="awaiting_children",
                    version=t.task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
            readiness = await completion.mark_ready_on(conn, task_id, require_verifier=True)
            if readiness.get("state") != "integration_ready":
                raise HierarchyError("waiting", "parent cannot reverify: " + str(readiness))
            verifier_id = await conn.scalar(
                select(t.integration_repair_operations.c.verifier_task_id).where(
                    t.integration_repair_operations.c.id == replacement["id"]
                )
            )
            await conn.execute(
                insert(t.events).values(
                    project_id=source["project_id"],
                    event_type="integration.parent_source_advanced",
                    task_id=task_id,
                    payload=json.dumps(
                        {
                            "task_id": task_id,
                            "previous_verification_id": source["verification_id"],
                            "previous_head_sha": source["head"],
                            "head_sha": head,
                            "operation_id": replacement["id"],
                            "generation": new_checkpoint["generation"],
                            "collector_fence_token": fence.token,
                        }
                    ),
                    timestamp=self.clock(),
                )
            )
        await self.db.log_blocked_flips(transition.flipped)
        return {
            "outcome": "reverifying",
            "task_id": task_id,
            "operation_id": replacement["id"],
            "generation": new_checkpoint["generation"],
            "head_sha": head,
            "verifier_task_id": verifier_id,
        }

    async def _require_undelivered_on(self, conn, task_id, source, repo):
        if await conn.scalar(
            select(t.integration_batch_members.c.task_id)
            .join(
                t.integration_batches,
                t.integration_batches.c.id == t.integration_batch_members.c.batch_id,
            )
            .where(
                t.integration_batch_members.c.task_id == task_id,
                t.integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_LIFECYCLES),
            )
            .limit(1)
        ):
            raise HierarchyError("waiting", "parent already belongs to an active train batch")
        if await conn.scalar(
            select(t.task_delivery_receipts.c.id)
            .where(
                t.task_delivery_receipts.c.source_task_id == task_id,
                *_root_delivery_receipt_conditions(source["repository_id"], repo["default_branch"]),
            )
            .limit(1)
        ):
            raise HierarchyError("delivered", "parent has already been delivered")
        for table, branch, pending in (
            (
                t.integration_promotion_intents,
                t.integration_promotion_intents.c.target_branch,
                t.integration_promotion_intents.c.state.not_in(
                    ["committed", "conflict", "superseded"]
                ),
            ),
            (
                t.integration_candidate_ref_mutations,
                t.integration_candidate_ref_mutations.c.branch,
                t.integration_candidate_ref_mutations.c.state == "reserved",
            ),
        ):
            if await conn.scalar(
                select(table.c.id)
                .where(
                    table.c.repository_id == source["repository_id"],
                    branch.in_([source["branch"], "refs/heads/" + source["branch"]]),
                    pending,
                )
                .limit(1)
            ):
                raise HierarchyError("waiting", "parent has an unresolved external write")
