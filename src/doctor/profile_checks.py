"""``profiles.*`` doctor checks — system profile drift and retired overrides.

``profiles.system_drift``

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

``profiles.project_overrides``
Project-scoped profiles were retired: agents are shared between projects, so
pool lifecycle and sizing belong on the system profile.  This check finds
overrides an older release left behind — they no longer resolve, so their
configuration has silently stopped applying — and ``--fix`` promotes each
one into its system profile before deleting it
(:mod:`src.profiles.project_override_migration`).
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.profiles.drift import STATUS_UNREADABLE, scan_profile_drift
from src.profiles.project_override_migration import (
    find_project_override_paths,
    project_override_profile_id,
    promote_project_profile_overrides,
)

OWNER = "profiles"

CHECK_ID = "profiles.system_drift"

OVERRIDES_CHECK_ID = "profiles.project_overrides"

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


async def _override_row_ids(ctx: DoctorContext) -> list[str]:
    """Legacy ``project:<pid>:<type>`` rows still in ``agent_profiles``."""
    if ctx.db is None:
        return []
    return sorted(
        p.id for p in await ctx.db.list_profiles() if project_override_profile_id(p.id)
    )


async def _check_project_overrides(ctx: DoctorContext) -> CheckResult:
    """Report project-scoped profiles left over from before they were retired.

    Project-scoped profiles no longer resolve: a shared worker serves several
    projects, so pool lifecycle and sizing live on the system profile.  Any
    override still on disk or in the database is inert configuration that
    silently stops applying, which is exactly the kind of thing an operator
    should be told about rather than discover from a pool that never fills.
    """
    data_dir = getattr(ctx.config, "data_dir", "") or ""
    if not data_dir:
        return CheckResult(
            id=OVERRIDES_CHECK_ID, severity=Severity.INFO, detail="no data_dir configured"
        )

    try:
        paths = find_project_override_paths(data_dir)
    except OSError as exc:
        return CheckResult(
            id=OVERRIDES_CHECK_ID,
            severity=Severity.WARN,
            detail=f"could not scan for project profile overrides: {exc}",
            fixable=True,
        )
    rows = await _override_row_ids(ctx)

    if not paths and not rows:
        return CheckResult(
            id=OVERRIDES_CHECK_ID,
            severity=Severity.OK,
            detail="no project-scoped profile overrides remain",
        )

    named = ", ".join(
        f"{project}/{agent_type}" for project, agent_type, _ in paths[:_MAX_SUMMARIES]
    )
    if len(paths) > _MAX_SUMMARIES:
        named += f"; +{len(paths) - _MAX_SUMMARIES} more (see --json)"
    detail = (
        f"{len(paths)} project profile override file(s) and {len(rows)} legacy "
        f"agent_profiles row(s) remain and no longer resolve"
        + (f": {named}. " if named else ". ")
        + "Run `aq doctor --check profiles.project_overrides --fix` to promote each "
        "override's ## Config into its system profile and delete it."
    )
    return CheckResult(
        id=OVERRIDES_CHECK_ID,
        severity=Severity.WARN,
        detail=detail,
        fixable=True,
        data={
            "override_paths": [str(path) for _, _, path in paths],
            "profile_rows": rows,
        },
    )


async def _fix_project_overrides(ctx: DoctorContext) -> CheckResult:
    """Promote every remaining override, then drop its ``agent_profiles`` row."""
    from src.profiles.project_override_migration import delete_project_override_rows

    data_dir = getattr(ctx.config, "data_dir", "") or ""
    report = promote_project_profile_overrides(data_dir) if data_dir else {
        "success": True, "promoted": 0, "failed": 0, "details": [], "promotions": []
    }
    deleted = await delete_project_override_rows(ctx.db) if ctx.db is not None else []

    severity = Severity.OK if report["success"] else Severity.WARN
    detail = (
        f"promoted {report['promoted']} override(s) into their system profiles, "
        f"removed {len(deleted)} legacy profile row(s)"
    )
    if report["failed"]:
        detail += f"; {report['failed']} could not be promoted — see --json"
    return CheckResult(
        id=OVERRIDES_CHECK_ID,
        severity=severity,
        detail=detail,
        fixable=True,
        fix_applied=True,
        data={**report, "deleted_rows": deleted},
    )


def profile_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=CHECK_ID, run=_check_system_profile_drift, fix=None, owner=OWNER),
        DoctorCheck(
            id=OVERRIDES_CHECK_ID,
            run=_check_project_overrides,
            fix=_fix_project_overrides,
            owner=OWNER,
        ),
    ]
