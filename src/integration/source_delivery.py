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

Only ``contained`` on the exact target, revalidated against the exact
source identity the observation names, may withhold work.

An already-filed repair delegate is never killed here.  Withholding one is
:func:`delivered_queued_repairs`' decision at claim time, on a fresh proof,
and a claimed delegate keeps its writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from src.database.tables import integration_source_ci, tasks

#: Key under which a source-CI observation's evidence records what was
#: observed.  Audit only: no eligibility decision reads it back.
DELIVERY_KEY = "source_delivery"

#: The generation's work is on the exact default target.
DELIVERED = "delivered"
#: Git answered, and the work is not there.
UNDELIVERED = "undelivered"
#: No canonical answer was obtainable; never a licence to skip.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceDeliveryProof:
    """One answer about the observed source generation, and why."""

    state: str
    detail: str
    target_ref: str | None = None
    target_oid: str | None = None
    source_oid: str | None = None

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
    if proof.state != DeliveryState.CONTAINED:
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
    )


async def queued_source_repairs(db, *, project_id: str) -> list[tuple[str, str]]:
    """``(repair_task_id, source_task_id)`` for queued delegates, oldest first.

    A queued delegate is a READY repair root.  Nothing is read from a recorded
    proof here: this only finds which delegates a claim could still hand to an
    agent, so the caller can prove each one afresh.  Bounded by the repair
    lineage itself, and empty for every project that has filed none.
    """
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_source_ci.c.repair_task_id, integration_source_ci.c.task_id)
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
        ).all()
    return [(row[0], row[1]) for row in rows]


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
        for repair_id, source_task_id in candidates:
            # The identity is read before any git work, so the connection is
            # not held across a fetch, and a source that no longer resolves is
            # simply not a candidate.
            source = await producer._pull_request_source_on(conn, source_task_id)
            if source is not None:
                sources.append((repair_id, source_task_id, source))
    proved: dict[str, SourceDeliveryProof] = {}
    for repair_id, source_task_id, source in sources:
        proof = await prove_source_delivered(
            db, observer, task_id=source_task_id, source=source)
        if proof.delivered:
            proved[repair_id] = proof
    return proved


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
    "UNDELIVERED",
    "UNKNOWN",
    "SourceDeliveryProof",
    "delivered_queued_repairs",
    "prove_source_delivered",
    "queued_source_repairs",
    "record_delivery_evidence",
]