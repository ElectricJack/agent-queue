"""``projects.*`` doctor checks for project-onboarding configuration."""

from __future__ import annotations

from src.config import ProjectRoot
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "project-onboarding"


async def _check_project_roots(ctx: DoctorContext) -> CheckResult:
    """Report configured roots that disappeared or lost read access after load."""
    roots: list[ProjectRoot] = getattr(ctx.config, "project_roots", [])
    unavailable = [root for root in roots if not root.readable]
    data = {
        "roots": [
            {
                "id": root.id,
                "label": root.label,
                "path": root.path,
                "readable": root.readable,
                "writable": root.writable,
            }
            for root in roots
        ]
    }
    if unavailable:
        affected = ", ".join(f"{root.id} ({root.path})" for root in unavailable)
        return CheckResult(
            id="projects.roots",
            severity=Severity.ERROR,
            detail=f"{len(unavailable)} project root(s) missing or unreadable: {affected}",
            data=data,
        )
    return CheckResult(
        id="projects.roots",
        severity=Severity.OK,
        detail=f"{len(roots)} configured project root(s) are readable",
        data=data,
    )


def project_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id="projects.roots", run=_check_project_roots, owner=OWNER)]


CHECKS = project_checks()

