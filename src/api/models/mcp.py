"""Response models for MCP server registry + tool catalog commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.api.models.agent import CatalogEntryModel


class McpServerSummary(BaseModel):
    name: str
    transport: str
    scope: str  # "system" | "project"
    description: str = ""
    is_builtin: bool = False
    project_id: str | None = None
    tool_count: int | None = None
    last_probed_at: float | None = None
    last_error: str | None = None


class ListMcpServersResponse(BaseModel):
    servers: list[McpServerSummary] = []
    count: int = 0


class GetMcpServerResponse(McpServerSummary):
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    notes: str = ""
    adapter_config: dict[str, Any] = {}


class ListMcpToolCatalogResponse(BaseModel):
    servers: dict[str, CatalogEntryModel] = {}
    count: int = 0


class ProbeMcpServerResponse(BaseModel):
    probed: CatalogEntryModel


class CreateMcpServerResponse(BaseModel):
    created: str
    scope: str  # "system" | "project"
    project_id: str | None = None
    path: str


class EditMcpServerResponse(BaseModel):
    updated: str
    scope: str
    path: str


class DeleteMcpServerResponse(BaseModel):
    deleted: str
    scope: str
    path: str


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_mcp_servers": ListMcpServersResponse,
    "get_mcp_server": GetMcpServerResponse,
    "list_mcp_tool_catalog": ListMcpToolCatalogResponse,
    "probe_mcp_server": ProbeMcpServerResponse,
    "create_mcp_server": CreateMcpServerResponse,
    "edit_mcp_server": EditMcpServerResponse,
    "delete_mcp_server": DeleteMcpServerResponse,
}
