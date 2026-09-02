"""How many children is this agent running — natively and through AQ.

Two populations, deliberately kept apart and then added:

* **AQ sub-agents** — worker tasks this agent's session created and another
  agent is actively executing.  Provenance is the authenticated creating
  session, never the task tree.
* **native sub-agents** — children the harness itself spawned (a Claude Code
  ``Task``, a Codex sub-agent).  These are read from the harness's own
  ``SubagentStart`` / ``SubagentStop`` hooks, folded per session.

Coverage is a property of the *launch*: ``sessions.hooks_provisioned`` says
whether this process was actually started with its hook file wired.  A live
session without it yields ``None`` rather than a confident zero — an
under-count that announces itself is worth more than a plausible lie.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.models import AgentState, SessionRecord, Task, TaskStatus

_ACTIVE_SESSIONS = {"starting", "running", "draining"}
_ACTIVE_TASKS = {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_INPUT}

_NO_EVENTS: dict[str, int] = {"starts": 0, "stops": 0}


async def subagent_counts(
    db,
    agent_id: str,
    sessions: Sequence[SessionRecord],
    tasks: Sequence[Task],
    *,
    native_by_session: Mapping[str, Mapping[str, int]] | None = None,
) -> dict:
    """Count this agent's active children across all of its sessions.

    ``tasks`` must contain only live (not archived) task rows. A task tree is
    not delegation provenance: only its authenticated creating session links
    a worker to the parent. Read this from old sessions too, since children
    can outlive the parent's current session.

    *native_by_session* is ``{session_id: {"starts": n, "stops": n}}``, the
    fold of ``subagent_events``.  The flock view computes it once for every
    session and passes it in; omitting it costs one extra query per call.
    """
    task_by_id = {task.id: task for task in tasks}

    agents = {}

    async def get_agent(worker_id: str):
        if worker_id not in agents:
            agents[worker_id] = await db.get_agent(worker_id)
        return agents[worker_id]

    def is_busy_on(agent, task: Task) -> bool:
        return bool(
            agent is not None and agent.state == AgentState.BUSY
            and agent.current_task_id == task.id
        )

    owners = {}
    for session in sessions:
        owner = session.agent_id
        if owner is None and session.state in _ACTIVE_SESSIONS:
            # An old unlinked session cannot inherit today's task assignment.
            # Recover only an active execution whose worker pointer agrees;
            # a recorded epoch from an earlier claim cannot establish ownership.
            task = task_by_id.get(session.task_id)
            if (
                task is not None and task.status in _ACTIVE_TASKS
                and task.assigned_agent_id
                and (session.last_claim_epoch is None or session.last_claim_epoch == task.claim_epoch)
            ):
                worker = await get_agent(task.assigned_agent_id)
                if is_busy_on(worker, task):
                    owner = task.assigned_agent_id
        owners[session.id] = owner

    owned = [session for session in sessions if owners[session.id] == agent_id]
    parent_session_ids = {session.id for session in owned}
    active_holders = {
        (session.task_id, owners[session.id])
        for session in sessions if session.state in _ACTIVE_SESSIONS and session.task_id
    }

    children = set()
    for task in tasks:
        worker_id = task.assigned_agent_id
        if (
            task.status not in _ACTIVE_TASKS
            or task.created_by_kind != "session"
            or task.created_by_id not in parent_session_ids
            or not worker_id or worker_id == agent_id
        ):
            continue
        worker = await get_agent(worker_id)
        if worker is None or getattr(worker, "role", "worker") != "worker":
            continue
        if (task.id, worker_id) in active_holders or is_busy_on(worker, task):
            children.add(worker_id)

    if native_by_session is None:
        native_by_session = await db.subagent_counts_by_session(
            [session.id for session in owned]
        )

    parent = await get_agent(agent_id)
    parent_task = task_by_id.get(parent.current_task_id) if parent is not None else None
    live = [session for session in owned if session.state in _ACTIVE_SESSIONS]
    running = bool(live) or bool(
        parent_task is not None and parent_task.status in _ACTIVE_TASKS
        and parent_task.assigned_agent_id == agent_id and is_busy_on(parent, parent_task)
    )

    def events(session_id: str) -> Mapping[str, int]:
        return native_by_session.get(session_id) or _NO_EVENTS

    # Lifetime is every session this agent ever owned, live or not: a child
    # that ran an hour ago is still a child this agent spawned.
    spawned_total = sum(events(session.id)["starts"] for session in owned)

    if not running:
        # Nothing is executing, so nothing can be mid-flight. This is a real
        # zero, not an absence of telemetry.
        native_count = 0
        complete = True
    elif live and all(session.hooks_provisioned for session in live):
        # ``max(0, ...)`` tolerates a Stop whose Start was never delivered;
        # the alternative is a session pinned at a phantom child forever.
        native_count = sum(
            max(0, events(session.id)["starts"] - events(session.id)["stops"])
            for session in live
        )
        complete = True
    else:
        # Running, but at least one live session was launched without its
        # hook file wired (a harness with no hook support, or a checkout
        # where we withheld the trust flag). Say so.
        native_count = None
        complete = False

    aq_count = len(children)
    return {
        "active_subagent_count": None if native_count is None else aq_count + native_count,
        "subagent_count_complete": complete,
        "aq_subagent_count": aq_count,
        "native_subagent_count": native_count,
        "subagents_spawned_total": spawned_total,
    }


def flock_rollup(rows: Sequence[Mapping]) -> dict:
    """Sum active sub-agents across the flock, and per profile.

    *rows* are the dicts :func:`~src.agents.service.list_agent_flock`
    produces.  ``complete`` is the conjunction, not a majority: one live
    session without hooks makes the flock total a lower bound, and the
    caveat belongs on the total rather than hidden on one agent's row.
    """
    def blank(profile_id: str | None = None) -> dict:
        out = {
            "active_total": 0,
            "native_total": 0,
            "aq_total": 0,
            "spawned_total": 0,
            "complete": True,
        }
        if profile_id is not None:
            out["profile_id"] = profile_id
        return out

    total = blank()
    by_profile: dict[str, dict] = {}
    for row in rows:
        bucket = by_profile.setdefault(
            row.get("profile_id") or "", blank(row.get("profile_id") or "")
        )
        native = row.get("native_subagent_count")
        aq = int(row.get("aq_subagent_count") or 0)
        spawned = int(row.get("subagents_spawned_total") or 0)
        for target in (total, bucket):
            target["aq_total"] += aq
            target["spawned_total"] += spawned
            target["native_total"] += int(native or 0)
            target["active_total"] += aq + int(native or 0)
            if not row.get("subagent_count_complete", False):
                target["complete"] = False
    return {
        "totals": total,
        "by_profile": [by_profile[key] for key in sorted(by_profile)],
    }
