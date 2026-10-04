"""Explicit registration of shared CI ports; policy and rollout stay outside.

The integration command owner constructs this adapter with a producer resolver
from the pinned policy. Existing commands still own hosted writes, job
submission and App attestation. This module never pushes or runs a process.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.integration.ci_producers import CIProducer, LocalCIProducer, ProducerObservation, digest
from src.integration.main_promotion import RootAttestationSubject
from src.integration.subjects import (
    CIAttestArgs,
    CIObserveArgs,
    CIRequestArgs,
    HeadIdentity,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectEngine,
)

ProducerResolver = Callable[[Subject], Awaitable[CIProducer] | CIProducer]
AttestationResolver = Callable[
    [Subject], Awaitable[RootAttestationSubject | None] | RootAttestationSubject | None
]


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


class CIAdapters:
    """Typed CI adapters against the named prerequisite's PrimitivePorts."""

    def __init__(
        self,
        db: Any,
        producer_for: ProducerResolver,
        *,
        attestation_service: Any = None,
        attestation_subject_for: AttestationResolver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.producer_for, self.clock = db, producer_for, clock
        self.attestation_service = attestation_service
        self.attestation_subject_for = attestation_subject_for

    def bind(self, ports: PrimitivePorts) -> None:
        ports.bind(Primitive.CI_REQUEST, self.request)
        ports.bind(Primitive.CI_OBSERVE, self.observe)
        ports.bind(Primitive.CI_ATTEST, self.attest)

    @staticmethod
    def _matches(current: Subject, subject: Subject, head: HeadIdentity) -> bool:
        return (
            current.is_live
            and current.engine is SubjectEngine.RECONCILER
            and current.schedule.gate_id is None
            and current.id == subject.id
            and current.project_id == subject.project_id
            and current.repository_id == subject.repository_id
            and current.version == subject.version
            and current.policy == subject.policy
            and current.head == head
            and subject.head == head
        )

    async def _current(self, subject: Subject, head: HeadIdentity) -> bool:
        row = await self.db.get_integration_subject(subject.id)
        return (
            row is not None
            and row.get("engine") == SubjectEngine.RECONCILER.value
            and self._matches(Subject.from_row(row), subject, head)
        )

    async def _record(
        self,
        subject: Subject,
        head: HeadIdentity,
        result: PrimitiveOutcome,
    ) -> dict | None:
        # Recheck in the append transaction so a late result cannot establish
        # evidence for a replacement head or an answered/created human gate.
        async with self.db._engine.begin() as conn:
            row = await self.db.lock_integration_subject_on(conn, subject.id)
            if row is None or not self._matches(Subject.from_row(row), subject, head):
                return None
            identity = result.model_dump(mode="json")
            # Observation timestamps and queue age are not a new CI attempt.
            identity["detail"].pop("requested_at", None)
            observation = identity["detail"].get("evidence")
            if observation:
                for key in ("observed_at", "age_seconds"):
                    observation.pop(key, None)
            entry, _ = await self.db.append_integration_subject_journal_on(
                conn,
                {
                    "subject_id": subject.id,
                    "entry_kind": "action",
                    "mode": "active",
                    "idempotency_key": "ci:"
                    + digest(
                        {
                            "generation": head.generation,
                            "head": head.sha,
                            "result": identity,
                        }
                    ),
                    "policy_artifact_sha256": subject.policy.artifact_sha256,
                    "subject_version": subject.version,
                    "phase": subject.phase.value,
                    "head_sha": head.sha,
                    "generation": head.generation,
                    "primitive": result.primitive.value,
                    "outcome": result.outcome,
                    "payload": result.detail,
                    "recorded_at": self.clock(),
                },
            )
            return entry

    @staticmethod
    def _superseded(primitive: Primitive) -> PrimitiveOutcome:
        return PrimitiveOutcome(
            primitive=primitive,
            outcome={
                Primitive.CI_REQUEST: "unavailable",
                Primitive.CI_OBSERVE: "infra",
                Primitive.CI_ATTEST: "refused",
            }[primitive],
            reason="subject_changed_or_held",
            detail={"classification": "superseded", "is_attempt": False},
        )

    async def request(self, subject: Subject, args: CIRequestArgs) -> PrimitiveOutcome:
        if not await self._current(subject, args.head):
            return self._superseded(args.primitive)
        producer = await _resolve(self.producer_for(subject))
        requested = await producer.request(subject, args.head)
        if requested.outcome == "requested" and requested.requested_at is None:
            requested = requested.model_copy(update={"requested_at": self.clock()})
        result = PrimitiveOutcome(
            primitive=args.primitive,
            outcome=requested.outcome,
            reason=requested.reason,
            detail=requested.model_dump(mode="json"),
        )
        entry = await self._record(subject, args.head, result)
        if entry is None:
            return self._superseded(args.primitive)
        return result.model_copy(update={"detail": entry["payload"]})

    async def observe(self, subject: Subject, args: CIObserveArgs) -> PrimitiveOutcome:
        if not await self._current(subject, args.head):
            return self._superseded(args.primitive)
        producer = await _resolve(self.producer_for(subject))
        if (
            args.required_check_version is not None
            and args.required_check_version != producer.required_check_version
        ):
            return PrimitiveOutcome(
                primitive=args.primitive,
                outcome="untrusted",
                reason="required_check_version_mismatch",
            )
        observed = await producer.observe(subject, args.head)
        if (
            not isinstance(observed, ProducerObservation)
            or observed.head_sha != args.head.sha
            or observed.required_check_version != producer.required_check_version
        ):
            return PrimitiveOutcome(
                primitive=args.primitive,
                outcome="untrusted",
                reason="producer_identity_mismatch",
            )
        result = PrimitiveOutcome(
            primitive=args.primitive,
            outcome=observed.state.value,
            reason=observed.reason,
            detail={
                "evidence": observed.model_dump(mode="json"),
                "is_attempt": observed.is_attempt,
            },
        )
        entry = await self._record(subject, args.head, result)
        if entry is None:
            return self._superseded(args.primitive)
        # The append-only evidence remains the original observation. Current
        # age and observation time are fresh facts on every reconciler visit.
        return result.model_copy(
            update={
                "detail": {
                    **result.detail,
                    "journal_seq": entry["seq"],
                }
            }
        )

    async def attest(self, subject: Subject, args: CIAttestArgs) -> PrimitiveOutcome:
        def refused(reason):
            return PrimitiveOutcome(primitive=args.primitive, outcome="refused", reason=reason)

        if not await self._current(subject, args.head):
            return self._superseded(args.primitive)
        producer = await _resolve(self.producer_for(subject))
        if isinstance(producer, LocalCIProducer):
            return refused("local_evidence_is_not_app_attestation")
        if self.attestation_service is None or self.attestation_subject_for is None:
            return refused("attestation_unavailable")
        target = await _resolve(self.attestation_subject_for(subject))
        if (
            not isinstance(target, RootAttestationSubject)
            or target.batch_id != subject.batch_id
            or target.candidate_sha != args.head.sha
            or target.revision != args.head.generation
            or target.required_check_version != producer.required_check_version
        ):
            return refused("attestation_subject_mismatch")
        # Existing service rechecks App trust, exact live green, publication
        # leases, prewrite and read-back. Never replace it with a local receipt.
        published = await self.attestation_service.publish(target)
        outcome = {
            "published": "attested",
            "already_published": "already",
        }.get(published.outcome, "refused")
        proof = published.proof
        if outcome != "refused" and (proof is None or proof.subject() != target):
            return refused("attestation_proof_mismatch")
        result = PrimitiveOutcome(
            primitive=args.primitive,
            outcome=outcome,
            reason=published.outcome if outcome == "refused" else None,
            detail={"proof": proof.model_dump(mode="json") if proof else None},
        )
        if await self._record(subject, args.head, result) is None:
            return self._superseded(args.primitive)
        return result


def bind_ci_adapters(ports: PrimitivePorts, adapters: CIAdapters) -> PrimitivePorts:
    """Integration-owner registration seam; importing the module has no effects."""
    adapters.bind(ports)
    return ports
