"""Report-only cleanup gate; repairs require recorded operator decisions."""

from src.asks.reconcile import diagnostics
from src.doctor.models import CheckResult, DoctorCheck, Severity


async def check(ctx):
    if ctx.db is None:
        return CheckResult(
            id="asks.cleanup_ready", severity=Severity.INFO, detail="Database not configured"
        )
    async with ctx.db._engine.connect() as conn:
        blockers = await diagnostics(conn, cleanup=True)
    return CheckResult(
        id="asks.cleanup_ready",
        severity=Severity.WARN if blockers else Severity.OK,
        detail="Cleanup refused: reconciliation evidence remains"
        if blockers
        else "No ledger blockers; operator compatibility and rollout evidence still required",
        data={"cleanup_ready": not blockers, "blockers": blockers},
    )


def ask_checks():
    return [DoctorCheck(id="asks.cleanup_ready", owner="asks", run=check)]
