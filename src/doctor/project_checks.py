"""``projects.*`` doctor checks for project-onboarding configuration."""

from __future__ import annotations

from src.config import ProjectRoot
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.projects.roots import ProjectRootsState, RootFacts, assess_project_roots

OWNER = "project-onboarding"


#: How each shared verdict reads to an operator running ``aq doctor``.
#:
#: Neither warning is an ERROR: a machine that has not chosen where projects
#: live is under-configured rather than broken, and a read-only root can still
#: take a ``link``-mode onboard.  A root that vanished or lost read access *is*
#: an error — configuration names a place that is no longer there.
_SEVERITY: dict[ProjectRootsState, Severity] = {
    ProjectRootsState.OK: Severity.OK,
    ProjectRootsState.MISSING: Severity.WARN,
    ProjectRootsState.UNREADABLE: Severity.ERROR,
    ProjectRootsState.UNUSABLE: Severity.WARN,
}


async def _check_project_roots(ctx: DoctorContext) -> CheckResult:
    """Report whether any configured root can actually take a new project.

    The verdict comes from :func:`~src.projects.roots.assess_project_roots`,
    the same function the installation wizard's first-task readiness check
    uses, so the two surfaces cannot disagree about one configuration the way
    they did when each carried its own copy of the rule.
    """
    roots: list[ProjectRoot] = getattr(ctx.config, "project_roots", [])
    assessment = assess_project_roots(
        RootFacts(id=root.id, path=root.path, readable=root.readable, writable=root.writable)
        for root in roots
    )
    data = {
        "state": assessment.state.value,
        "roots": [
            {
                "id": root.id,
                "label": root.label,
                "path": root.path,
                "readable": root.readable,
                "writable": root.writable,
            }
            for root in roots
        ],
    }
    detail = assessment.detail
    if assessment.remediation:
        detail = f"{detail} {assessment.remediation}"
    return CheckResult(
        id="projects.roots",
        severity=_SEVERITY[assessment.state],
        detail=detail,
        data=data,
    )


def project_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id="projects.roots", run=_check_project_roots, owner=OWNER)]


CHECKS = project_checks()
