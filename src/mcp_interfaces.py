"""Type definitions and interfaces for the agent-queue MCP server.

Provides typed dataclasses for MCP request/response payloads, serialization
helpers for converting between agent-queue domain models and MCP-compatible
dicts, and enums for resource URI schemes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Resource URI schemes
# ---------------------------------------------------------------------------

class ResourceScheme(str, Enum):
    """URI scheme prefixes for agent-queue MCP resources."""
    TASK = "agentqueue://tasks"
    PROJECT = "agentqueue://projects"
    AGENT = "agentqueue://agents"
    EVENT = "agentqueue://events"
    PROFILE = "agentqueue://profiles"
    WORKSPACE = "agentqueue://workspaces"


# ---------------------------------------------------------------------------
# Serialization helpers — convert domain models to MCP-friendly dicts
# ---------------------------------------------------------------------------

def task_to_dict(task: Any) -> dict[str, Any]:
    """Serialize a Task dataclass to a JSON-serializable dict."""
    return {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "task_type": task.task_type.value if task.task_type and hasattr(task.task_type, "value") else str(task.task_type) if task.task_type else None,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "parent_task_id": task.parent_task_id,
        "assigned_agent_id": task.assigned_agent_id,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "profile_id": task.profile_id,
        "requires_approval": task.requires_approval,
        "is_plan_subtask": task.is_plan_subtask,
    }


def project_to_dict(project: Any) -> dict[str, Any]:
    """Serialize a Project dataclass to a JSON-serializable dict."""
    return {
        "id": project.id,
        "name": project.name,
        "credit_weight": project.credit_weight,
        "max_concurrent_agents": project.max_concurrent_agents,
        "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        "total_tokens_used": project.total_tokens_used,
        "budget_limit": project.budget_limit,
        "repo_url": project.repo_url,
        "repo_default_branch": project.repo_default_branch,
        "default_profile_id": project.default_profile_id,
    }


def agent_to_dict(agent: Any) -> dict[str, Any]:
    """Serialize an Agent dataclass to a JSON-serializable dict."""
    return {
        "id": agent.id,
        "name": agent.name,
        "agent_type": agent.agent_type,
        "state": agent.state.value if hasattr(agent.state, "value") else str(agent.state),
        "current_task_id": agent.current_task_id,
        "pid": agent.pid,
        "last_heartbeat": agent.last_heartbeat,
        "total_tokens_used": agent.total_tokens_used,
        "session_tokens_used": agent.session_tokens_used,
    }


def profile_to_dict(profile: Any) -> dict[str, Any]:
    """Serialize an AgentProfile dataclass to a JSON-serializable dict."""
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "model": profile.model,
        "permission_mode": profile.permission_mode,
        "allowed_tools": profile.allowed_tools,
        "mcp_servers": profile.mcp_servers,
        "system_prompt_suffix": profile.system_prompt_suffix,
    }


def workspace_to_dict(workspace: Any) -> dict[str, Any]:
    """Serialize a Workspace dataclass to a JSON-serializable dict."""
    return {
        "id": workspace.id,
        "project_id": workspace.project_id,
        "workspace_path": workspace.workspace_path,
        "source_type": workspace.source_type.value if hasattr(workspace.source_type, "value") else str(workspace.source_type),
        "name": workspace.name,
        "locked_by_agent_id": workspace.locked_by_agent_id,
        "locked_by_task_id": workspace.locked_by_task_id,
    }


# ---------------------------------------------------------------------------
# Tool argument schemas (for documentation / validation)
# ---------------------------------------------------------------------------

@dataclass
class ToolArgSpec:
    """Describes a single argument for an MCP tool."""
    name: str
    type: str  # "string", "integer", "boolean", "array", "object"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class ToolSpec:
    """Full specification of an MCP tool exposed by this server."""
    name: str
    description: str
    args: list[ToolArgSpec] = field(default_factory=list)
    category: str = ""


# ---------------------------------------------------------------------------
# Prompt template definitions
# ---------------------------------------------------------------------------

@dataclass
class PromptTemplate:
    """A reusable prompt template exposed via MCP prompts/list."""
    name: str
    description: str
    template: str
    arguments: list[dict[str, str]] = field(default_factory=list)
