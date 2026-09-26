"""``tasks.*`` doctor checks for stale task metadata."""

from __future__ import annotations

from sqlalchemy import or_, select

from src.database.queries.claim_queries import claim_frontier_predicates
from src.database.tables import tasks
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus

OWNER = "task-lifecycle"
_STALE_STATUSES = frozenset({TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED})


async def _check_ready_frontier_exclusions(ctx: DoctorContext) -> CheckResult:
    """Explain READY rows withheld by the profile-independent claim filters."""
    check_id = "tasks.ready_frontier_exclusions"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    predicates = claim_frontier_predicates()
    stmt = (
        select(
            tasks.c.id, tasks.c.project_id,
            *(predicate.label(name) for name, predicate in predicates.items()),
        )
        .where(tasks.c.status == TaskStatus.READY.value,
               or_(*(~predicate for predicate in predicates.values())))
        .order_by(tasks.c.project_id, tasks.c.id)
        .limit(51)
    )
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    findings = [
        {"task_id": row["id"], "project_id": row["project_id"],
         "reasons": [name for name in predicates if not row[name]]}
        for row in rows[:50]
    ]
    if not findings:
        return CheckResult(id=check_id, severity=Severity.OK,
                           detail="no READY task is excluded from the claim frontier")
    return CheckResult(
        id=check_id,
        severity=Severity.WARN,
        detail=f"{len(findings)} READY task(s) are excluded from the claim frontier",
        data={"tasks": findings, "truncated": len(rows) > 50},
    )


async def _find_stale_attention(ctx: DoctorContext) -> list[dict]:
    if ctx.db is None:
        return []
    stale = []
    for task in await ctx.db.list_tasks():
        if task.status not in _STALE_STATUSES:
            continue
        code = await ctx.db.get_task_meta(task.id, "needs_attention")
        if code is not None:
            stale.append({"task_id": task.id, "status": task.status.value, "code": code})
    return stale


async def _check_stale_attention(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="tasks.stale_attention", severity=Severity.INFO, detail="database unavailable"
        )
    stale = await _find_stale_attention(ctx)
    if not stale:
        return CheckResult(
            id="tasks.stale_attention", severity=Severity.OK, detail="no stale needs-attention flags"
        )
    return CheckResult(
        id="tasks.stale_attention",
        severity=Severity.WARN,
        detail=f"{len(stale)} IN_PROGRESS/COMPLETED task(s) have stale needs-attention flags",
        fixable=True,
        data={"count": len(stale), "tasks": stale[:50]},
    )


async def _check_archive_blocked(ctx: DoctorContext) -> CheckResult:
    """Report terminal roots the auto-archive sweep keeps skipping.

    Report-only and read-only: it reads the refusal each root's last sweep
    attempt actually recorded, so it never attempts an archive and never
    re-derives the archive path's rules.  There is no ``--fix`` — every
    reason here (integration bookkeeping, a sealed batch, an open
    descendant, a live session) is resolved by the subsystem that owns it,
    not by doctor.
    """
    check_id = "tasks.archive_blocked"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    cfg = getattr(ctx.config, "archive", None) if ctx.config is not None else None
    if cfg is None or not cfg.enabled or not cfg.statuses:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="auto-archive is disabled")
    blocked = await ctx.db.list_archive_blocked_roots(
        statuses=list(cfg.statuses), older_than_seconds=cfg.after_hours * 3600
    )
    if not blocked.total:
        return CheckResult(
            id=check_id, severity=Severity.OK, detail="no eligible root is blocked from archiving"
        )
    reasons = sorted({row["reason"] for row in blocked.roots})
    return CheckResult(
        id=check_id,
        severity=Severity.WARN,
        detail=(
            f"{blocked.total} terminal root(s) eligible for auto-archive cannot be "
            f"archived ({', '.join(reasons)})"
        ),
        data={"count": blocked.total, "roots": blocked.roots},
    )


async def _fix_stale_attention(ctx: DoctorContext) -> CheckResult:
    if ctx.db is not None:
        for stale in await _find_stale_attention(ctx):
            await ctx.db.delete_task_meta(stale["task_id"], "needs_attention")
    return await _check_stale_attention(ctx)


def task_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="tasks.ready_frontier_exclusions",
            run=_check_ready_frontier_exclusions,
            owner=OWNER,
            timeout_s=10.0,
        ),
        DoctorCheck(
            id="tasks.stale_attention",
            run=_check_stale_attention,
            fix=_fix_stale_attention,
            owner=OWNER,
        ),
        DoctorCheck(
            id="tasks.archive_blocked",
            run=_check_archive_blocked,
            owner=OWNER,
        ),
    ]


CHECKS = task_checks()
_BY_ID = {check.id: check for check in CHECKS}


async def run_check(db, check_id: str, *, config=None, repair: bool = False) -> CheckResult:
    from src.doctor.runner import apply_fix

    check = _BY_ID[check_id]
    ctx = DoctorContext(config=config, db=db)
    if repair and check.fix is not None:
        return await apply_fix(check, ctx)
    return await check.run(ctx)
