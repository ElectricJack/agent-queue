"""``routing.bypassed``: is every task going through its project's router?

Mandatory task routing (spec 2026-09-28 §10) makes the router the only writer
of a worker route and binds every project to one.  This check reports what
would let work skip it, or leave it waiting:

| Finding | Severity |
|---|---|
| In a ready project, a queued task whose route is not ``router``, ``override`` or ``role`` | error |
| A project whose binding is missing, or names a missing or non-routing playbook | error (fixable) |
| A project whose router is not ready | error, naming the activation to fix |
| An unrouted queued task older than 15 min | warn, oldest ids with the explain reason |
| Open overrides | info, listed |
| In-flight legacy tasks, and pins lost at the cutover | info, counted |

The spec's "project with ``default_profile_id`` still set" finding ended with
revision ``a00000000043``, which drops the column.

``--fix`` binds every unbound, missing or non-routing binding to
``routing.default_router``.  It touches project rows only: doctor fixes never
edit tasks.  A project already bound to the default router is never "fixed" —
its router is not ready, and the activation named in the finding is the fix.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import and_, exists, func, literal, or_, select

from src.database.queries.hierarchy_queries import container_flag_exists
from src.database.tables import gates, projects, task_gates, tasks
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import ProjectStatus, TaskStatus
from src.routing.readiness import (
    BINDING_MISSING,
    BINDING_NOT_READY,
    BINDING_NOT_ROUTER,
    BINDING_READY,
    BINDING_UNBOUND,
    RouterReadiness,
    artifact_grants,
    binding_state,
)
from src.routing.sources import CLAIMABLE_SOURCES, LEGACY, OVERRIDE, UNROUTED

OWNER = "mandatory-routing"
CHECK_ID = "routing.bypassed"

#: An unrouted queued task older than this is reported (spec §10).
STALE_UNROUTED_SECONDS = 15 * 60
#: How many ids a finding lists.
_LIST = 20

_QUEUED = (TaskStatus.DEFINED.value, TaskStatus.READY.value, TaskStatus.BLOCKED.value)
_TERMINAL = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
#: Binding states ``--fix`` repairs by binding the default router.
_REBINDABLE = frozenset({BINDING_UNBOUND, BINDING_MISSING, BINDING_NOT_ROUTER})

_child = tasks.alias("routing_doctor_child")


class _NoDatabase(Exception):
    """``ctx.db`` has no usable engine."""


def _engine(ctx: DoctorContext):
    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        raise _NoDatabase
    return engine


def _default_router(ctx: DoctorContext) -> str:
    return str(ctx.config.routing.default_router)


async def _bindings(ctx: DoctorContext) -> list[dict[str, Any]]:
    """One row per project: its binding, the binding's state and what a fix would do."""
    project_rows = await ctx.db.list_projects()
    activations = await ctx.db.list_playbook_activations()
    grants_of = artifact_grants(getattr(ctx.handler, "orchestrator", None), ctx.config)
    default = _default_router(ctx)
    rows = []
    for project in project_rows:
        bound = str(getattr(project, "assignment_playbook_id", "") or "").strip()
        state, detail = binding_state(project.id, bound, activations, grants_of)
        if state in _REBINDABLE and bound == default:
            # Re-binding to the router it already names changes nothing: the
            # default router's own activation is what needs repair.
            state = BINDING_NOT_READY
        rows.append({
            "project_id": project.id,
            "status": getattr(project.status, "value", project.status),
            "router": bound or None,
            "state": state,
            "detail": detail,
        })
    return rows


def _unrouted_reason(binding: dict[str, Any] | None) -> str:
    """The explain reason (§10) an unrouted task in *binding*'s project waits on."""
    if binding is None or binding["state"] in _REBINDABLE:
        return "router_unbound"
    if binding["state"] != BINDING_READY:
        return "router_not_ready"
    return "awaiting_route"


def _open_routing_gate():
    return (
        select(literal(1))
        .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
        .where(
            task_gates.c.task_id == tasks.c.id,
            gates.c.status == "open",
            gates.c.gate_type == "routing",
        )
        .exists()
    )


async def _scan(ctx: DoctorContext, ready: list[str]) -> dict[str, Any]:
    """The task-side findings, read in one transaction."""
    now = time.time()
    bypass = (
        select(tasks.c.id, tasks.c.project_id, tasks.c.profile_id, tasks.c.route_source)
        .where(
            tasks.c.project_id.in_(ready),
            tasks.c.status.in_(_QUEUED),
            tasks.c.profile_id.is_not(None),
            tasks.c.route_source.notin_(CLAIMABLE_SOURCES),
        )
        .order_by(tasks.c.updated_at)
    )
    # What route-needed emission would pick up (I5), minus the label filters:
    # an unrouted task nothing routes for longer than one emission window
    # plus a playbook run is stuck, not waiting.
    stale = (
        select(tasks.c.id, tasks.c.project_id, tasks.c.updated_at)
        .join(projects, projects.c.id == tasks.c.project_id)
        .where(
            projects.c.status == ProjectStatus.ACTIVE.value,
            or_(
                tasks.c.status.in_([TaskStatus.READY.value, TaskStatus.BLOCKED.value]),
                and_(
                    tasks.c.status == TaskStatus.DEFINED.value,
                    or_(tasks.c.is_blocked == 0, _open_routing_gate()),
                ),
            ),
            tasks.c.assigned_agent_id.is_(None),
            tasks.c.is_plan_subtask == 0,
            tasks.c.route_source == UNROUTED,
            tasks.c.updated_at < now - STALE_UNROUTED_SECONDS,
            ~container_flag_exists(),
            ~exists(select(literal(1)).where(_child.c.parent_task_id == tasks.c.id)),
        )
        .order_by(tasks.c.updated_at)
    )
    overrides = (
        select(tasks.c.id, tasks.c.project_id, tasks.c.profile_id, tasks.c.status)
        .where(tasks.c.route_source == OVERRIDE, tasks.c.status.notin_(_TERMINAL))
        .order_by(tasks.c.updated_at)
    )
    in_flight_legacy = select(func.count()).select_from(tasks).where(
        tasks.c.route_source == LEGACY,
        tasks.c.status.notin_((*_QUEUED, *_TERMINAL)),
    )
    pins_lost = select(func.count()).select_from(tasks).where(
        tasks.c.route["legacy"]["provider_intent"].astext == "pinned"
    )
    async with _engine(ctx).connect() as conn:
        return {
            "bypass": (await conn.execute(bypass)).mappings().fetchall(),
            "stale": (await conn.execute(stale)).mappings().fetchall(),
            "overrides": (await conn.execute(overrides)).mappings().fetchall(),
            "in_flight_legacy": int((await conn.execute(in_flight_legacy)).scalar_one()),
            "pins_lost": int((await conn.execute(pins_lost)).scalar_one()),
            "now": now,
        }


async def _check_bypassed(ctx: DoctorContext) -> CheckResult:
    try:
        _engine(ctx)
    except _NoDatabase:
        return CheckResult(
            id=CHECK_ID, severity=Severity.INFO,
            detail="database not initialised — routing state unknown",
        )
    bindings = await _bindings(ctx)
    by_project = {row["project_id"]: row for row in bindings}
    ready = sorted(row["project_id"] for row in bindings if row["state"] == BINDING_READY)
    scan = await _scan(ctx, ready)

    unbound = [row for row in bindings if row["state"] in _REBINDABLE]
    not_ready = [row for row in bindings if row["state"] == BINDING_NOT_READY]
    bypass = [
        {"task_id": r["id"], "project_id": r["project_id"], "profile_id": r["profile_id"],
         "route_source": r["route_source"]}
        for r in scan["bypass"]
    ]
    stale = [
        {"task_id": r["id"], "project_id": r["project_id"],
         "unrouted_for_s": int(scan["now"] - float(r["updated_at"] or 0)),
         "reason": _unrouted_reason(by_project.get(r["project_id"]))}
        for r in scan["stale"]
    ]
    overrides = [
        {"task_id": r["id"], "project_id": r["project_id"], "profile_id": r["profile_id"],
         "status": r["status"]}
        for r in scan["overrides"]
    ]

    errors: list[str] = []
    if bypass:
        errors.append(
            f"{len(bypass)} queued task(s) in a ready project carry a route the router "
            f"did not write ({', '.join(b['task_id'] for b in bypass[:5])})"
        )
    if unbound:
        errors.append(
            f"{len(unbound)} project(s) not bound to a routing playbook "
            f"({', '.join(r['project_id'] for r in unbound[:5])}); --fix binds them to "
            f"'{_default_router(ctx)}'"
        )
    if not_ready:
        errors.append(
            f"{len(not_ready)} project(s) whose router is not ready: {not_ready[0]['detail']}"
        )
    notes: list[str] = []
    if stale:
        notes.append(
            f"{len(stale)} unrouted queued task(s) older than "
            f"{STALE_UNROUTED_SECONDS // 60} min (oldest {stale[0]['task_id']}: "
            f"{stale[0]['reason']})"
        )
    info: list[str] = []
    if overrides:
        info.append(f"{len(overrides)} open override(s)")
    if scan["in_flight_legacy"]:
        info.append(f"{scan['in_flight_legacy']} in-flight legacy task(s)")
    if scan["pins_lost"]:
        info.append(f"{scan['pins_lost']} pin(s) lost at the cutover")

    if errors:
        severity = Severity.ERROR
    elif notes:
        severity = Severity.WARN
    elif info:
        severity = Severity.INFO
    else:
        severity = Severity.OK
    detail = "; ".join(errors + notes + info) or (
        f"{len(bindings)} project(s) bound to a ready router; nothing bypasses it"
    )
    return CheckResult(
        id=CHECK_ID,
        severity=severity,
        detail=detail,
        fixable=bool(unbound),
        data={
            "bypass": bypass[:_LIST],
            "bypass_count": len(bypass),
            "unbound_projects": unbound,
            "not_ready_projects": not_ready,
            "stale_unrouted": stale[:_LIST],
            "stale_unrouted_count": len(stale),
            "open_overrides": overrides[:_LIST],
            "open_override_count": len(overrides),
            "in_flight_legacy": scan["in_flight_legacy"],
            "pins_lost": scan["pins_lost"],
            "ready_projects": ready,
        },
    )


async def _fix_unbound(ctx: DoctorContext) -> CheckResult:
    """Bind every unbound, missing or non-routing binding to the default router."""
    try:
        _engine(ctx)
    except _NoDatabase:
        return CheckResult(
            id=CHECK_ID, severity=Severity.INFO,
            detail="database not initialised — routing state unknown",
        )
    default = _default_router(ctx)
    rebound = []
    for row in await _bindings(ctx):
        if row["state"] in _REBINDABLE:
            await ctx.db.update_project(row["project_id"], assignment_playbook_id=default)
            rebound.append({"project_id": row["project_id"], "from": row["router"]})
    readiness = getattr(getattr(ctx.handler, "orchestrator", None), "router_readiness", None)
    if rebound and isinstance(readiness, RouterReadiness):
        await readiness.refresh()
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail=f"bound {len(rebound)} project(s) to '{default}'",
        fixable=True,
        fix_applied=bool(rebound),
        data={"rebound_projects": rebound},
    )


def routing_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID, run=_check_bypassed, fix=_fix_unbound, owner=OWNER, timeout_s=15.0,
        )
    ]


CHECKS = routing_checks()
