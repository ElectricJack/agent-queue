"""tasks.awaiting_plan_approval — plan discovery was deleted (llm-direct-path §6.3);
rows left in that state need a human to reopen or close them.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus

OWNER = "llm-direct-path"
CHECK_ID = "tasks.awaiting_plan_approval"


async def _check(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="database not initialised")
    rows = await ctx.db.list_tasks(status=TaskStatus.AWAITING_PLAN_APPROVAL)
    ids = [t.id for t in rows]
    if not ids:
        return CheckResult(
            id=CHECK_ID, severity=Severity.OK, detail="no stranded plan-approval tasks"
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(ids)} task(s) in AWAITING_PLAN_APPROVAL; plan discovery was removed — "
            "`aq task reopen <id>` to run them again or `aq task close <id>` to drop them"
        ),
        data={"count": len(ids), "tasks": ids[:50]},
    )


def plan_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check, owner=OWNER)]


CHECKS = plan_checks()


async def run_check(check_id: str, ctx: DoctorContext) -> CheckResult:
    for c in CHECKS:
        if c.id == check_id:
            return await c.run(ctx)
    raise KeyError(check_id)
