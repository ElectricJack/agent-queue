"""Start fresh aggregate verification after a completed parent's PR advances."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any

from sqlalchemy import exists, insert, select, update

from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.integration_train_queries import (
    _ACTIVE_BATCH_LIFECYCLES,
    _root_delivery_receipt_conditions,
)
from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery
from src.integration.models import BranchKey, Fence, HierarchicalIntegrationPolicy
from src.integration.ownership import BranchOwnership
from src.integration.parent_completion import ParentCompletion
from src.integration.parent_engine import parent_engine_guard
from src.integration.review_evidence import ReviewEvidenceProducer
from src.models import TaskStatus

_OID = re.compile(r"^[0-9a-f]{40}$")


#: Refusals no reread can change while the PR head stays where it is: the
#: Git proof failed or the parent cannot reverify at all. The review poller
#: does not ask again until the head moves.
REFUSED_UNTIL_HEAD_MOVES = frozenset({"unproven", "invalid", "delivered"})


@dataclass(frozen=True)
class ParentHeadObservation:
    """A configured GitHub read of an open, canonical parent pull request.

    ``approval`` is the PR's human approval of exactly ``head_sha`` with no
    reviewer still requesting changes (``{"review_id", "reviewer_login"}``).
    It supersedes a rejection of the old verified head, so a reviewer who
    requested changes recovers the parent by approving the fixed head.
    """

    task_id: str
    source: dict[str, Any]
    head_sha: str
    policy_generation: int
    approval: dict[str, Any] | None = None


#: Escalations raised for a moved parent PR head AQ will not reverify.
ESCALATION_SOURCE_KIND = "integration_parent_source"


def refusal_escalation(
    observation: ParentHeadObservation, refusal: str, error: str
) -> dict[str, str] | None:
    """What a human must decide about a refused head, or None if nobody must.

    ``stale``, ``waiting`` and ``delivered`` clear without a decision; the
    rest leave the parent COMPLETED with an open PR until someone acts.
    """
    source, head = observation.source, observation.head_sha
    task, old, pr = observation.task_id, source["head"], source.get("pr_url") or "its PR"
    moved = f"Parent {task}'s PR head moved from verified {old[:12]} to {head[:12]}"
    if refusal == "rejected":
        summary = f"{moved} after a reviewer rejected the verified head"
        why = (
            "A review rejected the exact verified head, so automatic reverification "
            "waits for a human approval of the new head; operator root authorization "
            "also refuses while that rejection stands."
        )
        decision = (
            f"Ask a reviewer to approve PR head {head} on GitHub ({pr}) while no reviewer "
            "still requests changes. The review poller then starts a fresh aggregate "
            "verification of that head; a later push is checked again on its own."
        )
    elif refusal == "unproven":
        summary = f"{moved}, which does not preserve the verified aggregate"
        why = (
            f"A new head must descend from {old} and keep every accepted child receipt; "
            "Git proved this one does not (a rewrite or force-push). AQ does not probe "
            "this head again until the PR head moves."
        )
        decision = (
            f"Push the fix as new commits on top of {old} on {source['branch']} instead of "
            f"rewriting it, or close {pr} and re-plan the parent's work."
        )
    elif refusal == "unauthorized":
        summary = f"{moved}; the root policy does not allow automatic reverification"
        why = (
            "Automatic parent reverification needs root.admission authorized, "
            "root.repair.source_ci true and a task the root policy admits."
        )
        decision = (
            f"Enable it with `aq project set {source['project_id']} integration-policy ...`, "
            f"or restore {source['branch']} to {old} or close {pr}."
        )
    elif refusal == "invalid":
        summary = f"{moved}, but the parent cannot be reverified"
        why = "The parent's integration state cannot carry a new aggregate episode."
        decision = (
            f"Inspect the parent with `aq integration status` and `aq task show {task}`, "
            f"then repair its integration state or close {pr}."
        )
    else:
        return None
    return {
        "summary": summary,
        "investigation": (
            f"{error}\n\n{why} Until then the parent stays COMPLETED with its PR open "
            f"and nothing delivers it. PR: {pr}; branch {source['branch']}; "
            f"verified head {old}; PR head {head}."
        ),
        "decision_requested": decision,
    }


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
            if await self.producer._pull_request_source_on(conn, task_id) != source:
                raise HierarchyError("stale", "parent source changed")
            project = (
                (await conn.execute(select(t.projects).where(t.projects.c.id == source["project_id"])))
                .mappings()
                .one_or_none()
            )
            # Every refusal Postgres alone can decide comes before the remote
            # proof, so a parent that cannot reverify costs no Git reads.
            await self._admit_on(conn, task_id, observation, project)
            await self._completed_operation_on(conn, task_id)
            await self._require_undelivered_on(
                conn, task_id, source, await self._repo_on(conn, source)
            )
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
                or parent["assigned_agent_id"] is not None
                or await self.producer._pull_request_source_on(conn, task_id) != source
            ):
                raise HierarchyError("stale", "completed parent source changed")
            await self._admit_on(conn, task_id, observation, project)
            checkpoint, operation = await self._completed_operation_on(conn, task_id, lock=True)
            await self._require_undelivered_on(
                conn, task_id, source, await self._repo_on(conn, source)
            )
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
            completion = ParentCompletion(self.db, clock=self.clock)
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

    async def _admit_on(self, conn, task_id, observation, project) -> None:
        """Admission controls: checked before the remote proof and again under lock.

        The codes tell the poller what a refusal needs: ``stale`` and
        ``waiting`` clear without anyone, ``rejected`` needs a reviewer and
        ``unauthorized`` needs the root policy to change.
        """
        source = observation.source
        if (
            project is None
            or project["hierarchical_integration_generation"] != observation.policy_generation
        ):
            raise HierarchyError("stale", "parent project or its integration policy changed")
        if project["status"] != "ACTIVE" or project["hierarchical_integration_draining"]:
            raise HierarchyError("waiting", "parent project is inactive or draining")
        if await conn.scalar(
            select(
                exists(
                    select(t.task_labels.c.task_id).where(
                        t.task_labels.c.task_id == task_id, t.task_labels.c.label.like("hold:%")
                    )
                )
                | exists(
                    select(t.task_gates.c.task_id)
                    .select_from(t.task_gates.join(t.gates, t.gates.c.id == t.task_gates.c.gate_id))
                    .where(t.task_gates.c.task_id == task_id, t.gates.c.status == "open")
                )
            )
        ):
            raise HierarchyError("waiting", "parent has a hold or an open gate")
        generation = observation.policy_generation
        if not await self.producer._authorization_on(conn, task_id, source, generation):
            latest = await self.producer.latest_exact_evidence_on(conn, task_id, source)
            if latest is None or latest["verdict"] == "approved":
                raise HierarchyError(
                    "unauthorized", "the root policy does not admit automatic parent reverification"
                )
            if observation.approval is None or not await self.producer._authorization_on(
                conn, task_id, source, generation, rejection_superseded=True
            ):
                raise HierarchyError(
                    "rejected",
                    f"{latest['reviewer_login']} rejected verified head {source['head']} and "
                    f"no reviewer approved PR head {observation.head_sha}",
                )
        policy = HierarchicalIntegrationPolicy.model_validate(
            project["hierarchical_integration_policy"]
        )
        if not policy.root.repair.source_ci:
            raise HierarchyError(
                "unauthorized", "automatic parent reverification is off (root.repair.source_ci)"
            )
        if await self.db._read_manual_pause(conn, task_id) is not None:
            raise HierarchyError("waiting", "parent has a manual pause")

    async def _completed_operation_on(self, conn, task_id, *, lock=False):
        """The checkpoint and completed operation a new episode carries forward."""
        checkpoint_statement = select(t.task_integration_checkpoints).where(
            t.task_integration_checkpoints.c.task_id == task_id,
        )
        if lock:
            checkpoint_statement = checkpoint_statement.with_for_update()
        checkpoint = (await conn.execute(checkpoint_statement)).mappings().one_or_none()
        if checkpoint is None:
            raise HierarchyError("invalid", "parent has no integration checkpoint")
        operation_statement = select(t.integration_repair_operations).where(
            t.integration_repair_operations.c.id == checkpoint["last_completed_operation_id"],
            t.integration_repair_operations.c.parent_task_id == task_id,
            t.integration_repair_operations.c.episode_id == checkpoint["episode_id"],
            t.integration_repair_operations.c.state == "completed",
        )
        if lock:
            operation_statement = operation_statement.with_for_update()
        operation = (await conn.execute(operation_statement)).mappings().one_or_none()
        if operation is None:
            raise HierarchyError("invalid", "parent has no completed aggregate operation")
        for writer in {task_id, operation["verifier_task_id"]} - {None}:
            if await CancelledCollectionRecovery._live_holder_on(conn, writer):
                raise HierarchyError("waiting", "a session or workspace still holds the parent")
        return dict(checkpoint), operation

    async def _repo_on(self, conn, source):
        repo = (
            (await conn.execute(select(t.repos).where(t.repos.c.id == source["repository_id"])))
            .mappings()
            .one()
        )
        if source["branch"].removeprefix("refs/heads/") == repo["default_branch"]:
            raise HierarchyError("invalid", "parent source is the default branch")
        return repo

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
