"""Bounded waits, immutable human gates and audited ejection (primitives 17–19).

Gate definitions and answers live in the append-only subject journal. The
existing ``gates`` table is a dashboard projection, never approval evidence:
resolving or expiring it through a legacy command cannot release a subject.
Ejection is an explicit policy-authorized transaction with a repair link,
using a subject-kind adapter for its physical member/disposition projection.
These ports are inert until CommandHandler's reconciler wiring binds them.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_subjects,
    project_integration_leases,
    sessions,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.engine import RootPolicyEjection, root_engine_guard
from src.integration.records import (
    append_record_on,
    human_hold_on,
    journal_entry_on,
    journal_key,
    lock_current_on,
)
from src.integration.runtime_contracts import (
    ARTIFACT_PATTERN,
    EjectArgs,
    GateArgs,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WaitArgs,
    WriterStatus,
    schedule_values,
)
from src.integration.writers import OperationSafety


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


class RootMemberEjection:
    """Apply the root policy's journalled ejection to its physical membership."""

    def __init__(self, db, *, clock=time.time):
        self.db = db
        self.clock = clock

    @root_engine_guard("batch", outcome="invalid_state", refusal={"success": False})
    async def eject(
        self,
        batch_id: str,
        *,
        task_id: str,
        reason: str,
        operator_id: str,
        resolution_observer: Callable[[dict[str, Any]], Awaitable[str | None]] | None = None,
        policy_ejection: RootPolicyEjection | None = None,
    ) -> dict[str, Any]:
        """Eject before construction or rebuild a safely detached repair candidate."""
        from src.integration.scheduler import TrainService

        if not reason.strip():
            raise ValueError("ejection reason is required")
        now = self.clock()
        transition = None
        observations = {}
        if resolution_observer is not None:
            async with self.db._engine.connect() as read_conn:
                pending = (
                    (
                        await read_conn.execute(
                            select(integration_candidate_resolutions)
                            .join(
                                integration_batch_members,
                                (
                                    integration_batch_members.c.batch_id
                                    == integration_candidate_resolutions.c.batch_id
                                )
                                & (
                                    integration_batch_members.c.ordinal
                                    == integration_candidate_resolutions.c.member_ordinal
                                ),
                            )
                            .where(
                                integration_candidate_resolutions.c.batch_id == batch_id,
                                integration_candidate_resolutions.c.state == "pushed",
                                integration_batch_members.c.task_id == task_id,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            for resolution in pending:
                observations[resolution["id"]] = await resolution_observer(dict(resolution))
        async with self.db.immediate() as conn:
            batch = (
                (
                    await conn.execute(
                        select(integration_batches).where(integration_batches.c.id == batch_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                return {"outcome": "unknown_batch", "batch_id": batch_id}
            project_id = str(batch["project_id"])
            await self.db.lock_hierarchy_project(conn, project_id)
            batch = (
                (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == batch_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            policy_decision = None
            if policy_ejection is not None:
                policy_decision = await policy_ejection.validate_on(
                    self.db, conn, batch, task_id=task_id, reason=reason
                )
                operator_id = "service:root-reconciler"
            members = (
                (
                    await conn.execute(
                        select(integration_batch_members)
                        .where(integration_batch_members.c.batch_id == batch_id)
                        .order_by(integration_batch_members.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            if not any(member["task_id"] == task_id for member in members):
                return {"outcome": "not_a_member", "batch_id": batch_id, "task_id": task_id}
            revision = (
                (
                    await conn.execute(
                        select(integration_candidate_revisions)
                        .where(integration_candidate_revisions.c.batch_id == batch_id)
                        .where(
                            integration_candidate_revisions.c.revision == batch["current_revision"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            repairing = batch["lifecycle"] in {"repairing", "human_blocked"}
            if not repairing and (batch["lifecycle"] != "sealed" or revision is not None):
                return {"outcome": "invalid_state", "batch_id": batch_id, "task_id": task_id}
            operation = stage = None
            if repairing:
                operation = (
                    (
                        await conn.execute(
                            select(integration_repair_operations)
                            .where(
                                integration_repair_operations.c.batch_id == batch_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    revision is None
                    or operation is None
                    or operation["state"] not in {"active", "escalated", "human_required"}
                ):
                    return {"outcome": "invalid_state", "batch_id": batch_id, "task_id": task_id}
                blockers = await OperationSafety._ambiguous_writes_on(
                    conn,
                    operation,
                    allow_reserved_delegate=True,
                )
                rejections = await self._observed_ejection_rejections_on(
                    conn,
                    batch,
                    task_id,
                    observations,
                )
                rejected_ids = {row["id"] for row in rejections}
                blockers = [
                    blocker
                    for blocker in blockers
                    if blocker not in {f"resolution:{row_id}" for row_id in rejected_ids}
                ]
                # Even a historical root promotion intent freezes source
                # identities through its receipt FKs. It must never be rewritten.
                intent = (
                    await conn.execute(
                        select(integration_promotion_intents.c.id)
                        .where(
                            integration_promotion_intents.c.root_batch_id == batch_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                owner = (
                    (
                        await conn.execute(
                            select(integration_branch_owners)
                            .where(
                                integration_branch_owners.c.repository_id == batch["repository_id"],
                                integration_branch_owners.c.ref == batch["integration_branch"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if owner is not None and (
                    owner["handoff_state"] not in {"reserved", "released"}
                    or owner["session_id"] is not None
                    or owner["workspace_id"] is not None
                ):
                    blockers.append("writer")
                if intent is not None:
                    blockers.append("promotion")
                if blockers:
                    return {
                        "outcome": "invalid_state",
                        "batch_id": batch_id,
                        "task_id": task_id,
                        "blockers": sorted(set(blockers)),
                    }
                stage = (
                    (
                        await conn.execute(
                            select(integration_repair_stages)
                            .where(
                                integration_repair_stages.c.operation_id == operation["id"],
                                integration_repair_stages.c.ordinal == operation["active_stage"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if stage is None:
                    return {"outcome": "invalid_state", "batch_id": batch_id, "task_id": task_id}
                if stage["repair_task_id"] is not None:
                    delegate = (
                        (
                            await conn.execute(
                                select(tasks)
                                .where(tasks.c.id == stage["repair_task_id"])
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    from src.database.queries.integration_state_queries import (
                        session_attached_clause,
                    )

                    live_session = (
                        await conn.execute(
                            select(sessions.c.id)
                            .where(
                                sessions.c.task_id == stage["repair_task_id"],
                                session_attached_clause(),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if live_session is not None or (
                        delegate is not None
                        and (
                            delegate["assigned_agent_id"] is not None
                            or delegate["status"] in {"ASSIGNED", "IN_PROGRESS"}
                        )
                    ):
                        return {
                            "outcome": "invalid_state",
                            "batch_id": batch_id,
                            "task_id": task_id,
                            "blockers": ["repair_delegate_not_settled"],
                        }
                    if delegate is not None and delegate["status"] in {
                        "DEFINED",
                        "READY",
                        "PAUSED",
                        "BLOCKED",
                    }:
                        from src.models import TaskStatus

                        transition = await self.db._apply_transition(
                            conn,
                            delegate["id"],
                            TaskStatus.FAILED,
                            context="integration_ejected_delegate",
                            force=True,
                            _manual_pause_control=True,
                            resume_after=None,
                        )
                        await self.db._upsert_meta(
                            delegate["id"],
                            "integration_retirement",
                            {
                                "operation_id": operation["id"],
                                "disposition": "superseded",
                                "previous_status": delegate["status"],
                                "retired_at": now,
                                "reason": f"batch member {task_id} ejected from revision {revision['revision']}",
                            },
                            conn=conn,
                        )
                        await conn.execute(
                            delete(task_metadata).where(
                                task_metadata.c.task_id == delegate["id"],
                                task_metadata.c.key.in_(
                                    (
                                        "needs_attention",
                                        "blocked_terminal",
                                        "claim_prepare_backoff_until",
                                        "manual_pause",
                                    )
                                ),
                            )
                        )
                if owner is not None:
                    from src.integration.models import BranchKey, Fence
                    from src.integration.ownership import BranchOwnership

                    await BranchOwnership(self.db, clock=self.clock).transfer_detached_on(
                        conn,
                        Fence(
                            target=BranchKey(
                                repository_id=batch["repository_id"],
                                branch=batch["integration_branch"],
                            ),
                            owner_id=owner["owner_id"],
                            token=owner["fence_token"],
                        ),
                        operation["id"],
                        "collector",
                    )
                for resolution in rejections:
                    await conn.execute(
                        update(integration_candidate_resolutions)
                        .where(
                            integration_candidate_resolutions.c.id == resolution["id"],
                            integration_candidate_resolutions.c.state == "pushed",
                        )
                        .values(
                            state="rejected",
                            rejection_evidence={
                                "invariant": resolution["invariant"],
                                "rejected_at": now,
                                "disposition": "membership_ejection",
                                "operator_id": operator_id,
                                "remote_sha": observations[resolution["id"]],
                            },
                            updated_at=now,
                        )
                    )

            # The trigger requires this audit row from the same transaction,
            # together with the project lock and the transaction-local markers.
            event_id = await self.db.log_event(
                "integration.batch_ejected",
                project_id=project_id,
                task_id=task_id,
                payload=json.dumps(
                    {
                        "batch_id": batch_id, "reason": reason, "operator_id": operator_id,
                        "at": now,
                        **({"policy_decision": policy_decision} if policy_decision else {}),
                    }
                ),
                conn=conn,
            )
            await conn.execute(
                select(func.set_config("aq.integration_eject_batch", batch_id, True))
            )
            await conn.execute(
                select(func.set_config("aq.integration_eject_event", str(event_id), True))
            )

            if repairing:
                # The immutable revision snapshot keeps old ordinal/result
                # bindings meaningful after the live manifest is compacted.
                await conn.execute(
                    update(integration_candidate_revisions)
                    .where(
                        integration_candidate_revisions.c.batch_id == batch_id,
                        integration_candidate_revisions.c.source_manifest.is_(None),
                    )
                    .values(source_manifest=[dict(member) for member in members])
                )
                await conn.execute(
                    update(integration_candidate_revisions)
                    .where(
                        integration_candidate_revisions.c.batch_id == batch_id,
                    )
                    .values(state="superseded", updated_at=now)
                )

            await conn.execute(
                delete(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch_id,
                    integration_batch_members.c.task_id == task_id,
                )
            )
            remaining = [member for member in members if member["task_id"] != task_id]
            for ordinal, member in enumerate(remaining):
                if member["ordinal"] != ordinal:
                    await conn.execute(
                        update(integration_batch_members)
                        .where(
                            integration_batch_members.c.batch_id == batch_id,
                            integration_batch_members.c.task_id == member["task_id"],
                        )
                        .values(ordinal=ordinal)
                    )
            manifest = [
                {
                    "task_id": member["task_id"],
                    "repository_id": member["repository_id"],
                    "source_base": member["source_base_sha"],
                    "source_head": member["reviewed_head_sha"],
                    "review": {
                        "id": member["review_evidence_id"],
                        "reviewed_tree_sha": member["reviewed_tree_sha"],
                    },
                    "source_ref": member["source_ref"],
                    "source_ref_retention": member["source_ref_retention"],
                }
                for member in remaining
            ]
            values: dict[str, Any] = {"updated_at": now}
            if repairing:
                values.update(tested_candidate_sha=None, ci_evidence_id=None)
            if remaining:
                values.update(
                    source_manifest_digest=TrainService._manifest_digest(manifest),
                    base_sha=remaining[0]["source_base_sha"],
                )
            else:
                values.update(
                    lifecycle="aborted",
                    human_abort_reason=reason,
                    cleanup_state="complete",
                )
                await conn.execute(
                    update(integration_repair_operations)
                    .where(integration_repair_operations.c.batch_id == batch_id)
                    .values(state="cancelled", updated_at=now)
                )
                await conn.execute(
                    delete(project_integration_leases).where(
                        project_integration_leases.c.project_id == project_id,
                        project_integration_leases.c.batch_id == batch_id,
                    )
                )
                await TrainService._consume_request(conn, project_id, batch["request_id"], now)
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch_id)
                .values(**values)
            )
            if repairing and remaining:
                next_revision = int(batch["current_revision"]) + 1
                base = revision["construction_base_sha"]
                await conn.execute(
                    insert(integration_candidate_revisions).values(
                        batch_id=batch_id,
                        revision=next_revision,
                        construction_base_sha=base,
                        source_manifest=[
                            dict(member) | {"ordinal": ordinal}
                            for ordinal, member in enumerate(remaining)
                        ],
                        head_sha=base,
                        state="constructing",
                        repair_parent_revision=revision["revision"],
                        created_at=now,
                        updated_at=now,
                    )
                )
                dossier = dict(stage["dossier"] or {})
                dossier.setdefault("membership_ejections", []).append(
                    {
                        "task_id": task_id,
                        "revision": revision["revision"],
                        "at": now,
                        "previous_writer": stage["repair_task_id"],
                        "previous_subject": stage["current_subject"],
                        "reviewed_file_guard": dossier.get("reviewed_file_guard"),
                    }
                )
                for key in ("reviewed_file_guard", "repair_commits", "conflict", "main_rebuild"):
                    dossier.pop(key, None)
                dossier["manifest"] = {
                    "kind": "batch",
                    "batch_id": batch_id,
                    "source_manifest_digest": values["source_manifest_digest"],
                    "revision": next_revision,
                }
                dossier["starting_sha"] = base
                dossier["branch_sha"] = base
                stage_values = {
                    "state": "active",
                    "starting_sha": base,
                    "current_subject": {
                        "kind": "batch",
                        "revision": next_revision,
                        "candidate_sha": base,
                    },
                    "success_subject": None,
                    "success_evidence_id": None,
                    "repair_task_id": None,
                    "writer_kind": None,
                    "retained_workspace_id": None,
                    "retained_handoff": None,
                    "dossier": dossier,
                    "completed_at": None,
                }
                if operation["state"] == "human_required":
                    from src.integration.models import RepairPolicy
                    from src.integration.outbox import enqueue_integration_event

                    policy = RepairPolicy.model_validate(stage["policy"])
                    duration = (
                        policy.primary_seconds
                        if int(stage["ordinal"]) == 0
                        else policy.debug_seconds
                    )
                    deadline_id = f"repair-deadline-{operation['id']}-eject-{next_revision}"
                    stage_values.update(
                        started_at=now, deadline_at=now + duration, deadline_event_id=deadline_id
                    )
                    dossier["budget"] = dict(dossier.get("budget") or {}) | {
                        "started_at": now,
                        "deadline_at": now + duration,
                        "attempts": stage["attempts"],
                    }
                    await enqueue_integration_event(
                        conn,
                        event_id=deadline_id,
                        dedup_key=deadline_id,
                        project_id=project_id,
                        event_type="integration.repair_deadline_due",
                        available_at=now + duration,
                        payload={
                            "operation_id": operation["id"],
                            "stage": stage["ordinal"],
                            "deadline_event_id": deadline_id,
                        },
                    )
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(**stage_values)
                )
                await conn.execute(
                    update(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.id == operation["id"],
                    )
                    .values(
                        state="active" if int(stage["ordinal"]) == 0 else "escalated",
                        updated_at=now,
                    )
                )
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == batch_id,
                    )
                    .values(
                        current_revision=next_revision,
                        lifecycle="sealed",
                        tested_candidate_sha=None,
                        ci_evidence_id=None,
                        updated_at=now,
                    )
                )
                from src.integration.outbox import enqueue_integration_event

                event = f"integration-sealed:{batch_id}:eject:{next_revision}"
                await enqueue_integration_event(
                    conn,
                    event_id=event,
                    dedup_key=event,
                    project_id=project_id,
                    event_type="integration.sealed",
                    available_at=now,
                    payload={"batch_id": batch_id, "operation_id": operation["id"]},
                )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return {"outcome": "ejected", "batch_id": batch_id, "task_id": task_id, "reason": reason}


    async def _observed_ejection_rejections_on(self, conn, batch, task_id, observations):
        """Prove that a refused private push is retained evidence, not a live write."""
        from src.integration.migration_heads import REVIEWED_FILE_GUARDS

        if not observations:
            return []
        resolution = integration_candidate_resolutions
        stage = integration_repair_stages
        member = integration_batch_members
        rows = (
            (
                await conn.execute(
                    select(resolution, stage.c.dossier)
                    .join(
                        stage,
                        (stage.c.operation_id == resolution.c.operation_id)
                        & (stage.c.ordinal == resolution.c.stage_ordinal),
                    )
                    .join(
                        member,
                        (member.c.batch_id == resolution.c.batch_id)
                        & (member.c.ordinal == resolution.c.member_ordinal),
                    )
                    .where(
                        resolution.c.batch_id == batch["id"],
                        resolution.c.revision == batch["current_revision"],
                        resolution.c.state == "pushed",
                        member.c.task_id == task_id,
                        resolution.c.id.in_(observations),
                    )
                    .with_for_update(of=resolution)
                )
            )
            .mappings()
            .all()
        )
        verified = []
        for row in rows:
            guard = (row["dossier"] or {}).get("reviewed_file_guard") or {}
            if (
                observations[row["id"]] != row["resolved_head_sha"]
                or guard.get("reservation_id") != row["id"]
                or guard.get("revision") != row["revision"]
                or guard.get("invariant") not in REVIEWED_FILE_GUARDS
            ):
                continue
            session = (
                (
                    await conn.execute(
                        select(sessions)
                        .where(
                            sessions.c.id == row["repair_session_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            workspace = (
                (
                    await conn.execute(
                        select(workspaces)
                        .where(
                            workspaces.c.id == row["repair_workspace_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            applied = (
                await conn.execute(
                    select(integration_candidate_ref_mutations.c.id).where(
                        integration_candidate_ref_mutations.c.resolution_id == row["id"],
                        integration_candidate_ref_mutations.c.purpose == "repair_resolution",
                        integration_candidate_ref_mutations.c.state == "applied",
                        integration_candidate_ref_mutations.c.remote_sha
                        == row["resolved_head_sha"],
                    )
                )
            ).scalar_one_or_none()
            if (
                session is None
                or workspace is None
                or applied is None
                or session["state"] != "stopped"
                or session["desired_state"] != "stopped"
                or session["instance_token"] != row["repair_session_instance_token"]
                or session["project_id"] != row["project_id"]
                or session["task_id"] not in {None, row["repair_task_id"]}
                or session["work_dir"] != row["repair_workspace_path"]
                or workspace["workspace_path"] != row["repair_workspace_path"]
                or workspace["project_id"] != row["project_id"]
                or workspace["locked_by_task_id"] is not None
            ):
                continue
            verified.append(dict(row) | {"invariant": guard["invariant"]})
        return verified
