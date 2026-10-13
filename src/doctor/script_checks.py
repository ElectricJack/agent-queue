"""Read-only script prerequisite report. Installation is always operator-owned."""

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.jobs.script_sandbox import probe_launcher


async def check_script_sandbox(ctx: DoctorContext) -> CheckResult:
    settings = ctx.config.resources.scripts
    if not settings.enabled:
        return CheckResult(id="resources.script_sandbox", severity=Severity.INFO,
                           detail="script execution is disabled", data={"ready": False})
    if not ctx.config.resources.jobs.enabled:
        result = {"ready": False, "reason": "jobs_disabled"}
    else:
        result = await probe_launcher(settings)
    return CheckResult(
        id="resources.script_sandbox", severity=Severity.OK if result["ready"] else Severity.ERROR,
        detail="script sandbox prerequisites verified" if result["ready"] else
        "script admission denied: " + result.get("reason", "prerequisites unavailable"), data=result,
    )


def script_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id="resources.script_sandbox", run=check_script_sandbox,
                        owner="playbook-scripts", timeout_s=35)]
