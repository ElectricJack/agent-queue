"""``profiles.*`` doctor checks — system profile drift and retired overrides.

``profiles.system_drift``

``vault.ensure_default_profiles()`` never overwrites an existing
``vault/agent-types/<id>/profile.md``, so a system profile seeded by an
older release keeps its old schema and old semantics forever.  That is the
right default for operator edits and the wrong default for load-bearing
``## Config`` fields: a stale ``read_only: false`` on ``reviewer`` re-arms
the require-a-PR close gate for a session that is told never to push
(``src/orchestrator/git_ops.py`` ``_task_produces_no_code``).  The same blind
spot applies to capability grants: a shipped release adding a command to
``## Capabilities.aq_commands`` never reaches a vault copy that already
exists, so this check also reports ``missing_grants`` per profile
(:func:`src.profiles.drift.diff_profile`).

Report-only by design.  There is no ``--fix``: overwriting the vault copy
would silently discard operator edits, so the repair is the explicit
``profile_reseed`` command.  A full ``aq agent profile-reseed <id>``
overwrites the whole file and writes a ``.bak-<epoch>`` first; when the only
divergence is missing grants, ``aq agent profile-reseed --profile-id <id>
--grants-only`` (:func:`src.profiles.drift.merge_profile_grants`) instead
merges just the missing names into the vault copy's own ``## Capabilities``
block, preserving operator edits such as ``harness: codex``.

``profiles.supervisor_capability_drift``

The supervisor is the one profile whose shipped grants are merged into its
vault copy automatically (:mod:`src.profiles.capability_sync`, on daemon
start and on every vault reload), because each control a release added for
it used to stay denied until someone hand-edited the file.  This check names
the shipped capabilities the vault copy still lacks — between an upgrade and
the next start, or while the operator has opted out with ``capability_sync:
false`` in its frontmatter — and ``--fix`` runs the same additive merge
(nothing removed, other sections untouched, ``.bak-<epoch>`` kept).  An
opted-out profile reports ``info`` and is never merged by ``--fix``.

``profiles.project_overrides``
Project-scoped profiles were retired: agents are shared between projects, so
pool lifecycle and sizing belong on the system profile.  This check finds
overrides an older release left behind — they no longer resolve, so their
configuration has silently stopped applying — and ``--fix`` promotes each
one into its system profile before deleting it
(:mod:`src.profiles.project_override_migration`).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.profiles.capability_sync import (
    STATUS_FAILED,
    capability_sync_enabled,
    publish_sync_result,
    sync_profile_capabilities,
)
from src.profiles.drift import (
    STATUS_NOT_SEEDED,
    STATUS_RETIRED,
    STATUS_UNREADABLE,
    diff_profile,
    scan_profile_drift,
    vault_profile_path,
)
from src.profiles.project_override_migration import (
    find_project_override_paths,
    project_override_profile_id,
    promote_project_profile_overrides,
)

OWNER = "profiles"

CHECK_ID = "profiles.system_drift"

OVERRIDES_CHECK_ID = "profiles.project_overrides"

SUPERVISOR_DRIFT_CHECK_ID = "profiles.supervisor_capability_drift"

SUPERVISOR_PROFILE_ID = "supervisor"

logger = logging.getLogger(__name__)

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
    if any(d.missing_grants for d in diverged):
        detail += (
            " Add missing grants with `aq agent profile-reseed --profile-id <id> "
            "--grants-only` (keeps your edits, writes a .bak first)."
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


def _missing_summary(missing: dict[str, list[str]]) -> tuple[int, str]:
    names = [name for names in missing.values() for name in names]
    return len(names), ", ".join(names)


async def _check_supervisor_capability_drift(ctx: DoctorContext) -> CheckResult:
    """Report shipped supervisor capabilities its vault copy does not grant."""
    check_id = SUPERVISOR_DRIFT_CHECK_ID
    data_dir = getattr(ctx.config, "data_dir", "") or ""
    if not data_dir:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="no data_dir configured")

    drift = diff_profile(SUPERVISOR_PROFILE_ID, data_dir)
    if drift.status in (STATUS_NOT_SEEDED, STATUS_RETIRED):
        return CheckResult(id=check_id, severity=Severity.INFO, detail=drift.summary())
    if drift.status == STATUS_UNREADABLE:
        return CheckResult(
            id=check_id,
            severity=Severity.WARN,
            detail=(
                f"cannot compare the supervisor's capabilities: {'; '.join(drift.errors)}. "
                "Fix the vault file by hand (see `aq doctor --check profiles.system_drift`)."
            ),
            data={"profile_id": SUPERVISOR_PROFILE_ID, "errors": list(drift.errors)},
        )
    if "capabilities" in drift.missing_sections:
        return CheckResult(
            id=check_id,
            severity=Severity.WARN,
            detail=(
                "the supervisor's vault profile has no '## Capabilities' block to merge "
                "shipped grants into; restore it with `aq agent profile-reseed "
                "--profile-id supervisor` (writes a .bak first)"
            ),
            data={"profile_id": SUPERVISOR_PROFILE_ID, "missing_sections": ["capabilities"]},
        )

    vault_path = vault_profile_path(data_dir, SUPERVISOR_PROFILE_ID)
    try:
        vault_text = Path(vault_path).read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            id=check_id, severity=Severity.WARN, detail=f"cannot read {vault_path}: {exc}"
        )
    enabled = capability_sync_enabled(SUPERVISOR_PROFILE_ID, vault_text)
    missing = drift.missing_grants
    data = {
        "profile_id": SUPERVISOR_PROFILE_ID,
        "vault_path": vault_path,
        "capability_sync": enabled,
        "missing": {ns: list(names) for ns, names in missing.items()},
    }

    if not missing:
        detail = "the supervisor's vault profile grants every shipped capability"
        if not enabled:
            detail += " (capability_sync: false — shipped additions are not merged)"
        return CheckResult(id=check_id, severity=Severity.OK, detail=detail, data=data)

    count, names = _missing_summary(missing)
    if not enabled:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail=(
                f"{count} shipped capability grant(s) are not in the supervisor's vault "
                f"profile: {names}. Its frontmatter says capability_sync: false, so they "
                "are not merged; add them by hand or run `aq agent profile-reseed "
                "--profile-id supervisor --grants-only`."
            ),
            data=data,
        )
    return CheckResult(
        id=check_id,
        severity=Severity.WARN,
        detail=(
            f"{count} shipped capability grant(s) missing from the supervisor's vault "
            f"profile: {names}. The daemon merges them on its next start or profile "
            "reload; `aq doctor --check profiles.supervisor_capability_drift --fix` "
            "merges them now (additive, writes a .bak first)."
        ),
        data=data,
    )


async def _fix_supervisor_capability_drift(ctx: DoctorContext) -> CheckResult:
    """Run the additive capability sync for the supervisor, then sync the DB."""
    from src.profiles.sync import sync_profile_text_to_db

    check_id = SUPERVISOR_DRIFT_CHECK_ID
    data_dir = getattr(ctx.config, "data_dir", "") or ""
    if not data_dir:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="no data_dir configured")

    result = await asyncio.to_thread(sync_profile_capabilities, data_dir, SUPERVISOR_PROFILE_ID)
    if result.status == STATUS_FAILED:
        raise RuntimeError(result.error)

    if result.changed:
        if ctx.db is not None:
            vault_path = vault_profile_path(data_dir, SUPERVISOR_PROFILE_ID)
            markdown = Path(vault_path).read_text(encoding="utf-8")
            sync = await sync_profile_text_to_db(
                markdown, ctx.db, source_path=vault_path, fallback_id=SUPERVISOR_PROFILE_ID
            )
            if not sync.success:
                logger.warning(
                    "supervisor capability fix: DB sync failed: %s", "; ".join(sync.errors)
                )
        orchestrator = getattr(ctx.handler, "orchestrator", None)
        await publish_sync_result(
            result, event_bus=getattr(orchestrator, "bus", None), trigger="doctor"
        )
        count, names = _missing_summary(result.added)
        detail = f"added {count} shipped capability grant(s) to the supervisor: {names}"
    else:
        detail = f"nothing merged ({result.status})"
    return CheckResult(
        id=check_id,
        severity=Severity.OK,
        detail=detail,
        fixable=True,
        fix_applied=result.changed,
        data=result.to_dict(),
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
            id=SUPERVISOR_DRIFT_CHECK_ID,
            run=_check_supervisor_capability_drift,
            fix=_fix_supervisor_capability_drift,
            owner=OWNER,
        ),
        DoctorCheck(
            id=OVERRIDES_CHECK_ID,
            run=_check_project_overrides,
            fix=_fix_project_overrides,
            owner=OWNER,
        ),
    ]
