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
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    archived_tasks,
    integration_batches,
    integration_branch_owners,
    integration_child_dispositions,
    integration_episode_receipt_acceptances,
    integration_operation_artifact_pins,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_subject_journal,
    integration_subjects,
    playbook_artifacts,
    project_integration_schedules,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    task_session_attempts,
    tasks,
)
from src.integration.ci import ParentVerification, binding_diagnosis, required_checks
from src.integration.drain_owners import terminal_reservation_clause
from src.integration.models import (
    AWAITING_TRUSTED_VERIFICATION,
    HierarchicalIntegrationPolicy,
    deprecated_route_fields,
)
from src.integration.observe import ParentReadiness
from src.integration.outbox import enqueue_integration_event
from src.integration.owner_guards import (
    active_parent_scope,
    parent_checkpoint_allowed_on,
    parent_engine_guard,
)
from src.integration.promotion_steps import FlowSchema, flow_targets, protected_changes
from src.integration.runtime_contracts import (
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
from src.playbooks.artifact_ref import ArtifactRef

_OID = re.compile(r"^[0-9a-f]{40}$")
logger = logging.getLogger(__name__)






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
    from src.operator_decisions import history_on, related_on

    refs = set()
    for kind, identity in (("task", subject.task_id), ("task", task_id),
                           ("batch", subject.batch_id)):
        if identity:
            refs.update(await related_on(conn, kind, identity))
    holds = [row for row in await history_on(conn, subject.project_id, refs) if row["active"]]
    if holds:
        return "operator_decision_hold:" + holds[0]["id"]
    ids = sorted({value for value in (subject.task_id, task_id) if value})
    if ids:
        # manual_pause takes the task lock before writing its snapshot.
        await conn.execute(
            select(tasks.c.id).where(tasks.c.id.in_(ids)).order_by(tasks.c.id).with_for_update()
        )
        held = (
            await conn.execute(
                select(task_metadata.c.task_id)
                .where(
                    task_metadata.c.task_id.in_(ids),
                    task_metadata.c.key == "manual_pause",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
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


class ParentEpisodeRecords(ParentReadiness, ParentVerification):
    """Shared receipt and trusted-verification invariants for parent subjects."""

    async def reserve_episode_on(
        self,
        conn,
        *,
        parent: dict[str, Any],
        project: dict[str, Any],
        checkpoint: dict[str, Any],
        pre_collection_sha: str,
        carry_forward: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing_episode = checkpoint.get("episode_id")
        if existing_episode:
            existing = (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id == existing_episode,
                    )
                )
            ).mappings().one_or_none()
            if existing is None:
                raise HierarchyError("invariant_error", "checkpoint episode has no operation")
            return dict(existing)

        raw_policy = project.get("hierarchical_integration_policy")
        if raw_policy is None:
            raise HierarchyError("invalid", "hierarchical integration policy is missing")
        try:
            policy = HierarchicalIntegrationPolicy.model_validate(raw_policy)
        except Exception as exc:
            raise HierarchyError("invalid", f"hierarchical integration policy is invalid: {exc}") from exc
        boundary = policy.parent
        route = boundary.route
        artifact_row = (
            await conn.execute(
                select(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256 == route.artifact.artifact_sha256
                )
            )
        ).mappings().one_or_none()
        if artifact_row is None:
            raise HierarchyError("invalid", "parent route artifact is not stored")
        if ArtifactRef.from_row(artifact_row).as_dict() != route.artifact.model_dump(mode="json"):
            raise HierarchyError("invalid", "parent route artifact identity changed")
        if (
            artifact_row["playbook_id"] != route.playbook_id
            or artifact_row["scope"] != route.scope
            or artifact_row["scope_identifier"] != route.scope_identifier
        ):
            raise HierarchyError("invalid", "parent route does not match the stored artifact")

        now = self.clock()
        episode_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        await conn.execute(
            insert(integration_parent_episodes).values(
                id=episode_id,
                parent_task_id=parent["id"],
                repository_id=checkpoint["repository_id"],
                generation=int(checkpoint["generation"]),
                pre_collection_checkpoint_sha=pre_collection_sha,
                created_at=now,
            )
        )
        operation = {
            "id": operation_id,
            "target_kind": "parent",
            "parent_task_id": parent["id"],
            "episode_id": episode_id,
            "active_stage": 0,
            "state": "active",
            "policy_snapshot": policy.model_dump(mode="json"),
            "artifact_snapshot": route.artifact.model_dump(mode="json"),
            "required_check_version": boundary.required_checks.version,
            "verifier_task_id": None,
            "route_playbook_id": route.playbook_id,
            "route_scope": route.scope,
            "route_scope_identifier": route.scope_identifier,
            "route_activation_id": route.activation_id,
            "created_at": now,
            "updated_at": now,
        }
        await conn.execute(insert(integration_repair_operations).values(**operation))
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id=operation_id,
                artifact_sha256=route.artifact.artifact_sha256,
            )
        )
        if carry_forward is not None:
            previous = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.id
                        == carry_forward["verification_id"],
                        integration_parent_verifications.c.operation_id
                        == carry_forward["operation_id"],
                        integration_parent_verifications.c.parent_task_id == parent["id"],
                        integration_parent_verifications.c.episode_id
                        == carry_forward["episode_id"],
                        integration_parent_verifications.c.head_sha
                        == carry_forward["head_sha"],
                    )
                )
            ).mappings().one_or_none()
            previous_operation = (
                await conn.execute(
                    select(integration_repair_operations.c.id).where(
                        integration_repair_operations.c.id
                        == carry_forward["operation_id"],
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id
                        == carry_forward["episode_id"],
                        integration_repair_operations.c.state == "completed",
                    )
                )
            ).one_or_none()
            previous_completion = (
                await conn.execute(
                    select(integration_parent_operation_completions.c.operation_id).where(
                        integration_parent_operation_completions.c.operation_id
                        == carry_forward["operation_id"],
                        integration_parent_operation_completions.c.verification_id
                        == carry_forward["verification_id"],
                        integration_parent_operation_completions.c.parent_task_id
                        == parent["id"],
                        integration_parent_operation_completions.c.episode_id
                        == carry_forward["episode_id"],
                    )
                )
            ).one_or_none()
            previous_episode = (
                await conn.execute(
                    select(integration_parent_episodes.c.id).where(
                        integration_parent_episodes.c.id == carry_forward["episode_id"],
                        integration_parent_episodes.c.parent_task_id == parent["id"],
                        integration_parent_episodes.c.repository_id
                        == checkpoint["repository_id"],
                    )
                )
            ).one_or_none()
            if (
                previous is None
                or previous_operation is None
                or previous_completion is None
                or previous_episode is None
            ):
                raise HierarchyError(
                    "stale_head", "previous verified aggregate changed before rollover"
                )
            receipt_ids = (
                await conn.execute(
                    select(task_delivery_receipts.c.id).where(
                        task_delivery_receipts.c.target_task_id == parent["id"],
                        task_delivery_receipts.c.repository_id
                        == checkpoint["repository_id"],
                        task_delivery_receipts.c.target_branch == checkpoint["branch"],
                        or_(
                            and_(
                                task_delivery_receipts.c.parent_operation_id
                                == carry_forward["operation_id"],
                                task_delivery_receipts.c.parent_episode_id
                                == carry_forward["episode_id"],
                            ),
                            task_delivery_receipts.c.id.in_(
                                select(integration_episode_receipt_acceptances.c.receipt_id).where(
                                    integration_episode_receipt_acceptances.c.operation_id
                                    == carry_forward["operation_id"],
                                    integration_episode_receipt_acceptances.c.episode_id
                                    == carry_forward["episode_id"],
                                )
                            ),
                        ),
                    )
                )
            ).scalars().all()
            for receipt_id in receipt_ids:
                await conn.execute(
                    insert(integration_episode_receipt_acceptances).values(
                        episode_id=episode_id,
                        receipt_id=receipt_id,
                        operation_id=operation_id,
                        previous_episode_id=carry_forward["episode_id"],
                        previous_operation_id=carry_forward["operation_id"],
                        previous_verification_id=carry_forward["verification_id"],
                        ancestry_from_sha=carry_forward["head_sha"],
                        ancestry_to_sha=pre_collection_sha,
                        created_at=now,
                    )
                )
        return operation


    async def mark_ready_on(
        self, conn, task_id: str, *, require_verifier: bool = False, event_suffix: str = ""
    ) -> dict[str, Any]:
        """Project readiness into checkpoint state and one durable event."""
        if not await parent_checkpoint_allowed_on(conn, self.db, task_id):
            return {"outcome": "waiting", "task_id": task_id}
        parent, project, checkpoint, operation = await self._locked_context_on(conn, task_id)
        if parent["status"] != "PAUSED":
            return {"outcome": "waiting", "task_id": task_id}
        readiness = await self.readiness_on(
            conn,
            parent=parent,
            project=project,
            checkpoint=checkpoint,
            operation=operation,
        )
        if readiness["outcome"] != "ready":
            return readiness
        if active_parent_scope(self.db, task_id) and operation.get("verifier_task_id") is None:
            # A receipt committed by an active port may call this lifecycle
            # hook. Filing belongs to the table's next visit. Only the explicit
            # link step after writer_file may project a linked verifier here.
            return readiness
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        attempts = (
            await conn.execute(
                select(task_session_attempts.c.id).where(
                    task_session_attempts.c.task_id == task_id
                ).limit(1)
            )
        ).first()
        if require_verifier or (attempts is None and policy.branchless_parent == "verifier"):
            route = policy.parent
            # Only the class hint is required: the verifier is filed unrouted
            # and the router writes its profile (``verifier_profile_id`` is
            # deprecated and ignored).
            if not route.verifier_intelligence_class:
                await enqueue_integration_event(
                    conn,
                    event_id=f"parent-verifier-route-missing-{operation['id']}",
                    dedup_key=f"task.integration_configuration_blocked:{operation['id']}",
                    project_id=parent["project_id"],
                    event_type="task.integration_configuration_blocked",
                    payload={
                        "project_id": parent["project_id"],
                        "operation_id": operation["id"],
                        "task_id": task_id,
                        "title": parent["title"],
                        "reason": "verifier_routing_missing",
                    },
                    available_at=self.clock(),
                )
                return readiness | {
                    "outcome": "configuration_blocked",
                    "reason": "verifier_routing_missing",
                }
            if operation.get("verifier_task_id") is None:
                from src.models import Task, TaskStatus

                verifier_id = f"verify-{operation['id']}"
                existing = (
                    await conn.execute(select(tasks.c.id).where(tasks.c.id == verifier_id))
                ).first()
                archived = await conn.scalar(
                    select(archived_tasks.c.id).where(archived_tasks.c.id == verifier_id)
                )
                if existing is not None or archived is not None:
                    # Recovery preserves the failed verifier and its completion.
                    # Bind a fresh task to the new generation, never re-arm it.
                    verifier_id = f"verify-{operation['id']}-g{checkpoint['generation']}"
                    for table in (tasks, archived_tasks):
                        if await conn.scalar(select(table.c.id).where(table.c.id == verifier_id)):
                            raise HierarchyError(
                                "invariant_error", "fresh aggregate verifier identity already exists"
                            )
                    existing = None
                if existing is None:
                    await self.db.create_task(
                        Task(
                            id=verifier_id,
                            project_id=parent["project_id"],
                            title=f"Verify aggregate for {task_id}",
                            description=(
                                "Verify the exact collected aggregate and record trusted "
                                "integration check evidence."
                            ),
                            status=TaskStatus.PAUSED,
                            repo_id=checkpoint["repository_id"],
                            branch_name=checkpoint["branch"],
                            class_hint=route.verifier_intelligence_class,
                            dedup_key=f"integration-verifier:{operation['id']}:{checkpoint['generation']}",
                        ),
                        conn=conn,
                    )
                await conn.execute(
                    update(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation["id"])
                    .where(integration_repair_operations.c.verifier_task_id.is_(None))
                    .values(verifier_task_id=verifier_id, updated_at=self.clock())
                )
                operation = dict(operation) | {"verifier_task_id": verifier_id}
        owner = (
            await conn.execute(
                select(integration_branch_owners).where(
                    integration_branch_owners.c.repository_id
                    == checkpoint["repository_id"],
                    integration_branch_owners.c.ref == checkpoint["branch"],
                )
            )
        ).mappings().one_or_none()
        if owner is None:
            raise HierarchyError(
                "invariant_error", "integration-ready parent has no branch owner"
            )
        next_owner_id = operation.get("verifier_task_id") or task_id
        projected = await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == task_id)
            .where(task_integration_checkpoints.c.episode_id == checkpoint["episode_id"])
            .where(task_integration_checkpoints.c.state != "integration_ready")
            .values(state="integration_ready", updated_at=self.clock())
        )
        if projected.rowcount:
            await enqueue_integration_event(
                conn,
                event_id=f"parent-ready-{operation['id']}-{checkpoint['generation']}{event_suffix}",
                dedup_key=(
                    f"task.integration_ready:{operation['id']}:{checkpoint['generation']}{event_suffix}"
                ),
                project_id=parent["project_id"],
                event_type="task.integration_ready",
                payload={
                    "project_id": parent["project_id"],
                    "operation_id": operation["id"],
                    "task_id": task_id,
                    "title": parent["title"],
                    "episode_id": checkpoint["episode_id"],
                    "generation": int(checkpoint["generation"]),
                    "head_sha": readiness["head_sha"],
                    "verifier_task_id": operation.get("verifier_task_id"),
                    "target": {
                        "repository_id": checkpoint["repository_id"],
                        "branch": checkpoint["branch"],
                    },
                    "expected_token": int(owner["fence_token"]),
                    "next_owner_id": next_owner_id,
                    "next_role": "verifier",
                },
                available_at=self.clock(),
            )
        return readiness | {"state": "integration_ready"}


    async def record_disposition(
        self,
        child_task_id: str,
        *,
        disposition: str,
        reviewed_head_sha: str,
        reviewed_tree_sha: str,
        verification_evidence: dict[str, Any],
        resolution_evidence: dict[str, Any],
        verified_completion_id: str | None = None,
        verified_review_id: str | None = None,
    ) -> dict[str, Any]:
        if disposition not in {"noop", "ineligible", "skipped"}:
            raise HierarchyError("invalid", "unsupported delivery disposition")
        if not resolution_evidence or (disposition == "noop" and not verification_evidence):
            raise HierarchyError("invalid", "disposition evidence is required")
        async with self.db.immediate() as conn:
            child = (
                await conn.execute(select(tasks).where(tasks.c.id == child_task_id))
            ).mappings().one_or_none()
            if child is None or not child["parent_task_id"]:
                raise HierarchyError("invalid", "disposition child has no parent")
            parent, _project, checkpoint, operation = await self._locked_context_on(
                conn, child["parent_task_id"]
            )
            child = (
                await conn.execute(select(tasks).where(tasks.c.id == child_task_id))
            ).mappings().one()
            child_checkpoint = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == child_task_id
                    )
                )
            ).mappings().one_or_none()
            if (
                child["status"] not in {"COMPLETED", "FAILED"}
                or child_checkpoint is None
                or child_checkpoint["checkpoint_sha"] != reviewed_head_sha
            ):
                raise HierarchyError("invalid", "disposition source is not terminal at that head")
            if verified_completion_id is not None:
                completion = (
                    await conn.execute(
                        select(task_completion_records)
                        .where(task_completion_records.c.task_id == child_task_id)
                        .order_by(
                            task_completion_records.c.completed_at.desc(),
                            task_completion_records.c.id.desc(),
                        )
                        .limit(1)
                    )
                ).mappings().one_or_none()
                if (
                    completion is None
                    or completion["id"] != verified_completion_id
                    or completion["outcome"] != "pass"
                    or completion["work_outcome"] != "no-op"
                    or completion["branch"] != child["branch_name"]
                    or child["status"] != "COMPLETED"
                ):
                    raise HierarchyError("invalid", "current child close is not a verified no-op")
                if child["profile_id"] in {"reviewer", "final-reviewer"}:
                    review = (
                        await conn.execute(
                            select(integration_review_evidence).where(
                                integration_review_evidence.c.id == verified_review_id,
                                integration_review_evidence.c.reviewer_task_id == child_task_id,
                                integration_review_evidence.c.verdict == "approved",
                            )
                        )
                    ).mappings().one_or_none()
                    if review is None:
                        raise HierarchyError("invalid", "approved reviewer evidence is required")
            code_receipt = (
                await conn.execute(
                    select(task_delivery_receipts.c.id).where(
                        task_delivery_receipts.c.source_task_id == child_task_id,
                        task_delivery_receipts.c.disposition == "code",
                    ).limit(1)
                )
            ).first()
            if code_receipt:
                raise HierarchyError("delivery_target_fixed", "delivered code cannot be disposed")
            origin = (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == child_task_id,
                        task_branch_origins.c.repository_id == checkpoint["repository_id"],
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings().one_or_none()
            if origin is None:
                raise HierarchyError("invalid", "disposition child has no active branch origin")
            if verified_completion_id is not None and origin["base_sha"] != reviewed_head_sha:
                raise HierarchyError("invalid", "no-op child head differs from its reserved base")
            current = (
                await conn.execute(
                    select(integration_child_dispositions)
                    .where(
                        integration_child_dispositions.c.parent_task_id == parent["id"],
                        integration_child_dispositions.c.child_task_id == child_task_id,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            revision = 0 if current is None else int(current["revision"])
            completion_changed = False
            if current is not None and verified_completion_id is not None:
                prior_key = (
                    f"disposition:{parent['id']}:{child_task_id}:"
                    f"{current['parent_operation_id']}:{revision}"
                )
                prior = (
                    await conn.execute(
                        select(task_delivery_receipts).where(
                            task_delivery_receipts.c.domain_key == prior_key
                        )
                    )
                ).mappings().one_or_none()
                completion_changed = bool(
                    prior is not None
                    and (prior["resolution_evidence"] or {}).get("completion_id")
                    != verified_completion_id
                )
            if current is not None and (current["disposition"] != disposition or completion_changed):
                revision += 1
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(task_integration_checkpoints.c.task_id == parent["id"])
                    .values(
                        generation=task_integration_checkpoints.c.generation + 1,
                        verified_sha=None,
                        verified_generation=None,
                        current_verification_id=None,
                        version=task_integration_checkpoints.c.version + 1,
                        updated_at=self.clock(),
                    )
                )
            if current is None:
                await conn.execute(
                    insert(integration_child_dispositions).values(
                        parent_task_id=parent["id"],
                        child_task_id=child_task_id,
                        revision=revision,
                        disposition=disposition,
                        parent_operation_id=operation["id"],
                        parent_episode_id=checkpoint["episode_id"],
                        updated_at=self.clock(),
                    )
                )
            elif (
                current["disposition"] != disposition
                or completion_changed
                or current["parent_operation_id"] != operation["id"]
                or current["parent_episode_id"] != checkpoint["episode_id"]
            ):
                await conn.execute(
                    update(integration_child_dispositions)
                    .where(
                        integration_child_dispositions.c.parent_task_id == parent["id"],
                        integration_child_dispositions.c.child_task_id == child_task_id,
                    )
                    .values(
                        revision=revision,
                        disposition=disposition,
                        parent_operation_id=operation["id"],
                        parent_episode_id=checkpoint["episode_id"],
                        updated_at=self.clock(),
                    )
                )
            domain_key = (
                f"disposition:{parent['id']}:{child_task_id}:"
                f"{operation['id']}:{revision}"
            )
            existing = (
                await conn.execute(
                    select(task_delivery_receipts).where(
                        task_delivery_receipts.c.domain_key == domain_key
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                if existing["disposition"] != disposition:
                    raise HierarchyError("invariant_error", "disposition identity changed")
                return dict(existing) | {"revision": revision}
            receipt = {
                "id": str(uuid.uuid4()),
                "domain_key": domain_key,
                "source_task_id": child_task_id,
                "target_task_id": parent["id"],
                "repository_id": checkpoint["repository_id"],
                "target_branch": checkpoint["branch"],
                "reviewed_head_sha": reviewed_head_sha,
                "reviewed_tree_sha": reviewed_tree_sha,
                "before_sha": None,
                "squash_sha": None,
                "after_sha": None,
                "verification_evidence": verification_evidence,
                "resolution_evidence": {
                    **resolution_evidence,
                    "origin_id": origin["id"],
                    "origin_base_sha": origin["base_sha"],
                    "episode_id": checkpoint["episode_id"],
                },
                "disposition": disposition,
                "disposition_revision": revision,
                "parent_operation_id": operation["id"],
                "parent_episode_id": checkpoint["episode_id"],
                "created_at": self.clock(),
            }
            await conn.execute(insert(task_delivery_receipts).values(**receipt))
            await self.mark_ready_on(conn, parent["id"])
            return receipt | {"revision": revision}


    @parent_engine_guard()
    async def complete_parent(
        self, task_id: str, generation: int, head_sha: str,
        *, accepted_close: dict | None = None,
    ) -> dict[str, Any]:
        """Complete a verified parent.  ``accepted_close`` is the identity of
        the parent's own ``task close`` when that close drives this completion;
        it is recorded with the COMPLETED transition."""
        result = await self._complete_parent_transition(
            task_id, generation, head_sha, accepted_close=accepted_close
        )
        if result["outcome"] not in {"completed", "already_completed"} or self.git_manager is None:
            return result

        async with self.db._engine.connect() as conn:
            route = (
                await conn.execute(
                    select(tasks.c.parent_task_id, tasks.c.pr_url,
                           projects.c.hierarchical_integration_mode)
                    .join(projects, projects.c.id == tasks.c.project_id)
                    .where(tasks.c.id == task_id)
                )
            ).one_or_none()
        if route is None or route.parent_task_id is not None or route.hierarchical_integration_mode != "train":
            return result
        # Completion is already durable and its PR was recorded. Admission
        # owns subsequent GitHub liveness checks; a retry must remain idempotent
        # even while GitHub is unavailable or the PR has since closed.
        if result["outcome"] == "already_completed" and route.pr_url:
            return result

        from src.integration.root_pull_requests import EpicPullRequestService

        try:
            pr = await EpicPullRequestService(
                self.db, git_manager=self.git_manager, clock=self.clock
            ).open_for_epic(task_id)
        except Exception as exc:
            logger.exception("Could not open pull request for completed epic %s", task_id)
            return {"outcome": "pr_open_failed", "task_id": task_id, "reason": str(exc)}
        logger.info("Parent %s pull request outcome: %s", task_id, pr["outcome"])
        if pr["outcome"] in {"branch_missing", "repository_missing", "unknown_epic"}:
            return {"outcome": "pr_open_refused", "task_id": task_id, "reason": pr["outcome"]}
        return result


    async def _complete_parent_transition(
        self, task_id: str, generation: int, head_sha: str,
        *, accepted_close: dict | None = None,
    ) -> dict[str, Any]:
        from src.database.queries.task_queries import _INTEGRATION_COMPLETION_TOKEN
        from src.models import TaskStatus

        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            # A verifier can crash after this method has committed the parent
            # completion but before its own session close is persisted.  The
            # retry retains the same fenced verifier and must be able to
            # close, but only when the durable completion is for this exact
            # operation, episode, generation, head, and verification.
            if (
                operation["state"] == "completed"
                and parent["status"] == TaskStatus.COMPLETED.value
                and int(checkpoint["generation"]) == generation
                and checkpoint["verified_generation"] == generation
                and checkpoint["verified_sha"] == head_sha
                and checkpoint["current_verification_id"] is not None
                and checkpoint["last_completed_operation_id"] == operation["id"]
                and checkpoint["last_completed_verification_id"]
                == checkpoint["current_verification_id"]
            ):
                verification = (
                    await conn.execute(
                        select(integration_parent_verifications.c.id).where(
                            integration_parent_verifications.c.id
                            == checkpoint["current_verification_id"],
                            integration_parent_verifications.c.operation_id == operation["id"],
                            integration_parent_verifications.c.parent_task_id == task_id,
                            integration_parent_verifications.c.episode_id
                            == checkpoint["episode_id"],
                            integration_parent_verifications.c.generation == generation,
                            integration_parent_verifications.c.head_sha == head_sha,
                        )
                    )
                ).first()
                completion = (
                    await conn.execute(
                        select(integration_parent_operation_completions.c.operation_id).where(
                            integration_parent_operation_completions.c.operation_id
                            == operation["id"],
                            integration_parent_operation_completions.c.verification_id
                            == checkpoint["current_verification_id"],
                            integration_parent_operation_completions.c.parent_task_id == task_id,
                            integration_parent_operation_completions.c.episode_id
                            == checkpoint["episode_id"],
                        )
                    )
                ).first()
                if verification is not None and completion is not None:
                    return {
                        "outcome": "already_completed",
                        "task_id": task_id,
                        "generation": generation,
                        "head_sha": head_sha,
                        "operation_id": operation["id"],
                    }
            if await self.db._read_manual_pause(conn, task_id) is not None:
                return {"outcome": "invariant_error", "task_id": task_id, "reason": "manual_pause"}
            if int(checkpoint["generation"]) != generation:
                return {"outcome": "stale_verification", "task_id": task_id}
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            if readiness["head_sha"] != head_sha:
                # The collected aggregate advanced past the subject this close
                # quoted: a genuinely superseded head, and no trusted evidence
                # is missing.  ``stale_verification`` is the right answer and
                # the caller must re-read readiness before acting at all.
                return {"outcome": "stale_verification", "task_id": task_id}
            required = required_checks(operation)
            binding = binding_diagnosis(checkpoint, generation, head_sha, required)
            if binding is not None:
                # No trusted verification binds this exact generation and head.
                # This is a wait for the CI producer, not a stale subject, and
                # no worker-side re-run can change it -- answering it as
                # ``stale_verification`` is what sent every retry back through
                # the whole local suite (calm-grove-25 generation 5).
                return {
                    "outcome": AWAITING_TRUSTED_VERIFICATION,
                    "task_id": task_id,
                    **binding,
                    **await self._awaiting_evidence_detail_on(
                        conn, operation["id"], task_id, generation, head_sha
                    ),
                }
            verification = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.id
                        == checkpoint["current_verification_id"],
                        integration_parent_verifications.c.operation_id == operation["id"],
                        integration_parent_verifications.c.generation == generation,
                        integration_parent_verifications.c.head_sha == head_sha,
                    )
                )
            ).first()
            if verification is None:
                # The checkpoint names a verification that does not resolve to
                # this operation/generation/head.  The trusted binding is
                # absent from the evidence tables, so this is the same wait as
                # above, not a superseded subject.
                return {
                    "outcome": AWAITING_TRUSTED_VERIFICATION,
                    "task_id": task_id,
                    **binding_diagnosis(
                        checkpoint, generation, head_sha, required,
                        force_reason="verification_record_missing",
                    ),
                }
            expected_owner = operation["verifier_task_id"] or task_id
            owner = (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id
                        == checkpoint["repository_id"],
                        integration_branch_owners.c.ref == checkpoint["branch"],
                    )
                )
            ).mappings().one_or_none()
            if (
                operation["state"] not in {"active", "escalated"}
                or owner is None
                or owner["owner_id"] != expected_owner
                or owner["owner_role"] != "verifier"
                or owner["handoff_state"] not in {"reserved", "attached"}
                or checkpoint["branch_owner_id"] != expected_owner
            ):
                return {"outcome": "invariant_error", "task_id": task_id}
            transition = await self.db._apply_transition(
                conn,
                task_id,
                TaskStatus.COMPLETED,
                context="integration_parent_verified",
                force=True,
                # The locked metadata check above distinguishes an operator
                # hold from this integration-owned PAUSED checkpoint.
                _manual_pause_control=True,
                _integration_completion_token=_INTEGRATION_COMPLETION_TOKEN,
                accepted_close=accepted_close,
            )
            completed_at = self.clock()
            await conn.execute(
                insert(integration_parent_operation_completions).values(
                    operation_id=operation["id"],
                    verification_id=checkpoint["current_verification_id"],
                    parent_task_id=task_id,
                    episode_id=checkpoint["episode_id"],
                    completed_at=completed_at,
                )
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    last_completed_operation_id=operation["id"],
                    last_completed_verification_id=checkpoint["current_verification_id"],
                )
            )
            await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == operation["id"],
                    integration_repair_operations.c.state.in_(("active", "escalated")),
                )
                .values(state="completed", updated_at=completed_at)
            )
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                    integration_repair_stages.c.state.in_(
                        ("active", "awaiting_completion")
                    ),
                )
                .values(state="passed", completed_at=completed_at)
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_settled(transition.settled)
        await self.db._notify_ready(transition.ready)
        return {
            "outcome": "completed",
            "task_id": task_id,
            "generation": generation,
            "head_sha": head_sha,
            "operation_id": operation["id"],
        }




class PolicyActivation:
    """Current project inputs; existing subjects retain their frozen artifact."""

    def __init__(self, db, *, clock=time.time):
        self.db = db
        self.clock = clock

    async def configure(
        self,
        project_id: str,
        *,
        updates: dict[str, Any],
        expected_generation: int,
        reason: str,
        operator_id: str,
        promotion_manifest: dict[str, Any] | None = None,
        dry_run: bool = False,
        cutover=None,
    ) -> dict[str, Any]:
        """Activate repository and policy inputs behind the project generation CAS.

        A ``promotion_flow`` update is re-validated against the fenced default
        branch (layer 3 only when ``promotion_manifest`` is given) and refused
        while a protected change meets an open promotion or live work holds a
        new target (§3.11). ``dry_run`` runs every refusal check and returns
        ``checked`` without writing, so network work such as creating the flow's
        targets happens outside the fence and the write re-checks behind the
        same generation. The private cutover hook instead holds generation and
        target locks through ref preparation and its atomic binding/data fixes;
        ordinary callers retain the strict repository barrier.
        """
        allowed = {
            "integration_repository",
            "integration_repository_id",
            "hierarchical_integration_policy",
            "integration_mode",
            "hierarchical_integration_mode",
            "promotion_flow",
        }
        if not updates or set(updates) - allowed:
            raise ValueError(
                "only repository, review mode, hierarchical policy, and promotion flow "
                "are configurable here"
            )
        if expected_generation < 0:
            raise ValueError("expected integration generation must be non-negative")
        updates = dict(updates)
        configured_fields = sorted(updates)
        has_repository_config = "integration_repository" in updates
        repository_config = updates.pop("integration_repository", None)
        if has_repository_config:
            if "integration_repository_id" in updates:
                raise ValueError(
                    "integration_repository and integration_repository_id are mutually exclusive"
                )
            if not isinstance(repository_config, dict) or set(repository_config) != {
                "id",
                "url",
                "default_branch",
            }:
                raise ValueError(
                    "integration_repository must contain exactly id, url, and default_branch"
                )
            if any(
                not isinstance(repository_config[key], str)
                or not repository_config[key]
                or repository_config[key] != repository_config[key].strip()
                for key in ("id", "url", "default_branch")
            ):
                raise ValueError("integration_repository fields must be non-empty strings")
        if "integration_mode" in updates and updates["integration_mode"] != "pull_request":
            raise ValueError("integration review mode must be pull_request")
        mode = updates.get("hierarchical_integration_mode")
        if mode is not None and mode not in {"disabled", "observe", "hierarchy", "train", "development"}:
            raise ValueError("unsupported integration mode")
        if not reason.strip() or not operator_id.strip():
            raise ValueError("policy activation requires a reason and operator identity")
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            if project is None:
                return {"outcome": "not_found", "project_id": project_id}
            generation = int(project["hierarchical_integration_generation"])
            if generation != expected_generation:
                return {"outcome": "stale", "project_id": project_id, "generation": generation}
            if cutover is not None:
                refusal = await cutover.check_on(conn, project)
                if refusal:
                    return {"project_id": project_id, "generation": generation, **refusal}
                updates["repo_default_branch"] = cutover.default_branch
            effective_mode = mode or project["hierarchical_integration_mode"]
            if "hierarchical_integration_policy" in updates:
                raw_policy = updates["hierarchical_integration_policy"]
                if effective_mode == "development":
                    from src.integration.development import DevelopmentPolicy
                    from src.integration.models import PlaybookRoute

                    settings = dict(raw_policy or {})
                    boundary = settings.pop("development", None)
                    policy = DevelopmentPolicy.model_validate(settings).checked()
                    configured_policy = policy.model_dump(mode="json")
                    if boundary is not None:
                        if not isinstance(boundary, dict) or set(boundary) != {"route"}:
                            raise ValueError("development policy must contain exactly its route")
                        route = PlaybookRoute.model_validate(boundary["route"])
                        if not route.is_available_to_project(project_id):
                            raise ValueError("integration route is scoped to another project")
                        configured_policy["development"] = {"route": route.model_dump(mode="json")}
                else:
                    policy = HierarchicalIntegrationPolicy.model_validate(raw_policy or {})
                    deprecated = deprecated_route_fields(policy)
                    if deprecated:
                        raise ValueError("integration route profile fields are deprecated: "
                                         + ", ".join(deprecated))
                    for boundary in (policy.parent, policy.root):
                        if not boundary.route.is_available_to_project(project_id):
                            raise ValueError("integration route is scoped to another project")
                    configured_policy = policy.model_dump(mode="json")
                updates["hierarchical_integration_policy"] = configured_policy
            if effective_mode in {"hierarchy", "train", "development"} and not (
                updates.get("integration_repository_id", project["integration_repository_id"])
                or has_repository_config
            ):
                return {"outcome": "blocked", "project_id": project_id,
                        "generation": generation, "error": "designated repository is required"}
            repository_row = None
            if has_repository_config:
                repository_id = repository_config["id"]
                repository_row = (
                    await conn.execute(select(repos).where(repos.c.id == repository_id))
                ).mappings().one_or_none()
                if repository_row is not None and repository_row["project_id"] != project_id:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "designated repository does not belong to the project",
                    }
                project_url = str(project["repo_url"] or "")
                if not project_url:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "project repository URL is missing",
                    }
                if repository_config["url"] != project_url:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "integration repository URL must equal the project repository URL",
                    }
                local_development = (
                    effective_mode == "development"
                    and Path(repository_config["url"]).is_absolute()
                )
                if not self._github_origin(repository_config["url"]) and not local_development:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "integration repository URL must be canonical GitHub HTTPS",
                    }
                project_branch = str(updates.get("repo_default_branch",
                                                project["repo_default_branch"]) or "")
                if repository_config["default_branch"] != project_branch:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": (
                            "integration repository default branch must equal the project "
                            "default branch"
                        ),
                    }
                updates["integration_repository_id"] = repository_id
            else:
                repository_id = updates.get(
                    "integration_repository_id", project["integration_repository_id"]
                )
            if repository_id is not None and not has_repository_config:
                repository = (
                    await conn.execute(
                        select(repos.c.id).where(
                            repos.c.id == repository_id,
                            repos.c.project_id == project_id,
                        )
                    )
                ).scalar_one_or_none()
                if repository is None:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "designated repository does not belong to the project",
                    }
            repository_changed = repository_id != project["integration_repository_id"]
            if has_repository_config and repository_row is not None:
                repository_changed = repository_changed or any(
                    repository_row[field] != repository_config[field]
                    for field in ("url", "default_branch")
                )
            if repository_changed and await self._has_active_work_on(
                conn, project_id, allow_epics=cutover is not None and cutover.allow_epics,
            ):
                return {"outcome": "busy", "project_id": project_id, "generation": generation,
                        "error": "a live subject retains the repository"}
            if "promotion_flow" in updates:
                if has_repository_config:
                    default_branch = repository_config["default_branch"]
                else:
                    default_branch = (
                        await conn.scalar(select(repos.c.default_branch).where(
                            repos.c.id == repository_id)) if repository_id is not None else None
                    ) or project["repo_default_branch"]
                refusal = await self._flow_refusal(
                    conn, project, updates, default_branch=str(default_branch or ""),
                    manifest=promotion_manifest)
                if refusal is not None:
                    return {"project_id": project_id, "generation": generation, **refusal}
            if dry_run:
                return {"outcome": "checked", "project_id": project_id,
                        "generation": generation, "updates": dict(updates)}
            if cutover is not None:
                await cutover.prepare()
                await cutover.write_on(conn, project)
            if has_repository_config:
                values = {"url": repository_config["url"],
                          "default_branch": repository_config["default_branch"]}
                if repository_row is None:
                    await conn.execute(insert(repos).values(
                        id=repository_id, project_id=project_id, checkout_base_path="",
                        source_type="clone", source_path="", **values))
                else:
                    await conn.execute(update(repos).where(
                        repos.c.id == repository_id, repos.c.project_id == project_id
                    ).values(**values))
            if mode is not None:
                updates.update(hierarchical_integration_desired_mode=mode,
                               hierarchical_integration_draining=False)
            changed = await conn.execute(update(projects).where(
                projects.c.id == project_id,
                projects.c.hierarchical_integration_generation == generation,
            ).values(**updates, hierarchical_integration_generation=generation + 1))
            if changed.rowcount != 1:
                raise RuntimeError("policy activation lost its generation fence")
            if cutover is not None:
                await cutover.verify_on(conn, generation + 1)
            effective = mode or project["hierarchical_integration_mode"]
            if effective == "train":
                await self.db.lock_integration_schedule_on(
                    conn, project_id=project_id, now=now, default_interval_seconds=300)
            await conn.execute(update(project_integration_schedules).where(
                project_integration_schedules.c.project_id == project_id
            ).values(enabled=effective == "train", updated_at=now))
        return {"outcome": "configured", "project_id": project_id,
                "generation": generation + 1, "fields": configured_fields}


    @staticmethod
    def _github_origin(url: str) -> bool:
        parsed = urlparse(str(url or ""))
        return bool(
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and parsed.path.count("/") == 2
            and parsed.path.endswith(".git")
        )


    async def _flow_refusal(self, conn, project, updates, *, default_branch, manifest):
        result = FlowSchema.validate(
            updates["promotion_flow"], default_branch=default_branch, manifest=manifest)
        problems = [p for p in result.problems if manifest is not None or p.layer < 3]
        if problems:
            return {"outcome": "invalid", "error": problems[0].code,
                    "problems": [problem.as_dict() for problem in problems]}
        flow, stored = result.flow, project["promotion_flow"]
        for change in protected_changes(stored, flow):
            batch = (await conn.execute(select(
                integration_batches.c.id, integration_batches.c.request_id
            ).where(
                integration_batches.c.project_id == project["id"],
                integration_batches.c.trigger == "promotion",
                integration_batches.c.target_ref == f"refs/heads/{change['target']}",
                integration_batches.c.intent != "aborted",
                integration_batches.c.lifecycle != "promoted",
            ).limit(1))).first()
            if batch is not None:
                return {"outcome": "in_use", "error": "promotion_flow_in_use", **change,
                        "batch_id": batch.id, "request_id": batch.request_id}
        added = sorted(set(flow_targets(flow)) - set(flow_targets(stored)))
        if added and await self._has_active_work_on(conn, project["id"], branches=added):
            return {"outcome": "busy", "error": "repository_busy", "targets": added}
        updates["promotion_flow"] = flow
        return None

    async def has_active_work(self, project_id):
        async with self.db._engine.connect() as conn:
            return await self._has_active_work_on(conn, project_id)

    @staticmethod
    async def _has_active_work_on(conn, project_id, *, branches=None, allow_epics=False):
        """Whether live work holds the repository or, given ``branches``, any of them.

        The strict project-wide path also counts open train batches (F10).
        """
        # Open as the train counts it (``train_sources._open_batch_rows``).
        open_batches = select(integration_batches.c.id).where(
            integration_batches.c.project_id == project_id,
            integration_batches.c.target_ref.is_not(None),
            integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted")
        subjects = select(integration_subjects.c.id).where(
            integration_subjects.c.project_id == project_id,
            integration_subjects.c.phase != "done")
        owners = select(integration_branch_owners.c.id).join(
            repos, repos.c.id == integration_branch_owners.c.repository_id
        ).where(repos.c.project_id == project_id,
                integration_branch_owners.c.handoff_state != "released",
                ~terminal_reservation_clause(allow_cleanup_history=True))
        if branches is not None:
            refs = [f"refs/heads/{branch}" for branch in branches]
            statements = (
                subjects.where(integration_subjects.c.target_ref.in_(refs)),
                owners.where(integration_branch_owners.c.ref.in_([*branches, *refs])),
                open_batches.where(integration_batches.c.target_ref.in_(refs)),
            )
        else:
            intents = select(integration_promotion_intents.c.id).where(
                integration_promotion_intents.c.project_id == project_id,
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")))
            if allow_epics:
                subjects = subjects.where(
                    or_(integration_subjects.c.target_ref.is_(None),
                        ~integration_subjects.c.target_ref.startswith("refs/heads/aq/epic/")))
                owners = owners.where(
                    ~integration_branch_owners.c.ref.startswith("aq/epic/"),
                    ~integration_branch_owners.c.ref.startswith("refs/heads/aq/epic/"))
                open_batches = open_batches.where(
                    ~integration_batches.c.target_ref.startswith("refs/heads/aq/epic/"))
                intents = intents.where(
                    ~integration_promotion_intents.c.target_branch.startswith("aq/epic/"),
                    ~integration_promotion_intents.c.target_branch.startswith("refs/heads/aq/epic/"))
            statements = (
                subjects,
                owners,
                intents,
                open_batches,
            )
        for statement in statements:
            if await conn.scalar(statement.limit(1)) is not None:
                return True
        return False
