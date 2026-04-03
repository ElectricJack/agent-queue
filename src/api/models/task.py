"""Response models for task commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from . import TaskBrief, TaskRef


# ---------------------------------------------------------------------------
# Shared task structures
# ---------------------------------------------------------------------------


class TaskDetail(BaseModel):
    id: str
    project_id: str
    title: str
    description: str = ""
    status: str = ""
    priority: int = 0
    assigned_agent: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    requires_approval: bool = False
    is_plan_subtask: bool = False
    task_type: str | None = None
    parent_task_id: str | None = None
    profile_id: str | None = None
    auto_approve_plan: bool = False
    pr_url: str | None = None
    depends_on: list[TaskRef] = []
    blocks: list[TaskRef] = []
    subtasks: list[TaskRef] = []


class TaskDict(BaseModel):
    """Loose task dict as returned in list results."""

    model_config = {"extra": "allow"}
    id: str
    title: str = ""
    status: str = ""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ListTasksResponse(BaseModel):
    display_mode: str = "flat"
    tasks: list[dict[str, Any]] = []
    total: int = 0
    hidden_completed: int = 0
    filtered: bool = False
    dependency_display: str | None = None


class CreateTaskResponse(BaseModel):
    created: str
    title: str
    project_id: str
    requires_approval: bool = False
    task_type: str | None = None
    profile_id: str | None = None
    preferred_workspace_id: str | None = None
    attachments: list[str] | None = None
    auto_approve_plan: bool = False
    warning: str | None = None


class GetTaskResponse(TaskDetail):
    pass


class EditTaskResponse(BaseModel):
    updated: str
    fields: list[str]
    old_status: str | None = None
    new_status: str | None = None


class DeleteTaskResponse(BaseModel):
    deleted: str
    title: str


class ApproveTaskResponse(BaseModel):
    approved: str
    title: str


class ApprovePlanResponse(BaseModel):
    approved: str
    title: str
    subtask_count: int = 0
    subtasks: list[dict[str, Any]] = []


class RejectPlanResponse(BaseModel):
    rejected: str
    title: str
    status: str = "READY"
    feedback_added: bool = False
    draft_subtasks_deleted: int = 0


class DeletePlanResponse(BaseModel):
    deleted: str
    title: str
    status: str = "COMPLETED"
    draft_subtasks_deleted: int = 0


class StopTaskResponse(BaseModel):
    stopped: str


class RestartTaskResponse(BaseModel):
    restarted: str
    title: str
    previous_status: str = ""


class ReopenWithFeedbackResponse(BaseModel):
    reopened: str
    title: str
    previous_status: str = ""
    status: str = "READY"
    feedback_added: bool = False
    requires_approval: bool = False


class SkipTaskResponse(BaseModel):
    skipped: str
    unblocked_count: int = 0
    unblocked: list[TaskRef] = []


class ArchiveTaskResponse(BaseModel):
    archived: str
    title: str
    status: str = ""


class ArchiveTasksResponse(BaseModel):
    archived_count: int = 0
    archived_ids: list[str] = []
    archived: list[dict[str, Any]] = []
    archive_dir: str | None = None
    project_id: str | None = None


class RestoreTaskResponse(BaseModel):
    restored: str
    title: str
    new_status: str = "DEFINED"


class ListArchivedResponse(BaseModel):
    tasks: list[dict[str, Any]] = []
    count: int = 0
    total: int = 0
    project_id: str | None = None


class ArchiveSettingsResponse(BaseModel):
    enabled: bool = False
    after_hours: int = 0
    statuses: list[str] = []
    archived_count: int = 0
    eligible_count: int = 0


class SetTaskStatusResponse(BaseModel):
    task_id: str
    old_status: str
    new_status: str
    title: str


class AddDependencyResponse(BaseModel):
    ok: bool = True
    task_id: str
    depends_on: str
    task_title: str
    depends_on_title: str


class RemoveDependencyResponse(BaseModel):
    ok: bool = True
    task_id: str
    removed_dependency: str
    task_title: str


class TaskDepsResponse(BaseModel):
    task_id: str
    title: str
    status: str = ""
    depends_on: list[TaskRef] = []
    blocks: list[TaskRef] = []


class GetTaskDiffResponse(BaseModel):
    diff: str = ""
    branch: str = ""


class GetTaskResultResponse(BaseModel):
    model_config = {"extra": "allow"}


class GetTaskTreeResponse(BaseModel):
    root: dict[str, Any] = {}
    formatted: str = ""
    subtask_completed: int = 0
    subtask_total: int = 0
    subtask_by_status: dict[str, int] = {}
    progress_bar: str | None = None


class GetChainHealthResponse(BaseModel):
    model_config = {"extra": "allow"}
    task_id: str | None = None
    project_id: str | None = None
    status: str | None = None
    title: str | None = None
    stuck_downstream: list[dict[str, Any]] | None = None
    stuck_count: int | None = None
    stuck_chains: list[dict[str, Any]] | None = None
    total_stuck_chains: int | None = None
    message: str | None = None


class ListActiveTasksAllProjectsResponse(BaseModel):
    by_project: dict[str, list[dict[str, Any]]] = {}
    tasks: list[dict[str, Any]] = []
    total: int = 0
    project_count: int = 0
    hidden_completed: int = 0


class ProcessPlanResponse(BaseModel):
    model_config = {"extra": "allow"}
    status: str = ""
    project_id: str = ""
    task_id: str | None = None
    plan_path: str | None = None
    title: str | None = None
    phases: int | None = None
    draft_subtasks: int | None = None
    total_plan_files_found: int | None = None
    workspaces_scanned: int | None = None
    message: str | None = None
    note: str | None = None


class ProcessTaskCompletionResponse(BaseModel):
    model_config = {"extra": "allow"}
    plan_found: bool = False
    reason: str | None = None
    plan_file: str | None = None
    archived_path: str | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_tasks": ListTasksResponse,
    "create_task": CreateTaskResponse,
    "get_task": GetTaskResponse,
    "edit_task": EditTaskResponse,
    "delete_task": DeleteTaskResponse,
    "approve_task": ApproveTaskResponse,
    "approve_plan": ApprovePlanResponse,
    "reject_plan": RejectPlanResponse,
    "delete_plan": DeletePlanResponse,
    "stop_task": StopTaskResponse,
    "restart_task": RestartTaskResponse,
    "reopen_with_feedback": ReopenWithFeedbackResponse,
    "skip_task": SkipTaskResponse,
    "archive_task": ArchiveTaskResponse,
    "archive_tasks": ArchiveTasksResponse,
    "restore_task": RestoreTaskResponse,
    "list_archived": ListArchivedResponse,
    "archive_settings": ArchiveSettingsResponse,
    "set_task_status": SetTaskStatusResponse,
    "add_dependency": AddDependencyResponse,
    "remove_dependency": RemoveDependencyResponse,
    "task_deps": TaskDepsResponse,
    "get_task_dependencies": TaskDepsResponse,
    "get_task_diff": GetTaskDiffResponse,
    "get_task_result": GetTaskResultResponse,
    "get_task_tree": GetTaskTreeResponse,
    "get_chain_health": GetChainHealthResponse,
    "list_active_tasks_all_projects": ListActiveTasksAllProjectsResponse,
    "process_plan": ProcessPlanResponse,
    "process_task_completion": ProcessTaskCompletionResponse,
}
