"""Response models for project commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ProjectSummary(BaseModel):
    id: str
    name: str
    status: str = ""
    credit_weight: float = 1.0
    max_concurrent_agents: int = 1
    workspace: str | None = None
    repo_url: str | None = None
    assignment_playbook_id: str | None = None
    supervisor_enabled: bool = False


class GitIdentityPair(BaseModel):
    name: str
    email: str


class EffectiveGitIdentity(BaseModel):
    """The identity a project's AQ-authored commits use, and where it came from.

    ``source`` is ``project`` (its override), ``installation`` (the
    ``git_identity`` default) or ``fallback`` (no default chosen yet;
    ``configured`` is then false).
    """

    name: str
    email: str
    source: Literal["project", "installation", "fallback"]
    configured: bool
    installation: GitIdentityPair | None = None
    project_override: GitIdentityPair | None = None
    fallback: GitIdentityPair


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
    assignment_playbook_id: str | None = None
    supervisor_enabled: bool = False
    #: The project's own override (both null = inherits the installation default).
    git_identity_name: str | None = None
    git_identity_email: str | None = None
    git_identity: EffectiveGitIdentity | None = None


class WorkspaceOwnerTask(BaseModel):
    id: str
    found: bool
    status: str | None = None
    claim_epoch: int | None = None
    claimed_by_session: str | None = None


class WorkspaceOwnerSession(BaseModel):
    id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    lifecycle: str | None = None
    state: str | None = None
    claim_phase: str | None = None
    last_claim_epoch: int | None = None
    last_activity: float | None = None
    work_dir: str | None = None


class WorkspaceOwner(BaseModel):
    """Owner identity as the database proves it (``src/workspace_listing.py``)."""

    lock_state: str
    agent_id: str | None = None
    task_id: str | None = None
    locked_at: float | None = None
    #: ``complete`` | ``timeout`` | ``unavailable``.
    facts: str
    task: WorkspaceOwnerTask | None = None
    session: WorkspaceOwnerSession | None = None
    sessions_matched: int | None = None
    #: ``consistent`` | ``mismatch`` | ``unknown``; ``None`` when not applicable.
    fence: str | None = None
    session_matches_claim: bool | None = None
    #: ``unlocked`` | ``unlocked_session_present`` | ``held`` | ``agent_held``
    #: | ``preparing`` | ``unverified`` | ``unknown``.
    classification: str


class WorkspaceSummary(BaseModel):
    id: str
    project_id: str
    workspace_path: str
    source_type: str = ""
    name: str | None = None
    kind_id: str | None = None
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None
    lock_mode: str | None = None
    enabled: bool = True
    role: str | None = None
    slot_index: int | None = None
    mode: str | None = None
    #: ``None`` whenever the checkout could not be read; never a guessed value.
    branch: str | None = None
    dirty: bool | None = None
    #: ``present`` | ``missing`` | ``unknown``.
    presence: str | None = None
    #: ``ok`` | ``missing`` | ``not_a_checkout`` | ``error`` | ``timeout`` |
    #: ``not_started`` | ``skipped`` | ``skipped_active_writer``.
    git_status: str | None = None
    git_error: str | None = None
    beyond_cap: bool | None = None
    owner: WorkspaceOwner | None = None


class WorkspaceListDiagnostics(BaseModel):
    complete: bool
    deadline_seconds: float
    elapsed_seconds: float
    owner_facts: str
    owner_facts_error: str | None = None
    git_requested: bool
    git_status_counts: dict[str, int] = {}
    presence_counts: dict[str, int] = {}
    incomplete_rows: int = 0
    authority: str


class ListProjectsResponse(BaseModel):
    projects: list[ProjectSummary] = []


class ProjectDoctorResponse(BaseModel):
    success: bool = True
    project_id: str
    ready: bool
    code: str
    recovery_command: str | None = None
    message: str | None = None


class CreateProjectResponse(BaseModel):
    created: str
    name: str
    assignment_playbook_id: str | None = None


class EditProjectResponse(BaseModel):
    updated: str
    fields: list[str] = []


class BindProjectRepositoryResponse(BaseModel):
    """First repository binding, including the audit identity when it changes."""

    success: bool
    project_id: str
    repo_url: str
    changed: bool
    event_id: int | None = None


class DeleteProjectResponse(BaseModel):
    deleted: str
    name: str


class PauseProjectResponse(BaseModel):
    paused: str
    name: str


class ResumeProjectResponse(BaseModel):
    resumed: str
    name: str


class SetDefaultBranchResponse(BaseModel):
    project_id: str
    default_branch: str
    previous_branch: str = ""
    status: str = ""
    branch_created: bool | None = None


class AddWorkspaceResponse(BaseModel):
    created: str
    project_id: str
    workspace_path: str
    source_type: str = ""


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceSummary] = []
    diagnostics: WorkspaceListDiagnostics | None = None


class RemoveWorkspaceResponse(BaseModel):
    deleted: str
    name: str | None = None
    project_id: str = ""
    workspace_path: str = ""


class ReleaseWorkspaceResponse(BaseModel):
    workspace_id: str
    released_from_agent: str | None = None
    released_from_task: str | None = None


class WorkspaceRetireClaimResponse(BaseModel):
    workspace_id: str
    retired: bool = False
    detail: str | None = None
    holder_task_id: str | None = None
    holder_claim_epoch: int | None = None
    holder_session_id: str | None = None
    reason: str | None = None


class EditWorkspaceResponse(BaseModel):
    updated: str
    fields: list[str] = []
    workspace_path: str = ""
    source_type: str = ""
    enabled: bool = True


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


class SetProjectConstraintResponse(BaseModel):
    project_id: str
    constraint_set: bool = False
    active_fields: list[str] = []


class ReleaseProjectConstraintResponse(BaseModel):
    project_id: str
    constraint_released: bool = False
    fields: str | None = None
    fields_released: list[str] = []
    remaining_fields: list[str] = []


class SetActiveProjectResponse(BaseModel):
    active_project: str | None = None
    name: str | None = None
    message: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_projects": ListProjectsResponse,
    "create_project": CreateProjectResponse,
    "project_doctor": ProjectDoctorResponse,
    "edit_project": EditProjectResponse,
    "bind_project_repository": BindProjectRepositoryResponse,
    "delete_project": DeleteProjectResponse,
    "pause_project": PauseProjectResponse,
    "resume_project": ResumeProjectResponse,
    "set_default_branch": SetDefaultBranchResponse,
    "get_project": GetProjectResponse,
    "add_workspace": AddWorkspaceResponse,
    "list_workspaces": ListWorkspacesResponse,
    "remove_workspace": RemoveWorkspaceResponse,
    "release_workspace": ReleaseWorkspaceResponse,
    "workspace_retire_claim": WorkspaceRetireClaimResponse,
    "edit_workspace": EditWorkspaceResponse,
    "find_merge_conflict_workspaces": FindMergeConflictWorkspacesResponse,
    "queue_sync_workspaces": QueueSyncWorkspacesResponse,
    "set_project_constraint": SetProjectConstraintResponse,
    "release_project_constraint": ReleaseProjectConstraintResponse,
    "set_active_project": SetActiveProjectResponse,
}
