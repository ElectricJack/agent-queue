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
from src.integration.reconciler import (
    CompiledPolicyAdapter, IntegrationReconciler, ScopedIntegrationDB, VisitTransition,
)
from src.integration.root_adapters import RootPrimitiveAdapters
from src.integration.shadow import diagnostics_for
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
    WriterLease,
    WriterStatus,
    budget_values,
    schedule_values,
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
    def __init__(self, db, git, *, open_prs=None, **kwargs):
        self.db = db
        super().__init__(_RootObservationReader(db, open_prs=open_prs), git, **kwargs)

    async def observe(self, subject):
        # Legacy repair stages own the clock/attempt rows during phase one.
        # Re-read those rather than letting a mirrored budget hide new results.
        facts = await super().observe(subject)
        if subject.kind is SubjectKind.ROOT_BATCH and not subject.batch_id:
            contained = {m.task_id for m in facts.members if m.ancestry == "contained"}
            facts = facts.model_copy(update={
                "members": tuple(m for m in facts.members if m.task_id not in contained),
                # Admission fetches and verifies each exact source. Missing
                # local source objects must not prevent reaching that port.
                "unknown": tuple(r for r in facts.unknown if not r.startswith((
                    "ancestry_unknown:", "member_head_missing:",
                ))),
            })
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
        if subject.engine is SubjectEngine.RECONCILER and subject.batch_id and facts.writer.task_id:
            facts = await self._legacy_writer(subject, facts)
        return facts

    async def _legacy_writer(self, subject, facts):
        """Read phase-one legacy repair receipts as the table's writer facts.

        The legacy stage keeps naming its writer after the work is done. Its
        own durable receipts say when that writer let go: an accepted repair
        handed the fenced ref to the collector, or the delegate closed under
        its exact fence and its session and checkout are gone. A stopped
        writer whose work a journalled merged build already adopted is no
        longer the subject's writer. Live, attached or unproven writers keep
        the observer's answer.
        """
        task_id = facts.writer.task_id
        async with self.db._engine.connect() as conn:

            async def rows(table, *conditions, order=None):
                statement = select(table).where(*conditions)
                if order is not None:
                    statement = statement.order_by(order)
                return (await conn.execute(statement)).mappings().all()

            journal = await rows(
                t.integration_subject_journal,
                t.integration_subject_journal.c.subject_id == subject.id,
                t.integration_subject_journal.c.mode == "active",
                t.integration_subject_journal.c.primitive == Primitive.GIT_MERGE_MEMBERS.value,
                order=t.integration_subject_journal.c.seq,
            )
            batch = (
                await rows(t.integration_batches, t.integration_batches.c.id == subject.batch_id)
            )[0]
            owner = next(
                iter(
                    await rows(
                        t.integration_branch_owners,
                        t.integration_branch_owners.c.repository_id == subject.repository_id,
                        t.integration_branch_owners.c.ref == batch["integration_branch"],
                    )
                ),
                None,
            )
            handoffs = await rows(
                t.integration_candidate_resolutions,
                t.integration_candidate_resolutions.c.batch_id == subject.batch_id,
                t.integration_candidate_resolutions.c.repair_task_id == task_id,
                t.integration_candidate_resolutions.c.state == "accepted",
            )
            sessions = await rows(t.sessions, t.sessions.c.task_id == task_id)
            locked = await rows(t.workspaces, t.workspaces.c.locked_by_task_id == task_id)
            task = next(iter(await rows(t.tasks, t.tasks.c.id == task_id)), None)
            stages = await rows(
                t.integration_repair_stages,
                t.integration_repair_stages.c.repair_task_id == task_id,
            )
            closes = await rows(
                t.integration_outbox,
                t.integration_outbox.c.project_id == subject.project_id,
                t.integration_outbox.c.event_type == "integration.repair_delegate_closed",
            )
        handoff = (subject.writer.stop_proof or {}).get("stop_proof", {})
        latest = max(sessions, key=lambda row: row["started_at"] or 0, default=None)
        closed_handoff = (
            handoff.get("kind") == "accepted_handoff"
            and handoff.get("task_id") == task_id
            and task is not None and task["status"] in {"COMPLETED", "BLOCKED", "FAILED"}
            and handoff.get("claim_epoch") == task["claim_epoch"]
            and not locked
            and (owner is None or owner["owner_id"] != task_id
                 or (owner["handoff_state"] == "released"
                     and owner["fence_token"] == handoff.get("fence_token")))
            and (latest is None or (
                handoff.get("session_id") == latest["id"]
                and handoff.get("instance_token") == latest["instance_token"]
                and handoff.get("confirmed_at", 0) >= (latest["started_at"] or 0)
            ))
        )
        if closed_handoff and handoff.get("outcome") == "fail":
            # The worker explicitly surrendered this attempt. The pinned
            # successor action still enforces its original absolute deadline.
            stopped = self._project_writer(facts, facts.writer.model_copy(update={
                "status": WriterStatus.STOPPED, "stop_proof": subject.writer.stop_proof,
            }))
            return stopped.model_copy(update={"ladder_exhausted": True})
        if (owner is not None and owner["owner_id"] == task_id
                and owner["handoff_state"] != "released"):
            return facts  # the writer still holds its fenced ref
        decided = {
            row["subject_version"]: row for row in journal if row["entry_kind"] == "decision"
        }
        for row in journal:
            observed = (decided.get(row["subject_version"], {}).get("payload") or {}).get("facts")
            if (
                row["entry_kind"] == "action"
                and row["outcome"] == "merged"
                and observed
                and (observed.get("writer") or {}).get("task_id") == task_id
                and observed.get("writer_status") == WriterStatus.STOPPED.value
            ):
                return self._project_writer(facts, WriterLease())
        if closed_handoff:
            return self._project_writer(facts, facts.writer.model_copy(update={
                "status": WriterStatus.STOPPED, "stop_proof": subject.writer.stop_proof,
            }))
        if (
            not sessions
            and not locked
            and task is not None
            and not task["claim_epoch"]
            and task["status"] in {"COMPLETED", "FAILED"}
        ):
            # Retired before any claim (an ejection supersedes it): no
            # process ever ran and nothing is left to preserve.
            return self._project_writer(facts, WriterLease())
        handed = next(
            (
                row
                for row in handoffs
                if owner is not None
                and row["handoff_owner_id"] == owner["owner_id"]
                and row["handoff_fence_token"] is not None
                and owner["fence_token"] >= row["handoff_fence_token"]
            ),
            None,
        )
        proof = None
        if handed is not None:
            proof = {
                "kind": "accepted_handoff",
                "reservation_id": handed["id"],
                "successor_owner_id": handed["handoff_owner_id"],
                "successor_fence_token": handed["handoff_fence_token"],
                "confirmed_at": handed["updated_at"],
            }
        latest = max(sessions, key=lambda row: row["started_at"] or 0, default=None)
        if (
            proof is None
            and latest is not None
            and all(row["state"] == "stopped" for row in sessions)
            and not locked
            and task is not None
            and task["status"] == "COMPLETED"
        ):
            receipts = [
                {"kind": "accepted_delegate_completion", **completion}
                for stage in stages
                if (completion := (stage["dossier"] or {}).get("accepted_delegate_completion"))
            ] + [
                {"kind": "delegate_close", "event_id": row["id"], **row["payload"]}
                for row in closes
                if (row["payload"] or {}).get("task_id") == task_id
            ]
            proof = next(
                (
                    {**receipt, "confirmed_at": latest["ended_at"] or latest["started_at"]}
                    for receipt in receipts
                    if receipt.get("task_id") == task_id
                    and receipt.get("session_id") == latest["id"]
                    and receipt.get("instance_token") == latest["instance_token"]
                ),
                None,
            )
        if proof is None:
            return facts
        stopped = facts.writer.model_copy(
            update={"status": WriterStatus.STOPPED, "stop_proof": {"stop_proof": proof}}
        )
        return self._project_writer(facts, stopped)

    @staticmethod
    def _project_writer(facts, writer):
        task_id = facts.writer.task_id
        return facts.model_copy(
            update={
                "writer": writer,
                "unknown": tuple(
                    reason
                    for reason in facts.unknown
                    if reason
                    not in {"writer_stop_unproven:" + task_id, "writer_liveness_unknown:" + task_id}
                ),
            }
        )


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
    def __init__(self, db, *, open_prs=None):
        self.db, self.open_prs = db, open_prs
        self.reader = DatabaseObservationReader(db)

    async def read(self, subject_id):
        snapshot = await self.reader.read(subject_id)
        if snapshot is None:
            return None
        subject = snapshot.subject
        if subject.kind is SubjectKind.ROOT_BATCH and not subject.batch_id:
            eligible, after = set(), None
            async with self.db._engine.connect() as conn:
                while True:
                    page = await self.db.eligible_root_page_on(
                        conn, project_id=subject.project_id, repository_id=subject.repository_id,
                        after=after, limit=100,
                    )
                    if not page:
                        break
                    eligible.update(row["task_id"] for row in page)
                    after = (page[-1]["task_id"], page[-1]["source_head"])
            tasks = tuple(row for row in snapshot.all("tasks") if row["id"] in eligible)
            if self.open_prs and tasks:
                try:
                    urls = await self.open_prs(snapshot.repository)
                    tasks = tuple(row for row in tasks if row.get("pr_url") in urls)
                except Exception:
                    # Seal will report a fresh per-source observation failure.
                    # Do not interpret an unavailable remote as an empty frontier.
                    pass
            snapshot = replace(snapshot, rows={**snapshot.rows, "tasks": tasks})
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
            operation = next((r for r in snapshot.all("integration_repair_operations")
                              if r["batch_id"] == subject.batch_id), None)
            stage = next((r for r in snapshot.all("integration_repair_stages")
                          if operation and r["operation_id"] == operation["id"]
                          and r["ordinal"] == operation["active_stage"]), None)
            if (stage and stage["repair_task_id"]
                    and stage["repair_task_id"] != subject.writer.task_id):
                # The legacy stage can dispatch a successor inside a visit,
                # before its subject writer mirror has been committed.
                changes["writer"] = WriterLease()
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

    def __init__(
        self, db, observer, policy, ports, *, shadow=False, active=False, clock=time.time,
        diagnostics=None,
    ):
        self.db, self.policy, self.clock = db, policy, clock
        scoped_db = ScopedIntegrationDB(db, ("train", "hierarchy"))
        self.loops = []
        if active:
            self.loops.append(
                IntegrationReconciler(
                    scoped_db, observer.observe, policy, ports, mode=JournalMode.ACTIVE,
                    clock=clock, diagnostics=diagnostics,
                )
            )
        if shadow:
            self.loops.append(
                IntegrationReconciler(
                    _ShadowDB(scoped_db),
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
            if project["outstanding_request_id"] is not None:
                await self._supersede_admitting(
                    conn, project["id"], repository_id, project["outstanding_request_id"], now
                )
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
            if phase is SubjectPhase.ADMITTING:
                blocker = (
                    await conn.execute(
                        select(t.integration_subjects.c.id).where(
                            t.integration_subjects.c.project_id == project["id"],
                            t.integration_subjects.c.repository_id == repository_id,
                            t.integration_subjects.c.kind == "root_batch",
                            t.integration_subjects.c.phase == "admitting",
                        )
                    )
                ).scalar_one_or_none()
                if blocker is not None:
                    logger.warning(
                        "integration: root subject for %s waits; admitting subject %s "
                        "still holds %s",
                        request_id,
                        blocker,
                        repository_id,
                    )
                    return
            await self.db.ensure_integration_subject_on(conn, subject.to_row())

    async def _supersede_admitting(self, conn, project_id, repository_id, request_id, now):
        """Close admitting roots whose sweep request is no longer outstanding.

        Sealing refuses a request that is not outstanding, so such a subject
        can never form a batch, yet it holds the repository's one admitting
        slot (``uq_integration_subjects_admitting_root``) against the request
        that replaced it: a released stale request, or the next one after a
        promotion. A subject already bound to a batch is never touched.
        """
        current = subject_key(SubjectKind.ROOT_BATCH, repository_id, request_id)
        rows = (
            (
                await conn.execute(
                    select(t.integration_subjects)
                    .where(
                        t.integration_subjects.c.project_id == project_id,
                        t.integration_subjects.c.repository_id == repository_id,
                        t.integration_subjects.c.kind == "root_batch",
                        t.integration_subjects.c.engine == "reconciler",
                        t.integration_subjects.c.phase == "admitting",
                        t.integration_subjects.c.batch_id.is_(None),
                        t.integration_subjects.c.subject_key != current,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            subject = Subject.from_row(row)
            stale = subject.subject_key.removeprefix(f"root_batch:{repository_id}:")
            reason = f"superseded: request {stale} is no longer outstanding ({request_id} is)"
            schedule = SubjectSchedule.close(
                now=now, reason=reason, max_wait_seconds=subject.schedule.max_wait_seconds
            )
            closed = await self.db.update_integration_subject_on(
                conn,
                subject_id=subject.id,
                expected_version=subject.version,
                values={"phase": SubjectPhase.DONE.value, **schedule_values(schedule)},
                now=now,
            )
            if closed is None:
                continue  # a visit moved it first; the next seed re-reads it
            await self.db.append_integration_subject_journal_on(
                conn,
                {
                    "subject_id": subject.id,
                    "entry_kind": "action",
                    "idempotency_key": f"seed-supersede:{subject.version}",
                    "visit_id": f"seed:{subject.version}",
                    "mode": JournalMode.ACTIVE.value,
                    "policy_artifact_sha256": subject.policy.artifact_sha256,
                    "subject_version": subject.version,
                    "phase": subject.phase.value,
                    "head_sha": subject.head_sha,
                    "generation": subject.generation,
                    "primitive": Primitive.RECORD_DECISION.value,
                    "outcome": "recorded",
                    "payload": {"reason": reason, "superseded_by": current},
                    "recorded_at": now,
                },
            )
            logger.warning("integration: %s (subject %s)", reason, subject.id)


def root_runtime_for(orchestrator):
    config = orchestrator.config.integration
    if not (config.reconciler_shadow or config.reconciler_active):
        return None
    observer = RootObserver(
        orchestrator.db,
        RootGitObservationReader(
            orchestrator.git, orchestrator.integration_attestation_service._store
        ),
        facts_type=IntegrationPolicyFacts,
        session_probe=orchestrator._root_subject_session_probe,
        candidate_ci=root_candidate_ci_reader(orchestrator),
        open_prs=root_open_prs_reader(orchestrator.git),
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
        diagnostics=diagnostics_for(config, orchestrator.db, orchestrator.git,
                                    reader=observer.reader),
    )
    runtime.subscribe(orchestrator.bus)
    return runtime


class RootGitObservationReader(GitObservationReader):
    """Prefer retained candidate objects; unsealed frontiers still use the base checkout."""

    def __init__(self, git, store_for):
        super().__init__(git)
        self.store_for = store_for

    async def remote_head(self, repository, ref):
        store = self.store_for(repository["id"])
        if store.exists():
            reader = GitObservationReader(self.git, checkout=lambda _: str(store))
            result = await reader.remote_head(repository, ref)
            if result.state != "unknown":
                return result
        return await super().remote_head(repository, ref)

    async def remote_heads(self, repository, refs):
        heads = {}
        store = self.store_for(repository["id"])
        if store.exists():
            reader = GitObservationReader(self.git, checkout=lambda _: str(store))
            retained = await reader.remote_heads(repository, refs)
            heads = {ref: head for ref, head in retained.items() if head.state != "unknown"}
        rest = [ref for ref in refs if ref not in heads]
        if rest:
            heads.update(await super().remote_heads(repository, rest))
        return heads

    async def is_ancestor(self, repository, ancestor, descendant):
        result = await self.git.ais_ancestor(
            str(self.store_for(repository["id"])), ancestor, descendant, strict=True
        )
        if result is not None:
            return result
        return await super().is_ancestor(repository, ancestor, descendant)


def root_candidate_ci_reader(orchestrator):
    """Read authenticated checks for the frozen candidate, without an evidence-row prerequisite."""
    from src.integration.ci_producers import HostedCIProducer
    from src.integration.observe import _required
    from src.integration.subjects import CIEvidence

    async def read(snapshot, head):
        repo = await orchestrator.db.get_repo(head.repository_id)
        binding = await orchestrator.github_repository_binding_resolver(repo)
        required = _required(snapshot)
        state = {
            "project_id": snapshot.subject.project_id,
            "canonical_repository_id": head.repository_id,
            "repository_numeric_id": binding.repository_id,
            "repository_full_name": binding.full_name,
            "batch_id": snapshot.subject.batch_id,
            "revision": head.generation,
            "candidate_sha": head.sha,
            "operation_id": snapshot.subject.id,
            "policy_snapshot": {"root": {"required_checks": dict(required)}},
        }
        trust, client = await orchestrator.integration_attestation_service._load_trust(state)
        observed = await HostedCIProducer(client, trust).observe(snapshot.subject, head)
        return CIEvidence(
            head_sha=head.sha, state=observed.state, producer=observed.producer,
            observed_at=observed.observed_at, age_seconds=0,
        )

    return read


def root_open_prs_reader(git):
    async def read(repository):
        binding = await git.bind_github_repository(repository["url"])
        pulls = await git._github_client(binding).paged_list(
            f"/repositories/{binding.repository_id}/pulls?state=open&per_page=100"
        )
        if any(pull.get("state") != "open" or not pull.get("html_url") for pull in pulls):
            raise ValueError("open PR listing is malformed")
        return {pull["html_url"] for pull in pulls}

    return read
