"""``sessions.*`` doctor checks (session-runtime).

Includes inherited harness-environment detection and stuck-composer recovery.

Shape mirrors ``src/doctor/pool_checks.py``: a private ``_find_*`` /
``_check_*`` / ``_fix_*`` trio plus a factory returning the list of
:class:`DoctorCheck`.

sessions.stuck_composer — the rule
----------------------------------

A nudge is *typed* into the harness's composer and then submitted with
Enter.  Enter races the composer's repaint, and when it loses, the text
stays in the input line.  That is not a self-healing state: the next nudge
runs ``TmuxProvider._require_empty_composer``, refuses to type into a
non-empty composer, and defers — forever.  The stall ladder stops climbing
and the message is never seen, which is precisely what a single manual
``tmux send-keys Enter`` fixes in a second.

The provider remembers the marker of any nudge it typed and could not
confirm, so this check is a read of provider state plus one screen capture
per suspect session — never a scan of every pane.  A session is reported
only while the composer *still* shows that marker on its input line; an
agent that submitted or deleted the text in the meantime clears the record
and reports OK.

``--fix`` presses Enter, gated on the same marker match.  That is the same
key the operator would send by hand, and it can only ever submit text this
daemon typed: a human draft never carries the marker, so it is never
touched.
"""

from __future__ import annotations

import logging
import os

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.env_scrub import harness_session_markers
from src.sessions.provider import SessionHandle

OWNER = "session-runtime"
logger = logging.getLogger(__name__)

STUCK_CHECK_ID = "sessions.stuck_composer"

_LIVE_STATES = ("starting", "running", "draining")

ENV_CHECK_ID = "sessions.env_markers"


async def _check_env_markers(ctx: DoctorContext) -> CheckResult:
    """Report control variables that should have been scrubbed at boot."""
    markers = harness_session_markers(os.environ)
    if markers:
        return CheckResult(
            id=ENV_CHECK_ID,
            severity=Severity.ERROR,
            detail=(
                "daemon inherited harness session marker(s): "
                + ", ".join(markers)
                + "; restart with a clean environment"
            ),
            data={"markers": markers},
        )
    return CheckResult(
        id=ENV_CHECK_ID,
        severity=Severity.OK,
        detail="daemon environment has no inherited harness session markers",
    )



def _providers(ctx: DoctorContext):
    """``(registry, config)`` for resolving a session's provider, or None."""
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    registry = getattr(orchestrator, "session_providers", None)
    if registry is None:
        return None
    return registry, getattr(orchestrator, "config", ctx.config)


def _handle(row) -> SessionHandle:
    return SessionHandle(
        name=row.name, provider=row.provider, instance_token=row.instance_token
    )


async def _find_stuck(ctx: DoctorContext, *, resubmit: bool = False) -> list[dict]:
    """Sessions whose composer still holds a nudge this daemon never sent.

    With ``resubmit`` the Enter is actually pressed; each entry then carries
    ``recovered`` so the caller can say what it repaired.  Providers without
    the ``pending_submit`` hook (subprocess, any third-party one) simply
    have no composer and are skipped rather than reported unknown.
    """
    resolved = _providers(ctx)
    if resolved is None or ctx.db is None:
        return []
    registry, config = resolved
    try:
        rows = await ctx.db.list_sessions(states=_LIVE_STATES)
    except Exception:
        logger.debug("could not list sessions for stuck-composer check", exc_info=True)
        return []
    stuck: list[dict] = []
    for row in rows:
        try:
            provider = registry.create(row.provider, config)
        except Exception:
            logger.debug("could not construct session provider %s", row.provider, exc_info=True)
            continue
        probe = getattr(provider, "pending_submit", None)
        if probe is None:
            continue
        try:
            marker = await probe(_handle(row))
        except Exception:
            logger.debug("could not inspect composer for session %s", row.id, exc_info=True)
            continue
        if not marker:
            continue
        entry = {
            "session_id": row.id,
            "name": row.name,
            "task_id": row.task_id,
            "project_id": row.project_id,
            "marker": marker,
        }
        if resubmit:
            fix = getattr(provider, "resubmit_pending", None)
            recovered = False
            if fix is not None:
                try:
                    recovered = bool(await fix(_handle(row)))
                except Exception:
                    logger.debug("could not resubmit composer for session %s", row.id, exc_info=True)
                    recovered = False
            entry["recovered"] = recovered
        stuck.append(entry)
    return stuck


def _describe(stuck: list[dict]) -> str:
    return ", ".join(
        f"{e['name']}" + (f" (task {e['task_id']})" if e.get("task_id") else "")
        for e in stuck[:5]
    )


async def _check_stuck_composer(ctx: DoctorContext) -> CheckResult:
    stuck = await _find_stuck(ctx)
    if not stuck:
        return CheckResult(
            id=STUCK_CHECK_ID,
            severity=Severity.OK,
            detail="no session is holding an unsubmitted nudge",
            fixable=True,
        )
    return CheckResult(
        id=STUCK_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(stuck)} session(s) have a nudge stuck in the composer "
            f"(Enter was never confirmed): {_describe(stuck)}"
        ),
        fixable=True,
        data={"count": len(stuck), "sessions": stuck},
    )


async def _fix_stuck_composer(ctx: DoctorContext) -> CheckResult:
    repaired = await _find_stuck(ctx, resubmit=True)
    recovered = [e for e in repaired if e.get("recovered")]
    return CheckResult(
        id=STUCK_CHECK_ID,
        severity=Severity.OK if len(recovered) == len(repaired) else Severity.WARN,
        detail=f"resubmitted {len(recovered)} of {len(repaired)} stuck composer(s)",
        fixable=True,
        data={"recovered": recovered, "attempted": repaired},
    )


def session_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=ENV_CHECK_ID, run=_check_env_markers, owner=OWNER),
        DoctorCheck(
            id=STUCK_CHECK_ID,
            run=_check_stuck_composer,
            fix=_fix_stuck_composer,
            owner=OWNER,
            timeout_s=15.0,
        ),
    ]


#: Snapshot of the checks this module owns, keyed by id — for tests and any
#: one-off invocation that does not want a full :class:`DoctorRegistry`.
CHECKS = {check.id: check for check in session_checks()}


async def run_check(db, handler, check_id: str, *, config=None, repair: bool = False):
    """Run one session check directly (no registry needed).

    ``repair=True`` runs the check's ``fix`` then re-runs it, mirroring
    :func:`src.doctor.runner.apply_fix`.
    """
    from src.doctor.runner import apply_fix

    check = CHECKS[check_id]
    ctx = DoctorContext(config=config, db=db, handler=handler)
    if repair and check.fix is not None:
        return await apply_fix(check, ctx)
    return await check.run(ctx)
