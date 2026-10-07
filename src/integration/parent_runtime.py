"""Active parent subject visits share the integration remote pass."""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import select

from src.database import tables as t
from src.integration.models import BranchKey, Fence
from src.integration.observe import GitObservationReader
from src.integration.parent_adapters import (
    REOPEN_REFUSED_META_KEY,
    ParentPolicyFacts,
    ParentPrimitiveAdapters,
    PendingParentPublication,
)
from src.integration.parent_subjects import (
    ParentDatabaseObservationReader,
    ParentIntegrationObserver,
    ensure_parent_subject_on as ensure_parent_subject_on,
    _SnapshotReader,
)
from src.integration.reconciler import IntegrationReconciler, VisitTransition
from src.integration.root_runtime import PinnedRootPolicy
from src.integration.shadow import diagnostics_for
from src.integration.runtime_contracts import (
    JournalMode,
    MemberRef,
    Primitive,
    PrimitivePorts,
    SubjectKind,
    SubjectSchedule,
    WriterLease,
    WriterStatus,
    writer_values,
)
from src.integration.verifier_subject import latest_red_parent_evidence

logger = logging.getLogger(__name__)


class ParentVisitObserver(ParentIntegrationObserver):
    async def observe_subject(self, subject_id, *, include_remote=True):
        snapshot = await self.reader.read(subject_id)
        if snapshot is None:
            return None
        facts = await ParentIntegrationObserver(
            _SnapshotReader(snapshot),
            self.git,
            clock=self.clock,
            session_probe=self.session_probe,
            facts_type=ParentPolicyFacts,
        ).observe_subject(subject_id, include_remote=include_remote)
        subject = snapshot.subject
        operation = next(
            (
                r
                for r in snapshot.all("integration_repair_operations")
                if r["episode_id"] == subject.parent_episode_id
            ),
            {},
        )
        stages = [
            r
            for r in snapshot.all("integration_repair_stages")
            if r["operation_id"] == operation.get("id") and r["state"] != "passed"
        ]
        owner = next(
            (
                r
                for r in snapshot.all("integration_branch_owners")
                if r["owner_id"] == operation.get("id")
                and r["owner_role"] == "collector"
                and r["handoff_state"] == "reserved"
                and r["session_id"] is None
                and r["workspace_id"] is None
            ),
            None,
        )
        children = {c.task_id: c for c in facts.children}
        members = tuple(
            MemberRef(task_id=m.task_id, head_sha=m.head_sha, base_sha=m.base_sha)
            for m in facts.members
            if m.head_sha
            and m.base_sha
            and not m.held
            and not m.ejected
            and m.review != "rejected"
            and children[m.task_id].pending_collection
        )
        writer = facts.writer
        if (
            subject.writer.status is WriterStatus.FILED
            and subject.writer.fence_token is None
            and any(
                r["id"] == subject.writer.task_id and r["status"] == "BLOCKED"
                for r in snapshot.all("tasks")
            )
            and not any(
                r.get("task_id") == subject.writer.task_id for r in snapshot.all("sessions")
            )
        ):
            writer = subject.writer
        fence = (
            Fence(
                target=BranchKey(
                    repository_id=subject.repository_id,
                    branch=subject.target_ref.removeprefix("refs/heads/"),
                ),
                owner_id=owner["owner_id"],
                token=owner["fence_token"],
            )
            if owner
            else None
        )
        pending = next(
            (
                r
                for r in snapshot.all("integration_promotion_intents")
                if r["operation_key"] == operation.get("id")
                and r["state"] in {"prepared", "pushed"}
                and r["prepared_sha"]
                and r["expected_target"] == subject.head_sha
                and any(
                    m.task_id == r["source_task_id"] and m.head_sha == r["source_head"]
                    for m in members
                )
            ),
            None,
        )
        checkpoint = next(
            (r for r in snapshot.all("task_integration_checkpoints")
             if r["task_id"] == subject.task_id),
            None,
        )
        evidence = [
            r for r in snapshot.all("integration_check_evidence")
             if r["operation_id"] == operation.get("id")
             and r["parent_generation"] == subject.generation
             and r["parent_head_sha"] == subject.head_sha
        ]
        red_verifier = bool(
            operation.get("verifier_task_id") and checkpoint and evidence
            and latest_red_parent_evidence(evidence, operation=operation, checkpoint=checkpoint)
        )
        return facts.model_copy(
            update={
                "identity_moved": bool(
                    set(facts.unknown) & {"parent_checkpoint_moved", "parent_collection_head_moved"}
                ),
                "collection_members": members,
                "pending_publication": PendingParentPublication(
                    intent_id=pending["id"],
                    expected_old_sha=pending["expected_target"],
                    new_sha=pending["prepared_sha"],
                    fence=fence,
                )
                if pending and fence
                else None,
                "failed_child_count": facts.binding()["failed_child_count"],
                "collector_fence": fence,
                "legacy_repair_dossier": {"operation_id": operation.get("id"), "stages": stages}
                if stages
                else None,
                "verifier_task_id": operation.get("verifier_task_id"),
                "verifier_failed": bool(
                    facts.verification and facts.verification.status == "failed"
                ) or red_verifier,
                "reopen_refused": _reopen_refused(snapshot, subject, operation),
                "parent_completed": operation.get("state") == "completed",
                "aggregate_verified": bool(
                    facts.verification
                    and facts.verification.verification_id
                    and facts.verification.status != "failed"
                ),
                "writer": writer,
                "writer_file_ordinal": (
                    subject.budget.ordinal
                    if subject.budget
                    and subject.writer.task_id
                    and operation.get("verifier_task_id") is None
                    else subject.budget.ordinal + 1
                    if subject.budget
                    else 1
                ),
                "ci_evidence_ids": tuple(
                    r.evidence_id
                    for r in facts.ci
                    if r.head_sha == subject.head_sha and r.evidence_id
                ),
            }
        )


def _reopen_refused(snapshot, subject, operation) -> bool:
    """Whether this exact head's collection reopen was durably refused.

    Only a marker naming this episode, operation, head and generation counts; a
    marker from an earlier generation or a malformed one never holds a parent.
    """
    marker = next(
        (
            row["value"]
            for row in snapshot.all("task_metadata")
            if row["task_id"] == subject.task_id and row["key"] == REOPEN_REFUSED_META_KEY
        ),
        None,
    )
    if not marker:
        return False
    try:
        value = json.loads(marker)
    except (TypeError, ValueError):
        return False
    return (
        value.get("episode_id") == subject.parent_episode_id
        and value.get("operation_id") == operation.get("id")
        and value.get("head_sha") == subject.head_sha
        and value.get("generation") == subject.generation
    )


class PinnedParentPolicy(PinnedRootPolicy):
    async def settle(self, subject, decision, outcome, *, now):
        policy = await self.policy_for(subject.policy)
        transition = await policy.settle(subject, decision, outcome, now=now)
        facts = self.observations.pop((subject.id, subject.version), None)
        values = dict(transition.values)
        if facts and facts.holds and subject.schedule.gate_id:
            return VisitTransition(
                schedule=SubjectSchedule.hold(
                    now=now,
                    gate_id=subject.schedule.gate_id,
                    max_wait_seconds=subject.schedule.max_wait_seconds,
                    revisit_at=now + decision.request.seconds,
                )
            )
        if outcome.primitive is Primitive.WAIT and facts and not facts.holds:
            if facts.collection_current and facts.collection_generation >= subject.generation:
                values.update(
                    head_sha=facts.collection_head_sha, generation=facts.collection_generation
                )
            # Recovery's proof retired the previous verifier and restored the
            # collector. Its historical completion/receipt rows stay intact.
            if (
                facts.collection_reopened
                and facts.verifier_task_id is None
                and facts.collector_fence
            ):
                # Keep the ordinal ledger: writer-file journals survive
                # recovery, so resetting it would replay the failed writer.
                values.update(writer_values(WriterLease()))
            elif facts.writer.task_id == subject.writer.task_id:
                values.update(writer_values(facts.writer))
            # An unleased task missed capacity, not a repair attempt. The
            # table's finite wait controls the next capacity window.
            if (
                facts.writer.status is WriterStatus.FILED
                and facts.budget
                and facts.budget.expired(now)
                and subject.writer.fence_token is None
            ):
                values["budget_deadline_at"] = now + decision.request.seconds
        return VisitTransition(schedule=transition.schedule, values=values)


class ParentSubjectRuntime:
    def __init__(
        self,
        db,
        observer,
        policy,
        adapters,
        loader,
        *,
        shadow=False,
        active=False,
        clock=time.time,
        page_size=20,
        diagnostics=None,
    ):
        self.db, self.loader, self.clock = db, loader, clock
        self.adapters, self.cursor, self.page_size = adapters, "", page_size
        ports = adapters.bind(PrimitivePorts())
        self.loops = [
            IntegrationReconciler(
                db,
                observer.observe,
                policy,
                ports,
                mode=mode,
                kinds=(SubjectKind.PARENT_EPISODE,),
                clock=clock,
                diagnostics=diagnostics,
            )
            for enabled, mode in ((active, JournalMode.ACTIVE),)
            if enabled
        ]

    def subscribe(self, bus):
        for loop in self.loops:
            loop.subscribe(bus)

    async def stop(self):
        for loop in self.loops:
            await loop.stop()

    async def tick(self, now):
        async with self.db.immediate() as conn:
            rows = (
                (
                    await conn.execute(
                        select(t.tasks.c.id)
                        .join(
                            t.task_integration_checkpoints,
                            t.task_integration_checkpoints.c.task_id == t.tasks.c.id,
                        )
                        .where(
                            t.tasks.c.id > self.cursor,
                            t.task_integration_checkpoints.c.episode_id.is_not(None),
                        )
                        .order_by(t.tasks.c.id)
                        .limit(self.page_size)
                    )
                )
                .scalars()
                .all()
            )
            for task_id in rows:
                try:
                    async with conn.begin_nested():
                        await ensure_parent_subject_on(
                            self.db, conn, task_id, self.loader, clock=self.clock
                        )
                except Exception:
                    logger.exception("Parent subject seed failed for %s; page continues", task_id)
            self.cursor = rows[-1] if len(rows) == self.page_size else ""
            gate_ids = (
                (
                    await conn.execute(
                        select(t.gates.c.id)
                        .join(
                            t.integration_subjects, t.integration_subjects.c.gate_id == t.gates.c.id
                        )
                        .where(
                            t.integration_subjects.c.kind == "parent_episode",
                            t.integration_subjects.c.phase != "done",
                            t.integration_subjects.c.next_due_at.is_(None),
                            t.gates.c.status == "resolved",
                        )
                    )
                )
                .scalars()
                .all()
            )
        if gate_ids:
            await self.db.wake_integration_subjects(now=now, gate_ids=gate_ids)
        for loop in self.loops:
            await loop.tick(now, background=True)


def parent_runtime_for(orchestrator, parent_ci):
    config = orchestrator.config.integration
    if not config.reconciler_active:
        return None
    observer = ParentVisitObserver(
        ParentDatabaseObservationReader(orchestrator.db),
        GitObservationReader(orchestrator.git),
        session_probe=orchestrator._root_subject_session_probe,
        facts_type=ParentPolicyFacts,
    )
    adapters = ParentPrimitiveAdapters(
        orchestrator.db, lambda: orchestrator._command_handler, observer, parent_ci=parent_ci
    )
    runtime = ParentSubjectRuntime(
        orchestrator.db,
        observer,
        PinnedParentPolicy(orchestrator._load_playbook_artifact),
        adapters,
        orchestrator._load_playbook_artifact,
        shadow=False,
        active=config.reconciler_active,
        diagnostics=diagnostics_for(config, orchestrator.db, orchestrator.git,
                                    reader=observer.reader),
    )
    runtime.subscribe(orchestrator.bus)
    return runtime
