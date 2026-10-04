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
and reports OK.  A record whose composer cannot be read at all (a harness
layout the guard does not recognise) is reported too, as ``unreadable``:
that text blocks every later wake exactly the same way, and it is the state
Codex 0.157 panes sat in, invisibly, on 2026-09-27.

``--fix`` presses Enter, gated on the same marker match.  That is the same
key the operator would send by hand, and it can only ever submit text this
daemon typed: a human draft never carries the marker, so it is never
touched.  An unreadable record is never submitted by ``--fix``.  One
unreadable shape is AQ's own and marked ``clearable``: text the composer
shows collapsed (``[Pasted text #N +M lines]``) or windowed (its last rows
only).  ``--fix`` clears that with the harness's clear keys instead, exactly
as the next nudge would, and the message behind it stays queued
(2026-10-01, a task comment stranded a Claude worker that way).

sessions.stall_unreachable — the rule
-------------------------------------

The stall ladder nudges a task holder idle past the session lease, and a
nudge the composer guard defers on *a person* is silent: no rung is spent,
no ``task.stalled`` is emitted, and the worker waits for a human.  That is
how OpenCode workers sat idle for 20+ minutes on 2026-09-26/27 — the guard
did not recognise OpenCode's box composer, and the fast-jev plugin had
painted its log line over it.  This check lists every live task holder idle
past the lease whose composer would refuse the nudge right now, with the
refusal, its structured ``reason_kind``, and the text the composer shows, so
a human draft, a painted-over box and an unrecognised layout can be told
apart — and so the operator can see which of them will *not* clear on their
own.  Since 2026-10-03 the ladder no longer waits forever on the rest: a
stale frame or an unreadable composer spends a rung once no provider activity
and no transcript write corroborate the stall.  Read-only: it neither
repaints nor presses a key.  Sessions in a durable wait are not stalled and
are skipped.
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

UNREACHABLE_CHECK_ID = "sessions.stall_unreachable"

STOP_INTENT_CHECK_ID = "sessions.stop_intent_pending"

#: Refusal reasons a person is responsible for: the ladder will not advance
#: on these however long the holder is idle, so they are the ones an operator
#: has to clear.  Anything else (a stale frame, an unreadable composer) is
#: spent as a rung once progress evidence independent of the composer says
#: the holder is frozen -- the reader-resolved transcript, or for a CLI with a
#: store of its own (``opencode``) that store -- and announced as
#: ``evidence="unverified"`` only where AQ can measure no progress at all.
_LADDER_HELD_REASONS = frozenset({"draft", "terminal_busy", "recent_input"})

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
        detail_probe = getattr(provider, "pending_submit_detail", None)
        probe = getattr(provider, "pending_submit", None)
        if detail_probe is None and probe is None:
            continue
        try:
            if detail_probe is not None:
                detail = await detail_probe(_handle(row))
            else:
                marker = await probe(_handle(row))
                detail = {"marker": marker, "observable": True} if marker else None
        except Exception:
            logger.debug("could not inspect composer for session %s", row.id, exc_info=True)
            continue
        if not detail or not detail.get("marker"):
            continue
        observable = bool(detail.get("observable", True))
        clearable = not observable and bool(detail.get("clearable", False))
        entry = {
            "session_id": row.id,
            "name": row.name,
            "task_id": row.task_id,
            "project_id": row.project_id,
            "marker": detail["marker"],
            # False: the durable record says AQ typed this text, but no screen
            # parse can attribute the composer, so Enter is never pressed.
            "observable": observable,
            # AQ's own text shown collapsed or windowed: never submitted, but
            # --fix clears it and the message behind it is redelivered.
            "clearable": clearable,
            # Providers can expose durable provenance without publishing the
            # injected text itself. Legacy/third-party hooks remain honest.
            "evidence": getattr(provider, "pending_submit_evidence", "provider_pending_submit"),
        }
        if resubmit:
            action = "resubmit_pending" if observable else "clear_pending" if clearable else None
            fix = getattr(provider, action, None) if action else None
            recovered = False
            if fix is not None:
                try:
                    recovered = bool(await fix(_handle(row)))
                except Exception:
                    logger.debug("could not repair composer for session %s", row.id, exc_info=True)
                    recovered = False
            entry["recovered"] = recovered
            entry["action"] = ("resubmitted" if observable else "cleared") if recovered else None
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
    readable = [e for e in stuck if e["observable"]]
    unreadable = [e for e in stuck if not e["observable"]]
    parts = []
    if readable:
        parts.append(
            f"{len(readable)} session(s) have a nudge stuck in the composer "
            f"(Enter was never confirmed): {_describe(readable)}"
        )
    clearable = [e for e in unreadable if e.get("clearable")]
    unclearable = [e for e in unreadable if not e.get("clearable")]
    if clearable:
        parts.append(
            f"{len(clearable)} session(s) hold AQ-typed text the composer shows collapsed "
            f"or windowed, so it can never be submitted; --fix clears it and its message "
            f"is redelivered: {_describe(clearable)}"
        )
    if unclearable:
        # --fix cannot help these: attaching and looking is the next step.
        parts.append(
            f"{len(unclearable)} session(s) hold AQ-typed text the composer guard "
            f"cannot read, so every later wake defers on it: {_describe(unclearable)}"
        )
    return CheckResult(
        id=STUCK_CHECK_ID,
        severity=Severity.WARN,
        detail="; ".join(parts),
        fixable=True,
        data={"count": len(stuck), "unreadable": len(unreadable), "sessions": stuck},
    )


async def _fix_stuck_composer(ctx: DoctorContext) -> CheckResult:
    repaired = await _find_stuck(ctx, resubmit=True)
    recovered = [e for e in repaired if e.get("recovered")]
    return CheckResult(
        id=STUCK_CHECK_ID,
        severity=Severity.OK if len(recovered) == len(repaired) else Severity.WARN,
        detail=(
            f"resubmitted {sum(e['action'] == 'resubmitted' for e in recovered)} and cleared "
            f"{sum(e['action'] == 'cleared' for e in recovered)} of {len(repaired)} "
            "stuck composer(s)"
        ),
        fixable=True,
        data={"recovered": recovered, "attempted": repaired},
    )


def _lease_ttl(config) -> float:
    from src.config import SessionsConfig

    sessions = getattr(config, "sessions", None)
    ttl = getattr(sessions, "lease_ttl_seconds", None)
    return float(SessionsConfig.lease_ttl_seconds if ttl is None else ttl)


def _stop_intent_report_seconds(config) -> float:
    from src.config import SessionsConfig

    sessions = getattr(config, "sessions", None)
    value = getattr(sessions, "stop_intent_report_seconds", None)
    if value is None:
        return float(SessionsConfig.stop_intent_report_seconds)
    return float(value)


async def _last_attempt_end(ctx: DoctorContext, session_id: str) -> float | None:
    """When this session last let go of a task, from its own attempt history.

    The clock the reconciler's own stop grace cannot use, and the one this
    report has to: ``sessions.last_activity`` is tmux's ``window_activity``,
    which advances on *any* pane output, so a TUI that keeps repainting an
    idle summary never looks idle by it.  ``task_session_attempts.ended_at``
    is written by the very release that records the stop intent, so it ages
    the right way even for a session whose pane never goes quiet.
    """
    from sqlalchemy import func, select

    from src.database.tables import task_session_attempts

    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        return None
    try:
        async with engine.connect() as conn:
            value = (
                await conn.execute(
                    select(func.max(task_session_attempts.c.ended_at)).where(
                        task_session_attempts.c.session_id == session_id
                    )
                )
            ).scalar()
    except Exception:
        logger.debug("could not read attempt history for session %s", session_id, exc_info=True)
        return None
    return float(value) if value is not None else None


async def _find_stop_intent_pending(ctx: DoctorContext, *, threshold: float, now: float) -> list[dict]:
    """Live sessions whose recorded stop intent has outlasted *threshold*.

    ``desired_state='stopped'`` with nothing still held is the shape a close, a
    drain, a scale-down and ``aq session kill`` all leave behind; the harness is
    then supposed to go away on its own.  It does not always -- an OpenCode
    worker's pane simply stays -- and the reconciler stops it within
    ``sessions.idle_stop_grace_seconds``.  A session still here well past that
    is the case where the graceful path did not run: the grace disabled, the
    reconciler not ticking, or an instance fence that keeps failing.
    """
    from src.pool_claims import is_live_pool_claim_task_status

    try:
        rows = await ctx.db.list_sessions(desired_state="stopped", live_only=True)
    except Exception:
        logger.debug("could not list stop-pending sessions", exc_info=True)
        return []
    pending: list[dict] = []
    for row in rows:
        if row.claim_phase is not None:
            continue  # inside a claim: not idle, and the claim may still be won
        if row.task_id:
            task = await ctx.db.get_task(row.task_id)
            if task is not None and is_live_pool_claim_task_status(task.status):
                continue  # still holds open work; mid-turn is not reportable
            if task is not None:
                try:
                    if await ctx.db.blocking_wait_for(row, task.claim_epoch, now):
                        continue
                except Exception:
                    logger.debug(
                        "could not read the wait for session %s", row.id, exc_info=True
                    )
        ended = await _last_attempt_end(ctx, row.id)
        source = "attempt" if ended else "activity"
        since = ended or row.last_activity or row.started_at
        if since is None:
            continue
        age = now - since
        pending.append({
            "session_id": row.id,
            "name": row.name,
            "lifecycle": row.lifecycle,
            "harness": row.harness,
            "task_id": row.task_id,
            "project_id": row.project_id,
            "age_seconds": int(age),
            "age_source": source,
            "state": row.state,
            "desired_state": row.desired_state,
        })
    pending.sort(key=lambda entry: -entry["age_seconds"])
    return [entry for entry in pending if entry["age_seconds"] >= threshold]


async def _check_stop_intent_pending(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id=STOP_INTENT_CHECK_ID,
            severity=Severity.INFO,
            detail="no database is available to read sessions from",
        )
    threshold = _stop_intent_report_seconds(ctx.config)
    if threshold <= 0:
        return CheckResult(
            id=STOP_INTENT_CHECK_ID,
            severity=Severity.INFO,
            detail="the report is disabled (sessions.stop_intent_report_seconds <= 0)",
        )
    now = time.time()
    pending = await _find_stop_intent_pending(ctx, threshold=threshold, now=now)
    if not pending:
        return CheckResult(
            id=STOP_INTENT_CHECK_ID,
            severity=Severity.OK,
            detail=(
                "no session has held a stop intent while its process stayed alive "
                f"for {int(threshold)}s"
            ),
        )
    shown = "; ".join(
        f"{entry['name']} ({entry['lifecycle']}, state={entry['state']}, "
        f"{entry['age_seconds'] // 60}m by {entry['age_source']})"
        for entry in pending[:5]
    )
    return CheckResult(
        id=STOP_INTENT_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(pending)} session(s) still running with a stop intent after "
            f"{int(threshold) // 60}m, so their harness process never left: {shown}. "
            "`aq session kill <id>` returns the slot; the reconciler does this "
            "automatically within sessions.idle_stop_grace_seconds."
        ),
        data={"count": len(pending), "sessions": pending},
    )


async def _stalled_holders(ctx: DoctorContext, ttl: float, now: float) -> list:
    """Live sessions the stall ladder would try to nudge on its next tick."""
    rows = await ctx.db.list_sessions(state="running")
    stalled = []
    for row in rows:
        if row.lifecycle not in ("task", "pool") or not row.task_id:
            continue
        if row.lifecycle == "pool" and row.claim_phase != "active":
            continue
        if now - (row.last_activity or row.started_at or now) <= ttl:
            continue
        task = await ctx.db.get_task(row.task_id)
        if task is None or task.status is not TaskStatus.IN_PROGRESS:
            continue
        try:
            if await ctx.db.blocking_wait_for(row, task.claim_epoch, now):
                continue
        except Exception:
            logger.debug("could not read the wait for session %s", row.id, exc_info=True)
        stalled.append(row)
    return stalled


async def _find_unreachable(ctx: DoctorContext, registry, config, ttl: float) -> list[dict]:
    """Stalled task holders whose composer would defer the ladder's nudge."""
    now = time.time()
    unreachable: list[dict] = []
    for row in await _stalled_holders(ctx, ttl, now):
        try:
            provider = registry.create(row.provider, config)
            probe = getattr(provider, "composer_probe", None)
            # A provider without a composer (subprocess) has no nudge rung.
            result = await probe(_handle(row)) if probe is not None else None
        except Exception:
            logger.debug("could not probe the composer of session %s", row.id, exc_info=True)
            continue
        if result is None or result.get("ready"):
            continue
        unreachable.append({
            "session_id": row.id,
            "name": row.name,
            "harness": row.harness,
            "task_id": row.task_id,
            "project_id": row.project_id,
            "idle_seconds": int(now - (row.last_activity or row.started_at or now)),
            "reason": result.get("reason"),
            # The structured NudgeReason behind ``reason``: which of these the
            # stall ladder may escalate on is a mechanical question, so the
            # report answers it instead of leaving it to be re-derived.
            "reason_kind": result.get("reason_kind"),
            "input": result.get("input") or "",
        })
    return unreachable


async def _check_stall_unreachable(ctx: DoctorContext) -> CheckResult:
    resolved = _providers(ctx)
    if resolved is None or ctx.db is None:
        return CheckResult(
            id=UNREACHABLE_CHECK_ID,
            severity=Severity.INFO,
            detail="session providers are unavailable outside the daemon",
        )
    registry, config = resolved
    ttl = _lease_ttl(config or ctx.config)
    if ttl <= 0:
        return CheckResult(
            id=UNREACHABLE_CHECK_ID,
            severity=Severity.INFO,
            detail="the stall ladder is disabled (sessions.lease_ttl_seconds <= 0)",
        )
    unreachable = await _find_unreachable(ctx, registry, config, ttl)
    if not unreachable:
        return CheckResult(
            id=UNREACHABLE_CHECK_ID,
            severity=Severity.OK,
            detail="every task holder idle past the session lease can be nudged",
        )
    shown = "; ".join(
        f"{e['name']} (task {e['task_id']}, idle {e['idle_seconds'] // 60}m"
        + (f", {e['reason_kind']}" if e.get("reason_kind") else "")
        + ")"
        + (f" shows: {e['input'][:120]}" if e["input"] else f": {e['reason']}")
        for e in unreachable[:5]
    )
    # Only the refusals a person is responsible for hold the ladder still.
    # A stale frame or an unreadable composer is escalated by the ladder once
    # no progress corroborates it, so reporting it as "cannot climb" was
    # both wrong and the reason nobody noticed the real hole (2026-10-03).
    held = [e for e in unreachable if e.get("reason_kind") in _LADDER_HELD_REASONS]
    detail = (
        f"{len(unreachable)} task holder(s) idle past the {int(ttl) // 60} min lease "
        f"cannot be nudged, so the stall ladder cannot climb: {shown}"
    )
    if len(held) < len(unreachable):
        detail = (
            f"{len(held)} of {len(unreachable)} task holder(s) idle past the "
            f"{int(ttl) // 60} min lease hold the stall ladder on a composer AQ will "
            f"not touch; the rest are escalated once no progress corroborates them: {shown}"
        )
    return CheckResult(
        id=UNREACHABLE_CHECK_ID,
        severity=Severity.WARN,
        detail=detail,
        data={"count": len(unreachable), "sessions": unreachable},
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


async def _check_flock(ctx: DoctorContext) -> CheckResult:
    from src.sessions.flock_audit import audit_flock

    resolved = _providers(ctx)
    if ctx.db is None or resolved is None:
        return CheckResult("sessions.untracked", Severity.INFO,
                           "session providers unavailable; flock was not checked")
    registry, config = resolved
    findings = await audit_flock(ctx.db, registry, config)
    return CheckResult(
        "sessions.untracked", Severity.ERROR if findings else Severity.OK,
        f"{len(findings)} untracked execution/probe finding(s)" if findings
        else "zero untracked sessions or AQ-marked processes",
        data={"count": len(findings), "findings": findings},
    )


def session_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="sessions.untracked", run=_check_flock, owner=OWNER, timeout_s=15.0),
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
        DoctorCheck(
            id=UNREACHABLE_CHECK_ID,
            run=_check_stall_unreachable,
            owner=OWNER,
            timeout_s=15.0,
        ),
        DoctorCheck(
            id=STOP_INTENT_CHECK_ID,
            run=_check_stop_intent_pending,
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
