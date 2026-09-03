"""``sessions.env_markers`` doctor check for inherited harness state."""

from __future__ import annotations

import os

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.env_scrub import harness_session_markers

OWNER = "session-runtime"
CHECK_ID = "sessions.env_markers"


async def _check_env_markers(ctx: DoctorContext) -> CheckResult:
    """Report control variables that should have been scrubbed at boot."""
    markers = harness_session_markers(os.environ)
    if markers:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.ERROR,
            detail=(
                "daemon inherited harness session marker(s): "
                + ", ".join(markers)
                + "; restart with a clean environment"
            ),
            data={"markers": markers},
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail="daemon environment has no inherited harness session markers",
    )


def session_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check_env_markers, owner=OWNER)]

