"""Response models for agent commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentSummary(BaseModel):
    workspace_id: str
    project_id: str
    name: str = ""
    state: str = ""
    current_task_id: str | None = None
    current_task_title: str | None = None


class ProfileSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    model: str = ""
    allowed_tools: list[str] = []
    mcp_servers: list[str] = []
    has_system_prompt: bool = False


class ListAgentsResponse(BaseModel):
    agents: list[AgentSummary] = []
    project_id: str = ""


class GetAgentErrorResponse(BaseModel):
    task_id: str
    title: str = ""
    status: str = ""
    retries: str = ""
    message: str | None = None
    result: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    suggested_fix: str | None = None
    agent_summary: str | None = None


class ListProfilesResponse(BaseModel):
    profiles: list[ProfileSummary] = []
    count: int = 0


class CreateProfileResponse(BaseModel):
    created: str
    name: str
    warnings: list[str] | None = None


class ProfileDetail(BaseModel):
    id: str
    name: str
    description: str = ""
    model: str = ""
    permission_mode: str = ""
    allowed_tools: list[str] = []
    mcp_servers: list[str] = []
    system_prompt_suffix: str = ""
    install: dict[str, Any] = {}


class GetProfileResponse(ProfileDetail):
    pass


class EditProfileResponse(BaseModel):
    updated: str
    fields: list[str] = []
    warnings: list[str] | None = None


class DeleteProfileResponse(BaseModel):
    deleted: str
    name: str


class ListAvailableToolsResponse(BaseModel):
    tools: list[dict[str, Any]] = []
    mcp_servers: list[dict[str, Any]] = []


class CheckProfileResponse(BaseModel):
    profile_id: str
    valid: bool = False
    issues: list[str] = []
    manifest: dict[str, Any] = {}


class InstallProfileResponse(BaseModel):
    profile_id: str
    installed: list[str] = []
    already_present: list[str] = []
    manual: list[str] = []
    ready: bool = False


class ExportProfileResponse(BaseModel):
    yaml: str = ""
    gist_url: str | None = None
    gist_error: str | None = None


class ImportProfileResponse(BaseModel):
    imported: bool = False
    name: str = ""
    id: str = ""
    installed: list[str] | None = None
    already_present: list[str] | None = None
    manual: list[str] | None = None
    ready: bool = False


# --- Project-scoped profile responses --------------------------------------


class CreateProjectProfileResponse(BaseModel):
    created: str
    project_id: str
    agent_type: str
    path: str
    warnings: list[str] | None = None


class EditProjectProfileResponse(BaseModel):
    updated: str
    fields: list[str] = []
    warnings: list[str] | None = None


class DeleteProjectProfileResponse(BaseModel):
    deleted: str
    project_id: str
    agent_type: str


class ProbedToolModel(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = {}


class CatalogEntryModel(BaseModel):
    server_name: str
    project_id: str | None = None
    scope: str
    transport: str
    tools: list[ProbedToolModel] = []
    tool_count: int = 0
    last_probed_at: float = 0.0
    last_error: str | None = None
    ok: bool = True
    is_builtin: bool = False


class ProjectProfileRow(BaseModel):
    agent_type: str
    global_profile: ProfileDetail | None = Field(default=None, alias="global")
    scoped: ProfileDetail | None = None
    effective: ProfileDetail | None = None
    has_override: bool = False

    model_config = {"populate_by_name": True}


class ListProjectProfilesResponse(BaseModel):
    project_id: str
    agent_types: list[ProjectProfileRow] = []
    tool_catalog: dict[str, CatalogEntryModel] = {}


class ShowEffectiveProfileResponse(BaseModel):
    project_id: str
    agent_type: str
    profile: ProfileDetail | None = None
    source: str | None = None  # "project" | "global" | None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_agents": ListAgentsResponse,
    "create_agent": ListAgentsResponse,  # deprecated, returns error
    "edit_agent": ListAgentsResponse,  # deprecated, returns error
    "delete_agent": ListAgentsResponse,  # deprecated, returns error
    "pause_agent": ListAgentsResponse,  # deprecated, returns error
    "resume_agent": ListAgentsResponse,  # deprecated, returns error
    "get_agent_error": GetAgentErrorResponse,
    "list_profiles": ListProfilesResponse,
    "create_profile": CreateProfileResponse,
    "get_profile": GetProfileResponse,
    "edit_profile": EditProfileResponse,
    "delete_profile": DeleteProfileResponse,
    "list_available_tools": ListAvailableToolsResponse,
    "check_profile": CheckProfileResponse,
    "install_profile": InstallProfileResponse,
    "export_profile": ExportProfileResponse,
    "import_profile": ImportProfileResponse,
    "create_project_profile": CreateProjectProfileResponse,
    "edit_project_profile": EditProjectProfileResponse,
    "delete_project_profile": DeleteProjectProfileResponse,
    "list_project_profiles": ListProjectProfilesResponse,
    "show_effective_profile": ShowEffectiveProfileResponse,
}
