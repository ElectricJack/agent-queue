"""Bounded waits, immutable human gates and audited ejection (primitives 17–19).

Gate definitions and answers live in the append-only subject journal. The
existing ``gates`` table is a dashboard projection, never approval evidence:
resolving or expiring it through a legacy command cannot release a subject.
Ejection is an explicit policy-authorized transaction with a repair link,
using a subject-kind adapter for its physical member/disposition projection.
These ports are inert until CommandHandler's reconciler wiring binds them.
"""

from __future__ import annotations

import time
import uuid
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import gates, integration_subjects, task_metadata, tasks
from src.integration.records import (
    append_record_on,
    human_hold_on,
    journal_entry_on,
    journal_key,
    lock_current_on,
)
from src.integration.subjects import (
    ARTIFACT_PATTERN,
    EjectArgs,
    GateArgs,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectPhase,
    SubjectSchedule,
    WaitArgs,
    WriterStatus,
    schedule_values,
)


class EjectionPlan(BaseModel):
    """Server-derived membership facts and the pinned policy's disposition.

    The reader must not mutate. ``apply_ejection_on`` then removes membership
    and applies the chosen disposition in the SAME transaction as the journal
    and the subject's return to building. Required work may only be skipped
    when the policy explicitly grants that disposition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    member_task_id: str = Field(min_length=1)
    source_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy_artifact_sha256: str = Field(pattern=ARTIFACT_PATTERN)
    allowed: bool = False
    required: bool = True
    disposition: Literal["repair_required", "skipped"] = "repair_required"
    allow_required_skip: bool = False
    repair_task_id: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(min_length=1)


EjectionReader = Callable[[Any, Subject, EjectArgs], Awaitable[EjectionPlan | None]]
EjectionWriter = Callable[[Any, Subject, EjectArgs, EjectionPlan], Awaitable[None]]


class GatePrimitives:
    """Each mutating port locks a version, commits its audit, and bumps version.

    ``answer_on`` is for a CommandHandler adapter with verified human evidence;
    it is deliberately absent from ``PrimitivePorts`` (policy cannot answer a
    human gate). Callers reload a subject before scheduling after these ports.
    """

    def __init__(
        self,
        db,
        *,
        ejection_reader: EjectionReader | None = None,
        apply_ejection_on: EjectionWriter | None = None,
        clock=time.time,
    ):
        self.db = db
        self.ejection_reader = ejection_reader
        self.apply_ejection_on = apply_ejection_on
        self.clock = clock

    def bind(self, ports: PrimitivePorts) -> None:
        ports.bind(Primitive.WAIT, self.wait)
        ports.bind(Primitive.GATE, self.gate)
        ports.bind(Primitive.EJECT, self.eject)

    async def _update_on(self, conn, subject, values, now) -> dict:
        row = await self.db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values=values,
            now=now,
            visit_started_at=now,
        )
        if row is None:
            raise RuntimeError("locked subject changed during gate mutation")
        return row

    async def wait(self, subject: Subject, args: WaitArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.wait_on(conn, subject, args)

    async def wait_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.WAIT
        current = await lock_current_on(self.db, conn, subject)
        if current is None:
            return PrimitiveOutcome.unknown(primitive, "stale_subject")
        now = self.clock()
        schedule = SubjectSchedule.wait(
            now=now,
            until=now + args.seconds,
            reason=args.reason,
            max_wait_seconds=current.schedule.max_wait_seconds,
        )
        # Waiting is legal during a product hold; it never erases an open gate.
        if current.schedule.gate_id:
            schedule = schedule.model_copy(update={"gate_id": current.schedule.gate_id})
        row = await self._update_on(conn, current, schedule_values(schedule), now)
        return PrimitiveOutcome(
            primitive=primitive,
            outcome="waiting",
            detail={**schedule_values(schedule), "subject_version": row["version"]},
        )

    async def gate(self, subject: Subject, args: GateArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.gate_on(conn, subject, args)

    async def gate_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.GATE
        current = await lock_current_on(self.db, conn, subject)
        if current is None:
            return PrimitiveOutcome.unknown(primitive, "stale_subject")
        now = self.clock()
        if not args.question.strip() or any(not choice.strip() for choice in args.choices):
            return PrimitiveOutcome.unknown(primitive, "invalid_gate_definition")
        gate_id = current.schedule.gate_id
        if gate_id:
            definition = await journal_entry_on(conn, current.id, f"gate:{gate_id}")
            if definition is None or not self._same_identity(definition, current):
                return PrimitiveOutcome.unknown(
                    primitive, "gate_identity_mismatch", gate_id=gate_id
                )
            if definition["payload"]["request"] != args.model_dump(mode="json"):
                return PrimitiveOutcome.unknown(primitive, "existing_human_gate", gate_id=gate_id)
            answer = await journal_entry_on(conn, current.id, f"gate-answer:{gate_id}")
            timeout_at = definition["payload"]["timeout_at"]
            if answer is None and timeout_at is not None and now >= timeout_at:
                answered = await self._answer_on(
                    conn,
                    current,
                    gate_id,
                    choice=args.default_choice,
                    answered_by="policy:timeout",
                    verified_human=False,
                    defaulted=True,
                )
                if answered.is_unknown:
                    return answered
                answer = await journal_entry_on(conn, current.id, f"gate-answer:{gate_id}")
            if answer:
                if (
                    not answer["payload"]["defaulted"]
                    and timeout_at is not None
                    and now >= timeout_at
                ):
                    return PrimitiveOutcome.unknown(primitive, "approval_expired", gate_id=gate_id)
                # A generic gate resolve/expiry cannot fabricate this answer.
                hold = await human_hold_on(conn, current)
                if hold:
                    return PrimitiveOutcome.unknown(primitive, hold, gate_id=gate_id)
                row = await self._update_on(
                    conn,
                    current,
                    schedule_values(
                        SubjectSchedule.progress(
                            now=now, max_wait_seconds=current.schedule.max_wait_seconds
                        )
                    ),
                    now,
                )
                return PrimitiveOutcome(
                    primitive=primitive,
                    outcome="answered",
                    detail={**answer["payload"], "subject_version": row["version"]},
                )
            return PrimitiveOutcome(
                primitive=primitive,
                outcome="reused",
                detail={
                    "gate_id": gate_id,
                    "no_default": args.no_default,
                    "timeout_at": timeout_at,
                },
            )
        gate_id = "gate-" + uuid.uuid4().hex[:16]
        timeout_at = now + args.default_after_seconds if args.default_after_seconds else None
        await conn.execute(
            insert(gates).values(
                id=gate_id,
                project_id=current.project_id,
                gate_type="human",
                title=f"Integration subject {current.id}",
                question=args.question,
                await_id=f"integration-subject:{current.id}",
                # The legacy sweep may expire the projection, but not its journal.
                timeout_at=timeout_at,
                status="open",
                created_at=now,
            )
        )
        await append_record_on(
            self.db,
            conn,
            current,
            key=f"gate:{gate_id}",
            primitive=primitive,
            entry_kind="action",
            outcome="created",
            now=now,
            payload={
                "gate_id": gate_id,
                "head": self._head(current),
                "request": args.model_dump(mode="json"),
                "timeout_at": timeout_at,
            },
        )
        schedule = SubjectSchedule.hold(
            now=now,
            gate_id=gate_id,
            max_wait_seconds=current.schedule.max_wait_seconds,
            revisit_at=timeout_at,
        )
        row = await self._update_on(conn, current, schedule_values(schedule), now)
        return PrimitiveOutcome(
            primitive=primitive,
            outcome="created",
            detail={
                "gate_id": gate_id,
                "no_default": args.no_default,
                "timeout_at": timeout_at,
                "subject_version": row["version"],
            },
        )

    @staticmethod
    def _head(subject) -> dict | None:
        return subject.head.model_dump(mode="json") if subject.head else None

    @classmethod
    def _same_identity(cls, entry, subject) -> bool:
        return (
            entry["policy_artifact_sha256"] == subject.policy.artifact_sha256
            and entry["generation"] == subject.generation
            and entry["payload"].get("head") == cls._head(subject)
        )

    async def answer(
        self, subject: Subject, gate_id: str, *, choice: str, answered_by: str, verified_human: bool
    ) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.answer_on(
                conn,
                subject,
                gate_id,
                choice=choice,
                answered_by=answered_by,
                verified_human=verified_human,
            )

    async def answer_on(
        self, conn, subject, gate_id, *, choice, answered_by, verified_human
    ) -> PrimitiveOutcome:
        current = await lock_current_on(self.db, conn, subject)
        if current is None:
            return PrimitiveOutcome.unknown(Primitive.GATE, "stale_subject")
        return await self._answer_on(
            conn,
            current,
            gate_id,
            choice=choice,
            answered_by=answered_by,
            verified_human=verified_human,
            defaulted=False,
        )

    async def _answer_on(
        self, conn, subject, gate_id, *, choice, answered_by, verified_human, defaulted
    ) -> PrimitiveOutcome:
        primitive = Primitive.GATE
        if subject.schedule.gate_id != gate_id:
            return PrimitiveOutcome.unknown(primitive, "gate_not_current")
        definition = await journal_entry_on(conn, subject.id, f"gate:{gate_id}")
        if definition is None or not self._same_identity(definition, subject):
            return PrimitiveOutcome.unknown(primitive, "gate_identity_mismatch")
        request = GateArgs.model_validate(definition["payload"]["request"])
        if not defaulted and not verified_human:
            return PrimitiveOutcome.unknown(primitive, "verified_human_required")
        previous = await journal_entry_on(conn, subject.id, f"gate-answer:{gate_id}")
        now = self.clock()
        timeout_at = definition["payload"]["timeout_at"]
        if not defaulted and timeout_at is not None and now >= timeout_at:
            return PrimitiveOutcome.unknown(primitive, "approval_expired")
        if previous:
            if choice != previous["payload"]["choice"]:
                return PrimitiveOutcome.unknown(primitive, "answer_immutable")
            return PrimitiveOutcome(
                primitive=primitive, outcome="answered", detail=previous["payload"]
            )
        if choice not in request.choices or not str(answered_by).strip():
            return PrimitiveOutcome.unknown(primitive, "invalid_gate_answer")
        if defaulted:
            if (
                request.no_default
                or timeout_at is None
                or now < timeout_at
                or choice != request.default_choice
            ):
                return PrimitiveOutcome.unknown(primitive, "default_not_authorized")
        elif not verified_human:
            return PrimitiveOutcome.unknown(primitive, "verified_human_required")
        elif timeout_at is not None and now >= timeout_at:
            return PrimitiveOutcome.unknown(primitive, "approval_expired")
        payload = {
            "gate_id": gate_id,
            "head": self._head(subject),
            "choice": choice,
            "answered_by": answered_by,
            "answered_at": now,
            "defaulted": defaulted,
        }
        await append_record_on(
            self.db,
            conn,
            subject,
            key=f"gate-answer:{gate_id}",
            primitive=primitive,
            entry_kind="action",
            outcome="answered",
            payload=payload,
            now=now,
        )
        await conn.execute(
            update(gates)
            .where(gates.c.id == gate_id)
            .values(status="resolved", resolution=choice, resolved_by=answered_by)
        )
        # Answering wakes the held subject; gate_on consumes this exact answer
        # after checking product holds. The answer itself never clears a hold.
        await conn.execute(
            update(integration_subjects)
            .where(
                integration_subjects.c.id == subject.id,
            )
            .values(next_due_at=now, wake_requested_at=now)
        )
        return PrimitiveOutcome(primitive=primitive, outcome="answered", detail=payload)

    async def eject(self, subject: Subject, args: EjectArgs) -> PrimitiveOutcome:
        async with self.db._engine.begin() as conn:
            return await self.eject_on(conn, subject, args)

    async def eject_on(self, conn, subject, args) -> PrimitiveOutcome:
        primitive = Primitive.EJECT
        current = await lock_current_on(self.db, conn, subject)
        if current is None:
            return PrimitiveOutcome.unknown(primitive, "stale_subject")
        if not args.reason.strip():
            return PrimitiveOutcome.unknown(primitive, "ejection_reason_required")
        if current.schedule.gate_id:
            return PrimitiveOutcome.unknown(primitive, "human_gate_held")
        hold = await human_hold_on(conn, current, task_id=args.member_task_id)
        if hold:
            return PrimitiveOutcome.unknown(primitive, hold)
        if current.writer.status not in {WriterStatus.NONE, WriterStatus.STOPPED}:
            return PrimitiveOutcome.unknown(primitive, "writer_stop_proof_required")
        if current.phase in {
            SubjectPhase.PUBLISHING,
            SubjectPhase.PUBLISHED,
            SubjectPhase.CLEANING,
        }:
            return PrimitiveOutcome.unknown(primitive, "publication_already_started")
        if self.ejection_reader is None or self.apply_ejection_on is None:
            return PrimitiveOutcome.unknown(primitive, "ejection_adapter_unavailable")
        plan = await self.ejection_reader(conn, current, args)
        if plan is None:
            return PrimitiveOutcome(primitive=primitive, outcome="not_a_member")
        if (
            plan.member_task_id != args.member_task_id
            or not plan.allowed
            or plan.policy_artifact_sha256 != current.policy.artifact_sha256
        ):
            return PrimitiveOutcome.unknown(primitive, "ejection_not_authorized")
        if plan.required and plan.disposition == "skipped" and not plan.allow_required_skip:
            return PrimitiveOutcome.unknown(primitive, "required_work_skip_not_authorized")
        repair = (
            (
                await conn.execute(
                    select(tasks)
                    .where(
                        tasks.c.id == plan.repair_task_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            repair is None
            or repair["project_id"] != current.project_id
            or repair["id"] == args.member_task_id
            or repair["status"] in {"COMPLETED", "FAILED"}
        ):
            return PrimitiveOutcome.unknown(primitive, "repair_provenance_invalid")
        key = journal_key(
            "eject",
            {
                "generation": current.generation,
                "task_id": args.member_task_id,
                "source_head_sha": plan.source_head_sha,
            },
        )
        if await journal_entry_on(conn, current.id, key):
            return PrimitiveOutcome(primitive=primitive, outcome="not_a_member")
        now = self.clock()
        payload = {
            **plan.model_dump(mode="json"),
            "reason": args.reason,
            "previous_head": self._head(current),
        }
        await append_record_on(
            self.db,
            conn,
            current,
            key=key,
            primitive=primitive,
            entry_kind="action",
            outcome="ejected",
            payload=payload,
            now=now,
        )
        # Exceptions roll back the ejection, journal and all projections. The
        # kind adapter must not open a separate transaction or commit here.
        await self.apply_ejection_on(conn, current, args, plan)
        # The member carries its repair relationship even if the kind adapter
        # is later replaced. The journal outlives task archival.
        await conn.execute(
            pg_insert(task_metadata)
            .values(
                task_id=args.member_task_id,
                key=f"integration_ejection:{current.id}:{current.generation}",
                value=json.dumps(payload, sort_keys=True),
            )
            .on_conflict_do_nothing()
        )
        row = await self._update_on(
            conn,
            current,
            {
                "phase": SubjectPhase.BUILDING.value,
                "generation": current.generation + 1,
                **schedule_values(
                    SubjectSchedule.progress(
                        now=now, max_wait_seconds=current.schedule.max_wait_seconds
                    )
                ),
            },
            now,
        )
        return PrimitiveOutcome(
            primitive=primitive,
            outcome="ejected",
            detail={**payload, "subject_version": row["version"], "generation": row["generation"]},
        )
