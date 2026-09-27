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
import time

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.env_scrub import harness_session_markers
from src.models import TaskStatus
from src.sessions.provider import SessionHandle

OWNER = "session-runtime"
logger = logging.getLogger(__name__)

STUCK_CHECK_ID = "sessions.stuck_composer"

_LIVE_STATES = ("starting", "running", "draining")

ENV_CHECK_ID = "sessions.env_markers"

BACKLOG_CHECK_ID = "messages.idle_worker_backlog"

#: The delivery cascade nudges an idle recipient on every pass, so mail this
#: old for an idle live worker is a failed wake, not a queue that is draining.
_BACKLOG_AGE_SECONDS = 300.0
_BACKLOG_LIMIT = 200
#: Terminal results the engine deliberately holds for a manually paused task.
_HELD_FOR_PAUSE = frozenset({"wait_result", "job_result"})


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
            # Providers can expose durable provenance without publishing the
            # injected text itself. Legacy/third-party hooks remain honest.
            "evidence": getattr(provider, "pending_submit_evidence", "provider_pending_submit"),
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


async def _old_pending_mail(ctx: DoctorContext) -> tuple[list[dict], bool]:
    """Undelivered task/session mail older than the backlog age, oldest first."""
    from sqlalchemy import select

    from src.database.tables import messages

    cutoff = time.time() - _BACKLOG_AGE_SECONDS
    stmt = (
        select(
            messages.c.id, messages.c.project_id, messages.c.to_kind, messages.c.to_id,
            messages.c.from_kind, messages.c.from_id, messages.c.body_kind,
            messages.c.created_at,
        )
        .where(
            messages.c.delivered_at.is_(None),
            messages.c.archived_at.is_(None),
            messages.c.to_kind.in_(("task", "session")),
            messages.c.created_at <= cutoff,
        )
        .order_by(messages.c.created_at, messages.c.id)
        .limit(_BACKLOG_LIMIT + 1)
    )
    async with ctx.db._engine.connect() as conn:
        rows = [dict(row) for row in (await conn.execute(stmt)).mappings().all()]
    return rows[:_BACKLOG_LIMIT], len(rows) > _BACKLOG_LIMIT


def _backlog_entry(row: dict, now: float, session=None, failure=None) -> dict:
    return {
        "message_id": row["id"],
        "project_id": row["project_id"],
        "to_kind": row["to_kind"],
        "to_id": row["to_id"],
        "from": f"{row['from_kind']}:{row['from_id']}",
        "body_kind": row["body_kind"],
        "created_at": row["created_at"],
        "age_seconds": int(now - row["created_at"]),
        "session_id": getattr(session, "id", None),
        "session_name": getattr(session, "name", None),
        "task_id": getattr(session, "task_id", None),
        "last_nudge_failure": failure,
    }


async def _check_idle_worker_backlog(ctx: DoctorContext) -> CheckResult:
    """Mail older than five minutes addressed to a live worker the lens reads as idle.

    Uses the delivery engine's own :class:`SessionLens`, so "idle" means
    exactly what the cascade acted on, and each entry carries the lens's
    last refused-nudge reason for that session.
    """
    if ctx.db is None:
        return CheckResult(
            id=BACKLOG_CHECK_ID, severity=Severity.INFO, detail="database unavailable"
        )
    rows, truncated = await _old_pending_mail(ctx)
    now = time.time()
    lens = getattr(getattr(ctx.handler, "orchestrator", None), "session_lens", None)
    if lens is None:
        if not rows:
            return CheckResult(
                id=BACKLOG_CHECK_ID, severity=Severity.OK,
                detail="no task or session message has waited more than 5 minutes",
            )
        return CheckResult(
            id=BACKLOG_CHECK_ID,
            severity=Severity.INFO,
            detail=(
                f"{len(rows)} task/session message(s) older than 5 minutes; "
                "worker activity is unknown without the daemon's session lens"
            ),
            data={"messages": [_backlog_entry(row, now) for row in rows],
                  "truncated": truncated},
        )

    entries: list[dict] = []
    recipients: dict[tuple, tuple] = {}
    for row in rows:
        key = (row["to_kind"], row["to_id"], row["project_id"])
        if key not in recipients:
            try:
                activity = await lens.activity(
                    kind=row["to_kind"], target_id=row["to_id"], project_id=row["project_id"]
                )
                session = (
                    await lens.session_for(
                        kind=row["to_kind"], target_id=row["to_id"],
                        project_id=row["project_id"],
                    )
                    if activity == "idle"
                    else None
                )
            except Exception:
                logger.debug("could not read activity for %s:%s", *key[:2], exc_info=True)
                activity, session = "unknown", None
            recipients[key] = (activity, session)
        activity, session = recipients[key]
        if activity != "idle" or session is None:
            continue
        if row["to_kind"] == "task" and row["body_kind"] in _HELD_FOR_PAUSE:
            task = await ctx.db.get_task(row["to_id"])
            if task is not None and task.status == TaskStatus.PAUSED:
                continue
        entries.append(_backlog_entry(row, now, session, lens.nudge_failure(session.id)))

    if not entries:
        return CheckResult(
            id=BACKLOG_CHECK_ID, severity=Severity.OK,
            detail="no idle live worker has mail older than 5 minutes",
        )
    names = sorted({entry["session_name"] for entry in entries})
    reasons = sorted({
        entry["last_nudge_failure"]["reason"]
        for entry in entries if entry["last_nudge_failure"]
    })
    return CheckResult(
        id=BACKLOG_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(entries)} message(s) older than 5 minutes are waiting for "
            f"{len(names)} idle worker(s): {', '.join(names[:5])}"
            + (f"; last nudge refusal: {reasons[0]}" if reasons else "")
        ),
        data={"messages": entries, "truncated": truncated},
    )


def session_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=ENV_CHECK_ID, run=_check_env_markers, owner=OWNER),
        DoctorCheck(
            id=BACKLOG_CHECK_ID,
            run=_check_idle_worker_backlog,
            owner=OWNER,
            timeout_s=15.0,
        ),
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
