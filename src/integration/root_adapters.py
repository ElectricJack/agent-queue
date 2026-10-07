"""Phase-one ports over the existing root commands, with exact identity guards.

These adapters translate outcomes, never choose a continuation. The reconciler
owns subject version/phase/schedule writes. Existing candidate, promotion and
cleanup journals remain the authority for ambiguous external writes.
"""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select

from src.commands.principal import ExecutionPrincipal, principal_context
from src.database import tables as t
from src.integration.engine import (
    EngineRefused,
    RootEngineOwnership,
    root_admission,
    root_policy_ejection,
)
from src.integration.ownership import BranchOwnership
from src.integration.subjects import (
    CIState,
    HeadIdentity,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    ReceiptKind,
    RecordAttemptArgs,
    WriterStatus,
    budget_values,
    writer_values,
)

_MUTATION_REFUSAL_LIMIT = 3
CANDIDATE_MUTATION_BLOCKER = "candidate_mutation_identity_blocked"


class RootPrimitiveAdapters:
    def __init__(self, db, commands, observer, *, ci_adapters=None, clock=time.time):
        self.db, self.commands, self.observer, self.clock = db, commands, observer, clock
        self.ci_adapters = ci_adapters
        self.ownership = RootEngineOwnership(db, clock=clock)

    def bind(self, ports: PrimitivePorts) -> PrimitivePorts:
        methods = {
            Primitive.SEAL: self.seal,
            Primitive.GIT_MERGE_MEMBERS: self.merge,
            Primitive.GIT_PUBLISH: self.publish,
            Primitive.CI_REQUEST: self.request_ci,
            Primitive.CI_OBSERVE: self.observe_ci,
            Primitive.WRITER_FILE: self.file_writer,
            Primitive.WRITER_STOP_PROOF: self.stop_writer,
            Primitive.RECORD_RECEIPT: self.receipt,
            Primitive.RECORD_ATTEMPT: self.attempt,
            Primitive.RECORD_DECISION: self.decision,
            Primitive.GATE: self.gate,
            Primitive.EJECT: self.eject,
            Primitive.CLEANUP: self.cleanup,
        }
        if self.ci_adapters is not None:
            shared = PrimitivePorts()
            self.ci_adapters.bind(shared)
            for primitive in shared.bound:

                async def shared_ci(subject, args, shared=shared):
                    await self._head(subject, args.head)
                    return await shared.invoke(subject, args)

                methods[primitive] = shared_ci
        for primitive, method in methods.items():
            ports.bind(primitive, self.guarded(method))

        async def observe(subject, args):
            return await self.observer(subject, args)

        ports.bind(Primitive.OBSERVE_SUBJECT, observe)
        return ports

    def guarded(self, method):
        async def call(subject, args):
            try:
                async with self.ownership.operation(subject.repository_id, subject=subject):
                    facts = await self.observer.observe(subject)
                    if facts.holds or (
                        facts.gate
                        and facts.gate.status == "open"
                        and args.primitive is not Primitive.GATE
                    ):
                        return PrimitiveOutcome.unknown(args.primitive, "binding_human_hold")
                    # A port called without the reconciler's committed decision
                    # is not an authorized action, even with a current subject.
                    async with self.db._engine.connect() as conn:
                        decision = (
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
                    if decision is None or (
                        decision["payload"].get("decision", {}).get("request")
                        != args.model_dump(mode="json")
                    ):
                        return PrimitiveOutcome.unknown(args.primitive, "decision_prewrite_missing")
                    if args.primitive is Primitive.EJECT:
                        with (
                            root_policy_ejection(subject, args, decision),
                            principal_context(ExecutionPrincipal.service("root-reconciler")),
                        ):
                            return await method(subject, args)
                    return await method(subject, args)
            except EngineRefused as exc:
                return PrimitiveOutcome.unknown(args.primitive, str(exc))

        return call

    async def _command(self, name: str, **args) -> dict:
        commands = self.commands() if callable(self.commands) else self.commands
        if commands is None:
            return {"success": False, "error": "command_handler_unavailable"}
        return await commands.execute(name, args)

    @staticmethod
    def _answer(args, outcome: str, **detail):
        return PrimitiveOutcome(primitive=args.primitive, outcome=outcome, detail=detail)

    @staticmethod
    def _unknown(args, result):
        return PrimitiveOutcome.unknown(
            args.primitive,
            result.get("reason")
            or result.get("error")
            or "primitive_outcome:" + str(result.get("outcome")),
        )

    async def _candidate_refusal(self, subject, args, result):
        answer = self._unknown(args, result)
        reason = answer.reason or ""
        if not any(marker in reason for marker in (
            "candidate mutation identity changed", "unresolved candidate ref mutation(s)"
        )):
            return answer
        async with self.db._engine.connect() as conn:
            previous = (await conn.execute(
                select(t.integration_subject_journal.c.payload).where(
                    t.integration_subject_journal.c.subject_id == subject.id,
                    t.integration_subject_journal.c.generation == subject.generation,
                    t.integration_subject_journal.c.entry_kind == "action",
                    t.integration_subject_journal.c.mode == "active",
                    t.integration_subject_journal.c.primitive == args.primitive.value,
                ).order_by(t.integration_subject_journal.c.seq.desc())
                .limit(_MUTATION_REFUSAL_LIMIT - 1)
            )).scalars().all()
        count = 1
        for payload in previous:
            prior = payload.get("result", {})
            if prior.get("outcome") != "unknown" or (
                prior.get("detail", {}).get("original_reason") or prior.get("reason")
            ) != reason:
                break
            count += 1
        if count < _MUTATION_REFUSAL_LIMIT:
            return answer
        return PrimitiveOutcome.unknown(
            args.primitive, CANDIDATE_MUTATION_BLOCKER,
            blocker=CANDIDATE_MUTATION_BLOCKER, original_reason=reason, refusals=count,
        )

    async def _rows(self, subject):
        if not subject.batch_id:
            raise EngineRefused("root subject has no sealed batch")
        batch = await self.db.get_integration_batch(subject.batch_id)
        if batch is None or (
            batch["project_id"],
            batch["repository_id"],
            batch["current_revision"],
        ) != (subject.project_id, subject.repository_id, subject.generation):
            raise EngineRefused("root batch identity/generation changed")
        async with self.db._engine.connect() as conn:
            revision = (
                (
                    await conn.execute(
                        select(t.integration_candidate_revisions).where(
                            t.integration_candidate_revisions.c.batch_id == subject.batch_id,
                            t.integration_candidate_revisions.c.revision == subject.generation,
                        )
                    )
                )
                .mappings()
                .first()
            )
            members = (
                (
                    await conn.execute(
                        select(t.integration_batch_members)
                        .where(
                            t.integration_batch_members.c.batch_id == subject.batch_id,
                        )
                        .order_by(t.integration_batch_members.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            operation = (
                (
                    await conn.execute(
                        select(t.integration_repair_operations).where(
                            t.integration_repair_operations.c.batch_id == subject.batch_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return batch, revision, members, operation

    async def _head(self, subject, head):
        batch, revision, _, _ = await self._rows(subject)
        expected_ref = batch["integration_branch"]
        if expected_ref and not expected_ref.startswith("refs/"):
            expected_ref = "refs/heads/" + expected_ref
        if (
            revision is None
            or head
            != HeadIdentity(
                repository_id=subject.repository_id,
                ref=expected_ref,
                sha=revision["head_sha"],
                generation=subject.generation,
                base_sha=revision["construction_base_sha"],
            )
            or subject.head_sha != head.sha
        ):
            raise EngineRefused("candidate exact head/ref/base changed")
        return batch, revision

    async def _journal(self, subject, args, kind, key, payload, *, head=None, outcome=None):
        identity = head or subject.head
        async with self.db.immediate() as conn:
            row = await self.db.lock_integration_subject_on(conn, subject.id)
            if row is None or row["version"] != subject.version or row["engine"] != "reconciler":
                raise EngineRefused("record subject changed")
            return await self.db.append_integration_subject_journal_on(
                conn,
                {
                    "subject_id": subject.id,
                    "entry_kind": kind,
                    "idempotency_key": key,
                    "mode": "active",
                    "policy_artifact_sha256": subject.policy.artifact_sha256,
                    "subject_version": subject.version,
                    "phase": subject.phase.value,
                    "head_sha": identity.sha if identity else None,
                    "generation": identity.generation if identity else subject.generation,
                    "primitive": args.primitive.value,
                    "outcome": outcome,
                    "payload": payload,
                    "recorded_at": self.clock(),
                },
            )

    async def seal(self, subject, args):
        project = await self.db.get_project(subject.project_id)
        policy = project.hierarchical_integration_policy
        if hasattr(policy, "model_dump"):
            policy = policy.model_dump(mode="json")
        boundary = (policy or {}).get("root", {})
        # Legacy sealing owns its frozen admission. Never silently ignore a
        # table predicate this phase-one command cannot represent.
        source_ci = (boundary.get("repair") or {}).get("source_ci", False)
        if (
            not args.admission.require_review
            or not args.admission.include_authorized
            or args.admission.require_source_ci != source_ci
        ):
            return PrimitiveOutcome.unknown(args.primitive, "admission_contract_mismatch")
        prefix = f"root_batch:{subject.repository_id}:"
        if not subject.subject_key.startswith(prefix):
            return PrimitiveOutcome.unknown(args.primitive, "root_request_identity_mismatch")
        request_id = subject.subject_key.removeprefix(prefix)
        with root_admission(args.admission):
            result = await self._command(
                "integration_seal",
                project_id=subject.project_id,
                request_id=request_id,
                now=self.clock(),
            )
        if result.get("outcome") not in {"sealed", "empty", "busy"}:
            return self._unknown(args, result)
        values = {}
        if result["outcome"] == "sealed":
            batch = await self.db.get_integration_batch(result["batch_id"])
            if batch is None or batch["repository_id"] != subject.repository_id:
                return PrimitiveOutcome.unknown(args.primitive, "sealed_repository_mismatch")
            values = {
                "batch_id": batch["id"],
                "generation": batch["current_revision"],
                "base_sha": batch["base_sha"],
            }
        return self._answer(args, result["outcome"], subject_values=values,
                            exclusions=result.get("exclusions", []))

    async def merge(self, subject, args):
        batch, _, members, _ = await self._rows(subject)
        if args.target_ref != subject.target_ref or not args.regenerate_generated:
            return PrimitiveOutcome.unknown(args.primitive, "construction_target_mismatch")
        expected = [(m["task_id"], m["reviewed_head_sha"], m["source_base_sha"]) for m in members]
        if [(m.task_id, m.head_sha, m.base_sha) for m in args.members] != expected:
            return PrimitiveOutcome.unknown(args.primitive, "member_manifest_moved")
        # The existing builder chooses/rechecks the observed base and owns its
        # journaled candidate push. Refuse a stale policy base before calling it.
        facts = await self.observer.observe(subject)
        if args.base_sha != facts.default_branch_head:
            return self._answer(args, "base_moved")
        result = await self._command(
            "integration_build_candidate",
            batch_id=subject.batch_id,
            expected_revision=subject.generation,
            expected_base_sha=args.base_sha,
        )
        code = result.get("outcome")
        if code in {"built", "already_built"}:
            updated = await self.db.get_integration_batch(subject.batch_id)
            async with self.db._engine.connect() as conn:
                revision = (
                    (
                        await conn.execute(
                            select(t.integration_candidate_revisions).where(
                                t.integration_candidate_revisions.c.batch_id == subject.batch_id,
                                t.integration_candidate_revisions.c.revision
                                == updated["current_revision"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            if result.get("head_sha") != revision["head_sha"]:
                return PrimitiveOutcome.unknown(args.primitive, "built_head_mismatch")
            if revision["construction_base_sha"] != args.base_sha:
                # Never report a candidate built on an older main as merged:
                # its stale base would re-fire base-moved on every visit.
                return self._answer(args, "base_moved")
            return self._answer(
                args,
                "merged",
                head=revision["head_sha"],
                subject_values={
                    "head_sha": revision["head_sha"],
                    "base_sha": revision["construction_base_sha"],
                    "generation": updated["current_revision"],
                },
            )
        if code == "conflict":
            ordinal = result.get("member_ordinal")
            member = next((m for m in members if m["ordinal"] == ordinal), None)
            if member:
                return self._answer(args, "conflict", member=member["task_id"], files=[])
        if code == "source_moved":
            ordinal = result.get("member_ordinal")
            member = next((m for m in members if m["ordinal"] == ordinal), None)
            if member:
                return self._answer(args, "source_moved", member=member["task_id"])
        if code == "base_moved":
            return self._answer(args, "base_moved")
        return await self._candidate_refusal(subject, args, result)

    async def publish(self, subject, args):
        batch, revision, _, _ = await self._rows(subject)
        if (
            not args.require_green
            or args.fence.target.repository_id != subject.repository_id
            or args.fence.target.branch != subject.target_ref
            or args.new_sha != subject.head_sha
            or revision is None
            or args.new_sha != revision["head_sha"]
            or args.expected_old_sha != revision["construction_base_sha"]
        ):
            return PrimitiveOutcome.unknown(args.primitive, "publication_identity_mismatch")
        facts = await self.observer.observe(subject)
        if facts.publisher_fence != args.fence:
            return PrimitiveOutcome.unknown(args.primitive, "publisher_authority_changed")
        if facts.ci_for(args.new_sha) is not CIState.GREEN:
            return PrimitiveOutcome.unknown(args.primitive, "exact_trusted_green_required")
        evidence = next((check for check in facts.ci if check.head_sha == args.new_sha), None)
        if evidence is not None and evidence.evidence_id is None:
            # The compatibility promotion adapter still reads its check cache.
            # Populate that exact observation on this green visit; never send
            # green through the writer-budget/attempt or stop-proof paths.
            await self._command(
                "integration_ci_evidence", batch_id=subject.batch_id, revision=subject.generation
            )
            facts = await self.observer.observe(subject)
            if facts.ci_for(args.new_sha) is not CIState.GREEN:
                return PrimitiveOutcome.unknown(args.primitive, "exact_trusted_green_required")
        # Hold actual default-ref authority through journal, expected-old push
        # and remote read-back inside the existing promotion implementation.
        async with BranchOwnership(self.db).mutation_exclusion(
            args.fence,
            expected_role="publisher",
        ):
            await self._journal(
                subject,
                args,
                "action",
                f"publish:{subject.version}:prewrite",
                args.model_dump(mode="json"),
                outcome="prewrite",
            )
            result = await self._command(
                "integration_promote_main", batch_id=batch["id"], revision=subject.generation
            )
        code = result.get("outcome")
        if code in {"promoted", "already_promoted"}:
            if result.get("head_sha") not in {None, args.new_sha}:
                return PrimitiveOutcome.unknown(args.primitive, "promoted_head_mismatch")
            return self._answer(
                args, "published", head=args.new_sha, receipt_ids=result.get("receipt_ids", ())
            )
        if code in {"base_moved", "non_fast_forward"}:
            return self._answer(args, "target_moved")
        if code == "reconciliation_blocked":
            return self._answer(args, "unknown_after_push")
        return self._unknown(args, result)

    async def request_ci(self, subject, args):
        await self._head(subject, args.head)
        result = await self._command(
            "integration_build_candidate",
            batch_id=subject.batch_id,
            expected_revision=subject.generation,
        )
        if result.get("outcome") not in {"built", "already_built"}:
            return self._answer(args, "unavailable", reason=result.get("reason"))
        if (
            result.get("head_sha") != args.head.sha
            or result.get("revision") != args.head.generation
        ):
            return PrimitiveOutcome.unknown(args.primitive, "CI_publication_head_changed")
        # Hosted CI is triggered by the existing publication. Seed its server
        # observation here too, otherwise a never-observed run remains NONE
        # and a table that requests NONE would never reach its observe action.
        await self._command(
            "integration_ci_evidence", batch_id=subject.batch_id, revision=subject.generation
        )
        return self._answer(
            args,
            "already_running" if result["outcome"] == "already_built" else "requested",
            requested_at=self.clock(),
        )

    async def observe_ci(self, subject, args):
        await self._head(subject, args.head)
        batch, _, _, _ = await self._rows(subject)
        required = batch["policy_snapshot"].get("root", {}).get("required_checks", {})
        if args.required_check_version and args.required_check_version != required.get("version"):
            return self._answer(args, "untrusted")
        result = await self._command(
            "integration_ci_evidence", batch_id=subject.batch_id, revision=subject.generation
        )
        facts = await self.observer.observe(subject)
        state = facts.ci_for(args.head.sha)
        if state is CIState.NONE and result.get("outcome") not in {"none", "pending"}:
            return self._unknown(args, result)
        values = {}
        evidence = next((e for e in facts.ci if e.head_sha == args.head.sha), None)
        if (
            state is CIState.RED
            and facts.budget is not None
            and evidence
            and evidence.evidence_id
            and facts.writer.status is WriterStatus.WORKING
        ):
            counted = await self.attempt(
                subject,
                RecordAttemptArgs(
                    head=args.head,
                    ordinal=facts.budget.ordinal,
                    evidence_id=evidence.evidence_id,
                    conclusion=state.value,
                ),
            )
            values = {**budget_values(facts.budget), **counted.detail.get("subject_values", {})}
        return self._answer(
            args,
            state.value,
            subject_values=values,
            evidence=[e.model_dump(mode="json") for e in facts.ci],
        )

    async def file_writer(self, subject, args):
        from src.integration.models import HierarchicalIntegrationPolicy, RepairPolicy

        _, _, _, operation = await self._rows(subject)
        if operation is None or args.role.value != "repair" or args.scope_members:
            return self._answer(args, "configuration_blocked")
        # Phase one preserves the legacy ladder. A table may not silently file
        # a different class/budget than the frozen operation actually grants.
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        boundary = policy.root
        repair = boundary.repair
        primary = args.ordinal == 0
        expected = (
            boundary.primary_intelligence_class if primary else repair.debug_intelligence_class,
            repair.primary_seconds if primary else repair.debug_seconds,
            repair.primary_attempts if primary else repair.debug_attempts,
        )
        if (args.intelligence_class, args.budget_seconds, args.attempt_limit) != expected:
            return self._answer(args, "configuration_blocked")

        async def stage_row():
            async with self.db._engine.connect() as conn:
                return (
                    (
                        await conn.execute(
                            select(t.integration_repair_stages).where(
                                t.integration_repair_stages.c.operation_id == operation["id"],
                                t.integration_repair_stages.c.ordinal == args.ordinal,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )

        stage = await stage_row()
        existed = stage is not None
        if stage is None and primary:
            started = await self._command(
                "integration_repair_start",
                operation_id=operation["id"],
                starting_sha=subject.head_sha or subject.base_sha,
                trigger_id=subject.batch_id,
            )
            if started.get("outcome") not in {"started", "already_started"}:
                return self._unknown(args, started)
            stage = await stage_row()
        elif stage is None:
            facts = await self.observer.observe(subject)
            if (
                operation["active_stage"] != args.ordinal - 1
                or facts.writer.status is not WriterStatus.STOPPED
                or not facts.writer.stop_proof
                or facts.budget is None
                or not facts.budget.expired(self.clock())
            ):
                return PrimitiveOutcome.unknown(args.primitive, "legacy_successor_not_ready")
            # Reuse the existing bounded, fenced handoff and incident recovery.
            await self._command(
                "integration_repair_timeout", operation_id=operation["id"], stage=args.ordinal - 1
            )
            stage = await stage_row()
        if stage is None:
            return PrimitiveOutcome.unknown(args.primitive, "legacy_successor_not_ready")
        stage_policy = RepairPolicy.model_validate(stage["policy"])
        limit = stage_policy.primary_attempts if primary else stage_policy.debug_attempts
        if (
            stage["intelligence_class"] != args.intelligence_class
            or stage["started_at"] is None
            or stage["deadline_at"] is None
            or stage["deadline_at"] - stage["started_at"] != args.budget_seconds
            or limit != args.attempt_limit
        ):
            return self._answer(args, "configuration_blocked")
        result = await self._command(
            "integration_repair_dispatch", operation_id=operation["id"], stage=args.ordinal
        )
        if not result.get("repair_task_id"):
            return self._unknown(args, result)
        facts = await self.observer.observe(subject)
        values = {**writer_values(facts.writer), **budget_values(facts.budget)}
        return self._answer(
            args,
            "exists" if existed else "filed",
            subject_values=values,
            task_id=result["repair_task_id"],
        )

    async def stop_writer(self, subject, args):
        facts = await self.observer.observe(subject)
        if facts.writer.task_id != args.task_id or facts.writer.fence_token != args.fence_token:
            return PrimitiveOutcome.unknown(args.primitive, "writer_identity_changed")
        if facts.writer.status in {WriterStatus.CLAIMED, WriterStatus.WORKING}:
            return self._answer(args, "live")
        result = await self._command(
            "integration_release_owner", task_id=args.task_id, dry_run=False
        )
        outcomes = result.get("results", result.get("outcomes", ()))
        if any(r.get("outcome") == "preserved_and_released" for r in outcomes):
            return self._answer(args, "preserved_and_released")
        refreshed = await self.observer.observe(subject)
        if refreshed.writer.status is WriterStatus.STOPPED and refreshed.writer.stop_proof:
            return self._answer(args, "released", subject_values=writer_values(refreshed.writer))
        return PrimitiveOutcome.unknown(args.primitive, "writer_stop_not_proven")

    async def receipt(self, subject, args):
        _, revision, members, _ = await self._rows(subject)
        if (
            args.kind is not ReceiptKind.CODE
            or not any(
                m["task_id"] == args.source_task_id
                and m["reviewed_head_sha"] == args.source_head_sha
                for m in members
            )
            or revision is None
            or args.target
            != HeadIdentity(
                repository_id=subject.repository_id,
                ref=subject.target_ref,
                sha=subject.head_sha,
                generation=subject.generation,
                base_sha=revision["construction_base_sha"],
            )
        ):
            return PrimitiveOutcome.unknown(args.primitive, "receipt_identity_mismatch")
        # Promotion already wrote the authoritative member receipts atomically.
        async with self.db._engine.connect() as conn:
            intent = (
                await conn.execute(
                    select(t.integration_promotion_intents.c.id).where(
                        t.integration_promotion_intents.c.root_batch_id == subject.batch_id,
                        t.integration_promotion_intents.c.root_candidate_revision
                        == subject.generation,
                        t.integration_promotion_intents.c.prepared_sha == args.target.sha,
                        t.integration_promotion_intents.c.state == "committed",
                    )
                )
            ).first()
        if intent is None:
            return PrimitiveOutcome.unknown(args.primitive, "receipt_publication_not_committed")
        key = f"receipt:{args.source_task_id}:{args.source_head_sha}:{args.target.generation}:{args.target.sha}"
        _, created = await self._journal(
            subject,
            args,
            "receipt",
            key,
            args.model_dump(mode="json"),
            head=args.target,
            outcome="recorded",
        )
        return self._answer(args, "recorded" if created else "exists")

    async def attempt(self, subject, args):
        try:
            await self._head(subject, args.head)
        except EngineRefused:
            return self._answer(args, "stale")
        facts = await self.observer.observe(subject)
        if facts.budget is None or facts.budget.ordinal != args.ordinal:
            return self._answer(args, "stale")
        evidence = next(
            (
                e
                for e in facts.ci
                if e.evidence_id == args.evidence_id and e.head_sha == args.head.sha
            ),
            None,
        )
        if (
            facts.writer.status is not WriterStatus.WORKING
            or evidence is None
            or evidence.state.value != args.conclusion
        ):
            return self._answer(args, "not_an_attempt")
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.integration_check_evidence).where(
                            t.integration_check_evidence.c.id == args.evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if (
            row is None
            or row["batch_id"] != subject.batch_id
            or row["candidate_revision"] != args.head.generation
            or row["classification"] != "conclusive"
            or row["conclusion"] != {"green": "success", "red": "failure"}[args.conclusion]
        ):
            return self._answer(args, "not_an_attempt")
        # One exact writer head is one attempt, even across reruns/evidence IDs.
        key = f"attempt:{args.ordinal}:{args.head.sha}"
        entry, created = await self._journal(
            subject,
            args,
            "attempt",
            key,
            {**args.model_dump(mode="json"), "budget_attempts": facts.budget.attempts + 1},
            head=args.head,
            outcome="counted",
        )
        count = max(facts.budget.attempts, entry["payload"]["budget_attempts"])
        return self._answer(
            args,
            "counted" if created else "not_an_attempt",
            subject_values={"budget_attempts": count},
        )

    async def decision(self, subject, args):
        # A successful record means the exact prewrite is present, including
        # the declared rule/facts/primitive and the subject's immutable pin.
        async with self.db._engine.connect() as conn:
            present = await conn.scalar(
                select(t.integration_subject_journal.c.seq).where(
                    t.integration_subject_journal.c.subject_id == subject.id,
                    t.integration_subject_journal.c.subject_version == subject.version,
                    t.integration_subject_journal.c.entry_kind == "decision",
                    t.integration_subject_journal.c.policy_artifact_sha256
                    == subject.policy.artifact_sha256,
                    t.integration_subject_journal.c.head_sha == subject.head_sha,
                    t.integration_subject_journal.c.generation == subject.generation,
                    t.integration_subject_journal.c.rule == args.rule,
                    t.integration_subject_journal.c.facts_digest == args.facts_digest,
                    t.integration_subject_journal.c.primitive == args.decided.value,
                    t.integration_subject_journal.c.mode == args.mode.value,
                )
            )
        if present is None:
            return PrimitiveOutcome.unknown(args.primitive, "exact_decision_missing")
        return self._answer(args, "recorded")

    async def gate(self, subject, args):
        if subject.schedule.gate_id:
            row = await self.db.get_gate(subject.schedule.gate_id)
            if row is None:
                return PrimitiveOutcome.unknown(args.primitive, "gate_missing")
            if row["status"] == "resolved":
                if row["resolution"] not in args.choices:
                    return PrimitiveOutcome.unknown(args.primitive, "gate_answer_invalid")
                return self._answer(args, "answered", gate_id=row["id"], choice=row["resolution"])
            return self._answer(args, "reused", gate_id=row["id"], timeout_at=row["timeout_at"])
        key = hashlib.sha256(
            json.dumps(args.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        result = await self._command(
            "gate_create",
            project_id=subject.project_id,
            gate_type="human",
            title=args.question,
            question=args.question + "\nChoices: " + ", ".join(args.choices),
            await_id=f"integration-subject:{subject.id}:{key}",
            timeout_at=None if args.no_default else self.clock() + args.default_after_seconds,
        )
        if not result.get("gate_id"):
            return self._unknown(args, result)
        row = await self.db.get_gate(result["gate_id"])
        return self._answer(
            args,
            "created" if result.get("was_created") else "reused",
            gate_id=row["id"],
            timeout_at=row["timeout_at"],
        )

    async def eject(self, subject, args):
        _, _, members, _ = await self._rows(subject)
        if not any(m["task_id"] == args.member_task_id for m in members):
            return self._answer(args, "not_a_member")
        result = await self._command(
            "integration_eject",
            batch_id=subject.batch_id,
            task_id=args.member_task_id,
            reason=args.reason,
            dry_run=False,
        )
        if result.get("outcome") == "ejected":
            batch = await self.db.get_integration_batch(subject.batch_id)
            return self._answer(
                args,
                "ejected",
                subject_values={
                    "generation": batch["current_revision"],
                    "head_sha": None,
                },
            )
        return self._unknown(args, result)

    async def cleanup(self, subject, args):
        from src.integration.models import IntegrationCleanupPolicy

        batch, _, _, _ = await self._rows(subject)
        policy = IntegrationCleanupPolicy.model_validate(
            batch["policy_snapshot"].get("cleanup", {})
        )
        if (args.delete_successful_sources, args.retain_failed_seconds, args.max_tries) != (
            policy.successful_source_refs == "delete",
            policy.failed_work_retention_seconds,
            policy.max_attempts,
        ):
            return PrimitiveOutcome.unknown(args.primitive, "cleanup_contract_mismatch")
        result = await self._command("integration_cleanup", batch_id=subject.batch_id)
        code = result.get("outcome")
        # Primitive 20 folds release in: the published batch frees its request
        # and project lease from exact main-delivery evidence, independent of
        # cleanup progress, so the next request can be sealed. Cleanup is
        # best-effort, so an unreadable cleanup answer never withholds it:
        # returning early there stranded the lease behind a delivered batch.
        released = await self._command("integration_release", batch_id=subject.batch_id)
        if released.get("outcome") not in {"released", "already_released", "wait"}:
            return self._unknown(args, released)
        if code in {"complete", "already_complete"} and released["outcome"] != "wait":
            return self._answer(args, "clean")
        detail = {"cleanup_outcome": code}
        reason = result.get("reason") or result.get("error")
        if reason:
            detail["cleanup_reason"] = reason
        return self._answer(args, "pending", **detail)
