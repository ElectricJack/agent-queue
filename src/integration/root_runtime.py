"""Default-off root wiring at the existing service's single remote-pass boundary."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from uuid import uuid5, NAMESPACE_URL

from sqlalchemy import or_, select

from src.database import tables as t
from src.integration.engine import RootEngineOwnership
from src.integration.observe import (
    DatabaseObservationReader,
    GitObservationReader,
    IntegrationObserver,
)
from src.integration.reconciler import CompiledPolicyAdapter, IntegrationReconciler, VisitTransition
from src.integration.root_adapters import RootPrimitiveAdapters
from src.integration.subjects import (
    JournalMode,
    PolicyArtifactPin,
    Primitive,
    PrimitivePorts,
    Subject,
    SubjectEngine,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    budget_values,
    subject_key,
    writer_values,
)
from src.playbooks.integration_policy import CompiledIntegrationPolicy, IntegrationPolicyFacts

logger = logging.getLogger(__name__)


class PinnedRootPolicy(CompiledPolicyAdapter):
    """Resolve the immutable subject pin, never the current project activation.

    A WAIT visit can refresh mirrored domain identity from the observation. A
    repaired candidate is committed by existing services, so the next visit
    must adopt that exact head before CI/publication. This is bookkeeping;
    every action and schedule still comes from the pinned decision table.
    """

    def __init__(self, loader):
        self.loader = loader
        self.policies = {}
        self.observations = {}

    async def policy_for(self, pin):
        key = pin.artifact_sha256
        if key not in self.policies:
            definition = await asyncio.to_thread(self.loader, key)
            policy = CompiledIntegrationPolicy(definition)
            if policy.pin != pin:
                raise ValueError("loaded root policy does not match the exact pin")
            self.policies[key] = CompiledPolicyAdapter(policy)
        return self.policies[key]

    async def decide(self, subject, facts):
        policy = await self.policy_for(subject.policy)
        # Shadow visits do not settle; keep at most the current bounded page.
        if len(self.observations) >= 100:
            self.observations.clear()
        self.observations[(subject.id, subject.version)] = facts
        return await policy.decide(subject, facts)

    async def settle(self, subject, decision, outcome, *, now):
        policy = await self.policy_for(subject.policy)
        transition = await policy.settle(subject, decision, outcome, now=now)
        facts = self.observations.pop((subject.id, subject.version), None)
        values = dict(transition.values)
        if outcome.primitive is Primitive.WAIT and facts and not facts.holds:
            values.update(writer_values(facts.writer), **budget_values(facts.budget))
            if facts.candidate and facts.candidate.generation >= subject.generation:
                values.update(
                    head_sha=facts.candidate.sha,
                    base_sha=facts.candidate.base_sha,
                    generation=facts.candidate.generation,
                )
        return VisitTransition(schedule=transition.schedule, values=values)


class RootObserver(IntegrationObserver):
    def __init__(self, db, git, **kwargs):
        self.db = db
        super().__init__(_RootObservationReader(db), git, **kwargs)

    async def observe(self, subject):
        # Legacy repair stages own the clock/attempt rows during phase one.
        # Re-read those rather than letting a mirrored budget hide new results.
        facts = await super().observe(subject)
        if (
            subject.engine is SubjectEngine.RECONCILER
            and facts.candidate
            and (facts.candidate.generation, facts.candidate.sha, facts.candidate.base_sha)
            != (subject.generation, subject.head_sha, subject.base_sha)
        ):
            facts = facts.model_copy(
                update={"unknown": (*facts.unknown, "subject_identity_unmirrored")}
            )
        if facts.budget:
            # Count survives an interrupted visit between the append-only
            # attempt and the loop's projection/schedule CAS.
            async with self.db._engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(t.integration_subject_journal.c.payload).where(
                                t.integration_subject_journal.c.subject_id == subject.id,
                                t.integration_subject_journal.c.entry_kind == "attempt",
                                t.integration_subject_journal.c.payload["ordinal"].as_integer()
                                == facts.budget.ordinal,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            attempts = max([facts.budget.attempts, *(r.get("budget_attempts", 0) for r in rows)])
            facts = facts.model_copy(
                update={"budget": facts.budget.model_copy(update={"attempts": attempts})}
            )
        return facts


def _legacy_phase(lifecycle):
    return {
        "testing": "testing",
        "repairing": "repairing",
        "human_blocked": "repairing",
        "promoting": "publishing",
        "cleanup_pending": "published",
        "promoted": "published",
        "delivered": "published",
        "completed": "published",
    }.get(lifecycle, "building")


class _RootObservationReader:
    def __init__(self, db):
        self.reader = DatabaseObservationReader(db)

    async def read(self, subject_id):
        snapshot = await self.reader.read(subject_id)
        if snapshot is None:
            return None
        subject = snapshot.subject
        changes = {"budget": None}
        batch = next(iter(snapshot.all("integration_batches")), None)
        if subject.kind is SubjectKind.ROOT_BATCH and batch:
            revision = next(
                (
                    r
                    for r in snapshot.all("integration_candidate_revisions")
                    if r["revision"] == batch["current_revision"]
                ),
                None,
            )
            changes.update(
                generation=batch["current_revision"],
                head_sha=revision["head_sha"] if revision else None,
                base_sha=revision["construction_base_sha"] if revision else batch["base_sha"],
            )
            if subject.engine is SubjectEngine.LEGACY:
                changes["phase"] = SubjectPhase(_legacy_phase(batch["lifecycle"]))
        return replace(snapshot, subject=subject.model_copy(update=changes))


class _ShadowDB:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    async def due_integration_subject_page(self, **kwargs):
        return await self.db.due_integration_subject_page(**{**kwargs, "engine": "legacy"})

    async def get_integration_subject(self, subject_id):
        row = await self.db.get_integration_subject(subject_id)
        if row is None or row["engine"] != "legacy" or not row["batch_id"]:
            return row
        # Observe legacy's current substrate without changing the subject's
        # domain columns. The journal records this exact virtual identity.
        async with self.db._engine.connect() as conn:
            batch = (
                (
                    await conn.execute(
                        select(t.integration_batches).where(
                            t.integration_batches.c.id == row["batch_id"]
                        )
                    )
                )
                .mappings()
                .first()
            )
            if batch is None:
                return row
            revision = (
                (
                    await conn.execute(
                        select(t.integration_candidate_revisions).where(
                            t.integration_candidate_revisions.c.batch_id == batch["id"],
                            t.integration_candidate_revisions.c.revision
                            == batch["current_revision"],
                        )
                    )
                )
                .mappings()
                .first()
            )
        phase = _legacy_phase(batch["lifecycle"])
        return {
            **row,
            "phase": phase,
            "generation": batch["current_revision"],
            "head_sha": revision["head_sha"] if revision else None,
            "base_sha": revision["construction_base_sha"] if revision else batch["base_sha"],
        }


class RootSubjectRuntime:
    """A service-owned pair of loops; no second background remote pass."""

    def __init__(self, db, observer, policy, ports, *, shadow=False, active=False, clock=time.time):
        self.db, self.policy, self.clock = db, policy, clock
        self.loops = []
        if active:
            self.loops.append(
                IntegrationReconciler(
                    db, observer.observe, policy, ports, mode=JournalMode.ACTIVE, clock=clock
                )
            )
        if shadow:
            self.loops.append(
                IntegrationReconciler(
                    _ShadowDB(db),
                    observer.observe,
                    policy,
                    ports,
                    mode=JournalMode.SHADOW,
                    clock=clock,
                )
            )
        self.cursor = None

    def subscribe(self, bus):
        for loop in self.loops:
            loop.subscribe(bus)

    async def stop(self):
        for loop in self.loops:
            await loop.stop()

    async def tick(self, now):
        await self.seed(now)
        # Gate resolution is durable; a lost bus wake must only cost latency.
        async with self.db._engine.connect() as conn:
            gates = (
                (
                    await conn.execute(
                        select(t.gates.c.id)
                        .join(
                            t.integration_subjects, t.integration_subjects.c.gate_id == t.gates.c.id
                        )
                        .where(
                            t.integration_subjects.c.kind == "root_batch",
                            t.integration_subjects.c.engine == "reconciler",
                            t.integration_subjects.c.phase != "done",
                            t.integration_subjects.c.next_due_at.is_(None),
                            t.gates.c.status == "resolved",
                        )
                        .order_by(t.gates.c.id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        if gates:
            await self.db.wake_integration_subjects(now=now, gate_ids=gates)
        for loop in self.loops:
            await loop.tick(now)

    async def seed(self, now):
        """Bounded creation over current schedule/batch facts; no live cutover.

        Old bundles without a table remain legacy. Importing a new artifact is
        an operator prerequisite; this path never imports or activates one.
        """
        async with self.db._engine.connect() as conn:
            statement = (
                select(t.projects, t.project_integration_schedules.c.outstanding_request_id)
                .join(
                    t.project_integration_schedules,
                    t.projects.c.id == t.project_integration_schedules.c.project_id,
                )
                .where(
                    t.project_integration_schedules.c.enabled.is_(True),
                    t.projects.c.hierarchical_integration_mode.in_(["train", "hierarchy"]),
                )
                .order_by(t.projects.c.id)
                .limit(100)
            )
            if self.cursor:
                statement = statement.where(t.projects.c.id > self.cursor)
            projects = (await conn.execute(statement)).mappings().all()
        self.cursor = projects[-1]["id"] if len(projects) == 100 else None
        for project in projects:
            try:
                await self._seed_project(project, now)
            except (ValueError, FileNotFoundError, KeyError):
                logger.debug("root subject policy unavailable for %s", project["id"])

    async def _seed_project(self, project, now):
        repository_id = project["integration_repository_id"]
        if not repository_id:
            return
        async with RootEngineOwnership(self.db).exclusion(repository_id) as conn:
            batch = (
                (
                    await conn.execute(
                        select(t.integration_batches)
                        .where(
                            t.integration_batches.c.project_id == project["id"],
                            t.integration_batches.c.lifecycle.in_(
                                [
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                    "promoted",
                                ]
                            ),
                            or_(
                                t.integration_batches.c.lifecycle != "promoted",
                                t.integration_batches.c.cleanup_state != "complete",
                            ),
                            ~select(t.integration_subjects.c.id)
                            .where(
                                t.integration_subjects.c.batch_id == t.integration_batches.c.id,
                                t.integration_subjects.c.kind == "root_batch",
                            )
                            .exists(),
                        )
                        .order_by(
                            t.integration_batches.c.created_at.desc(), t.integration_batches.c.id
                        )
                    )
                )
                .mappings()
                .first()
            )
            request_id = batch["request_id"] if batch else project["outstanding_request_id"]
            if request_id is None:
                return
            policy_data = (
                batch["policy_snapshot"] if batch else project["hierarchical_integration_policy"]
            )
            if not policy_data:
                return
            artifact = policy_data["root"]["route"]["artifact"]
            pin = PolicyArtifactPin(
                playbook_id=artifact["playbook_id"], artifact_sha256=artifact["artifact_sha256"]
            )
            await self.policy.policy_for(pin)
            key = subject_key(SubjectKind.ROOT_BATCH, repository_id, request_id)
            prior = (
                await conn.execute(
                    select(t.integration_subjects).where(
                        t.integration_subjects.c.project_id == project["id"],
                        t.integration_subjects.c.subject_key == key,
                    )
                )
            ).first()
            if prior:
                return
            owner = (
                await conn.execute(
                    select(t.integration_subjects.c.id)
                    .where(
                        t.integration_subjects.c.repository_id == repository_id,
                        t.integration_subjects.c.kind == "root_batch",
                        t.integration_subjects.c.engine == "reconciler",
                    )
                    .limit(1)
                )
            ).first()
            if not batch and not owner:
                return  # shadow starts after legacy seals, avoiding a stale admitting mirror
            repository = await self.db.get_repo(repository_id)
            phase = SubjectPhase.ADMITTING
            head, base, generation = None, None, 0
            if batch:
                phase = {
                    "testing": SubjectPhase.TESTING,
                    "repairing": SubjectPhase.REPAIRING,
                    "human_blocked": SubjectPhase.REPAIRING,
                    "promoting": SubjectPhase.PUBLISHING,
                    "cleanup_pending": SubjectPhase.PUBLISHED,
                    "promoted": SubjectPhase.PUBLISHED,
                }.get(batch["lifecycle"], SubjectPhase.BUILDING)
                generation = batch["current_revision"]
                revision = (
                    (
                        await conn.execute(
                            select(t.integration_candidate_revisions).where(
                                t.integration_candidate_revisions.c.batch_id == batch["id"],
                                t.integration_candidate_revisions.c.revision == generation,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                head = revision["head_sha"] if revision else None
                base = revision["construction_base_sha"] if revision else batch["base_sha"]
            subject = Subject(
                id=str(uuid5(NAMESPACE_URL, project["id"] + ":" + key)),
                project_id=project["id"],
                repository_id=repository_id,
                kind=SubjectKind.ROOT_BATCH,
                subject_key=key,
                engine=SubjectEngine.RECONCILER if owner else SubjectEngine.LEGACY,
                policy=pin,
                phase=phase,
                batch_id=batch["id"] if batch else None,
                target_ref="refs/heads/" + repository.default_branch.removeprefix("refs/heads/"),
                head_sha=head,
                base_sha=base,
                generation=generation,
                schedule=SubjectSchedule.progress(now=now, max_wait_seconds=3600),
                created_at=now,
                updated_at=now,
            )
            await self.db.ensure_integration_subject_on(conn, subject.to_row())


def root_runtime_for(orchestrator):
    config = orchestrator.config.integration
    if not (config.reconciler_shadow or config.reconciler_active):
        return None
    observer = RootObserver(
        orchestrator.db,
        GitObservationReader(orchestrator.git),
        facts_type=IntegrationPolicyFacts,
        session_probe=orchestrator._root_subject_session_probe,
    )
    policy = PinnedRootPolicy(orchestrator._load_playbook_artifact)
    ports = RootPrimitiveAdapters(
        orchestrator.db,
        lambda: orchestrator._command_handler,
        observer,
        ci_adapters=getattr(orchestrator, "integration_ci_adapters", None),
    ).bind(PrimitivePorts())
    runtime = RootSubjectRuntime(
        orchestrator.db,
        observer,
        policy,
        ports,
        shadow=config.reconciler_shadow,
        active=config.reconciler_active,
    )
    runtime.subscribe(orchestrator.bus)
    return runtime
