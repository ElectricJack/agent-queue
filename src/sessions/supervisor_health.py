"""Turn-based supervision health; terminal output never supplies liveness.

Observations read a bounded tail of the exact conversation, scoped to this
launch. Retry clocks live on the session row so restarting AQ cannot reset them.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from src.sessions.context import CONTEXT_TAIL_BYTES, context_from_tail
from src.sessions.transcripts import resolve_reader
from src.sessions.transcripts.base import parse_iso_ts

_ERROR = re.compile(
    r"at capacity|rate.?limit|too many requests|quota exceeded|overloaded|"
    r"authentication|unauthorized|invalid.api.key|insufficient.quota|usage limit|\b401\b|\b403\b", re.I,
)


def is_supervisor(row) -> bool:
    return (row.lifecycle == "named" and row.profile_id == "supervisor"
            and row.name.startswith("n-supervisor--"))


def observe_tail(path: Path, row, *, now: float) -> dict:
    result = {"known": True, "last_completed_at": None, "error": None,
              "idle": False, "input_tokens": None}
    with path.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - CONTEXT_TAIL_BYTES)
        stream.seek(start)
        data = stream.read(CONTEXT_TAIL_BYTES)
    if not data.endswith(b"\n"):
        return {**result, "known": False}
    lines = data.splitlines()
    if start:
        lines.pop(0)
    seen = False
    for line in lines:
        raw = json.loads(line)
        at = parse_iso_ts(raw.get("timestamp"))
        if at < row.started_at or at > now:
            continue
        seen = True
        kind = raw.get("type")
        payload = raw.get("payload") or {}
        if row.harness == "codex":
            ptype = payload.get("type")
            if kind == "event_msg" and ptype == "error":
                text = str(payload.get("message") or payload.get("error") or "")
                if _ERROR.search(text):
                    result.update(error={"at": at, "reason": text[:500]}, idle=True)
            elif kind == "event_msg" and ptype == "task_complete":
                result["idle"] = True
                if not result["error"]:
                    result["last_completed_at"] = at
            elif kind == "turn_context" or (
                kind == "event_msg" and ptype in {"task_started", "user_message"}
            ):
                result.update(error=None, idle=False)
        elif row.harness == "claude" and kind in {"user", "assistant"}:
            message = raw.get("message") or {}
            if kind == "user":
                result.update(error=None, idle=False)
            elif raw.get("isApiErrorMessage"):
                text = json.dumps(message.get("content", ""))
                if _ERROR.search(text):
                    result.update(error={"at": at, "reason": text[:500]}, idle=True)
            else:
                result.update(error=None, idle=message.get("stop_reason") == "end_turn")
                if result["idle"]:
                    result["last_completed_at"] = at
    result["known"] = seen
    result["input_tokens"] = context_from_tail(path, row.harness, now=now)["input_tokens"]
    return result


async def observe(row, *, now: float, base_dir=None) -> dict:
    def read():
        reader = resolve_reader(row.harness, base_dir=base_dir)
        path = reader.resolve_session(row) if reader else None
        return observe_tail(path, row, now=now) if path else {"known": False}
    try:
        return await asyncio.to_thread(read)
    except (OSError, ValueError, TypeError, AttributeError):
        return {"known": False}


def assess(row, observation: dict, requests, settings, *, now: float) -> dict:
    previous = row.supervisor_health or {}
    completed = max(previous.get("last_completed_at") or 0,
                    observation.get("last_completed_at") or 0)
    pending = list(requests)
    oldest = min((msg.created_at for msg in pending), default=None)
    baseline = max(row.started_at, completed, oldest or 0)
    unhealthy = bool(pending and now - baseline >= settings.turn_timeout_seconds)
    error = observation.get("error")
    reason = "provider_error" if error else "turn_timeout" if unhealthy else None
    rotate = bool(observation.get("known") and observation.get("idle") and (
        now - row.started_at >= settings.max_session_age_seconds
        or (observation.get("input_tokens") or 0) >= settings.context_ceiling_tokens
    ))
    # Fresh completion is the only event that clears a failure streak.
    recovered = completed > (previous.get("last_completed_at") or 0)
    return {**previous, "checked_at": now, "known": observation.get("known", False),
            "last_completed_at": completed or None, "pending_count": len(pending),
            "oldest_pending_at": oldest, "unhealthy": bool(reason), "reason": reason,
            "error": error, "rotate": rotate,
            "tried_harnesses": [] if recovered else previous.get("tried_harnesses", []),
            "attempts": 0 if recovered else previous.get("attempts", 0),
            "next_retry_at": 0 if recovered else previous.get("next_retry_at", 0)}


async def reconcile(reconciler, row, *, now: float) -> bool:
    """Return true when named-session idle handling must defer this tick."""
    db = reconciler.db
    settings = reconciler.config.supervisor
    requests = await db.supervisor_requests(row)
    observation = await observe(row, now=now, base_dir=reconciler.transcript_base_dir)
    health = assess(row, observation, requests, settings, now=now)
    if not await db.update_session_instance(
        row.id, row.instance_token, require_desired_state="running", supervisor_health=health,
    ):
        return True
    for msg in requests:
        if (msg.from_kind == "session" or msg.body_kind == "agent_question") and (
            now - msg.created_at >= settings.question_timeout_seconds
        ):
            await db.escalate_supervisor_request(msg, row, now=now)
    if not (health["unhealthy"] or health["rotate"]):
        return False
    if now < health["next_retry_at"]:
        return True
    attempts = health["attempts"]
    health.update(attempts=attempts + 1,
                  next_retry_at=now + min(1800, settings.retry_backoff_seconds * 2 ** min(attempts, 5)))
    if not await db.update_session_instance(
        row.id, row.instance_token, require_desired_state="running", supervisor_health=health,
    ):
        return True
    lens = reconciler.starter
    if lens is None:
        return True
    address = reconciler._named_address(row)
    if not health["rotate"] and attempts < settings.max_turn_retries:
        await lens.nudge(kind="session", target_id=address, project_id=row.project_id,
                         text="The previous supervisor turn failed or left inbound work unanswered. "
                              "Retry the pending work now; inspect your inbox and reply explicitly.")
        return True
    # Fresh replacement carries the durable pending-message pointers, not the
    # failing conversation. The lens confirms a fenced stop before any start.
    route = await lens.supervisor_recovery_route(row) if health["unhealthy"] else None
    health["tried_harnesses"] = sorted(set(health.get("tried_harnesses", [])) | {row.harness})
    health["recovery_harness"] = route
    health["recovery_class"] = row.intelligence_class
    reason = health["reason"] or "session_rotation"
    await db.supervisor_recovery_notice(row, health, requests, reason=reason, now=now)
    successor = await lens.restart_supervisor(
        target_id=address, project_id=row.project_id, expected_session_id=row.id,
        recovery_harness=route, recovery_health=health, restart_reason=reason,
    )
    # Keep the retry budget across fresh sessions until a successful turn.
    await db.update_session_instance(
        successor.id, successor.instance_token, supervisor_health={
            **health, "rotate": False, "predecessor_id": row.id,
        },
    )
    return True
