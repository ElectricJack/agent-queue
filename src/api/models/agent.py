"""Response models for agent commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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


class GetProfileResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    model: str = ""
    permission_mode: str = ""
    allowed_tools: list[str] = []
    mcp_servers: dict[str, Any] = {}
    system_prompt_suffix: str = ""
    install: dict[str, Any] = {}


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


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_agents": ListAgentsResponse,
    "create_agent": ListAgentsResponse,  # deprecated, returns error
    "edit_agent": ListAgentsResponse,    # deprecated, returns error
    "delete_agent": ListAgentsResponse,  # deprecated, returns error
    "pause_agent": ListAgentsResponse,   # deprecated, returns error
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
}
