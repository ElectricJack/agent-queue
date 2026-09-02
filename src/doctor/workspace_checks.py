"""``workspaces.*`` doctor checks (workspaces-v2, worktree-execution §2.2).

Shape mirrors ``src/doctor/pool_checks.py``: a private ``_check_*`` per
check, a factory returning the :class:`DoctorCheck` list, a ``CHECKS``
snapshot and a ``run_check`` wrapper for tests.

``workspaces.base_sessions`` is the observability half of the base-checkout
guard in :mod:`src.orchestrator.base_workspace`.  The guard refuses *new*
launches; this check answers "is anything running in a base right now?" —
which covers sessions started before the guard existed, sessions started by
a profile that opted in with ``allow_base_checkout: true``, and any launch
path that does not route through the orchestrator's two guarded entries
(named sessions, an operator's ``aq session start``).

It is registered unconditionally rather than behind ``worktrees.enabled``:
an install that turns worktrees off keeps its slot rows, so its base rows —
and any session sitting in one — stay just as reportable.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "workspaces-v2"


def _no_db_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="database not initialised — workspace state unknown",
    )


# ---------------------------------------------------------------------------
# workspaces.base_sessions
# ---------------------------------------------------------------------------


async def _find_base_sessions(ctx: DoctorContext) -> list[dict]:
    """Live sessions whose ``work_dir`` is a base workspace."""
    from src.orchestrator.base_workspace import (
        base_workspace_paths,
        normalize_workspace_path,
    )

    bases = await base_workspace_paths(ctx.db)
    if not bases:
        return []

    offenders: list[dict] = []
    for session in await ctx.db.list_sessions(live_only=True):
        base = bases.get(normalize_workspace_path(session.work_dir))
        if base is None:
            continue
        offenders.append(
            {
                "session_id": session.id,
                "session_name": session.name,
                "profile_id": session.profile_id,
                "lifecycle": session.lifecycle,
                "project_id": session.project_id,
                "task_id": session.task_id,
                "work_dir": session.work_dir,
                "workspace_id": base.id,
            }
        )
    return offenders


async def _check_workspaces_base_sessions(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("workspaces.base_sessions")
    offenders = await _find_base_sessions(ctx)
    if not offenders:
        return CheckResult(
            id="workspaces.base_sessions",
            severity=Severity.OK,
            detail="no live session is running in a base checkout",
        )
    detail = (
        f"{len(offenders)} live session(s) running in a base checkout — the base is "
        "reserved for fetch and `git worktree` bookkeeping and is often the "
        "operator's own working tree: "
        + ", ".join(f"{o['session_name']} in {o['work_dir']}" for o in offenders[:5])
    )
    return CheckResult(
        id="workspaces.base_sessions",
        # ERROR, not WARN: an agent with write tools pointed at a human's
        # checkout is the failure this whole guard exists to prevent.
        severity=Severity.ERROR,
        detail=detail,
        data={"count": len(offenders), "sessions": offenders},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def workspace_checks() -> list[DoctorCheck]:
    """The workspaces-v2 checks, for registration in the core registry."""
    return [
        DoctorCheck(
            id="workspaces.base_sessions",
            run=_check_workspaces_base_sessions,
            owner=OWNER,
        ),
    ]


#: Snapshot of the catalog, for tests and one-off invocation.
CHECKS = workspace_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None) -> CheckResult:
    """Run one workspace check directly against *db* (no registry needed).

    No ``repair``: stopping a session or re-homing a task is an operator
    call, not something ``--fix`` may do behind their back.
    """
    return await _BY_ID[check_id].run(DoctorContext(config=config, db=db))
