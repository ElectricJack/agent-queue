"""Audited subject receipts, attempts and decisions (primitives 14–16).

The foundation's append-only journal is the record; task delivery receipts are
the task-side projection. Proof readers are trusted, server-owned adapters to
git/CI, never fields accepted from a playbook. They run in the caller's
transaction and must establish publication and producer identity themselves.
No reader means no proof and no counted attempt. These ports add no public
command surface: CommandHandler remains the entry point.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    integration_subject_journal,
    projects,
    task_delivery_receipts,
    task_metadata,
    tasks,
)
from src.integration.subjects import (
    OPERATOR_HOLD_META_KEY,
    HeadIdentity,
    JournalMode,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    ReceiptKind,
    RecordAttemptArgs,
    RecordDecisionArgs,
    RecordReceiptArgs,
    Subject,
    SubjectPhase,
)


class ReceiptProjection(BaseModel):
    """Existing train/parent receipt bindings supplied by the proof adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    parent_operation_id: str | None = Field(default=None, min_length=1)
    parent_episode_id: str | None = Field(default=None, min_length=1)
    batch_id: str | None = Field(default=None, min_length=1)
    member_ordinal: int | None = Field(default=None, ge=0)
    candidate_revision: int | None = Field(default=None, ge=0)
    disposition_revision: int | None = Field(default=None, ge=0)
    squash_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    reviewed_tree_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    review_evidence: dict[str, Any] | None = None
    verification_evidence: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _parent_pair(self):
        if bool(self.parent_operation_id) != bool(self.parent_episode_id):
            raise ValueError("parent receipt needs both operation and episode")
        return self


class DeliveryProof(BaseModel):
    """Exact source/target proof obtained by the delivery adapter.

    Code needs ancestry/merge proof; noop needs content equivalence. A skipped
    receipt needs an explicit policy disposition, including its repair link.
    The proof is retained verbatim so a future receipt reader can explain it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_task_id: str = Field(min_length=1)
    source_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target: HeadIdentity
    kind: ReceiptKind
    evidence: dict[str, Any] = Field(min_length=1)
    projection: ReceiptProjection = ReceiptProjection()


class AttemptObservation(BaseModel):
    """A durable CI observation plus proof of who published the tested head."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    evidence_id: str = Field(min_length=1)
    head: HeadIdentity
    producer_id: str = Field(min_length=1)
    trusted: bool
    conclusion: str
    classification: str
    writer_task_id: str | None = None
    published_at: float | None = None
    observed_at: float
    cancelled: bool = False
    superseded: bool = False


DeliveryReader = Callable[[Any, Subject, RecordReceiptArgs], Awaitable[DeliveryProof | None]]
AttemptReader = Callable[[Any, Subject, RecordAttemptArgs], Awaitable[AttemptObservation | None]]


def journal_key(prefix: str, value: Any) -> str:
    """Stable domain identity, independent of visit ids and retry clocks."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return prefix + ":" + hashlib.sha256(canonical.encode()).hexdigest()


async def journal_entry_on(conn, subject_id: str, key: str) -> dict | None:
    row = (
        (
            await conn.execute(
                select(integration_subject_journal).where(
                    integration_subject_journal.c.subject_id == subject_id,
                    integration_subject_journal.c.idempotency_key == key,
                )
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def append_record_on(
    db,
    conn,
    subject: Subject,
    *,
    key: str,
    primitive: Primitive,
    entry_kind: str,
    payload: dict,
    now: float,
    outcome: str | None = None,
    mode: JournalMode = JournalMode.ACTIVE,
    rule: str | None = None,
    facts_digest: str | None = None,
) -> tuple[dict, bool]:
    return await db.append_integration_subject_journal_on(
        conn,
        {
            "subject_id": subject.id,
            "idempotency_key": key,
            "entry_kind": entry_kind,
            "mode": mode.value,
            "policy_artifact_sha256": subject.policy.artifact_sha256,
            "subject_version": subject.version,
            "phase": subject.phase.value,
            "head_sha": subject.head_sha,
            "generation": subject.generation,
            "rule": rule,
            "primitive": primitive.value,
            "outcome": outcome,
            "facts_digest": facts_digest,
            "payload": payload,
            "recorded_at": now,
        },
    )


async def lock_current_on(db, conn, subject: Subject) -> Subject | None:
    """Fence a mutation to the observation's version and owning engine."""
    row = await db.lock_integration_subject_on(conn, subject.id)
    if row is None:
        return None
    current = Subject.from_row(row)
    if current.version != subject.version or current.engine != subject.engine:
        return None
    if current.phase is SubjectPhase.DONE:
        return None
    return current


async def human_hold_on(conn, subject: Subject, *, task_id: str | None = None) -> str | None:
    """Binding product decisions are checked again immediately before mutation."""
    status = (
        await conn.execute(
            select(projects.c.status).where(projects.c.id == subject.project_id).with_for_update()
        )
    ).scalar_one_or_none()
    if status != "ACTIVE":
        return "project_inactive" if status else "project_missing"
    ids = sorted({value for value in (subject.task_id, task_id) if value})
    if ids:
        # manual_pause takes the task lock before writing its snapshot.
        await conn.execute(
            select(tasks.c.id).where(tasks.c.id.in_(ids)).order_by(tasks.c.id).with_for_update()
        )
        held = (
            await conn.execute(
                select(task_metadata.c.key)
                .where(
                    task_metadata.c.task_id.in_(ids),
                    task_metadata.c.key.in_(("manual_pause", OPERATOR_HOLD_META_KEY)),
                )
                .order_by(task_metadata.c.key)
                .limit(1)
            )
        ).scalar_one_or_none()
        if held == OPERATOR_HOLD_META_KEY:
            return "operator_hold"
        if held:
            return "manual_pause"
    return None


class RecordsPrimitives:
    """Transactional ports. ``*_on`` compose with a caller-owned transaction.

    Counting an attempt bumps the subject version; callers that schedule after
    a port invocation must reload it (or compose using ``record_attempt_on``).
    Receipts and decisions only advance the journal pointer.
    """

    def __init__(
        self,
        db,
        *,
        delivery_reader: DeliveryReader | None = None,
        attempt_reader: AttemptReader | None = None,
        clock=time.time,
    ):
        self.db = db
        self.delivery_reader = delivery_reader
        self.attempt_reader = attempt_reader
        self.clock = clock

    def bind(self, ports: PrimitivePorts) -> None:
        ports.bind(Primitive.RECORD_RECEIPT, self.record_receipt)
        ports.bind(Primitive.RECORD_ATTEMPT, self.record_attempt)
        ports.bind(Primitive.RECORD_DECISION, self.record_decision)

    async def record_receipt(self, subject: Subject, args: RecordReceiptArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.record_receipt_on(conn, subject, args)

    async def record_receipt_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.RECORD_RECEIPT
        key = journal_key("receipt", args.model_dump(mode="json"))
        current = await lock_current_on(self.db, conn, subject)
        previous = await journal_entry_on(conn, subject.id, key)
        if previous:
            return PrimitiveOutcome(
                primitive=primitive, outcome="exists", detail=previous["payload"]
            )
        if current is None:
            return PrimitiveOutcome.unknown(primitive, "stale_subject")
        if args.target != current.head:
            return PrimitiveOutcome.unknown(primitive, "target_identity_mismatch")
        source = (
            (
                await conn.execute(
                    select(tasks).where(
                        tasks.c.id == args.source_task_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            source is None
            or source["project_id"] != current.project_id
            or source["repo_id"] not in {None, current.repository_id}
        ):
            return PrimitiveOutcome.unknown(primitive, "source_identity_mismatch")
        if self.delivery_reader is None:
            return PrimitiveOutcome.unknown(primitive, "delivery_proof_unavailable")
        proof = await self.delivery_reader(conn, current, args)
        if proof is None or (
            proof.source_task_id != args.source_task_id
            or proof.source_head_sha != args.source_head_sha
            or proof.target != args.target
            or proof.kind != args.kind
        ):
            return PrimitiveOutcome.unknown(primitive, "delivery_proof_mismatch")
        if args.kind is ReceiptKind.SKIPPED:
            repair_id = proof.evidence.get("repair_task_id")
            repair = (
                (await conn.execute(select(tasks).where(tasks.c.id == repair_id)))
                .mappings()
                .first()
            )
            if (
                proof.evidence.get("skip_authorized") is not True
                or repair is None
                or repair["project_id"] != current.project_id
                or repair["status"] in {"COMPLETED", "FAILED"}
                or repair_id == args.source_task_id
            ):
                return PrimitiveOutcome.unknown(primitive, "skip_requires_repair_provenance")
        now = self.clock()
        receipt_id = journal_key("receipt", {"subject": current.id, "key": key})
        # A subject-specific domain key never rewrites a receipt for an older
        # generation or another parent. Exact identity is in both projections.
        domain_key = f"subject:{current.id}:{key}"
        values = {
            "id": receipt_id,
            "domain_key": domain_key,
            "source_task_id": args.source_task_id,
            "target_task_id": current.task_id,
            "repository_id": current.repository_id,
            "target_branch": args.target.ref.removeprefix("refs/heads/"),
            "reviewed_head_sha": args.source_head_sha,
            "before_sha": args.target.base_sha,
            "after_sha": args.target.sha,
            "disposition": args.kind.value,
            "resolution_evidence": proof.evidence,
            **proof.projection.model_dump(),
            "created_at": now,
        }
        row = (
            await conn.execute(
                pg_insert(task_delivery_receipts)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_task_delivery_receipts_domain_key")
                .returning(task_delivery_receipts.c.id)
            )
        ).scalar_one_or_none()
        if row is None:
            # Journal and projection are committed together, so an orphan or a
            # colliding key is a refusal rather than fabricated delivery truth.
            return PrimitiveOutcome.unknown(primitive, "receipt_projection_conflict")
        payload = {"receipt_id": receipt_id, **proof.model_dump(mode="json")}
        await append_record_on(
            self.db,
            conn,
            current,
            key=key,
            primitive=primitive,
            entry_kind="receipt",
            outcome="recorded",
            payload=payload,
            now=now,
        )
        return PrimitiveOutcome(primitive=primitive, outcome="recorded", detail=payload)

    async def record_attempt(self, subject: Subject, args: RecordAttemptArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.record_attempt_on(conn, subject, args)

    async def record_attempt_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.RECORD_ATTEMPT
        current = await lock_current_on(self.db, conn, subject)
        if (
            current is None
            or current.head != args.head
            or (current.budget is None or current.budget.ordinal != args.ordinal)
        ):
            return PrimitiveOutcome(primitive=primitive, outcome="stale")
        # Counting is per exact head in an ordinal, not per repeated check/run.
        # A second CI run on the same head cannot consume another budget unit.
        key = journal_key(
            "attempt",
            {
                "repository_id": args.head.repository_id,
                "ref": args.head.ref,
                "head_sha": args.head.sha,
                "ordinal": args.ordinal,
            },
        )
        previous = await journal_entry_on(conn, current.id, key)
        if previous:
            return PrimitiveOutcome(
                primitive=primitive,
                outcome="counted",
                detail={**previous["payload"], "replayed": True},
            )
        if self.attempt_reader is None:
            return PrimitiveOutcome(
                primitive=primitive, outcome="not_an_attempt", reason="evidence_unavailable"
            )
        evidence = await self.attempt_reader(conn, current, args)
        if (
            evidence is None
            or evidence.evidence_id != args.evidence_id
            or evidence.head != args.head
        ):
            return PrimitiveOutcome(primitive=primitive, outcome="stale")
        now = self.clock()
        valid = (
            evidence.trusted
            and evidence.conclusion == args.conclusion
            and evidence.classification in {"conclusive", "full_suite_fallback"}
            and not evidence.cancelled
            and not evidence.superseded
            and evidence.writer_task_id == current.writer.task_id
            and evidence.writer_task_id is not None
            and evidence.published_at is not None
            and current.writer.last_push_at is not None
            and current.budget.started_at <= evidence.published_at <= evidence.observed_at <= now
            and evidence.published_at <= current.writer.last_push_at
        )
        if not valid:
            return PrimitiveOutcome(
                primitive=primitive,
                outcome="not_an_attempt",
                reason="not_trusted_conclusive_writer_publication",
            )
        attempts = current.budget.attempts + 1
        payload = {
            **evidence.model_dump(mode="json"),
            "ordinal": args.ordinal,
            "attempts": attempts,
            "replayed": False,
        }
        await append_record_on(
            self.db,
            conn,
            current,
            key=key,
            primitive=primitive,
            entry_kind="attempt",
            outcome="counted",
            payload=payload,
            now=now,
        )
        updated = await self.db.update_integration_subject_on(
            conn,
            subject_id=current.id,
            expected_version=current.version,
            values={"budget_attempts": attempts},
            now=now,
        )
        if updated is None:
            raise RuntimeError("locked subject changed while recording an attempt")
        return PrimitiveOutcome(
            primitive=primitive,
            outcome="counted",
            detail={**payload, "subject_version": updated["version"]},
        )

    async def record_decision(self, subject: Subject, args: RecordDecisionArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.record_decision_on(conn, subject, args)

    async def record_decision_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.RECORD_DECISION
        # A decision is a visit at this observed version, not merely a rule
        # name. Active/shadow are separate audited evaluations.
        key = journal_key(
            "decision",
            {
                "version": subject.version,
                "policy": subject.policy.model_dump(mode="json"),
                **args.model_dump(mode="json"),
            },
        )
        current = await lock_current_on(self.db, conn, subject)
        previous = await journal_entry_on(conn, subject.id, key)
        if previous:
            return PrimitiveOutcome(
                primitive=primitive,
                outcome="recorded",
                detail={"journal_seq": previous["seq"], "replayed": True},
            )
        if current is None:
            return PrimitiveOutcome.unknown(primitive, "stale_subject")
        entry, created = await append_record_on(
            self.db,
            conn,
            current,
            key=key,
            primitive=args.decided,
            entry_kind="decision",
            payload=args.model_dump(mode="json"),
            rule=args.rule,
            facts_digest=args.facts_digest,
            mode=args.mode,
            now=self.clock(),
        )
        return PrimitiveOutcome(
            primitive=primitive,
            outcome="recorded",
            detail={"journal_seq": entry["seq"], "replayed": not created},
        )
