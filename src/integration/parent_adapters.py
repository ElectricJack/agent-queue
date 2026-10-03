"""Parent ports over existing collection proof, verification and shared writers."""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from src.database import tables as t
from src.integration.engine import EngineRefused
from src.integration.gates import GatePrimitives
from src.integration.models import Fence
from src.integration.parent_engine import ParentEngineOwnership, active_parent_scope
from src.integration.parent_subjects import ParentSubjectFacts
from src.integration.records import RecordsPrimitives
from src.integration.subjects import (
    MemberRef,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    PRIMITIVE_ARGS,
    Subject,
    WriterRole,
    SHA_PATTERN,
)
from src.integration.writers import WriterPrimitives


class PendingParentPublication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str
    expected_old_sha: str = Field(pattern=SHA_PATTERN)
    new_sha: str = Field(pattern=SHA_PATTERN)
    fence: Fence


class ParentPolicyFacts(ParentSubjectFacts):
    """Typed additions to the parent observer; all participate in its digest."""

    identity_moved: bool = False
    failed_child_count: int = 0
    collection_members: tuple[MemberRef, ...] = ()
    collector_fence: Fence | None = None
    pending_publication: PendingParentPublication | None = None
    legacy_repair_dossier: dict | None = None
    verifier_task_id: str | None = None
    verifier_failed: bool = False
    aggregate_verified: bool = False
    parent_completed: bool = False
    ci_evidence_ids: tuple[str, ...] = ()
    writer_file_ordinal: int = Field(default=1, ge=1)


class ParentPrimitiveAdapters:
    def __init__(self, db, commands, observer, *, parent_ci=None, clock=time.time):
        self.db, self.commands, self.observer = db, commands, observer
        self.parent_ci, self.clock = parent_ci, clock
        self.ownership = ParentEngineOwnership(db, clock=clock)

    def bind(self, ports):
        for primitive in (
            Primitive.GIT_MERGE_MEMBERS,
            Primitive.GIT_PUBLISH,
            Primitive.CI_REQUEST,
            Primitive.CI_OBSERVE,
            Primitive.WRITER_FILE,
            Primitive.WRITER_LEASE,
            Primitive.WRITER_STOP_PROOF,
            Primitive.RECORD_RECEIPT,
            Primitive.RECORD_ATTEMPT,
            Primitive.GATE,
            Primitive.EJECT,
            Primitive.CLEANUP,
        ):
            ports.bind(primitive, self.guarded)
        return ports

    async def command(self, name, **args):
        commands = self.commands() if callable(self.commands) else self.commands
        if commands is None:
            return {"success": False, "reason": "command_handler_unavailable"}
        return await commands.execute(name, args)

    async def guarded(self, subject, args):
        try:
            async with self.ownership.operation(subject.task_id, subject=subject):
                facts = await self.observer.observe(subject)
                if facts.holds or facts.identity_moved or not facts.collection_current:
                    return PrimitiveOutcome.unknown(args.primitive, "parent_stale_or_held")
                if (
                    facts.gate
                    and facts.gate.status == "open"
                    and args.primitive is not Primitive.GATE
                ):
                    return PrimitiveOutcome.unknown(args.primitive, "binding_human_gate")
                async with self.db._engine.connect() as conn:
                    entry = (
                        (
                            await conn.execute(
                                select(t.integration_subject_journal)
                                .where(
                                    t.integration_subject_journal.c.subject_id == subject.id,
                                    t.integration_subject_journal.c.subject_version
                                    == subject.version,
                                    t.integration_subject_journal.c.entry_kind == "decision",
                                    t.integration_subject_journal.c.mode == "active",
                                )
                                .order_by(t.integration_subject_journal.c.seq.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                if entry is None or entry["payload"].get("decision", {}).get(
                    "request"
                ) != args.model_dump(mode="json"):
                    return PrimitiveOutcome.unknown(args.primitive, "decision_prewrite_missing")
                result = await self.command(
                    "integration_parent_action",
                    subject_id=subject.id,
                    expected_version=subject.version,
                    request=args.model_dump(mode="json"),
                )
                if "value" not in result:
                    return PrimitiveOutcome.unknown(
                        args.primitive,
                        result.get("reason") or result.get("error") or "parent_action_unavailable",
                    )
                return PrimitiveOutcome.model_validate(result["value"])
        except EngineRefused as exc:
            return PrimitiveOutcome.unknown(args.primitive, str(exc))

    async def perform(self, commands, subject, request):
        """Called only inside CommandHandler and this visit's process-bound exclusion."""
        args = PRIMITIVE_ARGS[Primitive(request["primitive"])].model_validate(request)
        if not active_parent_scope(self.db, subject.task_id):
            return PrimitiveOutcome.unknown(args.primitive, "parent_action_scope_missing")
        facts = await self.observer.observe(subject)
        if facts.holds or facts.identity_moved:
            return PrimitiveOutcome.unknown(args.primitive, "parent_stale_or_held")
        writers = WriterPrimitives(
            self.db,
            owner_recovery=getattr(commands.orchestrator, "parent_owner_recovery", None),
            clock=self.clock,
        )
        gates = GatePrimitives(self.db, clock=self.clock)
        shared = PrimitivePorts()
        writers.bind(shared)
        gates.bind(shared)
        RecordsPrimitives(self.db, clock=self.clock).bind(shared)
        p = args.primitive
        if p in {Primitive.GIT_MERGE_MEMBERS, Primitive.GIT_PUBLISH}:
            repo = await self.db.get_repo(subject.repository_id)
            if repo is None or subject.target_ref.removeprefix("refs/heads/") == (
                repo.default_branch.removeprefix("refs/heads/")
            ):
                return PrimitiveOutcome.unknown(p, "parent_target_is_default_branch")
        if p is Primitive.GIT_PUBLISH:
            pending = facts.pending_publication
            if (
                pending is None
                or args.require_green
                or (args.fence, args.expected_old_sha, args.new_sha)
                != (pending.fence, pending.expected_old_sha, pending.new_sha)
            ):
                return PrimitiveOutcome.unknown(p, "parent_intent_identity_changed")
            from src.integration.promotion import PromotionError, PromotionNotApplied

            promotion = commands._integration_promotion_service()
            try:
                try:
                    result = await promotion.reconcile(pending.intent_id)
                except PromotionNotApplied:
                    result = await promotion.push(pending.intent_id, args.fence)
            except PromotionError as exc:
                return PrimitiveOutcome.unknown(p, str(exc))
            return PrimitiveOutcome(
                primitive=p,
                outcome="published",
                detail={"head": result.prepared_sha, "intent_id": result.intent_id},
            )
        if p is Primitive.WRITER_FILE:
            if args.role is not WriterRole.VERIFIER:
                return PrimitiveOutcome.unknown(p, "parent_repair_requires_reviewed_adapter")
            if facts.failed_aggregate_unchanged or facts.readiness != "ready":
                return PrimitiveOutcome.unknown(p, "aggregate_not_ready_for_new_verifier")
            result = await writers.file(subject, args)
            if result.outcome in {"filed", "exists"}:
                # Replay completes this link if filing committed before a crash.
                async with self.db.immediate() as conn:
                    row = await self.db.lock_integration_subject_on(conn, subject.id)
                    current = Subject.from_row(row)
                    if (
                        current.engine.value != "reconciler"
                        or current.writer.task_id != result.detail["task_id"]
                    ):
                        raise EngineRefused("filed parent writer changed before link")
                    operation = (
                        (
                            await conn.execute(
                                select(t.integration_repair_operations)
                                .where(
                                    t.integration_repair_operations.c.id
                                    == facts.parent_operation_id,
                                    t.integration_repair_operations.c.episode_id
                                    == subject.parent_episode_id,
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if operation["verifier_task_id"] not in {None, current.writer.task_id}:
                        raise EngineRefused("parent verifier link changed")
                    await conn.execute(
                        update(t.integration_repair_operations)
                        .where(
                            t.integration_repair_operations.c.id == operation["id"],
                        )
                        .values(verifier_task_id=current.writer.task_id, updated_at=self.clock())
                    )
                    completion = commands._hierarchy_integration_service().parent_completion
                    await completion.mark_ready_on(conn, subject.task_id)
            return result
        if p is Primitive.WRITER_LEASE:
            result = await writers.lease(subject, args)
            if result.outcome == "leased":
                await commands._hierarchy_integration_service().wake_verifier(
                    subject.task_id,
                    Fence.model_validate(result.detail["fence"]),
                )
            return result
        if p in shared.bound:
            return await shared.invoke(subject, args)
        if p is Primitive.GIT_MERGE_MEMBERS:
            if args.target_ref != subject.target_ref or args.base_sha != subject.head_sha:
                return PrimitiveOutcome(primitive=p, outcome="base_moved")
            if facts.verifier_failed and facts.collection_state in {"verifying", "integration_ready"}:
                result = await self.command(
                    "integration_reopen_collection",
                    task_id=subject.task_id,
                    dry_run=False,
                    expected_head_sha=subject.head_sha,
                    reason="pinned parent policy collects fixes after failed aggregate",
                )
                return self.translate(args, result, {"reopened": "merged"}, head=subject.head_sha)
            if tuple(args.members) != facts.collection_members:
                return PrimitiveOutcome.unknown(p, "collection_members_changed")
            if not args.regenerate_generated or not facts.collector_fence:
                return PrimitiveOutcome.unknown(p, "collector_authority_unavailable")
            member = args.members[0]
            if member.head_sha == member.base_sha:
                result = await self.command(
                    "integration_record_noop",
                    child_task_id=member.task_id,
                    expected_head_sha=member.head_sha,
                )
                return self.translate(args, result, {"recorded": "merged"}, head=subject.head_sha)
            from src.integration.child_delivery import ChildDelivery

            delivery = ChildDelivery(self.db, commands._integration_promotion_service())
            await delivery.ensure_evidence(member.task_id, self.clock())
            result = await self.command(
                "delivery_promote",
                operation_key=facts.parent_operation_id,
                source_task_id=member.task_id,
                source_head=member.head_sha,
                source_base=member.base_sha,
                expected_target=subject.head_sha,
                fence=facts.collector_fence.model_dump(mode="json"),
            )
            return self.translate(
                args,
                result,
                {
                    "promoted": "merged",
                    "already_promoted": "merged",
                    "conflict": "conflict",
                    "source_moved": "source_moved",
                    "target_moved": "base_moved",
                },
                head=result.get("after_sha") or subject.head_sha,
                member=member.task_id,
                files=result.get("files", []),
            )
        if p in {Primitive.CI_REQUEST, Primitive.CI_OBSERVE}:
            if args.head != subject.head or self.parent_ci is None:
                return PrimitiveOutcome.unknown(p, "parent_ci_identity_or_adapter_unavailable")
            async with self.db._engine.connect() as conn:
                op = (
                    (
                        await conn.execute(
                            select(t.integration_repair_operations).where(
                                t.integration_repair_operations.c.id == facts.parent_operation_id,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            observed = await self.parent_ci.handle(
                {
                    "task_id": subject.task_id,
                    "project_id": subject.project_id,
                    "repository_id": subject.repository_id,
                    "generation": subject.generation,
                    "head_sha": subject.head_sha,
                    "operation_id": op["id"],
                    "policy_snapshot": op["policy_snapshot"],
                }
            )
            if not observed:
                return PrimitiveOutcome(
                    primitive=p, outcome="unavailable" if p is Primitive.CI_REQUEST else "none"
                )
            if p is Primitive.CI_REQUEST:
                return PrimitiveOutcome(primitive=p, outcome="requested")
            if observed["outcome"] == "green":
                result = await self.command(
                    "integration_parent_verify",
                    task_id=subject.task_id,
                    generation=subject.generation,
                    head_sha=subject.head_sha,
                    evidence_ids=observed["evidence_ids"],
                )
                if result.get("outcome") != "verified":
                    return PrimitiveOutcome.unknown(p, "exact_parent_verification_refused")
            return self.translate(
                args,
                observed,
                {x: x for x in ("green", "red", "pending", "infra", "none", "untrusted")},
            )
        if p is Primitive.CLEANUP:
            result = await self.command(
                "integration_complete_parent",
                task_id=subject.task_id,
                generation=subject.generation,
                head_sha=subject.head_sha,
            )
            return self.translate(
                args, result, {"completed": "clean", "already_completed": "clean"}
            )
        return PrimitiveOutcome.unknown(p, "parent_primitive_unavailable")

    @staticmethod
    def translate(args, result, codes, **detail):
        code = codes.get(result.get("outcome"))
        if code is None:
            return PrimitiveOutcome.unknown(
                args.primitive,
                result.get("reason") or result.get("error") or str(result.get("outcome")),
            )
        return PrimitiveOutcome(primitive=args.primitive, outcome=code, detail=detail)
