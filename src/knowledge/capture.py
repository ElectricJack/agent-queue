"""Replayable capture from durable evidence; event delivery is only a wake signal."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace

from sqlalchemy import select, update

from src.database.tables import (
    events,
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_revisions,
    records,
    task_completion_records,
    task_session_attempts,
)
from src.knowledge.extraction_store import ExtractionStore
from src.knowledge.models import canonical_bytes, content_hash
from src.records.artifacts import RetainedArtifacts
from src.records.models import RecordError

EVIDENCE_TYPES = frozenset({"observed", "hypothesis", "decision", "instruction_claim"})
CAPTURE_VERSION = "generation:1"
CONSUMER = "knowledge-extraction:1"
PAGE_SIZE = 8


@dataclass(frozen=True)
class CaptureInput:
    """Server-resolved evidence; no role or scope is inferred from another item."""

    source_identity: str
    scope_key: str
    actor_id: str
    permission_key: str
    source_policy: str
    provider_id: str
    content: str | None
    evidence_type: str = "hypothesis"
    event_id: int = 0
    attempt_id: str | None = None
    line_start: int = 1
    line_end: int = 1
    target_record_id: str | None = None
    target_revision_id: str | None = None

    @property
    def partition(self):
        return (
            self.scope_key,
            self.actor_id,
            self.permission_key,
            self.source_policy,
            self.provider_id,
        )


def partition_inputs(inputs):
    groups = {}
    for item in inputs:
        if not isinstance(item, CaptureInput) or item.evidence_type not in EVIDENCE_TYPES:
            raise RecordError("extraction.invalid_input")
        if not item.scope_key.startswith("project:") or not all(
            isinstance(value, str) and value.strip()
            for value in (
                item.source_identity,
                item.actor_id,
                item.permission_key,
                item.source_policy,
            )
        ):
            raise RecordError("extraction.invalid_input")
        if (
            type(item.event_id) is not int
            or item.event_id < 0
            or type(item.line_start) is not int
            or item.line_start < 1
            or type(item.line_end) is not int
            or item.line_end < item.line_start
            or item.content is not None
            and not isinstance(item.content, str)
        ):
            raise RecordError("extraction.invalid_input")
        groups.setdefault(item.partition, []).append(item)
    return [
        group[offset : offset + PAGE_SIZE]
        for group in groups.values()
        for offset in range(0, len(group), PAGE_SIZE)
    ]


class KnowledgeCapture:
    def __init__(self, db, config, *, store=None):
        self.db = db
        self._config = config
        self.store = store or ExtractionStore()

    @property
    def config(self):
        return self._config() if callable(self._config) else self._config

    def enabled(self, feature, project_id):
        cfg = self.config
        return bool(
            cfg.knowledge.enabled
            and cfg.knowledge.writes_enabled
            and cfg.memory.enabled
            and getattr(cfg.knowledge, feature).enabled
            and project_id in cfg.knowledge.enabled_projects
            and cfg.knowledge.legacy_memory_mode == "disabled"
        )

    def artifacts(self):
        return RetainedArtifacts(self.db, self.config.knowledge, self.config.vault_root)

    async def capture_on(self, inputs, *, feature="extraction", conn):
        if feature not in {"extraction", "consolidation"}:
            raise RecordError("extraction.invalid_feature")
        jobs = []
        for group in partition_inputs(inputs):
            scope = group[0].scope_key
            project = scope.removeprefix("project:")
            if not self.enabled(feature, project):
                raise RecordError("knowledge.disabled")
            # Require the current project to exist even for internal capture.
            from src.database.tables import projects

            if not await conn.scalar(select(projects.c.id).where(projects.c.id == project)):
                raise RecordError("record.not_found")
            settings = getattr(self.config.knowledge, feature)
            envelopes, receipts = [], []
            for item in group:
                envelope = {**asdict(item), "feature": feature, "format_version": 1}
                envelopes.append(envelope)
                source = await self.artifacts().retain_on(
                    canonical_bytes(envelope), access=SimpleNamespace(scope_key=scope), conn=conn
                )
                receipts.append(
                    dict(
                        event_id=item.event_id,
                        attempt_id=item.attempt_id,
                        artifact_id=source["artifact_id"],
                        source_scope=scope,
                        actor_id=item.actor_id,
                    )
                )
            job = await self.store.enqueue_on(
                scope_key=scope,
                source_identity=group[0].source_identity
                if len(group) == 1
                else "batch:" + content_hash([item.source_identity for item in group]),
                source_sha256=content_hash(envelopes),
                extractor_version=f"{feature}:{CAPTURE_VERSION}",
                policy_version=settings.policy_version,
                inputs=receipts,
                conn=conn,
            )
            if any(item.content is None for item in group) and job["state"] == "pending":
                await conn.execute(
                    update(self.store.jobs)
                    .where(self.store.jobs.c.job_id == job["job_id"])
                    .values(state="quarantined", error_code="source_unavailable")
                )
                job = await self.store.get_job_on(job["job_id"], conn=conn)
            jobs.append(job)
        return jobs

    async def capture(self, inputs, *, feature="extraction"):
        async with self.db.immediate() as conn:
            return await self.capture_on(inputs, feature=feature, conn=conn)

    async def completion_input_on(self, row, project, *, event_id=0, conn):
        attempt = (
            (
                await conn.execute(
                    select(task_session_attempts)
                    .where(
                        task_session_attempts.c.task_id == row["task_id"],
                        task_session_attempts.c.project_id == project,
                        task_session_attempts.c.started_at <= row["completed_at"],
                    )
                    .order_by(task_session_attempts.c.started_at.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        settings = self.config.knowledge.extraction
        body = row["summary"] or None
        return CaptureInput(
            source_identity=f"completion:{row['id']}",
            scope_key=f"project:{project}",
            actor_id=f"attempt:{attempt['id']}" if attempt else "system:completion",
            permission_key=f"profile:{attempt['profile_id']}" if attempt else "system:completion",
            source_policy=settings.policy_version,
            provider_id=settings.provider_id,
            # Summaries are claims. Only a retained log adapter may label observed facts.
            content=body,
            evidence_type="hypothesis",
            event_id=event_id,
            attempt_id=attempt["id"] if attempt else None,
            line_end=max(1, len(body.splitlines())) if body else 1,
        )

    async def reconcile(self, project):
        """A bounded event page plus missing-completion scan, in one transaction.

        Completion IDs are used on both paths, so a late/lost bus event cannot
        duplicate a job. The canonical input excludes the incidental wake ID:
        evidence always carries event_id=0 when sourced from the completion row.
        Its exact completion/attempt identity and ranges are retained instead.
        """
        if not self.enabled("extraction", project):
            return 0
        async with self.db.immediate() as conn:
            scope = f"project:{project}"
            await self.db.ensure_record_scope_on(project_id=project, conn=conn)
            checkpoint = (
                await conn.scalar(
                    select(self.store.checkpoints.c.last_event_id).where(
                        self.store.checkpoints.c.consumer_id == CONSUMER,
                        self.store.checkpoints.c.scope_key == scope,
                    )
                )
                or 0
            )
            page = (
                (
                    await conn.execute(
                        select(events)
                        .where(
                            events.c.project_id == project,
                            events.c.id > checkpoint,
                        )
                        .order_by(events.c.id)
                        .limit(PAGE_SIZE)
                    )
                )
                .mappings()
                .all()
            )
            from src.database.tables import archived_tasks, tasks

            domain = (
                select(tasks.c.id, tasks.c.project_id)
                .union_all(select(archived_tasks.c.id, archived_tasks.c.project_id))
                .subquery()
            )
            selected = (
                select(task_completion_records)
                .join(
                    domain,
                    domain.c.id == task_completion_records.c.task_id,
                )
                .where(domain.c.project_id == project)
            )
            captured = 0
            for event in page:
                if event["event_type"] not in {"task.completed", "task_completed"}:
                    continue
                completion = (
                    (
                        await conn.execute(
                            selected.where(
                                task_completion_records.c.task_id == event["task_id"],
                                task_completion_records.c.completed_at <= event["timestamp"],
                            )
                            .order_by(task_completion_records.c.completed_at.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .first()
                )
                if completion:
                    item = await self.completion_input_on(completion, project, conn=conn)
                else:
                    item = CaptureInput(
                        source_identity=f"event:{event['id']}",
                        scope_key=scope,
                        actor_id=f"agent:{event['agent_id']}"
                        if event["agent_id"]
                        else "system:event",
                        permission_key="unavailable",
                        source_policy=self.config.knowledge.extraction.policy_version,
                        provider_id=self.config.knowledge.extraction.provider_id,
                        content=None,
                        event_id=event["id"],
                    )
                await self.capture_on([item], conn=conn)
                captured += 1
            # A rescan repairs event retention/lost delivery without resetting a cursor.
            known = (
                select(self.store.jobs.c.job_id)
                .where(
                    self.store.jobs.c.scope_key == scope,
                    self.store.jobs.c.source_identity
                    == "completion:" + task_completion_records.c.id,
                    self.store.jobs.c.extractor_version == f"extraction:{CAPTURE_VERSION}",
                    self.store.jobs.c.policy_version
                    == self.config.knowledge.extraction.policy_version,
                )
                .exists()
            )
            missing = (
                (
                    await conn.execute(
                        selected.where(~known)
                        .order_by(
                            task_completion_records.c.completed_at,
                            task_completion_records.c.id,
                        )
                        .limit(2)
                    )
                )
                .mappings()
                .all()
            )
            for row in missing:
                await self.capture_on(
                    [await self.completion_input_on(row, project, conn=conn)], conn=conn
                )
                captured += 1
            if page:
                await self.store.advance_checkpoint_on(
                    consumer_id=CONSUMER, scope_key=scope, last_event_id=page[-1]["id"], conn=conn
                )
            return captured

    async def reconcile_consolidation(self, project):
        """Pin an exact current revision for reversible correction proposals."""
        if not self.enabled("consolidation", project):
            return 0
        async with self.db.immediate() as conn:
            scope = f"project:{project}"
            known = (
                select(self.store.jobs.c.job_id)
                .where(
                    self.store.jobs.c.scope_key == scope,
                    self.store.jobs.c.source_identity
                    == "revision:"
                    + knowledge_revisions.c.revision_id.cast(records.c.scope_key.type),
                    self.store.jobs.c.extractor_version == f"consolidation:{CAPTURE_VERSION}",
                    self.store.jobs.c.policy_version
                    == self.config.knowledge.consolidation.policy_version,
                )
                .exists()
            )
            rows = (
                (
                    await conn.execute(
                        select(knowledge_revisions, knowledge_revision_payloads.c.snapshot)
                        .join(
                            knowledge_records,
                            knowledge_records.c.current_revision_id
                            == knowledge_revisions.c.revision_id,
                        )
                        .join(records, records.c.record_id == knowledge_records.c.record_id)
                        .join(
                            knowledge_revision_payloads,
                            knowledge_revision_payloads.c.revision_id
                            == knowledge_revisions.c.revision_id,
                        )
                        .where(
                            records.c.scope_key == scope,
                            knowledge_revision_payloads.c.snapshot.is_not(None),
                            knowledge_revision_payloads.c.snapshot["lifecycle"].astext == "active",
                            knowledge_revision_payloads.c.snapshot["verification"].astext
                            != "disputed",
                            ~known,
                        )
                        .order_by(knowledge_revisions.c.created_at)
                        .limit(2)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                doc = row["snapshot"]
                settings = self.config.knowledge.consolidation
                item = CaptureInput(
                    source_identity=f"revision:{row['revision_id']}",
                    scope_key=scope,
                    actor_id=row["actor_id"],
                    permission_key="scoped-knowledge",
                    source_policy=settings.policy_version,
                    provider_id=settings.provider_id,
                    content=doc["body"],
                    evidence_type="instruction_claim"
                    if doc["category"] in {"policy", "procedure"}
                    else "hypothesis",
                    line_end=max(1, len(doc["body"].splitlines())),
                    target_record_id=str(row["record_id"]),
                    target_revision_id=str(row["revision_id"]),
                )
                await self.capture_on([item], feature="consolidation", conn=conn)
            return len(rows)
