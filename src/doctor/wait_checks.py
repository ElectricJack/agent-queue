"""Cutover inventory and conservative repair of abandoned wait ownership."""

from __future__ import annotations

import dataclasses

from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from src.database.tables import metadata
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

WAIT_TABLES = tuple(name for name in metadata.tables if name.startswith("wait_")) + (
    "continuations", "admission_reservations",
)


async def _check_schema(ctx: DoctorContext) -> CheckResult:
    policy = ctx.config.wait_policy
    data = {"flags": {name: getattr(policy, name) for name in (
        "v2_enabled", "hibernation_enabled", "scheduler_waits_enabled", "v1_bodies",
    )}, "projects": {pid: dataclasses.asdict(policy.resolve_project(pid))
                     for pid in policy.projects}}
    engine = getattr(ctx.db, "_engine", None)
    if engine is None:
        return CheckResult("waits.schema", Severity.INFO, "database unavailable", data=data)
    try:
        async with engine.connect() as conn:
            present = await conn.run_sync(lambda bind: set(inspect(bind).get_table_names()))
            data["missing_tables"] = sorted(set(WAIT_TABLES) - present)
            data["revisions"] = list((await conn.execute(text(
                "SELECT version_num FROM alembic_version",
            ))).scalars()) if "alembic_version" in present else []
            data["row_counts"] = {name: await conn.scalar(select(func.count()).select_from(
                metadata.tables[name],
            )) for name in WAIT_TABLES if name in present}
    except SQLAlchemyError as exc:
        return CheckResult("waits.schema", Severity.ERROR, f"schema unreadable: {exc}", data=data)
    missing = data["missing_tables"]
    return CheckResult(
        "waits.schema", Severity.ERROR if missing else Severity.OK,
        "missing wait tables; operator must run aq db upgrade" if missing else
        f"wait schema present; revision(s) {data['revisions']}; flags {data['flags']}",
        data=data,
    )


async def _check_orphans(ctx: DoctorContext) -> CheckResult:
    from src.waits.recovery import orphan_report

    if getattr(ctx.db, "_engine", None) is None:
        return CheckResult("waits.orphans", Severity.INFO, "database unavailable")
    async with ctx.db._engine.connect() as conn:
        rows = await orphan_report(conn, project_id=ctx.project_id)
    return CheckResult(
        "waits.orphans", Severity.WARN if rows else Severity.OK,
        f"{len(rows)} orphan wait owner(s); --fix preserves summaries and saved heads"
        if rows else "no orphan wait owners",
        fixable=bool(rows), data={"orphans": rows},
    )


async def _fix_orphans(ctx: DoctorContext) -> CheckResult:
    # Repairs are reachable only through the mutating CommandHandler doctor path.
    if ctx.read_only or ctx.handler is None:
        return CheckResult("waits.orphans", Severity.ERROR,
                           "repair requires the CommandHandler doctor --fix boundary")
    result = await ctx.handler.repair_wait_orphans(project_id=ctx.project_id)
    remaining = await _check_orphans(ctx)
    remaining.fix_applied = bool(result["repaired"])
    remaining.data.update(result)
    return remaining


def wait_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck("waits.schema", run=_check_schema, owner="agent-waits", timeout_s=15),
        DoctorCheck("waits.orphans", run=_check_orphans, fix=_fix_orphans,
                    owner="agent-waits", timeout_s=30),
    ]
