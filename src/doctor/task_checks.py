"""``tasks.*`` doctor checks for stale task metadata."""

from __future__ import annotations

import time

from sqlalchemy import exists, literal, or_, select

from src.database.queries.claim_queries import (
    PREPARE_BACKOFF_UNTIL_KEY,
    numeric_meta_value,
)
from src.database.queries.hierarchy_queries import (
    container_flag_exists,
    delivered_same_parent_prerequisites_when_hierarchical,
    materialized_origin_when_hierarchical,
)
from src.database.tables import (
    integration_repair_stages,
    task_labels,
    task_metadata,
    task_workspace_requirements,
    tasks,
)
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus

OWNER = "task-lifecycle"
_STALE_STATUSES = frozenset({TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED})


async def _check_ready_frontier_exclusions(ctx: DoctorContext) -> CheckResult:
    """Explain READY rows withheld by the profile-independent claim filters."""
    check_id = "tasks.ready_frontier_exclusions"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    origin_ok = materialized_origin_when_hierarchical()
    prerequisites_ok = delivered_same_parent_prerequisites_when_hierarchical()
    container = container_flag_exists()
    held = exists(select(literal(1)).where(
        task_labels.c.task_id == tasks.c.id,
        task_labels.c.label.like("hold:%"),
    ))
    retired_delegate = exists(select(literal(1)).where(
        integration_repair_stages.c.repair_task_id == tasks.c.id,
        integration_repair_stages.c.writer_kind == "repair_delegate",
        integration_repair_stages.c.state.notin_(("active", "awaiting_completion")),
    ))
    workspace_requirement = exists(select(literal(1)).where(
        task_workspace_requirements.c.task_id == tasks.c.id,
        task_workspace_requirements.c.kind_id.notin_(("project-repo", "vault")),
    ))
    prepare_backoff = exists(select(literal(1)).where(
        task_metadata.c.task_id == tasks.c.id,
        task_metadata.c.key == PREPARE_BACKOFF_UNTIL_KEY,
        numeric_meta_value(task_metadata.c.value) > time.time(),
    ))
    excluded = or_(
        tasks.c.is_blocked != 0,
        tasks.c.profile_id == "supervisor",
        tasks.c.assigned_agent_id.is_not(None),
        tasks.c.is_plan_subtask != 0,
        ~origin_ok,
        ~prerequisites_ok,
        container,
        held,
        retired_delegate,
        workspace_requirement,
        prepare_backoff,
    )
    stmt = (
        select(
            tasks.c.id, tasks.c.project_id, tasks.c.is_blocked, tasks.c.profile_id,
            tasks.c.assigned_agent_id, tasks.c.is_plan_subtask,
            origin_ok.label("origin_ok"), prerequisites_ok.label("prerequisites_ok"),
            container.label("container"), held.label("held"),
            retired_delegate.label("retired_delegate"),
            workspace_requirement.label("workspace_requirement"),
            prepare_backoff.label("prepare_backoff"),
        )
        .where(tasks.c.status == TaskStatus.READY.value, excluded)
        .order_by(tasks.c.project_id, tasks.c.id)
        .limit(51)
    )
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    findings = []
    for row in rows[:50]:
        reasons = []
        if row["is_blocked"]:
            reasons.append("dependency_blocked")
        if row["profile_id"] == "supervisor":
            reasons.append("supervisor_profile")
        if row["assigned_agent_id"] is not None:
            reasons.append("already_assigned")
        if row["is_plan_subtask"]:
            reasons.append("plan_subtask")
        if not row["origin_ok"]:
            reasons.append("origin_not_materialized")
        if not row["prerequisites_ok"]:
            reasons.append("sibling_prerequisite_not_delivered")
        if row["container"]:
            reasons.append("container_settles_without_worker")
        if row["held"]:
            reasons.append("hold_label")
        if row["retired_delegate"]:
            reasons.append("retired_repair_delegate")
        if row["workspace_requirement"]:
            reasons.append("workspace_requirement")
        if row["prepare_backoff"]:
            reasons.append("claim_prepare_backoff")
        findings.append({"task_id": row["id"], "project_id": row["project_id"],
                         "reasons": reasons})
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
