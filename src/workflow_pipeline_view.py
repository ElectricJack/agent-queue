"""Dashboard-ready workflow pipeline view — structured JSON for interactive rendering.

Produces a complete pipeline view of a coordination workflow suitable for
dashboard rendering.  Stages become horizontal pipeline columns, tasks within
each stage become cards with status badges, agent assignments show who is
working on what, and progress bars indicate completion.

This module is the data layer for spec §11 Q6 (Workflow Visualization):

- **Pipeline view**: stages as sequential pipeline columns, tasks as cards
  within each stage, agent assignments as badges, progress as bars.
- **Live state**: highlight the current stage and show task progress within it.
- **Agent assignments**: show which agents are working on which tasks with
  affinity indicators.
- **Progress summary**: overall workflow progress with task counts by status.

All functions are pure — they accept pre-fetched data and return dicts.  No
database access; the caller (command handler) is responsible for fetching
workflows and tasks.

See ``docs/specs/design/agent-coordination.md`` §11 Q6 and the
``workflow_pipeline_view`` command for related prior work.

Roadmap 7.6.1.

Example usage::

    from src.workflow_pipeline_view import build_pipeline_view
    from src.models import Workflow, Task

    workflow = ...   # fetched from database
    tasks = [...]    # all tasks in the workflow

    view = build_pipeline_view(workflow, tasks)
    # view is a dict ready for JSON serialization to a dashboard frontend

    # With agent details:
    view = build_pipeline_view(workflow, tasks, agents=agent_list)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.models import Task, Workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage status classification & color palette
# ---------------------------------------------------------------------------

STAGE_STATUS_COLORS: dict[str, dict[str, str]] = {
    "completed": {"fill": "#4CAF50", "stroke": "#2E7D32", "text": "#ffffff"},
    "active": {"fill": "#2196F3", "stroke": "#0D47A1", "text": "#ffffff"},
    "pending": {"fill": "#E0E0E0", "stroke": "#9E9E9E", "text": "#616161"},
    "failed": {"fill": "#F44336", "stroke": "#C62828", "text": "#ffffff"},
    "paused": {"fill": "#FF9800", "stroke": "#E65100", "text": "#ffffff"},
}

STAGE_STATUS_SYMBOLS: dict[str, str] = {
    "completed": "\u2713",  # checkmark
    "active": "\u25b6",  # play
    "pending": "\u25cb",  # circle
    "failed": "\u2717",  # cross
    "paused": "\u23f8",  # pause
}

# Task status → display category mapping
TASK_STATUS_CATEGORY: dict[str, str] = {
    "DEFINED": "pending",
    "READY": "pending",
    "ASSIGNED": "active",
    "IN_PROGRESS": "active",
    "WAITING_INPUT": "active",
    "PAUSED": "paused",
    "AWAITING_APPROVAL": "active",
    "AWAITING_PLAN_APPROVAL": "active",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "BLOCKED": "blocked",
}

TASK_STATUS_COLORS: dict[str, dict[str, str]] = {
    "pending": {"fill": "#E3F2FD", "stroke": "#1565C0", "text": "#000000"},
    "active": {"fill": "#FFF3E0", "stroke": "#E65100", "text": "#000000"},
    "completed": {"fill": "#E8F5E9", "stroke": "#2E7D32", "text": "#000000"},
    "failed": {"fill": "#FFEBEE", "stroke": "#C62828", "text": "#000000"},
    "paused": {"fill": "#FFF8E1", "stroke": "#F57F17", "text": "#000000"},
    "blocked": {"fill": "#FCE4EC", "stroke": "#880E4F", "text": "#000000"},
}

# Agent type display colors
AGENT_TYPE_COLORS: dict[str, dict[str, str]] = {
    "coding": {"fill": "#E3F2FD", "stroke": "#1565C0", "text": "#0D47A1"},
    "code-review": {"fill": "#F3E5F5", "stroke": "#7B1FA2", "text": "#4A148C"},
    "qa": {"fill": "#E8F5E9", "stroke": "#2E7D32", "text": "#1B5E20"},
    "research": {"fill": "#FFF3E0", "stroke": "#E65100", "text": "#BF360C"},
    "default": {"fill": "#F5F5F5", "stroke": "#616161", "text": "#212121"},
}

# Affinity reason indicators
AFFINITY_SYMBOLS: dict[str, str] = {
    "context": "\U0001f9e0",  # brain — has conversation context
    "workspace": "\U0001f4c2",  # folder — has workspace locked
    "type": "\U0001f3af",  # target — type match
}


# ---------------------------------------------------------------------------
# Task classification helpers
# ---------------------------------------------------------------------------


def _task_status_category(task: Task) -> str:
    """Map a task's status to a display category."""
    status_str = task.status.value if hasattr(task.status, "value") else str(task.status)
    return TASK_STATUS_CATEGORY.get(status_str, "pending")


def _task_progress(task: Task) -> float:
    """Compute a 0.0-1.0 progress indicator for a task."""
    category = _task_status_category(task)
    if category == "completed":
        return 1.0
    if category == "active":
        return 0.5  # in-progress tasks are roughly halfway
    if category == "failed":
        return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Stage inference from tasks (when explicit stages aren't stored)
# ---------------------------------------------------------------------------


def _infer_stages_from_tasks(
    workflow: Workflow,
    tasks: list[Task],
) -> list[dict[str, Any]]:
    """Infer stage groupings when the workflow doesn't have explicit stages.

    Uses a simple heuristic: group tasks by creation-time proximity.  Tasks
    created within a short window of each other are assumed to belong to the
    same stage.  Falls back to a single "all tasks" stage when timestamps
    are unavailable or too close.

    Returns a list of stage dicts compatible with the ``Workflow.stages``
    format.
    """
    if not tasks:
        return []

    # Sort by creation time
    sorted_tasks = sorted(tasks, key=lambda t: t.created_at or 0)

    # Group by time proximity — tasks more than 30s apart start a new stage
    gap_threshold = 30.0  # seconds
    groups: list[list[Task]] = []
    current_group: list[Task] = [sorted_tasks[0]]

    for task in sorted_tasks[1:]:
        prev_time = current_group[-1].created_at or 0
        curr_time = task.created_at or 0
        if curr_time - prev_time > gap_threshold:
            groups.append(current_group)
            current_group = [task]
        else:
            current_group.append(task)
    groups.append(current_group)

    # Build stage dicts
    stages = []
    for i, group in enumerate(groups):
        task_ids = [t.id for t in group]
        # Use current_stage name if this is the last (active) group
        stage_name = f"stage-{i + 1}"
        if i == len(groups) - 1 and workflow.current_stage:
            stage_name = workflow.current_stage
        elif i == 0 and len(groups) == 1 and workflow.current_stage:
            stage_name = workflow.current_stage

        # Determine stage status from tasks
        statuses = {_task_status_category(t) for t in group}
        if all(s == "completed" for s in statuses):
            stage_status = "completed"
        elif "failed" in statuses:
            stage_status = "failed"
        elif "active" in statuses:
            stage_status = "active"
        elif "paused" in statuses:
            stage_status = "paused"
        else:
            stage_status = "pending"

        # Timestamps from tasks
        created_times = [t.created_at for t in group if t.created_at]
        started_at = min(created_times) if created_times else None

        stages.append(
            {
                "name": stage_name,
                "task_ids": task_ids,
                "status": stage_status,
                "started_at": started_at,
                "completed_at": None,  # can't infer from tasks alone
            }
        )

    return stages


def _resolve_stages(
    workflow: Workflow,
    tasks: list[Task],
) -> list[dict[str, Any]]:
    """Get the effective stage list for a workflow.

    Uses explicit ``workflow.stages`` if available, otherwise infers stages
    from task creation timestamps.
    """
    if workflow.stages:
        return list(workflow.stages)
    return _infer_stages_from_tasks(workflow, tasks)


# ---------------------------------------------------------------------------
# Build individual pipeline view components
# ---------------------------------------------------------------------------


def build_task_card(
    task: Task,
    agent_affinity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a task card for display within a pipeline stage.

    Each card includes the task's identity, status, agent assignment,
    and progress indicator.
    """
    status_str = task.status.value if hasattr(task.status, "value") else str(task.status)
    category = _task_status_category(task)
    colors = TASK_STATUS_COLORS.get(category, TASK_STATUS_COLORS["pending"])

    card: dict[str, Any] = {
        "task_id": task.id,
        "title": task.title,
        "status": status_str,
        "status_category": category,
        "colors": colors,
        "progress": _task_progress(task),
    }

    # Agent assignment
    if task.assigned_agent_id:
        card["assigned_agent"] = task.assigned_agent_id

    # Agent type requirement
    if task.agent_type:
        card["agent_type"] = task.agent_type
        card["agent_type_colors"] = AGENT_TYPE_COLORS.get(
            task.agent_type, AGENT_TYPE_COLORS["default"]
        )

    # Affinity preference
    if task.affinity_agent_id:
        card["affinity_agent"] = task.affinity_agent_id
        reason = task.affinity_reason or "context"
        card["affinity_reason"] = reason
        card["affinity_symbol"] = AFFINITY_SYMBOLS.get(reason, "")

    # Task type for visual categorization
    if task.task_type:
        type_val = task.task_type.value if hasattr(task.task_type, "value") else str(task.task_type)
        card["task_type"] = type_val

    # Workspace mode
    if task.workspace_mode:
        mode_val = (
            task.workspace_mode.value
            if hasattr(task.workspace_mode, "value")
            else str(task.workspace_mode)
        )
        card["workspace_mode"] = mode_val

    # Timing
    if task.created_at:
        card["created_at"] = task.created_at

    # PR link
    if task.pr_url:
        card["pr_url"] = task.pr_url

    # Retry info
    if task.retry_count > 0:
        card["retry_count"] = task.retry_count
        card["max_retries"] = task.max_retries

    return card


def build_stages(
    workflow: Workflow,
    tasks: list[Task],
    *,
    include_task_details: bool = True,
) -> list[dict[str, Any]]:
    """Build the stage list for the pipeline view.

    Each stage includes its name, status, task cards, agent assignments,
    and progress indicators.  Stages are ordered by their position in the
    pipeline (first stage is index 0).
    """
    stage_defs = _resolve_stages(workflow, tasks)
    task_map = {t.id: t for t in tasks}

    stages: list[dict[str, Any]] = []
    for i, stage_def in enumerate(stage_defs):
        stage_task_ids = stage_def.get("task_ids", [])
        stage_tasks = [task_map[tid] for tid in stage_task_ids if tid in task_map]

        # Compute stage status from its tasks (override stored status if tasks exist)
        if stage_tasks:
            categories = [_task_status_category(t) for t in stage_tasks]
            if all(c == "completed" for c in categories):
                computed_status = "completed"
            elif "failed" in categories:
                computed_status = "failed"
            elif "active" in categories:
                computed_status = "active"
            elif "paused" in categories:
                computed_status = "paused"
            else:
                computed_status = "pending"
        else:
            computed_status = stage_def.get("status", "pending")

        stage_name = stage_def.get("name", f"stage-{i + 1}")
        colors = STAGE_STATUS_COLORS.get(computed_status, STAGE_STATUS_COLORS["pending"])
        symbol = STAGE_STATUS_SYMBOLS.get(computed_status, "\u25cb")

        # Compute progress
        total = len(stage_tasks) if stage_tasks else len(stage_task_ids)
        completed = sum(1 for t in stage_tasks if _task_status_category(t) == "completed")
        progress = completed / total if total > 0 else 0.0

        stage_data: dict[str, Any] = {
            "name": stage_name,
            "order": i,
            "status": computed_status,
            "symbol": symbol,
            "colors": colors,
            "task_count": total,
            "completed_count": completed,
            "progress": round(progress, 3),
            "is_current": stage_name == workflow.current_stage,
        }

        # Task cards
        if include_task_details and stage_tasks:
            stage_data["tasks"] = [build_task_card(t, workflow.agent_affinity) for t in stage_tasks]

        # Agent assignments within this stage
        agent_assignments: dict[str, list[str]] = {}
        for t in stage_tasks:
            agent_id = t.assigned_agent_id or t.affinity_agent_id
            if agent_id:
                agent_assignments.setdefault(agent_id, []).append(t.id)
        if agent_assignments:
            stage_data["agent_assignments"] = agent_assignments

        # Timing
        if stage_def.get("started_at"):
            stage_data["started_at"] = stage_def["started_at"]
        if stage_def.get("completed_at"):
            stage_data["completed_at"] = stage_def["completed_at"]

        stages.append(stage_data)

    return stages


def build_stage_connections(
    stages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build connections between sequential pipeline stages.

    Returns a list of edges connecting each stage to the next, suitable
    for rendering pipeline connectors (arrows) in the dashboard.
    """
    connections: list[dict[str, Any]] = []
    for i in range(len(stages) - 1):
        source = stages[i]
        target = stages[i + 1]

        # Connection status based on whether the source stage is done
        if source["status"] == "completed":
            conn_status = "completed"
            conn_color = "#4CAF50"
        elif source["status"] in ("active", "paused"):
            conn_status = "active"
            conn_color = "#2196F3"
        else:
            conn_status = "pending"
            conn_color = "#BDBDBD"

        connections.append(
            {
                "from_stage": source["name"],
                "to_stage": target["name"],
                "from_order": source["order"],
                "to_order": target["order"],
                "status": conn_status,
                "color": conn_color,
            }
        )

    return connections


def build_progress_summary(
    workflow: Workflow,
    tasks: list[Task],
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build an overall progress summary for the workflow.

    Returns aggregate counts and percentages for the entire pipeline.
    """
    total_tasks = len(tasks)
    categories = [_task_status_category(t) for t in tasks]

    completed = categories.count("completed")
    active = categories.count("active")
    failed = categories.count("failed")
    paused = categories.count("paused")
    blocked = categories.count("blocked")
    pending = total_tasks - completed - active - failed - paused - blocked

    overall_progress = completed / total_tasks if total_tasks > 0 else 0.0

    total_stages = len(stages)
    completed_stages = sum(1 for s in stages if s["status"] == "completed")
    active_stages = sum(1 for s in stages if s["status"] == "active")

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed,
        "active_tasks": active,
        "failed_tasks": failed,
        "paused_tasks": paused,
        "blocked_tasks": blocked,
        "pending_tasks": pending,
        "overall_progress": round(overall_progress, 3),
        "total_stages": total_stages,
        "completed_stages": completed_stages,
        "active_stages": active_stages,
        "pending_stages": total_stages - completed_stages - active_stages,
    }


def build_agent_summary(
    tasks: list[Task],
    agents: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a summary of agent involvement in the workflow.

    Returns a dict mapping agent_id to their workload summary: current task,
    tasks completed, agent type, and status.
    """
    agent_map: dict[str, dict[str, Any]] = {}

    for task in tasks:
        agent_id = task.assigned_agent_id
        if not agent_id:
            continue

        if agent_id not in agent_map:
            agent_map[agent_id] = {
                "agent_id": agent_id,
                "current_task": None,
                "tasks_completed": 0,
                "tasks_assigned": 0,
                "tasks_failed": 0,
                "agent_type": task.agent_type,
                "colors": AGENT_TYPE_COLORS.get(
                    task.agent_type or "default", AGENT_TYPE_COLORS["default"]
                ),
            }

        agent_map[agent_id]["tasks_assigned"] += 1

        category = _task_status_category(task)
        if category == "completed":
            agent_map[agent_id]["tasks_completed"] += 1
        elif category == "active":
            agent_map[agent_id]["current_task"] = task.id
        elif category == "failed":
            agent_map[agent_id]["tasks_failed"] += 1

    # Enrich with agent details if available
    if agents:
        for agent_info in agents:
            aid = agent_info.get("id") or agent_info.get("agent_id")
            if aid and aid in agent_map:
                agent_map[aid]["name"] = agent_info.get("name", aid)
                if agent_info.get("state"):
                    agent_map[aid]["state"] = agent_info["state"]

    return agent_map


def build_affinity_overlay(
    workflow: Workflow,
    tasks: list[Task],
) -> dict[str, Any]:
    """Build an overlay showing agent affinity relationships.

    Returns a visualization of which agents are preferred for which tasks
    and why, so the dashboard can draw affinity arcs between agents and tasks.
    """
    affinities: list[dict[str, Any]] = []

    for task in tasks:
        if not task.affinity_agent_id:
            continue

        entry: dict[str, Any] = {
            "task_id": task.id,
            "preferred_agent": task.affinity_agent_id,
            "reason": task.affinity_reason or "context",
            "symbol": AFFINITY_SYMBOLS.get(task.affinity_reason or "context", ""),
            "is_honored": task.assigned_agent_id == task.affinity_agent_id,
        }

        # Check workflow-level affinity too
        wf_affinity = workflow.agent_affinity.get(task.id)
        if wf_affinity and wf_affinity != task.affinity_agent_id:
            entry["workflow_affinity"] = wf_affinity

        affinities.append(entry)

    total = len(affinities)
    honored = sum(1 for a in affinities if a["is_honored"])

    return {
        "affinities": affinities,
        "total": total,
        "honored": honored,
        "honor_rate": round(honored / total, 3) if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Main entry point: build_pipeline_view
# ---------------------------------------------------------------------------


def build_pipeline_view(
    workflow: Workflow,
    tasks: list[Task],
    *,
    agents: list[dict[str, Any]] | None = None,
    include_task_details: bool = True,
    include_affinity: bool = True,
    direction: str = "LR",
) -> dict[str, Any]:
    """Build the complete pipeline view data structure for dashboard rendering.

    Combines workflow metadata, stage pipeline, task cards, agent assignments,
    and progress indicators into a single JSON-serializable dict.

    Parameters
    ----------
    workflow:
        The workflow to visualize.
    tasks:
        All tasks belonging to this workflow (pre-fetched).
    agents:
        Optional agent details for enriching agent summaries.
    include_task_details:
        Include individual task cards in each stage. Default: ``True``.
    include_affinity:
        Include agent affinity overlay. Default: ``True``.
    direction:
        Layout direction — ``"LR"`` (left-right pipeline) or ``"TD"``
        (top-down).  Default: ``"LR"`` (natural pipeline flow).

    Returns
    -------
    dict
        A JSON-serializable dict with keys:

        - ``workflow``: identity and status metadata
        - ``pipeline``: stages with tasks and connections
        - ``progress``: overall progress summary
        - ``agents``: agent assignment summary
        - ``affinity``: (optional) agent affinity overlay
        - ``layout``: layout direction metadata
        - ``legend``: color legend for stage/task statuses
    """
    # Filter tasks to only those in this workflow
    workflow_tasks = [t for t in tasks if t.workflow_id == workflow.workflow_id]

    # If no tasks match the workflow_id filter, use all provided tasks
    # (caller may have already filtered)
    if not workflow_tasks and tasks:
        workflow_tasks = tasks

    # Build stages
    stages = build_stages(
        workflow,
        workflow_tasks,
        include_task_details=include_task_details,
    )

    # Build connections between stages
    connections = build_stage_connections(stages)

    # Build progress summary
    progress = build_progress_summary(workflow, workflow_tasks, stages)

    # Build agent summary
    agent_summary = build_agent_summary(workflow_tasks, agents)

    # Build result
    result: dict[str, Any] = {
        "workflow": {
            "workflow_id": workflow.workflow_id,
            "playbook_id": workflow.playbook_id,
            "playbook_run_id": workflow.playbook_run_id,
            "project_id": workflow.project_id,
            "status": workflow.status,
            "current_stage": workflow.current_stage,
            "created_at": workflow.created_at,
            "completed_at": workflow.completed_at,
        },
        "pipeline": {
            "stages": stages,
            "connections": connections,
            "stage_count": len(stages),
        },
        "progress": progress,
        "agents": agent_summary,
        "layout": {
            "direction": direction,
        },
        "legend": _build_pipeline_legend(),
    }

    # Optional affinity overlay
    if include_affinity and workflow_tasks:
        result["affinity"] = build_affinity_overlay(workflow, workflow_tasks)

    return result


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def _build_pipeline_legend() -> dict[str, Any]:
    """Build the color legend for the pipeline view."""
    return {
        "stage_statuses": {
            status: {
                "symbol": STAGE_STATUS_SYMBOLS.get(status, "\u25cb"),
                "colors": colors,
                "label": status.title(),
            }
            for status, colors in STAGE_STATUS_COLORS.items()
        },
        "task_statuses": {
            category: {
                "colors": colors,
                "label": category.title(),
            }
            for category, colors in TASK_STATUS_COLORS.items()
        },
        "agent_types": {
            atype: {
                "colors": colors,
                "label": atype.replace("-", " ").title(),
            }
            for atype, colors in AGENT_TYPE_COLORS.items()
        },
        "affinity_reasons": {
            reason: {
                "symbol": symbol,
                "label": reason.title(),
            }
            for reason, symbol in AFFINITY_SYMBOLS.items()
        },
    }
