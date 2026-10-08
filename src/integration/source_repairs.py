"""Whether one exact train source generation is already on its default target.

Source-CI repair exists to recover red CI on a source branch whose work has
not reached the default branch.  When git already proves that generation's
work on the project's designated default branch, repairing the branch
repeats delivered work: on the box where this was found, a root whose source
was already integrated and adopted under other commits spent a whole agent
re-running the same shared fixes a second time.

:func:`prove_source_delivered` is that one answer, and it deliberately asks
the question the root scheduler already asks and trusts
(:meth:`~src.integration.scheduler.TrainService._git_delivered_roots_on` on
top of :mod:`src.integration.delivery_truth`): the *exact* completion
generation, the *exact* repository and the *exact* default target ref.  A
merge, a squash and a cherry-pick are equally delivered, because the proof
names the generation's retained source and asks git whether it is contained
in the target, not whether the observed pull-request head happens to be an
ancestor.

Two rules make this safe to act on, and both are why nothing here is a cache:

* **Every decision observes afresh.**  The default target can be retargeted,
  the target can lose containment, and a delivery or an adoption can arrive
  after an earlier negative answer, so a remembered answer answers a question
  nobody asked.  Admission asks before it files, and
  :func:`delivered_queued_repairs` asks again before a claim withholds a
  queued delegate.
* **A recorded proof is audit, not authority.**
  :func:`record_delivery_evidence` writes what was observed onto the
  observation so an operator and ``aq task explain`` can see it, and nothing
  reads it back to decide eligibility.  The claim frontier has no delivery
  predicate for exactly this reason.

Everything that is not a proof answers *not delivered*, because the failure
mode that matters is stranding real work:

* an unavailable observer, a repository the project does not designate, or a
  missing target -- no canonical evidence, so repair;
* an unknown git error or a missing retained source -- unknown, so repair;
* a new checkpoint generation or a reopened task -- the evidence was gathered
  about an identity that no longer exists, so repair;
* ``no_artifact`` and ``settled`` -- neither is delivery to the default
  branch, so repair.

Only ``contained`` or proven ``no_change`` on the exact target, revalidated
against the exact source identity the observation names, may withhold work.

An unclaimed READY delegate can be retired by the command handler after a
fresh proof and a locked identity check. A claimed delegate keeps its writer;
its completed repair remains deliverable even after the original lands.

Discarding the source completion revokes the purpose of every bound delegate.
Reopen retirement fences writers and retires completed repairs in that same
transaction. Train admission and publication recheck the current source head,
including the full lineage of repairs of repairs: that recheck is
:func:`~src.integration.delivery_truth.superseded_source_repairs_on`, which
lives beside the delivery requests it reads in
:mod:`src.integration.delivery_truth` because the reduced train must not
import this module. Admission asks the wider
:func:`~src.integration.delivery_truth.undeliverable_repairs_on`, which also
withholds a repair whose own task is terminal, and the train releases an open
batch carrying one rather than let it hold a target against the very source it
was filed for. Only the write side stays here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select, update

from src.database.tables import integration_source_ci, tasks
from src.integration.delivery_truth import (
    REOPEN_DISPOSITION,
    RETIREMENT_KEY,
    SUPERSEDED,
    repair_bindings_on,
    repair_ids,
    superseded_source_repairs_on,
)

#: Key under which a source-CI observation's evidence records what was
#: observed.  Audit only: no eligibility decision reads it back.
DELIVERY_KEY = "source_delivery"

#: The generation's work is on the exact default target.
DELIVERED = "delivered"
#: Git answered, and the work is not there.
UNDELIVERED = "undelivered"
#: No canonical answer was obtainable; never a licence to skip.
UNKNOWN = "unknown"


async def retire_reopened_source_repairs_on(db, conn, task_id, *, context, source_head=None):
    """Cancel all delegates of a discarded completion in its transaction.

    Retire origins and fence held writers before any reader can see the reopen.
    Keep lineage and comments, and recurse through repair-of-repair attempts.
    The caller merges transition notifications into its post-commit result.
    """
    from src.database.tables import task_branch_origins, task_metadata
    from src.models import TaskStatus

    pending, seen, transitions = {task_id}, {task_id}, []
    while pending:
        statement = select(integration_source_ci).where(
            integration_source_ci.c.task_id.in_(pending))
        if source_head is not None and pending == {task_id}:
            statement = statement.where(integration_source_ci.c.source_head == source_head)
        rows = (await conn.execute(statement)).mappings().all()
        pending = set()
        for row in rows:
            for repair_id in sorted(repair_ids(row) - seen):
                seen.add(repair_id)
                pending.add(repair_id)
                repair = (await conn.execute(select(tasks).where(
                    tasks.c.id == repair_id,
                ).with_for_update())).mappings().one_or_none()
                if repair is None:
                    continue  # Archived tasks cannot enter the completion frontier.
                existing = await conn.scalar(select(task_metadata.c.value).where(
                    task_metadata.c.task_id == repair_id,
                    task_metadata.c.key == RETIREMENT_KEY,
                ))
                if existing and json.loads(existing).get("disposition") == REOPEN_DISPOSITION:
                    continue
                now = time.time()
                reason = (f"Source CI repair superseded by reopen of {task_id} ({context}); "
                          f"bound source {row['task_id']} head {row['source_head']}")
                retirement = {
                    "disposition": REOPEN_DISPOSITION, "source_task_id": row["task_id"],
                    "source_head": row["source_head"], "source_base": row["source_base"],
                    "generation": row["generation"], "repository_id": row["repository_id"],
                    "reopened_task_id": task_id, "context": context,
                    "reason": reason, "retired_at": now,
                }
                await db._upsert_meta(repair_id, RETIREMENT_KEY, retirement, conn=conn)
                await db._upsert_meta(repair_id, "work_outcome", "abandoned", conn=conn)
                await conn.execute(update(task_branch_origins).where(
                    task_branch_origins.c.task_id == repair_id,
                    task_branch_origins.c.retired_at.is_(None),
                ).values(retired_at=now))
                transitions.append(await db._apply_transition(
                    conn, repair_id, TaskStatus.FAILED, context=SUPERSEDED, force=True,
                    _manual_pause_control=True, assigned_agent_id=None,
                    retry_count=repair["max_retries"],
                ))
                await db.add_task_comment(
                    repair_id, reason, author_kind="agent",
                    author_id="service:integration-source-ci", conn=conn,
                )
                await db.log_event(
                    "integration.source_ci_repair_superseded", project_id=repair["project_id"],
                    task_id=repair_id, payload=json.dumps(retirement), conn=conn,
                )
    return transitions


def source_identity(task_id: str, source) -> tuple[str, str, str, str, int]:
    return (task_id, source["repository_id"], source["base"], source["head"], source["generation"])


@dataclass(frozen=True)
class SourceDeliveryProof:
    """One answer about the observed source generation, and why."""

    state: str
    detail: str
    target_ref: str | None = None
    target_oid: str | None = None
    source_oid: str | None = None
    source_identity: tuple[str, str, str, str, int] | None = None

    @property
    def delivered(self) -> bool:
        return self.state == DELIVERED

    def as_evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "target_ref": self.target_ref,
            "target_oid": self.target_oid,
            "source_oid": self.source_oid,
        }


def _answer(state: str, detail: str, target_ref, proof=None) -> SourceDeliveryProof:
    return SourceDeliveryProof(
        state, detail, target_ref,
        getattr(proof, "target_oid", None),
        getattr(proof, "source_oid", None),
    )


def _undelivered(detail: str, target_ref, proof=None) -> SourceDeliveryProof:
    return _answer(UNDELIVERED, detail, target_ref, proof)


def _unknown(detail: str, target_ref=None) -> SourceDeliveryProof:
    return _answer(UNKNOWN, detail, target_ref)


async def prove_source_delivered(db, observer, *, task_id: str, source) -> SourceDeliveryProof:
    """Canonical delivery truth for *task_id*'s current generation.

    The target is resolved from the project's *current* designated repository
    and default branch on every call, so a retarget is never answered against
    the target an earlier call saw.

    *source* is the exact observed source identity
    (:meth:`~src.integration.review_evidence.ReviewEvidenceProducer._pull_request_source_on`),
    and only its repository scopes the question: the proof is about the
    generation, not about the pull-request head, because adoption lands that
    generation's work under a different commit.  The caller revalidates that
    *source* is still the task's identity under the hierarchy lock before it
    acts on a delivered answer.
    """
    from src.integration.delivery_observer import delivery_targets
    from src.integration.delivery_truth import DeliveryState

    if observer is None:
        return _unknown("no_delivery_observer")
    async with db._engine.connect() as conn:
        targets = await delivery_targets(conn, [task_id])
    target = targets.get(task_id)
    if target is None:
        # No designated repository: git can never prove delivery here, so the
        # delivery question is not this project's to ask.
        return _unknown("no_integration_target")
    if (target.project_id, target.repository_id) != (
        source["project_id"], source["repository_id"]
    ):
        return _unknown("target_is_not_the_source_repository")
    try:
        view = await observer.observe([task_id])
    except Exception as exc:  # noqa: BLE001 - any observer failure is unknown
        return _unknown(f"observer_error: {type(exc).__name__}")
    evidence = view.evidence.get(task_id)
    if evidence is None:
        return _unknown("no_delivery_evidence")
    # Re-read the identity the answer was made about. A task reopened,
    # re-completed or re-targeted since the fetch is left out, because its
    # evidence answers an earlier question.
    async with db._engine.connect() as conn:
        verified = await view.verified_on(conn, [task_id])
    proof = verified.get(task_id)
    if proof is None:
        return _unknown("identity_changed_since_observation")
    if (proof.request.project_id, proof.request.repository_id, proof.request.target_ref) != (
        target.project_id, target.repository_id, target.target_ref
    ):
        return _undelivered("scope_mismatch", target.target_ref, proof)
    if proof.state not in {DeliveryState.CONTAINED, DeliveryState.NO_CHANGE}:
        # ``no_artifact`` and ``settled`` answer a different question: a
        # generation that owes nothing to this target is not one that
        # delivered to it.
        return _undelivered(f"delivery:{proof.reason}", target.target_ref, proof)
    if proof.request.task_status != "COMPLETED":
        return _undelivered("task_not_completed", target.target_ref, proof)
    return SourceDeliveryProof(
        DELIVERED, proof.reason,
        target_ref=target.target_ref,
        target_oid=proof.target_oid,
        source_oid=proof.source_oid,
        source_identity=source_identity(task_id, source),
    )


async def queued_source_repairs(db, *, project_id: str) -> list[dict]:
    """Exact source observations for queued delegates, oldest first.

    A queued delegate is a READY repair root.  Nothing is read from a recorded
    proof here: this only finds which delegates a claim could still hand to an
    agent, so the caller can prove each one afresh.  Bounded by the repair
    lineage itself, and empty for every project that has filed none.
    """
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_source_ci)
                .select_from(
                    integration_source_ci.join(
                        tasks, tasks.c.id == integration_source_ci.c.repair_task_id
                    )
                )
                .where(
                    integration_source_ci.c.repair_task_id.is_not(None),
                    tasks.c.project_id == project_id,
                    tasks.c.status == "READY",
                )
                .order_by(integration_source_ci.c.observed_at, integration_source_ci.c.task_id)
                .limit(100)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def delivered_queued_repairs(db, observer, *, project_id: str) -> dict[str, SourceDeliveryProof]:
    """Queued source-CI repairs whose exact source is delivered *right now*.

    Each candidate is proved from scratch against the project's current default
    target, so withholding a delegate from a claim is a fresh git decision and
    never a remembered one.  A candidate whose source identity no longer
    resolves, or whose proof is anything but ``contained``, is simply not in
    the result: nothing is persisted and no frontier predicate reads a record,
    so the next claim re-proves it.
    """
    from src.integration.review_evidence import ReviewEvidenceProducer

    candidates = await queued_source_repairs(db, project_id=project_id)
    if not candidates:
        return {}
    producer = ReviewEvidenceProducer(db, None)
    sources: list[tuple[str, str, dict]] = []
    async with db._engine.connect() as conn:
        for record in candidates:
            repair_id, source_task_id = record["repair_task_id"], record["task_id"]
            # The identity is read before any git work, so the connection is
            # not held across a fetch, and a source that no longer resolves is
            # simply not a candidate.
            source = await producer._pull_request_source_on(conn, source_task_id)
            if source is not None and source_identity(source_task_id, source) == (
                source_task_id, record["repository_id"], record["source_base"],
                record["source_head"], record["generation"],
            ):
                sources.append((repair_id, source_task_id, source))
    proved: dict[str, SourceDeliveryProof] = {}
    for repair_id, source_task_id, source in sources:
        proof = await prove_source_delivered(
            db, observer, task_id=source_task_id, source=source)
        if proof.delivered:
            proved[repair_id] = proof
    return proved


async def delivered_repair_sources_on(db, conn, records, *, git_delivered, repository_id):
    """Exact repair ancestors already delivered by Git or a root receipt.

    A task-id proof cannot satisfy an observation for another generation.
    Receipts can also answer for an archived original, but must name its head
    and the repository's current default target.
    """
    from src.database.queries.integration_train_queries import _root_delivery_receipt_conditions
    from src.database.tables import repos, task_delivery_receipts
    from src.integration.review_evidence import ReviewEvidenceProducer

    receipts = set((await conn.execute(select(
        task_delivery_receipts.c.source_task_id, task_delivery_receipts.c.reviewed_head_sha,
    ).select_from(task_delivery_receipts.join(
        repos, repos.c.id == task_delivery_receipts.c.repository_id,
    )).where(
        task_delivery_receipts.c.source_task_id.in_({row["task_id"] for row in records}),
        *_root_delivery_receipt_conditions(repository_id, repos.c.default_branch),
    ))).all())
    producer = ReviewEvidenceProducer(db, None)
    satisfied = set()
    for row in records:
        key = (row["task_id"], row["source_base"], row["source_head"], row["generation"])
        if (row["task_id"], row["source_head"]) in receipts:
            satisfied.add(key)
        elif row["task_id"] in git_delivered:
            source = await producer._pull_request_source_on(conn, row["task_id"])
            if source is not None and source_identity(row["task_id"], source) == (
                row["task_id"], repository_id, *key[1:],
            ):
                satisfied.add(key)
    return satisfied


async def retire_delivered_queued_repairs(db, proofs, *, project_id: str, retired_by: str):
    """Retire proven, still-unclaimed READY repairs on the command path.

    Git runs before this transaction. Recheck lineage, completion identity and
    target under the hierarchy lock, then lock the repair row against claim.
    Holds, gates, children and any retained owner or writer remain binding.
    Return only proofs still valid under the lock, for this claim's exclusion.
    """
    from src.database.queries.blocked_state import OBSOLETE_META_KEY
    from src.database.queries.integration_state_queries import session_attached_clause
    from src.database.tables import (
        gates,
        integration_branch_owners,
        projects,
        sessions,
        task_completion_records,
        task_gates,
        task_labels,
        workspaces,
    )
    from src.integration.delivery_observer import delivery_targets
    from src.integration.review_evidence import ReviewEvidenceProducer
    from src.models import TaskStatus

    valid = {}
    transitions = []
    producer = ReviewEvidenceProducer(db, None)
    if not proofs:
        return valid
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, project_id)
        policy_generation = await conn.scalar(select(
            projects.c.hierarchical_integration_generation,
        ).where(projects.c.id == project_id))
        for repair_id, proof in proofs.items():
            if not proof.delivered or proof.source_identity is None:
                continue
            task_id, repository_id, base, head, generation = proof.source_identity
            source = await producer._pull_request_source_on(conn, task_id)
            target = (await delivery_targets(conn, [task_id])).get(task_id)
            if (source is None or source_identity(task_id, source) != proof.source_identity
                    or source["project_id"] != project_id or target is None
                    or target.repository_id != repository_id or target.target_ref != proof.target_ref):
                continue
            record = (await conn.execute(select(integration_source_ci).where(
                integration_source_ci.c.task_id == task_id,
                integration_source_ci.c.repository_id == repository_id,
                integration_source_ci.c.source_base == base,
                integration_source_ci.c.source_head == head,
                integration_source_ci.c.generation == generation,
                integration_source_ci.c.policy_generation == policy_generation,
                integration_source_ci.c.repair_task_id == repair_id,
            ))).mappings().one_or_none()
            if record is None:
                continue
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == repair_id, tasks.c.project_id == project_id,
            ).with_for_update())).mappings().one_or_none()
            if task is None or task["status"] != "READY":
                continue
            valid[repair_id] = proof
            if task["assigned_agent_id"] is not None:
                continue
            blockers = [
                select(sessions.c.id).where(sessions.c.task_id == repair_id, session_attached_clause()),
                select(workspaces.c.id).where(workspaces.c.locked_by_task_id == repair_id),
                select(integration_branch_owners.c.id).where(
                    integration_branch_owners.c.owner_id == repair_id,
                    integration_branch_owners.c.handoff_state != "released",
                ),
                select(tasks.c.id).where(tasks.c.parent_task_id == repair_id),
                select(task_labels.c.task_id).where(
                    task_labels.c.task_id == repair_id, task_labels.c.label.like("hold:%")),
                select(task_gates.c.task_id).select_from(task_gates.join(
                    gates, gates.c.id == task_gates.c.gate_id,
                )).where(task_gates.c.task_id == repair_id, gates.c.status == "open"),
            ]
            blocked = False
            for statement in blockers:
                if await conn.scalar(statement.limit(1)) is not None:
                    blocked = True
                    break
            if blocked:
                continue
            now = time.time()
            reason = f"Source CI repair superseded by delivery of {task_id} ({head})"
            retirement = {
                "disposition": "superseded_by_delivery", "source_task_id": task_id,
                "source_base": base, "source_head": head, "generation": generation,
                "repository_id": repository_id, "delivery": proof.as_evidence(),
                "reason": reason, "retired_by": retired_by, "retired_at": now,
            }
            await db._upsert_meta(repair_id, RETIREMENT_KEY, retirement, conn=conn)
            await db._upsert_meta(repair_id, "work_outcome", "abandoned", conn=conn)
            await db._upsert_meta(repair_id, OBSOLETE_META_KEY, {
                "reason": reason, "closed_by": retired_by, "closed_at": now,
                "previous_status": "READY", "cleanup": {"state": "clear"},
            }, conn=conn)
            transitions.append(await db._apply_transition(
                conn, repair_id, TaskStatus.COMPLETED,
                context="source_ci_repair_superseded", force=True,
            ))
            await conn.execute(insert(task_completion_records).values(
                id=f"source-ci-retired:{repair_id}:{task['claim_epoch']}", task_id=repair_id,
                outcome="pass", work_outcome="abandoned", summary=reason,
                verification=json.dumps(retirement), completed_at=now,
            ))
            await db.log_event(
                "integration.source_ci_repair_superseded", project_id=project_id,
                task_id=repair_id, payload=json.dumps(retirement), conn=conn,
            )
    for transition in transitions:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return valid


def record_delivery_evidence(
    evidence: dict[str, Any] | None, proof: SourceDeliveryProof
) -> dict[str, Any]:
    """The observation evidence with *proof* recorded under :data:`DELIVERY_KEY`.

    Audit only.  This records what one observation saw; it is never read back
    to decide whether work may be skipped, because delivery truth is
    request-scoped and a remembered answer goes stale the moment the target
    moves.
    """
    merged = dict(evidence or {})
    merged[DELIVERY_KEY] = proof.as_evidence()
    return merged


__all__ = [
    "DELIVERED",
    "DELIVERY_KEY",
    "REOPEN_DISPOSITION",
    "RETIREMENT_KEY",
    "SUPERSEDED",
    "UNDELIVERED",
    "UNKNOWN",
    "SourceDeliveryProof",
    "delivered_queued_repairs",
    "delivered_repair_sources_on",
    "prove_source_delivered",
    "queued_source_repairs",
    "record_delivery_evidence",
    "repair_bindings_on",
    "repair_ids",
    "retire_delivered_queued_repairs",
    "superseded_source_repairs_on",
]
