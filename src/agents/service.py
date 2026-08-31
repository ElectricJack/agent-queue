"""Aggregate global worker definitions with their exact current execution."""
from __future__ import annotations

from types import SimpleNamespace

from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
from src.models import AgentState, TaskStatus

_ACTIVE_SESSIONS = {"starting", "running", "draining"}
_ACTIVE_TASKS = {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_INPUT}


def configured_settings(agent) -> dict:
    return {
        "name": agent.name,
        "profile_id": agent.profile_id,
        "harness": getattr(agent, "harness", None),
        "model": getattr(agent, "model", None),
        "intelligence_class": getattr(agent, "intelligence_class", None),
        "enabled": getattr(agent, "enabled", True),
    }


async def list_agent_flock(orchestrator, *, project_id: str | None = None) -> list[dict]:
    """Return durable identities; a project filter means current assignment."""
    from src.agents.subagents import subagent_counts

    db = orchestrator.db
    agents = await db.list_agents()
    sessions = await db.list_sessions()
    tasks = await db.list_tasks()
    task_by_id = {task.id: task for task in tasks}
    questions_by_session: dict[str, list[dict]] = {}
    for question in await db.list_agent_questions(project_id=project_id, pending_only=True):
        questions_by_session.setdefault(question["session_id"], []).append(question)
    profiles = {profile.id: profile for profile in await db.list_profiles()}
    registry = getattr(orchestrator, "harness_registry", None)
    builder = getattr(orchestrator, "session_spec_builder", None)
    rows = []
    for agent in agents:
        owned = [
            session for session in sessions
            if session.agent_id == agent.id or (
                not session.agent_id and agent.current_task_id
                and session.task_id == agent.current_task_id
            )
        ]

        def is_current_session(candidate) -> bool:
            if candidate.state not in _ACTIVE_SESSIONS:
                return False
            if not candidate.task_id:
                return True
            owned_task = task_by_id.get(candidate.task_id)
            if (
                owned_task is None or owned_task.status not in _ACTIVE_TASKS
                or owned_task.assigned_agent_id != agent.id
            ):
                return False
            return (
                candidate.last_claim_epoch is None
                or candidate.last_claim_epoch == owned_task.claim_epoch
            )

        # Include exact legacy task links even when older linked history exists.
        # A stale process cannot claim a task that now belongs to another worker.
        owned.sort(
            key=lambda candidate: (
                is_current_session(candidate),
                bool(candidate.task_id and candidate.task_id == agent.current_task_id),
                candidate.started_at,
            ),
            reverse=True,
        )
        session = owned[0] if owned else None
        live = session if session and is_current_session(session) else None
        task_id = live.task_id if live and live.task_id else agent.current_task_id
        task = task_by_id.get(task_id)
        if task is not None and (
            task.status not in _ACTIVE_TASKS or task.assigned_agent_id != agent.id
        ):
            task = None
        current_project = task.project_id if task else (live.project_id if live else None)
        if project_id is not None and current_project != project_id:
            continue

        profile = profiles.get(agent.profile_id)
        if task is not None and task.profile_id:
            profile = (
                profiles.get(f"project:{task.project_id}:{task.profile_id}")
                or profiles.get(task.profile_id) or profile
            )
        effective = apply_agent_overrides(profile, agent)
        harness_id = (
            live.harness if live
            else getattr(effective, "harness", None)
            or getattr(agent, "harness", None)
        )
        harness = registry.get(harness_id, current_project) if registry and harness_id else None
        if harness is None:
            harness = SimpleNamespace(id=harness_id or "", command=harness_id or "", provider="")
        settings = {"llm_provider": None, "model": None, "intelligence_class": None}
        if effective is not None and builder is not None:
            settings = resolve_launch_settings(
                effective, harness, builder, task.intelligence_class if task else None,
            )
        elif effective is not None:
            settings["model"] = getattr(effective, "model", None) or None
            settings["intelligence_class"] = getattr(effective, "default_class", None) or None
        if live:
            # Null on legacy rows means unknown at launch; never claim today's
            # edited profile is the configuration of an already running process.
            settings = {
                "llm_provider": getattr(live, "llm_provider", None),
                "model": getattr(live, "model", None),
                "intelligence_class": getattr(live, "intelligence_class", None),
            }
            if not settings["llm_provider"]:
                from src.sessions.spec import _infer_provider_from_harness
                settings["llm_provider"] = (
                    getattr(harness, "provider", "") or _infer_provider_from_harness(harness) or None
                )
        waiting_question = None
        if (
            live and task and live.lifecycle in {"task", "pool"}
            and live.desired_state == "running"
            and task.status == TaskStatus.IN_PROGRESS
            and (live.lifecycle != "pool" or live.claim_phase == "active")
        ):
            matching = [
                question for question in questions_by_session.get(live.id, [])
                if (
                    question.get("state") in {"supervisor", "human", "answered"}
                    and question.get("session_name") == live.name
                    and question.get("instance_token") == live.instance_token
                    and question.get("task_id") == task.id
                    and question.get("project_id") == task.project_id
                    and question.get("agent_id") == agent.id
                    and question.get("claim_epoch") == task.claim_epoch
                )
            ]
            if matching:
                current_question = max(matching, key=lambda question: question["created_at"])
                # Do not expose instance fencing tokens or saved answer content.
                waiting_question = {
                    key: current_question[key]
                    for key in ("id", "question", "state", "requires_human", "created_at")
                }
        count = await subagent_counts(db, agent.id, sessions, tasks)
        state = agent.state.value.lower()
        if live:
            state = "busy" if task else live.state
        elif not getattr(agent, "enabled", True) and agent.state != AgentState.BUSY:
            state = "paused"
        elif agent.role == "supervisor" and session:
            state = session.state
        rows.append({
            "id": agent.id, "name": agent.name, "profile_id": agent.profile_id,
            "role": agent.role, "enabled": agent.enabled, "state": state,
            "provider": settings["llm_provider"], "harness": harness_id,
            "model": settings["model"], "intelligence_class": settings["intelligence_class"],
            "current_task_id": task.id if task else None,
            "current_task_title": task.title if task else None,
            "current_project_id": current_project, "project_id": current_project,
            "workspace_id": None,
            "session_id": live.id if live else None,
            "waiting_question": waiting_question,
            "session_state": session.state if session else None,
            "session_provider": session.provider if session else None,
            "settings": configured_settings(agent),
            "last_heartbeat": agent.last_heartbeat,
            "session_tokens_used": agent.session_tokens_used,
            **count,
        })
    return sorted(rows, key=lambda row: (row["role"] != "supervisor", row["name"].casefold(), row["id"]))
