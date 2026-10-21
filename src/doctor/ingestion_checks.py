"""Held-ingestion inventory and conservative, operator-owned legacy adoption."""

import time

from sqlalchemy import select

from src.database.queries.ingest_batch_queries import HELD_STATES
from src.database.tables import ingest_batch_receipts, ingest_batches, task_proposals, tasks
from src.doctor.models import CheckResult, DoctorCheck, Severity

HELD_CHECK = "ingestion.held_batches"
LEGACY_CHECK = "ingestion.legacy_proposals"


async def _legacy_rows(ctx):
    stmt = select(task_proposals).where(
        task_proposals.c.status == "ready", task_proposals.c.source.startswith("spec:"),
        ~select(ingest_batches.c.batch_id).where(
            ingest_batches.c.batch_id == task_proposals.c.id,
        ).exists(),
    ).order_by(task_proposals.c.id)
    if ctx.project_id:
        stmt = stmt.where(task_proposals.c.project_id == ctx.project_id)
    async with ctx.db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(stmt)).mappings()]


async def _legacy(ctx):
    if ctx.db is None:
        return CheckResult(id=LEGACY_CHECK, severity=Severity.INFO, detail="database not configured")
    rows = await _legacy_rows(ctx)
    return CheckResult(
        id=LEGACY_CHECK, severity=Severity.WARN if rows else Severity.OK,
        detail=f"{len(rows)} uncommitted legacy spec proposal(s); immutable approval required",
        fixable=bool(rows) and ctx.handler is not None,
        data={"proposals": [{k: row[k] for k in ("id", "project_id", "source", "status")}
                            for row in rows]},
    )


async def _fix_legacy(ctx):
    adopted, left_gated, errors = [], [], {}
    if ctx.db is not None and ctx.handler is not None:
        for row in await _legacy_rows(ctx):
            result = await ctx.handler.execute("spec_ingest_legacy_adopt", {
                "proposal_id": row["id"], "leave_gated": ctx.leave_gated,
            })
            if not result.get("success"):
                errors[row["id"]] = result.get("error", "Legacy adoption refused")
            elif result.get("left_gated"):
                left_gated.append(row["id"])
            else:
                adopted.append(row["id"])
    result = await _legacy(ctx)
    result.fix_applied = bool(adopted)
    result.data.update(adopted=adopted, left_gated=left_gated, errors=errors)
    return result


async def _held(ctx):
    if ctx.db is None:
        return CheckResult(id=HELD_CHECK, severity=Severity.INFO, detail="database not configured")
    stmt = select(ingest_batches).where(ingest_batches.c.state.in_(HELD_STATES)).order_by(
        ingest_batches.c.created_at, ingest_batches.c.batch_id,
    )
    if ctx.project_id:
        stmt = stmt.where(ingest_batches.c.project_id == ctx.project_id)
    findings, missing, stale, exhausted = [], [], [], []
    now = ctx.reference_time if ctx.reference_time is not None else time.time()
    async with ctx.db._engine.connect() as conn:
        for batch in (await conn.execute(stmt)).mappings():
            reviewer = (await conn.execute(select(tasks).where(
                tasks.c.id == batch["reviewer_task_id"],
                tasks.c.project_id == batch["project_id"],
            ))).mappings().first()
            receipts = (await conn.execute(select(ingest_batch_receipts).where(
                ingest_batch_receipts.c.batch_id == batch["batch_id"],
            ))).mappings().all()
            current = any(r["passing"] and r["structure_revision"] == batch["structure_revision"]
                          and r["structure_digest"] == batch["structure_digest"]
                          and r["reviewer_task_id"] == batch["reviewer_task_id"] for r in receipts)
            if not current and (not reviewer or reviewer["status"] in {"COMPLETED", "FAILED"}):
                missing.append(batch["batch_id"])
            if any(r["passing"] for r in receipts) and not current:
                stale.append(batch["batch_id"])
            if reviewer and reviewer["status"] in {"FAILED", "BLOCKED"}:
                exhausted.append(batch["batch_id"])
            findings.append({
                "batch_id": batch["batch_id"], "project_id": batch["project_id"],
                "state": batch["state"], "structure_revision": batch["structure_revision"],
                "reviewer_task_id": batch["reviewer_task_id"],
                "reviewer_status": reviewer["status"] if reviewer else None,
                "reviewer_retry_count": reviewer["retry_count"] if reviewer else None,
                "reviewer_max_retries": reviewer["max_retries"] if reviewer else None,
                "age_seconds": max(0, now - batch["created_at"]), "current_receipt": current,
            })
    legacy = await _legacy_rows(ctx)
    warning = bool(missing or stale or exhausted or legacy)
    return CheckResult(
        id=HELD_CHECK,
        severity=Severity.WARN if warning else Severity.INFO if findings else Severity.OK,
        detail=f"{len(findings)} held batch(es), {len(missing)} missing reviewer(s), "
               f"{len(stale)} stale receipt(s), {len(legacy)} legacy proposal(s)",
        data={"batches": findings, "missing_reviewers": missing, "stale_receipts": stale,
              "exhausted_reviewers": exhausted, "legacy_proposals": [r["id"] for r in legacy]},
    )


def ingestion_checks():
    return [
        DoctorCheck(id=HELD_CHECK, run=_held, owner="ingestion"),
        DoctorCheck(id=LEGACY_CHECK, run=_legacy, fix=_fix_legacy, owner="ingestion",
                    timeout_s=60),
    ]
