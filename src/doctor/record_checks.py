"""Read-only record diagnostics; optional providers are never initialized."""

from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import and_, func, or_, select

from src.database.tables import (
    archived_tasks,
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_revisions,
    knowledge_search,
    projects,
    record_export_state,
    record_link_heads,
    record_link_versions,
    record_outbox,
    records,
    tasks,
)
from src.doctor.models import CheckResult, DoctorCheck, Severity
from src.knowledge.models import content_hash
from src.records.backfill import task_mapping_inventory
from src.records.export import read_managed
from src.records.models import RecordError

SAMPLE_LIMIT = 100


def _result(name, count, *, data=None, error=False):
    return CheckResult(
        id=f"records.{name}",
        severity=(Severity.ERROR if error else Severity.WARN) if count else Severity.OK,
        detail=f"{count} issue(s)",
        data={"count": count, **(data or {})},
    )


def _no_db(ctx, name):
    if ctx.db is None or not hasattr(ctx.db, "immediate"):
        return CheckResult(
            id=f"records.{name}", severity=Severity.INFO, detail="record database unavailable"
        )
    return None


async def _mappings(ctx):
    if result := _no_db(ctx, "task_mappings"):
        return result
    report = await task_mapping_inventory(ctx.db)
    count = sum(report[source]["missing"] for source in ("tasks", "archived_tasks"))
    return _result(
        "task_mappings",
        count + report["duplicate_aliases"],
        data=report,
        error=bool(report["duplicate_aliases"]),
    )


async def _domains(ctx):
    if result := _no_db(ctx, "domains"):
        return result
    live = select(tasks.c.id).where(tasks.c.id == records.c.task_id).exists()
    archive = select(archived_tasks.c.id).where(archived_tasks.c.id == records.c.task_id).exists()
    wrong_scope = or_(
        *(
            select(table.c.id)
            .where(
                table.c.id == records.c.task_id,
                records.c.scope_key != "project:" + table.c.project_id,
            )
            .exists()
            for table in (tasks, archived_tasks)
        )
    )
    project = (
        select(projects.c.id).where(records.c.scope_key == "project:" + projects.c.id).exists()
    )
    async with ctx.db.immediate() as conn:
        count = await conn.scalar(
            select(func.count())
            .select_from(records)
            .where(
                or_(
                    and_(records.c.kind == "task", ~live, ~archive),
                    and_(records.c.kind == "task", wrong_scope),
                    and_(records.c.scope_key != "global", ~project),
                )
            )
        )
    return _result("domains", count)


async def _hashes(ctx):
    if result := _no_db(ctx, "revision_hashes"):
        return result
    async with ctx.db.immediate() as conn:
        stmt = (
            select(
                knowledge_revisions.c.revision_id,
                knowledge_revisions.c.content_sha256,
                knowledge_revision_payloads.c.snapshot,
            )
            .join(
                knowledge_revision_payloads,
                knowledge_revision_payloads.c.revision_id == knowledge_revisions.c.revision_id,
            )
            .where(knowledge_revision_payloads.c.snapshot.is_not(None))
        )
        rows = (
            (
                await conn.execute(
                    stmt.order_by(knowledge_revisions.c.revision_id).limit(SAMPLE_LIMIT + 1)
                )
            )
            .mappings()
            .all()
        )
    count = sum(
        content_hash(row["snapshot"]) != row["content_sha256"] for row in rows[:SAMPLE_LIMIT]
    )
    result = _result(
        "revision_hashes",
        count,
        error=True,
        data={"sampled": min(len(rows), SAMPLE_LIMIT), "truncated": len(rows) > SAMPLE_LIMIT},
    )
    if len(rows) > SAMPLE_LIMIT and not count:
        result.severity = Severity.INFO
        result.detail = "bounded hash sample clean; full corpus not inspected"
    return result


async def _exports(ctx):
    if result := _no_db(ctx, "exports"):
        return result
    async with ctx.db.immediate() as conn:
        rows = (
            (
                await conn.execute(
                    select(record_export_state, records.c.scope_key, records.c.knowledge_alias)
                    .join(records, record_export_state.c.record_id == records.c.record_id)
                    .order_by(record_export_state.c.record_id)
                    .limit(SAMPLE_LIMIT + 1)
                )
            )
            .mappings()
            .all()
        )
        failures = (
            (
                await conn.execute(
                    select(record_outbox.c.event_id)
                    .where(
                        record_outbox.c.destination == "export",
                        record_outbox.c.delivered_at.is_(None),
                        record_outbox.c.last_error_code.in_(
                            [
                                "record.export_diverged",
                                "record.export_filesystem",
                                "record.export_path",
                            ]
                        ),
                    )
                    .limit(SAMPLE_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    issues = []
    for row in rows[:SAMPLE_LIMIT]:
        if row["destination"] != "vault":
            continue
        try:
            value = await asyncio.to_thread(
                read_managed, ctx.config.vault_root, row["scope_key"], row["knowledge_alias"]
            )
            bad = value is None or hashlib.sha256(value).hexdigest() != row["export_sha256"]
        except (OSError, RecordError):
            bad = True
        if bad or row["diverged_at"]:
            issues.append(str(row["record_id"]))
    result = _result(
        "exports",
        len(issues) + len(failures),
        data={
            "record_ids": issues,
            "failed_events": [str(v) for v in failures],
            "truncated": len(rows) > SAMPLE_LIMIT,
        },
    )
    if len(rows) > SAMPLE_LIMIT and not issues and not failures:
        result.severity = Severity.INFO
        result.detail = "bounded export sample clean; full corpus not inspected"
    return result


async def _links(ctx):
    if result := _no_db(ctx, "links"):
        return result
    target = records.alias("target")
    live = select(tasks.c.id).where(tasks.c.id == target.c.task_id).exists()
    archive = select(archived_tasks.c.id).where(archived_tasks.c.id == target.c.task_id).exists()
    project = select(projects.c.id).where(target.c.scope_key == "project:" + projects.c.id).exists()
    async with ctx.db.immediate() as conn:
        count = await conn.scalar(
            select(func.count())
            .select_from(
                record_link_heads.join(
                    record_link_versions,
                    and_(
                        record_link_heads.c.link_id == record_link_versions.c.link_id,
                        record_link_heads.c.current_version == record_link_versions.c.version,
                    ),
                )
                .join(target, target.c.record_id == record_link_versions.c.target_record_id)
                .outerjoin(
                    knowledge_revision_payloads,
                    knowledge_revision_payloads.c.revision_id
                    == record_link_versions.c.target_revision_id,
                )
            )
            .where(
                record_link_versions.c.removed.is_(False),
                or_(
                    and_(target.c.kind == "task", ~live, ~archive),
                    and_(target.c.scope_key != "global", ~project),
                    and_(
                        record_link_versions.c.target_revision_id.is_not(None),
                        knowledge_revision_payloads.c.snapshot.is_(None),
                    ),
                ),
            )
        )
    return _result("links", count)


async def _index(ctx):
    if result := _no_db(ctx, "index_lag"):
        return result
    async with ctx.db.immediate() as conn:
        count = await conn.scalar(
            select(func.count())
            .select_from(
                knowledge_records.outerjoin(
                    knowledge_search, knowledge_search.c.record_id == knowledge_records.c.record_id
                ).join(
                    knowledge_revision_payloads,
                    knowledge_revision_payloads.c.revision_id
                    == knowledge_records.c.current_revision_id,
                )
            )
            .where(
                knowledge_revision_payloads.c.snapshot.is_not(None),
                or_(
                    knowledge_search.c.record_id.is_(None),
                    knowledge_search.c.revision_id != knowledge_records.c.current_revision_id,
                ),
            )
        )
    return _result(
        "index_lag", count, data={"index": "lexical", "optional_provider_initialized": False}
    )


async def _redaction(ctx):
    if result := _no_db(ctx, "redaction_cleanup"):
        return result
    async with ctx.db.immediate() as conn:
        exports = await conn.scalar(
            select(func.count())
            .select_from(
                record_export_state.join(
                    knowledge_revision_payloads,
                    knowledge_revision_payloads.c.revision_id == record_export_state.c.revision_id,
                )
            )
            .where(knowledge_revision_payloads.c.snapshot.is_(None))
        )
        index = await conn.scalar(
            select(func.count())
            .select_from(
                knowledge_search.join(
                    knowledge_revision_payloads,
                    knowledge_revision_payloads.c.revision_id == knowledge_search.c.revision_id,
                )
            )
            .where(knowledge_revision_payloads.c.snapshot.is_(None))
        )
        from src.database.tables import knowledge_redactions

        pending = await conn.scalar(
            select(func.count())
            .select_from(knowledge_redactions)
            .where(
                knowledge_redactions.c.completed_at.is_(None),
            )
        )
    return _result(
        "redaction_cleanup",
        exports + index + pending,
        data={"export_checkpoints": exports, "lexical_rows": index, "pending_redactions": pending},
    )


async def _outbox(ctx):
    if result := _no_db(ctx, "outbox"):
        return result
    async with ctx.db.immediate() as conn:
        pending = await conn.scalar(
            select(func.count())
            .select_from(record_outbox)
            .where(record_outbox.c.delivered_at.is_(None))
        )
        failed = await conn.scalar(
            select(func.count())
            .select_from(record_outbox)
            .where(record_outbox.c.delivered_at.is_(None), record_outbox.c.attempts >= 10)
        )
    return _result("outbox", failed, data={"pending": pending, "exhausted": failed})


def record_checks():
    slots = asyncio.Semaphore(2)

    def bounded(run):
        async def invoke(ctx):
            async with slots:
                return await run(ctx)

        return invoke

    return [
        DoctorCheck(id=f"records.{name}", run=bounded(run), owner="knowledge-records", fix=None)
        for name, run in (
            ("task_mappings", _mappings),
            ("domains", _domains),
            ("revision_hashes", _hashes),
            ("exports", _exports),
            ("links", _links),
            ("index_lag", _index),
            ("redaction_cleanup", _redaction),
            ("outbox", _outbox),
        )
    ]
