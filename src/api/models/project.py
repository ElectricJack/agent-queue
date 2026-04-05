"""Response models for project commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProjectSummary(BaseModel):
    id: str
    name: str
    status: str = ""
    credit_weight: float = 1.0
    max_concurrent_agents: int = 1
    workspace: str | None = None
    repo_url: str | None = None
    discord_channel_id: str | None = None


class GetProjectResponse(BaseModel):
    id: str
    name: str
    status: str = ""
    repo_url: str = ""
    repo_default_branch: str = "main"
    workspace: str | None = None
    credit_weight: float = 1.0
    max_concurrent_agents: int = 1
    total_tokens_used: int = 0
    tokens_used_recent: int = 0
    budget_limit: int | None = None
    discord_channel_id: str | None = None
    default_profile_id: str | None = None


class WorkspaceSummary(BaseModel):
    id: str
    project_id: str
    workspace_path: str
    source_type: str = ""
    name: str | None = None
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None


class ListProjectsResponse(BaseModel):
    projects: list[ProjectSummary] = []


class CreateProjectResponse(BaseModel):
    created: str
    name: str
    auto_create_channels: bool = False


class EditProjectResponse(BaseModel):
    updated: str
    fields: list[str] = []


class DeleteProjectResponse(BaseModel):
    deleted: str
    name: str
    channel_ids: dict[str, str] | None = None
    archive_channels: bool | None = None


class PauseProjectResponse(BaseModel):
    paused: str
    name: str


class ResumeProjectResponse(BaseModel):
    resumed: str
    name: str


class SetProjectChannelResponse(BaseModel):
    project_id: str
    channel_id: str
    status: str = ""


class SetDefaultBranchResponse(BaseModel):
    project_id: str
    default_branch: str
    previous_branch: str = ""
    status: str = ""
    branch_created: bool | None = None


class GetProjectChannelsResponse(BaseModel):
    project_id: str
    channel_id: str | None = None


class GetProjectForChannelResponse(BaseModel):
    channel_id: str
    project_id: str | None = None
    project_name: str | None = None


class AddWorkspaceResponse(BaseModel):
    created: str
    project_id: str
    workspace_path: str
    source_type: str = ""


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceSummary] = []


class RemoveWorkspaceResponse(BaseModel):
    deleted: str
    name: str | None = None
    project_id: str = ""
    workspace_path: str = ""


class ReleaseWorkspaceResponse(BaseModel):
    workspace_id: str
    released_from_agent: str | None = None
    released_from_task: str | None = None


class FindMergeConflictWorkspacesResponse(BaseModel):
    project_id: str
    workspaces_scanned: int = 0
    workspaces_with_conflicts: int = 0
    conflicts: list[dict[str, Any]] = []


class QueueSyncWorkspacesResponse(BaseModel):
    queued: str
    project_id: str
    title: str = ""
    priority: int = 0
    workspace_count: int = 0
    default_branch: str = ""
    message: str = ""


class SetActiveProjectResponse(BaseModel):
    active_project: str | None = None
    name: str | None = None
    message: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_projects": ListProjectsResponse,
    "create_project": CreateProjectResponse,
    "edit_project": EditProjectResponse,
    "delete_project": DeleteProjectResponse,
    "pause_project": PauseProjectResponse,
    "resume_project": ResumeProjectResponse,
    "set_project_channel": SetProjectChannelResponse,
    "set_control_interface": SetProjectChannelResponse,
    "set_default_branch": SetDefaultBranchResponse,
    "get_project": GetProjectResponse,
    "get_project_channels": GetProjectChannelsResponse,
    "get_project_for_channel": GetProjectForChannelResponse,
    "add_workspace": AddWorkspaceResponse,
    "list_workspaces": ListWorkspacesResponse,
    "remove_workspace": RemoveWorkspaceResponse,
    "release_workspace": ReleaseWorkspaceResponse,
    "find_merge_conflict_workspaces": FindMergeConflictWorkspacesResponse,
    "queue_sync_workspaces": QueueSyncWorkspacesResponse,
    "set_active_project": SetActiveProjectResponse,
}
