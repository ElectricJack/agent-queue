"""``projects.*`` doctor checks for project-onboarding configuration."""

from __future__ import annotations

from src.config import ProjectRoot
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "project-onboarding"

#: Where an operator adds the first project root.  The installation wizard's
#: first-task readiness check names the same two destinations
#: (``src/install/wizard.py``); an operator who meets the gap on either surface
#: must be sent to the same place, so keep the wording in step.
PROJECT_ROOT_REMEDIATION = (
    "Add one under Settings → Project Roots in the dashboard, or put a `project_roots:` "
    "entry (`id`, `label`, `path`) in config.yaml with `aq system config edit`."
)


async def _check_project_roots(ctx: DoctorContext) -> CheckResult:
    """Report a missing project root, and roots that lost read access after load."""
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
    if not roots:
        # Not an ERROR: a machine that has not yet chosen where projects live
        # is under-configured, not broken -- the daemon runs fine without a
        # root.  But it is not OK either, because `aq project onboard` has no
        # `--root-id` to take, which is exactly what the installer's closing
        # summary reports as "Project root: needs attention".
        return CheckResult(
            id="projects.roots",
            severity=Severity.WARN,
            detail=(
                "No project root is configured, so `aq project onboard` has no `--root-id` "
                f"to onboard into. {PROJECT_ROOT_REMEDIATION}"
            ),
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
