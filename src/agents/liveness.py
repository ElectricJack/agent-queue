"""The single definition of agent liveness.

An agent is **live** when it owns a live session (``starting`` / ``running``
/ ``draining``) whose ``sessions.last_activity`` is inside the session lease
TTL.  Liveness is a property of the *session*, and every surface that answers
"is this agent alive" must read it from here.

``agents.last_heartbeat`` is **task-scoped** and is not that answer.  Its only
writers are ``aq task heartbeat`` (``_cmd_task_heartbeat``) and the transcript
watcher's in-turn tail, and both require the agent to be holding a task.  A
pool worker parked in ``aq task claim --next --wait 60`` holds no task, so its
heartbeat sits at whatever the last task left there and an idle-but-healthy
worker reads as hours stale.  The field is a lease/reservation token — it
fences ``reserve_agent``'s compare-and-set and gives the agent reconciler a
grace window for a launch that has not attached a session yet — so it stays in
the schema and in payloads, labelled as the task heartbeat, never as liveness.
"""
from __future__ import annotations

#: Session states the reconciler treats as a running process.
LIVE_SESSION_STATES = ("starting", "running", "draining")


def session_last_activity(session) -> float | None:
    """Activity stamp for one session, falling back to its launch time."""
    if session is None:
        return None
    return getattr(session, "last_activity", None) or getattr(session, "started_at", None)


def liveness_by_agent(sessions) -> dict[str, float]:
    """Map agent id to the newest activity stamp across its live sessions."""
    newest: dict[str, float] = {}
    for session in sessions:
        agent_id = getattr(session, "agent_id", None)
        if not agent_id or getattr(session, "state", None) not in LIVE_SESSION_STATES:
            continue
        seen = session_last_activity(session)
        if seen is None:
            continue
        if seen > newest.get(agent_id, float("-inf")):
            newest[agent_id] = seen
    return newest


def is_live(last_activity: float | None, *, now: float, ttl: float | None) -> bool:
    """True when *last_activity* is inside the lease TTL.

    A missing stamp means no live session, so no liveness.  A non-positive
    TTL disables the staleness half of the check, exactly as
    ``session_list`` does when deriving ``stalled``.
    """
    if last_activity is None:
        return False
    if ttl and ttl > 0:
        return (now - last_activity) <= ttl
    return True
