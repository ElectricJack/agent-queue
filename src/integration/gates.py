"""Immutable human gates on integration subjects (primitive 18).

Gate definitions and answers live in the append-only subject journal. The
existing ``gates`` table is a dashboard projection, never approval evidence:
resolving or expiring it through a legacy command cannot release a subject.
Verified human answers arrive through the ask source and operator releases
through ``integration_release_held_gate``. The unbound wait and ejection ports
were retired with the primitive port adapters; train ejection is
``TrainControls.eject``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import insert, text, update

from src.database.tables import (
    gates,
    integration_subjects,
)
from src.integration.records import (
    append_record_on,
    human_hold_on,
    journal_entry_on,
    lock_current_on,
)
from src.integration.runtime_contracts import (
    GateArgs,
    Primitive,
    PrimitiveOutcome,
    Subject,
    SubjectKind,
    SubjectSchedule,
    schedule_values,
)


class GatePrimitives:
    """Each mutation locks a version, commits its audit, and bumps version.

    ``answer_on`` is for a CommandHandler adapter with verified human evidence;
    policy cannot answer a human gate. Callers reload a subject before
    scheduling after these mutations.
    """

    def __init__(self, db, *, clock=time.time):
        self.db = db
        self.clock = clock

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
        self, conn, subject, gate_id, *, choice, answered_by, verified_human,
        decision_id=None, evidence_entry_id=None,
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
            defaulted=False, decision_id=decision_id, evidence_entry_id=evidence_entry_id,
        )

    async def _answer_on(
        self, conn, subject, gate_id, *, choice, answered_by, verified_human, defaulted,
        decision_id=None, evidence_entry_id=None,
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
            if decision_id and previous["payload"].get("decision_id") != decision_id:
                return PrimitiveOutcome.unknown(primitive, "answer_immutable")
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
            **({"decision_id": decision_id, "evidence_entry_id": evidence_entry_id}
               if decision_id else {}),
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

    async def release_hold(
        self, subject: Subject, gate_id: str, *, reason: str, operator_id: str,
        verified_human: bool, dry_run: bool = False,
    ) -> dict[str, Any]:
        """Explicit operator release; never rewrite or consume the old answer.

        Parent mutations share this exclusion across remote actions. Take the
        exclusive lock before the subject row, just as an engine transfer does.
        This control is deliberately absent from the policy primitive ports.
        """
        from src.integration.engine import EngineRefused
        from src.integration.owner_guards import parent_lock_key

        if not verified_human or not operator_id.strip():
            raise EngineRefused("verified_human_required")
        if subject.kind is not SubjectKind.PARENT_EPISODE or not subject.is_live:
            raise EngineRefused("live_parent_subject_required")
        if not dry_run and not reason.strip():
            raise EngineRefused("release_reason_required")
        async with self.db.immediate() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": parent_lock_key(subject.task_id)},
            )
            current = await lock_current_on(self.db, conn, subject)
            if current is None:
                raise EngineRefused("stale_subject")
            if current.schedule.gate_id != gate_id:
                raise EngineRefused("gate_not_current")
            definition = await journal_entry_on(conn, current.id, f"gate:{gate_id}")
            answer = await journal_entry_on(conn, current.id, f"gate-answer:{gate_id}")
            if (definition is None or answer is None
                    or not self._same_identity(definition, current)
                    or not self._same_identity(answer, current)):
                raise EngineRefused("held_gate_identity_mismatch")
            if answer["payload"].get("choice") != "hold":
                raise EngineRefused("gate_not_held")
            detail = {
                "subject_id": current.id, "gate_id": gate_id,
                "expected_version": current.version, "engine": current.engine.value,
            }
            if dry_run:
                return {"outcome": "preview", **detail}
            now = self.clock()
            await append_record_on(
                self.db, conn, current, key=f"gate-release:{gate_id}",
                primitive=Primitive.RECORD_DECISION, entry_kind="action", outcome="released",
                now=now, payload={
                    **detail, "head": self._head(current), "previous_answer": answer["payload"],
                    "reason": reason, "operator_id": operator_id,
                    "command": "integration_release_held_gate",
                },
            )
            row = await self._update_on(
                conn, current, schedule_values(SubjectSchedule.progress(
                    now=now, max_wait_seconds=current.schedule.max_wait_seconds,
                )), now,
            )
            await conn.execute(
                update(integration_subjects).where(integration_subjects.c.id == current.id)
                .values(wake_requested_at=now),
            )
            return {"outcome": "released", **detail, "subject_version": row["version"]}

