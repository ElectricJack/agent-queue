"""Helpers to build notification events from domain objects.

These builders convert internal domain models (Task, WorkspaceAgent, etc.)
into the Pydantic API models (TaskDetail, AgentSummary) used by notification
events.  This ensures notifications carry the same data shapes as the REST
API, and centralises the conversion logic in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.api.models import TaskRef
from src.api.models.agent import AgentSettings, AgentSummary
from src.api.models.task import TaskDetail

if TYPE_CHECKING:
    from src.database import Database
    from src.models import Agent, Task, WorkspaceAgent


def build_task_detail(task: Task) -> TaskDetail:
    """Convert a domain Task to a TaskDetail API model.

    This is a lightweight conversion that maps task fields without
    fetching dependencies from the database.  For full dependency
    information, use ``build_task_detail_full()``.
    """
    return TaskDetail(
        id=task.id,
        project_id=task.project_id,
        title=task.title,
        description=task.description or "",
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        priority=task.priority,
        assigned_agent=task.assigned_agent_id,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        integration_mode=task.integration_mode,
        is_plan_subtask=task.is_plan_subtask,
        task_type=task.task_type.value if task.task_type else None,
        parent_task_id=task.parent_task_id,
        profile_id=task.profile_id,
        skip_verification=task.skip_verification,
        pr_url=task.pr_url,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def build_task_detail_full(task: Task, db: Database) -> TaskDetail:
    """Convert a domain Task to a TaskDetail with dependency information.

    Fetches upstream dependencies, downstream blockers, and subtasks
    from the database to populate the full TaskDetail model.
    """
    detail = build_task_detail(task)

    # Populate dependencies
    try:
        dep_ids = await db.get_task_dependencies(task.id)
        deps = []
        for dep_id in dep_ids:
            dep_task = await db.get_task(dep_id)
            if dep_task:
                deps.append(
                    TaskRef(
                        id=dep_task.id,
                        title=dep_task.title,
                        status=(
                            dep_task.status.value
                            if hasattr(dep_task.status, "value")
                            else str(dep_task.status)
                        ),
                    )
                )
        detail.depends_on = deps
    except Exception:
        pass

    # Populate blockers (tasks that depend on this one)
    try:
        blocker_ids = await db.get_dependents(task.id)
        blockers = []
        for bid in blocker_ids:
            b_task = await db.get_task(bid)
            if b_task:
                blockers.append(
                    TaskRef(
                        id=b_task.id,
                        title=b_task.title,
                        status=(
                            b_task.status.value
                            if hasattr(b_task.status, "value")
                            else str(b_task.status)
                        ),
                    )
                )
        detail.blocks = blockers
    except Exception:
        pass

    return detail


def build_agent_summary(agent: Agent | WorkspaceAgent) -> AgentSummary:
    """Keep worker identity and configured settings in task notifications.

    Workspace-only callers remain supported, without treating a global
    worker's ID as a workspace ID. Live model metadata needs a session snapshot
    and is deliberately left unknown by this synchronous converter.
    """
    workspace_id = getattr(agent, "workspace_id", None)
    agent_id = getattr(agent, "id", None) or workspace_id or ""
    project_id = getattr(agent, "project_id", None)
    name = getattr(agent, "name", None) or getattr(agent, "workspace_name", None) or agent_id
    profile_id = getattr(agent, "profile_id", "")
    enabled = getattr(agent, "enabled", True)
    state_val = agent.state
    if hasattr(state_val, "value"):
        state_val = state_val.value

    return AgentSummary(
        id=agent_id,
        workspace_id=workspace_id,
        project_id=project_id,
        current_project_id=project_id,
        name=name,
        profile_id=profile_id,
        role=getattr(agent, "role", "worker"),
        enabled=enabled,
        state=state_val,
        current_task_id=getattr(agent, "current_task_id", None),
        current_task_title=getattr(agent, "current_task_title", None),
        settings=AgentSettings(
            name=name,
            profile_id=profile_id,
            harness=getattr(agent, "harness", None),
            model=getattr(agent, "model", None),
            intelligence_class=getattr(agent, "intelligence_class", None),
            enabled=enabled,
        ),
    )
