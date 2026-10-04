"""One flock row per execution, independent of worker capacity definitions."""
from __future__ import annotations

import time


def session_role(session, agent=None, task=None) -> str:
    """Classify the persisted launch identity, including legacy unnamed owners."""
    identity = session.profile_id.casefold()
    if task is not None:
        title = task.title.casefold()
        if title.startswith(("repair development integration:", "repair for integration subject")):
            return "repair"
        if title.startswith("verifier for integration subject"):
            return "verifier"
        if title.startswith("adversarial review:"):
            return "reviewer"
    for fragment, role in (
        ("supervisor", "supervisor"), ("repair", "repair"),
        ("verifier", "verifier"), ("verify", "verifier"),
        ("review", "reviewer"),
    ):
        if fragment in identity:
            return role
    if agent is not None:
        return agent.role
    return "worker" if session.lifecycle in {"task", "pool"} else "other"


def flock_session_rows(sessions, agents, *, tasks=(), project_id=None, include_stopped=False) -> list[dict]:
    """Never fold two sessions into one agent row or suppress a missing owner."""
    owners = {agent.id: agent for agent in agents}
    task_by_id = {task.id: task for task in tasks}
    now = time.time()
    rows = []
    for session in sessions:
        if project_id is not None and session.project_id != project_id:
            continue
        if not include_stopped and session.state in {"stopped", "quarantined"}:
            continue
        agent = owners.get(session.agent_id)
        rows.append({
            "session_id": session.id, "name": session.name,
            "agent_id": session.agent_id, "role": session_role(session, agent, task_by_id.get(session.task_id)),
            "project_id": session.project_id, "scope": session.project_id or "global",
            "provider": session.llm_provider, "harness": session.harness,
            "model": session.model, "intelligence_class": session.intelligence_class,
            "profile_id": session.profile_id, "task_id": session.task_id,
            "state": session.state, "desired_state": session.desired_state,
            "started_at": session.started_at, "last_activity": session.last_activity,
            "uptime_seconds": max(0, (session.ended_at or now) - session.started_at),
            "lifecycle": session.lifecycle,
        })
    return sorted(rows, key=lambda row: (
        row["role"] != "supervisor", row["scope"], -row["started_at"], row["session_id"],
    ))
