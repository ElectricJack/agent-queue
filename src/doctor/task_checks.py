"""``tasks.*`` doctor checks for stale task metadata."""

from __future__ import annotations

import json
import time

from sqlalchemy import and_, exists, func, literal, or_, select, union_all

from src.agent_waits import TERMINAL_TASK_STATUSES
from src.database.queries.archive_queries import SETTLED_DEVELOPMENT_DELIVERY_STATES
from src.database.queries.blocked_state import OBSOLETE_META_KEY
from src.database.queries.claim_queries import claim_frontier_predicates
from src.database.queries.hierarchy_queries import container_flag_exists
from src.database.tables import (
    agent_waits,
    archived_tasks,
    development_deliveries,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    messages,
    task_metadata,
    tasks,
)
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus

OWNER = "task-lifecycle"
_STALE_STATUSES = frozenset({TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED})


async def _check_pending_timer_waits(ctx: DoctorContext) -> CheckResult:
    """Find missed timer resolution and resolved results that never reached their owner."""
    check_id = "waits.pending_timers"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    now = time.time()
    delivery_grace = max(30.0, ctx.config.messages.delivery_interval * 2)
    due_at = agent_waits.c.match["due_at"].as_float()
    stmt = (
        select(
            agent_waits.c.id.label("wait_id"), agent_waits.c.project_id,
            agent_waits.c.owner_kind, agent_waits.c.owner_id, agent_waits.c.session_id,
            due_at.label("due_at"), agent_waits.c.deadline_at, agent_waits.c.checked_at,
            agent_waits.c.state, agent_waits.c.resolved_at, agent_waits.c.result_message_id,
        )
        .select_from(agent_waits.outerjoin(
            messages, messages.c.id == agent_waits.c.result_message_id,
        ))
        .where(
            agent_waits.c.kind == "timer",
            or_(
                and_(agent_waits.c.state == "active",
                     or_(due_at <= now, agent_waits.c.deadline_at <= now)),
                and_(agent_waits.c.state.in_(("satisfied", "expired")),
                     agent_waits.c.resolved_at <= now - delivery_grace,
                     messages.c.delivered_at.is_(None), messages.c.archived_at.is_(None)),
            ),
        )
        .order_by(due_at, agent_waits.c.id)
        .limit(51)
    )
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    if not rows:
        return CheckResult(id=check_id, severity=Severity.OK,
                           detail="no overdue timer waits or stalled timer result deliveries")
    return CheckResult(
        id=check_id, severity=Severity.WARN,
        detail=(f"{len(rows[:50])} timer wait(s) need attention; "
                "check wait reconciliation and result delivery"),
        data={"waits": [dict(row) for row in rows[:50]], "truncated": len(rows) > 50},
    )


async def _check_pending_terminal_waits(ctx: DoctorContext) -> CheckResult:
    """Expose missed producer resolution without mutating waits or their owners."""
    check_id = "waits.pending_terminal_tasks"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    targets = union_all(
        select(tasks.c.id, tasks.c.project_id, tasks.c.status),
        select(archived_tasks.c.id, archived_tasks.c.project_id, archived_tasks.c.status),
    ).subquery()
    stmt = (
        select(
            agent_waits.c.id.label("wait_id"), agent_waits.c.project_id,
            agent_waits.c.owner_id, agent_waits.c.session_id,
            agent_waits.c.deadline_at, agent_waits.c.checked_at,
            targets.c.id.label("target_task_id"), targets.c.status.label("target_status"),
        )
        .select_from(agent_waits.join(targets, (
            targets.c.id == agent_waits.c.match["task_id"].as_string()
        ) & (targets.c.project_id == agent_waits.c.project_id)))
        .where(
            agent_waits.c.state == "active", agent_waits.c.kind == "task",
            targets.c.status.in_(TERMINAL_TASK_STATUSES),
        )
        .order_by(agent_waits.c.deadline_at, agent_waits.c.id)
        .limit(51)
    )
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    if not rows:
        return CheckResult(id=check_id, severity=Severity.OK,
                           detail="no active task wait has a terminal target")
    return CheckResult(
        id=check_id, severity=Severity.WARN,
        detail=f"{len(rows[:50])} active task wait(s) have terminal targets; check wait reconciliation",
        data={"waits": [dict(row) for row in rows[:50]], "truncated": len(rows) > 50},
    )


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


_LIFECYCLE_CHECK = "tasks.dangling_lifecycle"
_FINISHED = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
_ACTIVE_TRAIN_LIFECYCLES = (
    "sealing", "sealed", "building", "testing", "repairing", "human_blocked", "promoting",
    "cleanup_pending",
)
#: How many rows of each finding the check reports.
_LIFECYCLE_LIMIT = 50


async def _find_dangling_lifecycle(ctx: DoctorContext) -> dict[str, list[dict]]:
    """COMPLETED tasks still holding state, and finished containers left open.

    Read-only.  ``owners``: unreleased branch-owner rows a COMPLETED task
    owns.  ``batches``: unsettled development batches (and active train
    batches) that list a COMPLETED task.  ``obsolete_pending``: obsolete
    closes whose cleanup is still pending.  ``containers``: open containers
    with children, all of them COMPLETED or FAILED.
    """
    found: dict[str, list[dict]] = {
        "owners": [], "batches": [], "obsolete_pending": [], "containers": [],
    }
    async with ctx.db._engine.connect() as conn:
        owners = integration_branch_owners
        for row in (
            await conn.execute(
                select(tasks.c.id, owners.c.id, owners.c.ref, owners.c.handoff_state)
                .join(owners, owners.c.owner_id == tasks.c.id)
                .where(
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    owners.c.handoff_state != "released",
                    owners.c.owner_role != "collector",
                )
                .order_by(tasks.c.id, owners.c.ref)
            )
        ).all():
            found["owners"].append(
                {"task_id": row[0], "owner_row_id": row[1], "ref": row[2], "handoff_state": row[3]}
            )
        unsettled = (
            await conn.execute(
                select(
                    development_deliveries.c.id,
                    development_deliveries.c.state,
                    development_deliveries.c.manifest,
                )
                .where(development_deliveries.c.state.notin_(SETTLED_DEVELOPMENT_DELIVERY_STATES))
                .order_by(development_deliveries.c.created_at, development_deliveries.c.id)
            )
        ).all()
        listed: dict[str, list[tuple[str, str]]] = {}
        for batch_id, state, manifest in unsettled:
            for member in manifest if isinstance(manifest, list) else []:
                if isinstance(member, dict) and member.get("task_id"):
                    listed.setdefault(str(member["task_id"]), []).append((batch_id, state))
        if listed:
            completed = set(
                (
                    await conn.execute(
                        select(tasks.c.id).where(
                            tasks.c.id.in_(sorted(listed)),
                            tasks.c.status == TaskStatus.COMPLETED.value,
                        )
                    )
                ).scalars()
            )
            for task_id in sorted(completed):
                for batch_id, state in listed[task_id]:
                    found["batches"].append(
                        {"task_id": task_id, "batch_id": batch_id, "state": state,
                         "kind": "development"}
                    )
        for row in (
            await conn.execute(
                select(tasks.c.id, integration_batches.c.id, integration_batches.c.lifecycle)
                .join(integration_batch_members, integration_batch_members.c.task_id == tasks.c.id)
                .join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id,
                )
                .where(
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    integration_batches.c.lifecycle.in_(_ACTIVE_TRAIN_LIFECYCLES),
                )
                .order_by(tasks.c.id)
            )
        ).all():
            found["batches"].append(
                {"task_id": row[0], "batch_id": row[1], "state": row[2], "kind": "train"}
            )
        for task_id, raw in (
            await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value)
                .where(task_metadata.c.key == OBSOLETE_META_KEY)
                .order_by(task_metadata.c.task_id)
            )
        ).all():
            try:
                cleanup = (json.loads(raw) or {}).get("cleanup") or {}
            except (TypeError, ValueError, AttributeError):
                continue
            if cleanup.get("state") != "done":
                found["obsolete_pending"].append(
                    {
                        "task_id": task_id,
                        "pending": [p.get("reason") for p in cleanup.get("pending") or []],
                    }
                )
        child = tasks.alias("lifecycle_child")
        open_child = tasks.alias("lifecycle_open_child")
        failed = (
            select(func.count(child.c.id))
            .where(child.c.parent_task_id == tasks.c.id, child.c.status == TaskStatus.FAILED.value)
            .scalar_subquery()
        )
        for row in (
            await conn.execute(
                select(tasks.c.id, tasks.c.status, failed.label("failed"))
                .where(
                    tasks.c.status.notin_(_FINISHED),
                    container_flag_exists(),
                    exists(select(literal(1)).where(child.c.parent_task_id == tasks.c.id)),
                    ~exists(
                        select(literal(1)).where(
                            open_child.c.parent_task_id == tasks.c.id,
                            open_child.c.status.notin_(_FINISHED),
                        )
                    ),
                )
                .order_by(tasks.c.id)
            )
        ).all():
            found["containers"].append(
                {"task_id": row[0], "status": row[1], "failed_children": int(row[2] or 0)}
            )
    return found


async def _check_dangling_lifecycle(ctx: DoctorContext) -> CheckResult:
    """Report finished work that still holds lifecycle state (report-only).

    Each finding is resolved by the subsystem that owns it, not by doctor:
    stale containers settle on the next sweep once every child is delivered;
    an owner row is released by `aq integration release-owner` or the
    `integration.finished_branch_owners` fix; superseded work is retired with
    `aq task close <id> --obsolete --reason`, which also drops it from parked
    batches.  Membership of a batch still publishing is only informational.
    """
    if ctx.db is None or getattr(ctx.db, "_engine", None) is None:
        return CheckResult(id=_LIFECYCLE_CHECK, severity=Severity.INFO, detail="database unavailable")
    found = await _find_dangling_lifecycle(ctx)
    in_flight = [b for b in found["batches"] if b["state"] in ("prepared", "publishing")]
    stuck_batches = [b for b in found["batches"] if b not in in_flight]
    counts = {
        "owners": len(found["owners"]),
        "stuck_batches": len(stuck_batches),
        "publishing_batches": len(in_flight),
        "obsolete_pending": len(found["obsolete_pending"]),
        "containers": len(found["containers"]),
    }
    data = {
        "counts": counts,
        **{key: rows[:_LIFECYCLE_LIMIT] for key, rows in found.items()},
    }
    if not any(counts.values()):
        return CheckResult(
            id=_LIFECYCLE_CHECK,
            severity=Severity.OK,
            detail="no finished task holds lifecycle state and no finished container is open",
        )
    parts = []
    if counts["containers"]:
        parts.append(f"{counts['containers']} open container(s) whose children are all done")
    if counts["owners"]:
        parts.append(f"{counts['owners']} branch-owner row(s) held by COMPLETED tasks")
    if counts["stuck_batches"]:
        parts.append(f"{counts['stuck_batches']} parked/active batch membership(s) of COMPLETED tasks")
    if counts["obsolete_pending"]:
        parts.append(f"{counts['obsolete_pending']} obsolete close(s) with cleanup pending")
    if counts["publishing_batches"]:
        parts.append(f"{counts['publishing_batches']} COMPLETED task(s) in a batch still publishing")
    stuck = counts["containers"] or counts["owners"] or counts["stuck_batches"] or counts[
        "obsolete_pending"
    ]
    return CheckResult(
        id=_LIFECYCLE_CHECK,
        severity=Severity.WARN if stuck else Severity.INFO,
        detail=(
            "; ".join(parts)
            + ". Superseded work: `aq task close <id> --obsolete --reason \"...\"`; owner rows: "
            "`aq integration release-owner --task-id <id>`; a container settles once every "
            "child is COMPLETED and delivered."
        ),
        data=data,
    )


def task_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="waits.pending_timers",
            run=_check_pending_timer_waits,
            owner="agent-waits",
        ),
        DoctorCheck(
            id="waits.pending_terminal_tasks",
            run=_check_pending_terminal_waits,
            owner="agent-waits",
        ),
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
        DoctorCheck(
            id=_LIFECYCLE_CHECK,
            run=_check_dangling_lifecycle,
            owner=OWNER,
            timeout_s=10.0,
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
