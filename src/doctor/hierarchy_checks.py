"""hierarchy.* doctor checks (spec §16).  Registered by the core registry."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, literal, select, text, update

from src.database.tables import task_dependencies, tasks
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus


class _NoDatabase(Exception):
    """Raised internally when ``ctx.db`` has no usable engine."""


async def _rows(ctx: DoctorContext, stmt):
    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        raise _NoDatabase
    async with engine.begin() as conn:
        return (await conn.execute(stmt)).fetchall()


def _no_db_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="database not initialised — hierarchy state unknown",
    )


async def _check_parent_pointer(ctx: DoctorContext) -> CheckResult:
    pc = task_dependencies.alias()
    edge_parent = (
        select(pc.c.depends_on_task_id)
        .where(and_(pc.c.task_id == tasks.c.id, pc.c.dep_type == "parent-child"))
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(tasks.c.id, tasks.c.parent_task_id, edge_parent.label("edge")).where(
        func.coalesce(tasks.c.parent_task_id, "") != func.coalesce(edge_parent, "")
    )
    try:
        bad = await _rows(ctx, stmt)
    except _NoDatabase:
        return _no_db_result("hierarchy.parent_pointer")
    if not bad:
        return CheckResult(
            id="hierarchy.parent_pointer", severity=Severity.OK, detail="column matches edges"
        )
    return CheckResult(
        id="hierarchy.parent_pointer",
        severity=Severity.ERROR,
        detail=f"{len(bad)} task(s) whose parent_task_id disagrees with the parent-child edge",
        fixable=True,
        data={"tasks": [{"id": r[0], "column": r[1], "edge": r[2]} for r in bad[:50]]},
    )


async def _fix_parent_pointer(ctx: DoctorContext) -> CheckResult:
    pc = task_dependencies.alias()
    edge_parent = (
        select(pc.c.depends_on_task_id)
        .where(and_(pc.c.task_id == tasks.c.id, pc.c.dep_type == "parent-child"))
        .limit(1)
        .scalar_subquery()
    )
    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        return _no_db_result("hierarchy.parent_pointer")
    async with engine.begin() as conn:
        res = await conn.execute(update(tasks).values(parent_task_id=edge_parent))
    return CheckResult(
        id="hierarchy.parent_pointer",
        severity=Severity.OK,
        detail=f"rewrote parent_task_id from edges ({res.rowcount} row(s) touched)",
        fixable=True,
        fix_applied=True,
    )


async def _check_single_parent(ctx: DoctorContext) -> CheckResult:
    stmt = (
        select(task_dependencies.c.task_id, func.count())
        .where(task_dependencies.c.dep_type == "parent-child")
        .group_by(task_dependencies.c.task_id)
        .having(func.count() > 1)
    )
    try:
        bad = await _rows(ctx, stmt)
    except _NoDatabase:
        return _no_db_result("hierarchy.single_parent")
    sev = Severity.ERROR if bad else Severity.OK
    return CheckResult(
        id="hierarchy.single_parent",
        severity=sev,
        detail=f"{len(bad)} task(s) with more than one parent",
        data={"tasks": [r[0] for r in bad[:50]]},
    )


async def _check_depth(ctx: DoctorContext) -> CheckResult:
    # Four joins up is depth > 3.
    t1, t2, t3, t4 = (tasks.alias(f"t{i}") for i in range(1, 5))
    stmt = select(t1.c.id).select_from(
        t1.join(t2, t2.c.id == t1.c.parent_task_id)
        .join(t3, t3.c.id == t2.c.parent_task_id)
        .join(t4, t4.c.id == t3.c.parent_task_id)
    )
    try:
        bad = await _rows(ctx, stmt)
    except _NoDatabase:
        return _no_db_result("hierarchy.depth")
    return CheckResult(
        id="hierarchy.depth",
        severity=Severity.ERROR if bad else Severity.OK,
        detail=f"{len(bad)} task(s) deeper than 3",
        data={"tasks": [r[0] for r in bad[:50]]},
    )


async def _check_closed_container_children(ctx: DoctorContext) -> CheckResult:
    child = tasks.alias("child")
    terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
    stmt = select(tasks.c.id).where(
        and_(
            tasks.c.status == TaskStatus.COMPLETED.value,
            exists(
                select(literal(1)).where(
                    and_(child.c.parent_task_id == tasks.c.id, child.c.status.notin_(terminal))
                )
            ),
        )
    )
    try:
        bad = await _rows(ctx, stmt)
    except _NoDatabase:
        return _no_db_result("hierarchy.closed_container_children")
    return CheckResult(
        id="hierarchy.closed_container_children",
        severity=Severity.ERROR if bad else Severity.OK,
        detail=f"{len(bad)} COMPLETED container(s) with open children (invariant 6)",
        data={"containers": [r[0] for r in bad[:50]]},
    )


async def _check_migration_rejects(ctx: DoctorContext) -> CheckResult:
    try:
        rows = await _rows(
            ctx,
            text(
                "SELECT run_id, COUNT(*) FROM hierarchy_migration_rejects GROUP BY run_id ORDER BY run_id DESC"
            ),
        )
    except _NoDatabase:
        return _no_db_result("hierarchy.migration_rejects")
    count = sum(int(r[1]) for r in rows)
    if not count:
        return CheckResult(id="hierarchy.migration_rejects", severity=Severity.OK, detail="none")
    return CheckResult(
        id="hierarchy.migration_rejects",
        severity=Severity.WARN,
        detail=f"{count} rejected parent edge(s) from canonicalisation; re-attach with aq task reparent",
        data={"count": count, "latest_run_id": rows[0][0]},
    )


def hierarchy_checks() -> list[DoctorCheck]:
    owner = "swarm-work-model"
    return [
        DoctorCheck(
            id="hierarchy.parent_pointer",
            run=_check_parent_pointer,
            fix=_fix_parent_pointer,
            owner=owner,
        ),
        DoctorCheck(id="hierarchy.single_parent", run=_check_single_parent, owner=owner),
        DoctorCheck(id="hierarchy.depth", run=_check_depth, owner=owner),
        DoctorCheck(
            id="hierarchy.closed_container_children",
            run=_check_closed_container_children,
            owner=owner,
        ),
        DoctorCheck(id="hierarchy.migration_rejects", run=_check_migration_rejects, owner=owner),
    ]
