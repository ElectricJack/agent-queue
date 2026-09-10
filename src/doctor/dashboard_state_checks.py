"""Integrity check for project-keyed dashboard state documents."""

from __future__ import annotations

from src.dashboard_state.namespaces import NAMESPACES
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

CHECK_ID = "dashboard_state.orphans"
OWNER = "dashboard-state"


def _project_namespaces() -> tuple[str, ...]:
    return tuple(spec.name for spec in NAMESPACES.values() if spec.subject == "project")


async def _check_orphans(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="database not configured; dashboard-state orphans not checked",
        )
    rows = await ctx.db.list_orphan_dashboard_documents(project_namespaces=_project_namespaces())
    if not rows:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail="no orphaned project-keyed dashboard state documents",
        )
    addresses = [
        {
            "scope": row["scope"],
            "owner_id": row["owner_id"],
            "namespace": row["namespace"],
            "subject": row["subject"],
            "revision": row["revision"],
        }
        for row in rows
    ]
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        detail=f"{len(rows)} project-keyed dashboard state document(s) are orphaned",
        fixable=True,
        data={"count": len(rows), "documents": addresses},
    )


async def _fix_orphans(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return await _check_orphans(ctx)
    deleted = await ctx.db.delete_orphan_dashboard_documents(
        project_namespaces=_project_namespaces()
    )
    result = await _check_orphans(ctx)
    result.fix_applied = deleted > 0
    result.data["deleted"] = deleted
    return result


def dashboard_state_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check_orphans,
            fix=_fix_orphans,
            owner=OWNER,
        )
    ]


CHECKS = dashboard_state_checks()
