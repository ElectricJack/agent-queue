"""``profiles.*`` doctor checks — system profile drift.

``vault.ensure_default_profiles()`` never overwrites an existing
``vault/agent-types/<id>/profile.md``, so a system profile seeded by an
older release keeps its old schema and old semantics forever.  That is the
right default for operator edits and the wrong default for load-bearing
``## Config`` fields: a stale ``read_only: false`` on ``reviewer`` re-arms
the require-a-PR close gate for a session that is told never to push
(``src/orchestrator/git_ops.py`` ``_task_produces_no_code``).

Report-only by design.  There is no ``--fix``: overwriting the vault copy
would silently discard operator edits, so the repair is the explicit
``profile_reseed`` command (``aq agent profile-reseed <id>``), which writes
a ``.bak-<epoch>`` first.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.profiles.drift import STATUS_UNREADABLE, scan_profile_drift

OWNER = "profiles"

CHECK_ID = "profiles.system_drift"

#: How many per-profile summaries the one-line ``detail`` names before it
#: defers to ``data["profiles"]``.
_MAX_SUMMARIES = 3


async def _check_system_profile_drift(ctx: DoctorContext) -> CheckResult:
    data_dir = getattr(ctx.config, "data_dir", "") or ""
    if not data_dir:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="no data_dir configured")

    try:
        drifts = scan_profile_drift(data_dir)
    except OSError as exc:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=f"could not scan system profiles: {exc}",
        )

    if not drifts:
        return CheckResult(
            id=CHECK_ID, severity=Severity.INFO, detail="no shipped system profiles found"
        )

    diverged = [d for d in drifts if d.is_drifted]
    if not diverged:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"{len(drifts)} system profile(s) match the shipped defaults",
        )

    unreadable = [d for d in diverged if d.status == STATUS_UNREADABLE]
    severity = Severity.ERROR if unreadable else Severity.WARN
    # A whole fleet can drift at once (one upgrade touching every shipped
    # profile), so the terminal line names a few and ``data`` carries the rest.
    shown = "; ".join(d.summary() for d in diverged[:_MAX_SUMMARIES])
    if len(diverged) > _MAX_SUMMARIES:
        shown += f"; +{len(diverged) - _MAX_SUMMARIES} more (see --json)"
    detail = (
        f"{len(diverged)} of {len(drifts)} system profile(s) diverge from the shipped "
        f"default: {shown}. "
        "Reseed one with `aq agent profile-reseed <id>` (writes a .bak first)."
    )
    return CheckResult(
        id=CHECK_ID,
        severity=severity,
        detail=detail,
        data={
            "checked": len(drifts),
            "drifted": len(diverged),
            "profiles": [d.to_dict() for d in diverged],
        },
    )


def profile_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=CHECK_ID, run=_check_system_profile_drift, fix=None, owner=OWNER),
    ]
