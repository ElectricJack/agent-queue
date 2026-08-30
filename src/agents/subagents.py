"""Count direct AQ delegation without claiming unobserved native activity."""
from __future__ import annotations

from collections.abc import Sequence

from src.models import AgentState, SessionRecord, Task, TaskStatus

_ACTIVE_SESSIONS = {"starting", "running", "draining"}
_ACTIVE_TASKS = {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_INPUT}


async def subagent_counts(
    db, agent_id: str, sessions: Sequence[SessionRecord], tasks: Sequence[Task],
) -> dict:
    """Count distinct active direct workers across all of an agent's sessions.

    ``tasks`` must contain only live (not archived) task rows. A task tree is
    not delegation provenance: only its authenticated creating session links
    a worker to the parent. Read this from old sessions too, since children
    can outlive the parent's current session.
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

    parent = await get_agent(agent_id)
    parent_task = task_by_id.get(parent.current_task_id) if parent is not None else None
    native_active = any(session.state in _ACTIVE_SESSIONS for session in owned) or bool(
        parent_task is not None and parent_task.status in _ACTIVE_TASKS
        and parent_task.assigned_agent_id == agent_id and is_busy_on(parent, parent_task)
    )
    # Existing Claude/Codex transcript readers do not retain authoritative child
    # start/finish events. A live runtime therefore has unknown native coverage;
    # message activity, tool names and transcript parent UUIDs cannot establish it.
    native_count = None if native_active else 0
    aq_count = len(children)
    return {
        "active_subagent_count": None if native_count is None else aq_count + native_count,
        "subagent_count_complete": native_count is not None,
        "aq_subagent_count": aq_count,
        "native_subagent_count": native_count,
    }
