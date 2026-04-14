"""MCP tool, resource, and prompt registration for the agent-queue system.

Auto-registers all CommandHandler commands as MCP tools from
``_ALL_TOOL_DEFINITIONS``, plus read-only MCP resources and reusable prompt
templates.  Used by the embedded MCP server (``src/embedded_mcp.py``).

**Auto-discovery safety net:** After registering explicit tool definitions,
``register_command_tools`` scans ``CommandHandler`` for any ``_cmd_*``
methods that lack a corresponding entry in ``_ALL_TOOL_DEFINITIONS`` and
auto-registers them with a basic schema derived from the method docstring.
This ensures that *every* command is available via MCP without manual
registration — new commands added to ``CommandHandler`` are automatically
exposed.

Each MCP tool delegates execution to ``CommandHandler.execute(name, args)``,
ensuring feature parity with the Discord bot and the Supervisor LLM
tool-use loop — no business logic is reimplemented here.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from typing import Any

from mcp.server import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata, ArgModelBase
from pydantic import ConfigDict

from src.database import Database
from src.models import AgentState, TaskStatus
from src.tools.definitions import _ALL_TOOL_DEFINITIONS
from src.mcp_interfaces import (
    agent_to_dict,
    profile_to_dict,
    project_to_dict,
    task_to_dict,
    workspace_to_dict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excluded commands — dangerous or irrelevant for MCP clients
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDED_COMMANDS = {
    "shutdown",
    "restart_daemon",
    "update_and_restart",
    "run_command",  # dangerous for external MCP clients
    "browse_tools",
    "load_tools",  # meta-tools for LLM context management, not MCP
}


def get_effective_exclusions(
    config_path: str | None = None,
    config: Any | None = None,
) -> set[str]:
    """Compute the effective exclusion set by merging three sources.

    Merge order (all additive — union):
      1. ``DEFAULT_EXCLUDED_COMMANDS`` (hardcoded safe defaults)
      2. ``mcp_server.excluded_commands`` from config (``AppConfig`` object or
         raw YAML file)
      3. ``AGENT_QUEUE_MCP_EXCLUDED`` environment variable (comma-separated)

    Args:
        config_path: Path to config YAML. ``None`` skips config-file lookup.
            Ignored when *config* is provided.
        config: An ``AppConfig`` instance. When provided, exclusions are read
            from ``config.mcp_server.excluded_commands`` and *config_path* is
            not used.

    Returns:
        The merged set of command names to exclude.
    """
    excluded = set(DEFAULT_EXCLUDED_COMMANDS)

    # --- AppConfig object (preferred) ---
    if config is not None:
        mcp_cfg = getattr(config, "mcp_server", None)
        if mcp_cfg is not None:
            config_excluded = getattr(mcp_cfg, "excluded_commands", [])
            if isinstance(config_excluded, list):
                excluded.update(config_excluded)
    elif config_path:
        # --- Fallback: raw YAML parsing ---
        try:
            import yaml

            with open(config_path) as fh:
                raw = yaml.safe_load(fh) or {}
            mcp_section = raw.get("mcp_server", {})
            config_excluded = mcp_section.get("excluded_commands", [])
            if isinstance(config_excluded, list):
                excluded.update(config_excluded)
        except Exception:
            logger.debug("Could not read mcp_server.excluded_commands from %s", config_path)

    # --- Environment variable ---
    env_val = os.environ.get("AGENT_QUEUE_MCP_EXCLUDED", "")
    if env_val:
        excluded.update(name.strip() for name in env_val.split(",") if name.strip())

    return excluded


# ---------------------------------------------------------------------------
# Permissive argument model for dynamic tool registration
# ---------------------------------------------------------------------------


class _AnyArgs(ArgModelBase):
    """Accepts any JSON fields — used for dynamically registered tools
    whose schemas come from ``_ALL_TOOL_DEFINITIONS`` rather than from
    Python function signatures."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        result = super().model_dump_one_level()
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


# Shared FuncMetadata instance — all dynamic tools use the same permissive
# argument model (actual validation is done by CommandHandler).
_ANY_ARGS_METADATA = FuncMetadata(arg_model=_AnyArgs, fn_is_coroutine=True)


# ---------------------------------------------------------------------------
# Auto-discovery of CommandHandler commands
# ---------------------------------------------------------------------------


def _discover_all_commands() -> dict[str, dict]:
    """Discover all commands from CommandHandler by introspecting ``_cmd_*`` methods.

    Returns a dict mapping command name → basic tool definition dict for
    every ``_cmd_*`` method on ``CommandHandler``.  The tool definitions
    use a permissive schema (``type: object`` with no required properties)
    and derive descriptions from the method docstring.

    This is used as a safety net: any command present in ``CommandHandler``
    but absent from ``_ALL_TOOL_DEFINITIONS`` will still be registered via
    MCP with a basic (but functional) schema.
    """
    # Lazy import to avoid circular dependency at module level.
    # CommandHandler imports tool_registry → tool_registry is imported here.
    from src.commands.handler import CommandHandler  # noqa: E402

    discovered: dict[str, dict] = {}
    for attr_name in dir(CommandHandler):
        if not attr_name.startswith("_cmd_"):
            continue
        cmd_name = attr_name[5:]  # strip "_cmd_" prefix
        method = getattr(CommandHandler, attr_name, None)
        if method is None or not callable(method):
            continue

        # Extract first line of docstring as description
        doc = inspect.getdoc(method) or ""
        first_line = doc.split("\n")[0].strip() if doc else ""
        description = first_line or f"Execute the {cmd_name} command."

        discovered[cmd_name] = {
            "name": cmd_name,
            "description": description,
            "input_schema": {"type": "object", "properties": {}},
        }

    return discovered


# ---------------------------------------------------------------------------
# Dynamic tool registration from _ALL_TOOL_DEFINITIONS + auto-discovery
# ---------------------------------------------------------------------------


def register_command_tools(
    mcp_server: FastMCP,
    excluded: set[str] | None = None,
    plugin_tools: list[dict] | None = None,
) -> list[str]:
    """Auto-register all CommandHandler commands as MCP tools.

    **Three-pass registration:**

    1. **Explicit definitions** — iterates ``_ALL_TOOL_DEFINITIONS`` and
       registers each tool with its rich JSON Schema (descriptions,
       required fields, enums, etc.).
    2. **Plugin tools** — registers tool definitions contributed by plugins
       (internal and external) that are not already covered by pass 1.
       Plugin tools have rich schemas and are passed in by the caller
       (typically from ``PluginRegistry.get_all_tool_definitions()``).
    3. **Auto-discovered commands** — scans ``CommandHandler`` for any
       ``_cmd_*`` methods that were *not* covered in passes 1–2 and
       registers them with a basic schema derived from the method
       docstring.  This safety net ensures that newly added commands are
       automatically available via MCP without requiring manual
       tool-definition updates.

    Commands in the *excluded* set are skipped in both passes.

    Args:
        mcp_server: The FastMCP instance to register tools on.
        excluded: Set of command names to skip. Defaults to
            ``DEFAULT_EXCLUDED_COMMANDS``.

    Returns:
        List of registered tool names.
    """
    if excluded is None:
        excluded = DEFAULT_EXCLUDED_COMMANDS

    registered: list[str] = []

    # --- Pass 1: explicit tool definitions (rich schemas) -----------------

    def _make_handler(cmd_name: str, server_ref: FastMCP):
        """Create a closure that delegates to CommandHandler.execute()."""

        async def handler(**kwargs):
            ctx = server_ref.get_context()
            ch = ctx.request_context.lifespan_context["command_handler"]
            result = await ch.execute(cmd_name, kwargs)
            return json.dumps(result, default=str)

        handler.__name__ = cmd_name
        handler.__qualname__ = cmd_name
        return handler

    def _register_tool(name: str, description: str, input_schema: dict) -> bool:
        """Register a single tool on the MCP server. Returns True if registered."""
        if name in excluded:
            logger.debug("Excluding command from MCP: %s", name)
            return False
        if name in mcp_server._tool_manager._tools:
            logger.debug("Duplicate tool definition skipped: %s", name)
            return False

        handler_fn = _make_handler(name, mcp_server)
        tool = Tool(
            fn=handler_fn,
            name=name,
            description=description,
            parameters=input_schema,
            fn_metadata=_ANY_ARGS_METADATA,
            is_async=True,
        )
        mcp_server._tool_manager._tools[name] = tool
        return True

    explicit_names: set[str] = set()
    for tool_def in _ALL_TOOL_DEFINITIONS:
        name = tool_def["name"]
        explicit_names.add(name)
        description = tool_def.get("description", f"Execute the {name} command.")
        input_schema = tool_def.get("input_schema", {"type": "object", "properties": {}})
        if _register_tool(name, description, input_schema):
            registered.append(name)

    # --- Pass 2: plugin-contributed tool definitions (rich schemas) --------

    plugin_registered: list[str] = []
    for tool_def in plugin_tools or []:
        name = tool_def.get("name", "")
        if not name or name in explicit_names:
            continue  # already handled in pass 1
        explicit_names.add(name)  # prevent duplicate in pass 3
        description = tool_def.get("description", f"Execute the {name} command.")
        input_schema = tool_def.get("input_schema", {"type": "object", "properties": {}})
        if _register_tool(name, description, input_schema):
            plugin_registered.append(name)
            registered.append(name)

    if plugin_registered:
        logger.info(
            "Registered %d plugin-contributed MCP tools: %s",
            len(plugin_registered),
            ", ".join(plugin_registered),
        )

    # --- Pass 3: auto-discover any missing commands -----------------------

    try:
        all_commands = _discover_all_commands()
    except Exception:
        logger.warning(
            "Could not auto-discover CommandHandler commands; "
            "only explicit tool definitions will be available via MCP",
            exc_info=True,
        )
        all_commands = {}

    auto_registered: list[str] = []
    for cmd_name, tool_def in sorted(all_commands.items()):
        if cmd_name in explicit_names:
            continue  # already handled in pass 1
        description = tool_def.get("description", f"Execute the {cmd_name} command.")
        input_schema = tool_def.get("input_schema", {"type": "object", "properties": {}})
        if _register_tool(cmd_name, description, input_schema):
            auto_registered.append(cmd_name)
            registered.append(cmd_name)

    logger.info(
        "Registered %d MCP tools (%d explicit, %d plugin, %d auto-discovered, %d excluded)",
        len(registered),
        len(registered) - len(plugin_registered) - len(auto_registered),
        len(plugin_registered),
        len(auto_registered),
        len(excluded),
    )
    if auto_registered:
        logger.info(
            "Auto-discovered commands (no explicit tool definition): %s",
            ", ".join(auto_registered),
        )

    return registered


# ---------------------------------------------------------------------------
# Resource registration
# ---------------------------------------------------------------------------


def register_resources(mcp_server: FastMCP) -> None:
    """Register all read-only MCP resources on the given FastMCP instance."""

    async def _db(server: FastMCP) -> Database:
        ctx = server.get_context()
        return ctx.request_context.lifespan_context["db"]

    @mcp_server.resource("agentqueue://tasks")
    async def list_all_tasks() -> str:
        """List all active and recent tasks across all projects."""
        db = await _db(mcp_server)
        tasks = await db.list_tasks()
        return json.dumps([task_to_dict(t) for t in tasks], indent=2)

    @mcp_server.resource("agentqueue://tasks/active")
    async def list_active_tasks() -> str:
        """List all currently active tasks (IN_PROGRESS, ASSIGNED, READY)."""
        db = await _db(mcp_server)
        active_statuses = [TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED, TaskStatus.READY]
        all_tasks = await db.list_tasks()
        active = [t for t in all_tasks if t.status in active_statuses]
        return json.dumps([task_to_dict(t) for t in active], indent=2)

    @mcp_server.resource("agentqueue://tasks/{task_id}")
    async def get_task(task_id: str) -> str:
        """Get detailed information about a specific task."""
        db = await _db(mcp_server)
        task = await db.get_task(task_id)
        if not task:
            return json.dumps({"error": f"Task not found: {task_id}"})
        result = task_to_dict(task)
        deps = await db.get_dependencies(task_id)
        result["dependencies"] = list(deps)
        contexts = await db.get_task_contexts(task_id)
        result["context"] = contexts
        return json.dumps(result, indent=2)

    @mcp_server.resource("agentqueue://tasks/by-project/{project_id}")
    async def list_tasks_by_project(project_id: str) -> str:
        """List all tasks for a specific project."""
        db = await _db(mcp_server)
        tasks = await db.list_tasks(project_id=project_id)
        return json.dumps([task_to_dict(t) for t in tasks], indent=2)

    @mcp_server.resource("agentqueue://tasks/by-status/{status}")
    async def list_tasks_by_status(status: str) -> str:
        """List all tasks with a given status (e.g. IN_PROGRESS, READY, COMPLETED)."""
        db = await _db(mcp_server)
        try:
            task_status = TaskStatus(status)
        except ValueError:
            return json.dumps(
                {"error": f"Invalid status: {status}. Valid: {[s.value for s in TaskStatus]}"}
            )
        tasks = await db.list_tasks(status=task_status)
        return json.dumps([task_to_dict(t) for t in tasks], indent=2)

    @mcp_server.resource("agentqueue://projects")
    async def list_all_projects() -> str:
        """List all configured projects."""
        db = await _db(mcp_server)
        projects = await db.list_projects()
        return json.dumps([project_to_dict(p) for p in projects], indent=2)

    @mcp_server.resource("agentqueue://projects/{project_id}")
    async def get_project(project_id: str) -> str:
        """Get details for a specific project."""
        db = await _db(mcp_server)
        project = await db.get_project(project_id)
        if not project:
            return json.dumps({"error": f"Project not found: {project_id}"})
        return json.dumps(project_to_dict(project), indent=2)

    @mcp_server.resource("agentqueue://agents")
    async def list_all_agents() -> str:
        """List all registered agents and their current state."""
        db = await _db(mcp_server)
        agents = await db.list_agents()
        return json.dumps([agent_to_dict(a) for a in agents], indent=2)

    @mcp_server.resource("agentqueue://agents/active")
    async def list_active_agents() -> str:
        """List agents currently working on tasks."""
        db = await _db(mcp_server)
        agents = await db.list_agents(state=AgentState.BUSY)
        return json.dumps([agent_to_dict(a) for a in agents], indent=2)

    @mcp_server.resource("agentqueue://profiles")
    async def list_all_profiles() -> str:
        """List all agent profiles."""
        db = await _db(mcp_server)
        profiles = await db.list_profiles()
        return json.dumps([profile_to_dict(p) for p in profiles], indent=2)

    @mcp_server.resource("agentqueue://profiles/{profile_id}")
    async def get_profile(profile_id: str) -> str:
        """Get details for a specific agent profile."""
        db = await _db(mcp_server)
        profile = await db.get_profile(profile_id)
        if not profile:
            return json.dumps({"error": f"Profile not found: {profile_id}"})
        return json.dumps(profile_to_dict(profile), indent=2)

    @mcp_server.resource("agentqueue://events/recent")
    async def list_recent_events() -> str:
        """List recent system events (last 50)."""
        db = await _db(mcp_server)
        events = await db.get_recent_events(limit=50)
        return json.dumps(events, indent=2, default=str)

    @mcp_server.resource("agentqueue://workspaces")
    async def list_all_workspaces() -> str:
        """List all workspaces across all projects."""
        db = await _db(mcp_server)
        projects = await db.list_projects()
        all_workspaces = []
        for p in projects:
            ws_list = await db.list_workspaces(p.id)
            all_workspaces.extend([workspace_to_dict(w) for w in ws_list])
        return json.dumps(all_workspaces, indent=2)

    @mcp_server.resource("agentqueue://workspaces/by-project/{project_id}")
    async def list_workspaces_by_project(project_id: str) -> str:
        """List workspaces for a specific project."""
        db = await _db(mcp_server)
        workspaces = await db.list_workspaces(project_id)
        return json.dumps([workspace_to_dict(w) for w in workspaces], indent=2)


# ---------------------------------------------------------------------------
# Prompt registration
# ---------------------------------------------------------------------------


def register_prompts(mcp_server: FastMCP) -> None:
    """Register all MCP prompt templates on the given FastMCP instance."""

    async def _db(server: FastMCP) -> Database:
        ctx = server.get_context()
        return ctx.request_context.lifespan_context["db"]

    @mcp_server.prompt()
    async def create_task_prompt(
        project_id: str,
        task_type: str = "feature",
        context: str = "",
    ) -> str:
        """Generate a prompt for creating a well-structured task.

        Args:
            project_id: Target project for the task
            task_type: Type of task to create
            context: Additional context about the desired work
        """
        db = await _db(mcp_server)
        project = await db.get_project(project_id)
        project_name = project.name if project else project_id

        return f"""Create a task for the "{project_name}" project.

Task type: {task_type}
{f"Context: {context}" if context else ""}

Please provide:
1. A clear, concise title (under 80 characters)
2. A detailed description that includes:
   - What needs to be done
   - Why it's needed
   - Acceptance criteria
   - Any relevant technical details
3. Priority (1-1000, default 100)
4. Whether it requires human approval before completion

Format your response as JSON with keys: title, description, priority, requires_approval"""

    @mcp_server.prompt()
    async def review_task_prompt(task_id: str) -> str:
        """Generate a prompt for reviewing a completed task.

        Args:
            task_id: ID of the task to review
        """
        db = await _db(mcp_server)
        task = await db.get_task(task_id)
        if not task:
            return f"Task {task_id} not found."

        contexts = await db.get_task_contexts(task_id)
        context_text = (
            "\n".join(
                f"- [{c.get('type', 'unknown')}] {c.get('content', '')[:200]}" for c in contexts
            )
            if contexts
            else "No additional context."
        )

        return f"""Review the following completed task:

**Task:** {task.title} ({task.id})
**Project:** {task.project_id}
**Type:** {task.task_type.value if task.task_type else "unspecified"}
**Status:** {task.status.value}

**Description:**
{task.description}

**Context:**
{context_text}

Please evaluate:
1. Was the task completed as described?
2. Are there any issues or concerns?
3. Should this be approved or rejected? Why?
4. Any follow-up tasks needed?"""

    @mcp_server.prompt()
    async def project_overview_prompt(project_id: str) -> str:
        """Generate a prompt for getting a comprehensive project overview.

        Args:
            project_id: ID of the project to overview
        """
        db = await _db(mcp_server)
        project = await db.get_project(project_id)
        if not project:
            return f"Project {project_id} not found."

        tasks = await db.list_tasks(project_id=project_id)
        status_counts: dict[str, int] = {}
        for t in tasks:
            key = t.status.value
            status_counts[key] = status_counts.get(key, 0) + 1

        workspaces = await db.list_workspaces(project_id)

        return f"""Provide an overview of the "{project.name}" project:

**Project ID:** {project.id}
**Status:** {project.status.value}
**Credit Weight:** {project.credit_weight}
**Max Concurrent Agents:** {project.max_concurrent_agents}
**Repo:** {project.repo_url or "not configured"}
**Default Branch:** {project.repo_default_branch}

**Task Summary:**
{json.dumps(status_counts, indent=2)}
Total tasks: {len(tasks)}

**Workspaces:** {len(workspaces)}

Based on this information, please provide:
1. Current project health assessment
2. Any bottlenecks or concerns
3. Recommended next actions"""
