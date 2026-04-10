"""Tests for the agent-queue MCP server.

Tests cover:
- MCP protocol compliance (tool listing, resource listing, prompt listing)
- Dynamic tool registration from _ALL_TOOL_DEFINITIONS
- Excluded commands are not registered
- Resource reads return correct data
- Tool calls delegate to CommandHandler.execute()
- Error handling for missing entities
- Serialization helpers
- Exclusion configuration merging (defaults, config YAML, env var)
- Drift detection — registered tools vs. tool_registry definitions
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    ProjectStatus,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskType,
    Workspace,
)
from src.tool_registry import _ALL_TOOL_DEFINITIONS
from src.mcp_interfaces import (
    ResourceScheme,
    agent_to_dict,
    profile_to_dict,
    project_to_dict,
    task_to_dict,
    workspace_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a fresh in-memory database for each test."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def populated_db(db):
    """Database pre-populated with test data."""
    project = Project(
        id="test-project",
        name="Test Project",
        credit_weight=1.0,
        max_concurrent_agents=2,
        status=ProjectStatus.ACTIVE,
        repo_url="https://github.com/test/repo",
        repo_default_branch="main",
    )
    await db.create_project(project)

    agent = Agent(
        id="agent-1",
        name="Claude Agent 1",
        agent_type="claude",
        state=AgentState.BUSY,
        current_task_id=None,
    )
    await db.create_agent(agent)

    task1 = Task(
        id="task-001",
        project_id="test-project",
        title="Implement feature X",
        description="Build the X feature with tests",
        priority=100,
        status=TaskStatus.READY,
        task_type=TaskType.FEATURE,
    )
    task2 = Task(
        id="task-002",
        project_id="test-project",
        title="Fix bug Y",
        description="Fix the Y bug in module Z",
        priority=50,
        status=TaskStatus.IN_PROGRESS,
        task_type=TaskType.BUGFIX,
        assigned_agent_id="agent-1",
    )
    task3 = Task(
        id="task-003",
        project_id="test-project",
        title="Awaiting approval",
        description="Needs review",
        status=TaskStatus.AWAITING_APPROVAL,
        requires_approval=True,
    )
    await db.create_task(task1)
    await db.create_task(task2)
    await db.create_task(task3)

    await db.add_dependency("task-001", "task-002")
    await db.update_agent("agent-1", current_task_id="task-002")

    profile = AgentProfile(
        id="reviewer",
        name="Code Reviewer",
        description="Reviews pull requests",
        model="claude-sonnet-4-20250514",
        permission_mode="plan",
        allowed_tools=["Read", "Grep", "Glob"],
    )
    await db.create_profile(profile)

    yield db


# ---------------------------------------------------------------------------
# Serialization helper tests
# ---------------------------------------------------------------------------


class TestSerializationHelpers:
    def test_task_to_dict(self):
        task = Task(
            id="t1",
            project_id="p1",
            title="Test task",
            description="A test",
            priority=100,
            status=TaskStatus.READY,
            task_type=TaskType.FEATURE,
        )
        d = task_to_dict(task)
        assert d["id"] == "t1"
        assert d["status"] == "READY"
        assert d["task_type"] == "feature"

    def test_task_to_dict_no_type(self):
        task = Task(id="t1", project_id="p1", title="Test", description="", task_type=None)
        d = task_to_dict(task)
        assert d["task_type"] is None

    def test_project_to_dict(self):
        project = Project(id="p1", name="Proj", status=ProjectStatus.ACTIVE, credit_weight=2.0)
        d = project_to_dict(project)
        assert d["id"] == "p1"
        assert d["status"] == "ACTIVE"
        assert d["credit_weight"] == 2.0

    def test_agent_to_dict(self):
        agent = Agent(
            id="a1",
            name="Agent 1",
            agent_type="claude",
            state=AgentState.BUSY,
            current_task_id="t1",
        )
        d = agent_to_dict(agent)
        assert d["id"] == "a1"
        assert d["state"] == "BUSY"

    def test_profile_to_dict(self):
        profile = AgentProfile(
            id="dev",
            name="Developer",
            allowed_tools=["Read", "Write"],
            mcp_servers={"test": {"command": "npx test"}},
        )
        d = profile_to_dict(profile)
        assert d["id"] == "dev"
        assert "test" in d["mcp_servers"]

    def test_workspace_to_dict(self):
        ws = Workspace(
            id="ws1", project_id="p1", workspace_path="/tmp/ws", source_type=RepoSourceType.LINK
        )
        d = workspace_to_dict(ws)
        assert d["id"] == "ws1"
        assert d["source_type"] == "link"


class TestResourceSchemes:
    def test_uri_schemes(self):
        assert ResourceScheme.TASK == "agentqueue://tasks"
        assert ResourceScheme.PROJECT == "agentqueue://projects"
        assert ResourceScheme.AGENT == "agentqueue://agents"
        assert ResourceScheme.WORKSPACE == "agentqueue://workspaces"


# ---------------------------------------------------------------------------
# MCP server integration tests
# ---------------------------------------------------------------------------


def _make_mock_context(db, event_bus, command_handler=None):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "event_bus": event_bus,
        "orchestrator": MagicMock(),
        "command_handler": command_handler,
    }
    return ctx


def _build_test_mcp(populated_db, mock_context):
    from mcp.server import FastMCP
    from src.mcp_registration import (
        DEFAULT_EXCLUDED_COMMANDS,
        register_command_tools,
        register_resources,
        register_prompts,
    )

    server = FastMCP(name="test-agent-queue")
    register_command_tools(server, excluded=DEFAULT_EXCLUDED_COMMANDS)
    register_resources(server)
    register_prompts(server)
    server.get_context = lambda: mock_context
    return server


@pytest.fixture
async def mcp_server(populated_db):
    from src.event_bus import EventBus

    test_bus = EventBus()
    ctx = _make_mock_context(populated_db, test_bus)
    yield _build_test_mcp(populated_db, ctx)


@pytest.fixture
async def mcp_server_with_handler(populated_db):
    from src.event_bus import EventBus

    test_bus = EventBus()
    mock_handler = AsyncMock()
    ctx = _make_mock_context(populated_db, test_bus, mock_handler)
    yield _build_test_mcp(populated_db, ctx), mock_handler


class TestDynamicToolRegistration:
    async def test_all_non_excluded_tools_registered(self, mcp_server):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}
        for defn in _ALL_TOOL_DEFINITIONS:
            name = defn["name"]
            if name in DEFAULT_EXCLUDED_COMMANDS:
                assert name not in tool_names
            else:
                assert name in tool_names, f"Command {name} should be registered"

    async def test_excluded_commands_not_registered(self, mcp_server):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}
        for cmd in DEFAULT_EXCLUDED_COMMANDS:
            assert cmd not in tool_names

    async def test_tools_have_descriptions(self, mcp_server):
        tools = await mcp_server.list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    async def test_tool_schemas_match_registry(self, mcp_server):
        tools = await mcp_server.list_tools()
        tool_map = {t.name: t for t in tools}
        seen: set[str] = set()
        first_defs: dict[str, dict] = {}
        for defn in _ALL_TOOL_DEFINITIONS:
            name = defn["name"]
            if name not in seen:
                first_defs[name] = defn
                seen.add(name)
        for name, defn in first_defs.items():
            if name not in tool_map:
                continue
            expected = defn.get("input_schema", {"type": "object", "properties": {}})
            assert tool_map[name].inputSchema == expected, f"Schema mismatch for {name}"

    async def test_core_tools_present(self, mcp_server):
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}
        for name in [
            "create_task",
            "stop_task",
            "approve_task",
            "list_projects",
            "add_dependency",
            "list_workspaces",
            "list_agents",
        ]:
            assert name in tool_names

    async def test_dangerous_commands_excluded(self, mcp_server):
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}
        for name in ["shutdown", "restart_daemon", "update_and_restart", "run_command"]:
            assert name not in tool_names


class TestMCPToolCalls:
    async def test_tool_delegates_to_command_handler(self, mcp_server_with_handler):
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"success": True, "projects": []}
        result = await server.call_tool("list_projects", {})
        data = json.loads(result[0].text)
        mock_handler.execute.assert_called_once_with("list_projects", {})
        assert data["success"] is True

    async def test_tool_passes_arguments(self, mcp_server_with_handler):
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"success": True}
        await server.call_tool("create_task", {"project_id": "p1", "title": "New"})
        mock_handler.execute.assert_called_once_with(
            "create_task", {"project_id": "p1", "title": "New"}
        )

    async def test_tool_handles_error_response(self, mcp_server_with_handler):
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"error": "Not found"}
        result = await server.call_tool("pause_project", {"project_id": "bad"})
        data = json.loads(result[0].text)
        assert "error" in data


class TestMCPResourceListing:
    async def test_resources_registered(self, mcp_server):
        resources = await mcp_server.list_resources()
        uris = {str(r.uri) for r in resources}
        for uri in [
            "agentqueue://tasks",
            "agentqueue://tasks/active",
            "agentqueue://projects",
            "agentqueue://agents",
            "agentqueue://profiles",
            "agentqueue://workspaces",
        ]:
            assert uri in uris


class TestMCPPromptListing:
    async def test_prompts_registered(self, mcp_server):
        prompts = await mcp_server.list_prompts()
        prompt_names = {p.name for p in prompts}
        assert "create_task_prompt" in prompt_names
        assert "review_task_prompt" in prompt_names
        assert "project_overview_prompt" in prompt_names


class TestMCPResourceReads:
    async def test_read_all_tasks(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://tasks")
        assert len(json.loads(contents[0].content)) == 3

    async def test_read_active_tasks(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://tasks/active")
        assert len(json.loads(contents[0].content)) == 2

    async def test_read_projects(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://projects")
        data = json.loads(contents[0].content)
        assert data[0]["id"] == "test-project"

    async def test_read_profiles(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://profiles")
        assert len(json.loads(contents[0].content)) == 1


class TestMCPPrompts:
    async def test_create_task_prompt(self, mcp_server):
        result = await mcp_server.get_prompt(
            "create_task_prompt",
            {
                "project_id": "test-project",
                "task_type": "bugfix",
                "context": "500 errors",
            },
        )
        text = result.messages[0].content.text
        assert "Test Project" in text
        assert "bugfix" in text

    async def test_review_task_prompt(self, mcp_server):
        result = await mcp_server.get_prompt("review_task_prompt", {"task_id": "task-001"})
        text = result.messages[0].content.text
        assert "Implement feature X" in text

    async def test_review_task_prompt_not_found(self, mcp_server):
        result = await mcp_server.get_prompt("review_task_prompt", {"task_id": "nonexistent"})
        assert "not found" in result.messages[0].content.text


class TestRegisterCommandTools:
    def test_custom_exclusion_set(self):
        from mcp.server import FastMCP
        from src.mcp_registration import register_command_tools

        test_mcp = FastMCP(name="test")
        registered = register_command_tools(test_mcp, excluded={"list_projects", "create_task"})
        assert "list_projects" not in registered
        assert "pause_project" in registered

    def test_empty_exclusion_registers_all(self):
        from mcp.server import FastMCP
        from src.mcp_registration import register_command_tools

        test_mcp = FastMCP(name="test")
        registered = register_command_tools(test_mcp, excluded=set())
        explicit = {d["name"] for d in _ALL_TOOL_DEFINITIONS}
        # Auto-discovered commands may also be registered (safety net)
        assert explicit.issubset(set(registered))


class TestExclusionConfiguration:
    def test_defaults_only(self):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions

        assert get_effective_exclusions(config_path=None) == DEFAULT_EXCLUDED_COMMANDS

    def test_config_yaml_merges_with_defaults(self, tmp_path):
        import yaml
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"mcp_server": {"excluded_commands": ["list_tasks"]}}))
        result = get_effective_exclusions(config_path=str(config_file))
        assert DEFAULT_EXCLUDED_COMMANDS.issubset(result)
        assert "list_tasks" in result

    def test_env_var_merges_with_defaults(self, monkeypatch):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions

        monkeypatch.setenv("AGENT_QUEUE_MCP_EXCLUDED", "list_tasks,create_task")
        result = get_effective_exclusions(config_path=None)
        assert DEFAULT_EXCLUDED_COMMANDS.issubset(result)
        assert "list_tasks" in result

    def test_missing_config_file_uses_defaults(self):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions

        assert get_effective_exclusions(config_path="/nonexistent") == DEFAULT_EXCLUDED_COMMANDS

    def test_empty_env_var_no_effect(self, monkeypatch):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions

        monkeypatch.setenv("AGENT_QUEUE_MCP_EXCLUDED", "")
        assert get_effective_exclusions(config_path=None) == DEFAULT_EXCLUDED_COMMANDS


class TestDriftDetection:
    async def test_no_missing_tools(self, mcp_server):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS

        tools = await mcp_server.list_tools()
        registered = {t.name for t in tools}
        for name in {d["name"] for d in _ALL_TOOL_DEFINITIONS}:
            assert name in registered or name in DEFAULT_EXCLUDED_COMMANDS

    async def test_no_extra_tools(self, mcp_server):
        """Ensure auto-discovered commands are a known set.

        Auto-discovered commands (``_cmd_*`` methods without explicit
        ``_ALL_TOOL_DEFINITIONS`` entries) are registered via MCP as a
        safety net.  This test tracks which commands are auto-discovered
        so new ones are intentional.
        """
        # Known auto-discovered commands (have _cmd_* methods but no
        # explicit tool definitions yet).
        known_auto_discovered = {
            "fire_hook",
            "plugin_config",
            "plugin_disable",
            "plugin_enable",
            "plugin_info",
            "plugin_install",
            "plugin_list",
            "plugin_prompts",
            "plugin_reload",
            "plugin_remove",
            "plugin_reset_prompts",
            "plugin_update",
        }
        tools = await mcp_server.list_tools()
        extra = {t.name for t in tools} - {d["name"] for d in _ALL_TOOL_DEFINITIONS}
        unexpected = extra - known_auto_discovered
        assert not unexpected, (
            f"Unexpected auto-discovered commands: {unexpected}. "
            f"Add entries to _ALL_TOOL_DEFINITIONS or update known_auto_discovered."
        )

    async def test_registered_count_matches_expected(self, mcp_server):
        from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, _discover_all_commands

        tools = await mcp_server.list_tools()
        # Total expected = explicit definitions + auto-discovered, minus excluded
        all_commands = set(_discover_all_commands().keys()) | {
            d["name"] for d in _ALL_TOOL_DEFINITIONS
        }
        expected = len(all_commands) - len(DEFAULT_EXCLUDED_COMMANDS & all_commands)
        assert len(tools) == expected

    async def test_all_command_handler_methods_have_definitions(self):
        """Every _cmd_* method on CommandHandler should have an explicit
        tool definition in _ALL_TOOL_DEFINITIONS, or be a known
        auto-discovered command.

        Auto-discovery will catch missing commands at runtime, but explicit
        definitions provide better descriptions and parameter schemas.
        """
        from src.mcp_registration import _discover_all_commands

        # Known auto-discovered commands (have _cmd_* methods but no
        # explicit tool definitions yet).
        known_auto_discovered = {
            "fire_hook",
            "plugin_config",
            "plugin_disable",
            "plugin_enable",
            "plugin_info",
            "plugin_install",
            "plugin_list",
            "plugin_prompts",
            "plugin_reload",
            "plugin_remove",
            "plugin_reset_prompts",
            "plugin_update",
        }
        all_commands = _discover_all_commands()
        explicit = {d["name"] for d in _ALL_TOOL_DEFINITIONS}
        missing = sorted(set(all_commands) - explicit - known_auto_discovered)
        assert not missing, (
            f"CommandHandler has commands without explicit tool definitions: {missing}. "
            f"Add entries to _ALL_TOOL_DEFINITIONS or update known_auto_discovered."
        )

    def test_no_duplicate_tool_definitions(self):
        """_ALL_TOOL_DEFINITIONS must not contain duplicate names.

        Duplicate names cause the second definition to be silently ignored
        during MCP registration (the first wins).  If two different commands
        share a name (e.g. agent edit_profile vs. memory edit_profile),
        one must be renamed.
        """
        from collections import Counter

        names = [d["name"] for d in _ALL_TOOL_DEFINITIONS]
        dupes = {n: c for n, c in Counter(names).items() if c > 1}
        assert not dupes, (
            f"Duplicate names in _ALL_TOOL_DEFINITIONS: {dupes}. "
            f"Each command name must be unique — rename to avoid collisions."
        )

    def test_no_duplicate_cmd_methods(self):
        """CommandHandler must not have duplicate _cmd_* method names.

        In Python, the second definition of a method silently shadows the
        first.  This test catches the bug early — e.g. two _cmd_edit_profile
        methods where the agent-profile version shadows the memory-profile
        version.
        """
        import inspect
        import re
        from src.command_handler import CommandHandler

        source = inspect.getsource(CommandHandler)
        method_names = re.findall(r"async def (_cmd_\w+)\(self", source)
        from collections import Counter

        dupes = {n: c for n, c in Counter(method_names).items() if c > 1}
        assert not dupes, (
            f"Duplicate _cmd_* methods in CommandHandler: {dupes}. "
            f"The second definition silently shadows the first in Python."
        )


# ---------------------------------------------------------------------------
# Plugin tools MCP exposure
# ---------------------------------------------------------------------------


def _build_test_mcp_with_plugin_tools(populated_db, mock_context, plugin_tools):
    """Build a test MCP server with plugin-contributed tool definitions."""
    from mcp.server import FastMCP
    from src.mcp_registration import (
        DEFAULT_EXCLUDED_COMMANDS,
        register_command_tools,
        register_resources,
        register_prompts,
    )

    server = FastMCP(name="test-agent-queue-plugins")
    register_command_tools(server, excluded=DEFAULT_EXCLUDED_COMMANDS, plugin_tools=plugin_tools)
    register_resources(server)
    register_prompts(server)
    server.get_context = lambda: mock_context
    return server


class TestPluginToolRegistration:
    """Tests for the plugin tools pass (pass 2) in MCP registration."""

    async def test_plugin_tools_registered(self, populated_db):
        """Plugin-contributed tool definitions are exposed as MCP tools."""
        from src.event_bus import EventBus

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        plugin_tools = [
            {
                "name": "my_plugin_scan",
                "description": "Scan something.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "What to scan"},
                    },
                    "required": ["target"],
                },
            },
        ]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, plugin_tools)
        tools = await server.list_tools()
        tool_names = {t.name for t in tools}
        assert "my_plugin_scan" in tool_names

    async def test_plugin_tools_have_rich_schemas(self, populated_db):
        """Plugin tools are registered with their full input_schema, not basic stubs."""
        from src.event_bus import EventBus

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        plugin_tools = [
            {
                "name": "my_plugin_scan",
                "description": "Scan something.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "What to scan"},
                    },
                    "required": ["target"],
                },
            },
        ]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, plugin_tools)
        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        assert "my_plugin_scan" in tool_map
        schema = tool_map["my_plugin_scan"].inputSchema
        assert "target" in schema["properties"]
        assert schema["required"] == ["target"]

    async def test_plugin_tools_do_not_override_explicit(self, populated_db):
        """Plugin tools cannot shadow _ALL_TOOL_DEFINITIONS entries."""
        from src.event_bus import EventBus

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        # Try to register a plugin tool with the same name as an explicit tool
        plugin_tools = [
            {
                "name": "create_task",
                "description": "SHOULD NOT APPEAR — explicit def wins.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, plugin_tools)
        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        assert "create_task" in tool_map
        # The explicit definition should win, not the plugin one
        assert "SHOULD NOT APPEAR" not in tool_map["create_task"].description

    async def test_excluded_plugin_tools_not_registered(self, populated_db):
        """Excluded commands are skipped even when contributed by plugins."""
        from src.event_bus import EventBus

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        plugin_tools = [
            {
                "name": "shutdown",
                "description": "Should be excluded.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, plugin_tools)
        tools = await server.list_tools()
        tool_names = {t.name for t in tools}
        assert "shutdown" not in tool_names

    async def test_no_plugin_tools_is_safe(self, populated_db):
        """Passing None or empty plugin_tools is a no-op."""
        from src.event_bus import EventBus

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        server_none = _build_test_mcp_with_plugin_tools(populated_db, ctx, None)
        server_empty = _build_test_mcp_with_plugin_tools(populated_db, ctx, [])
        tools_none = await server_none.list_tools()
        tools_empty = await server_empty.list_tools()
        assert len(tools_none) == len(tools_empty)


class TestMemorySearchMCPTool:
    """Tests for memory_search exposure as an MCP tool (spec §7).

    Verifies that memory_search is available via MCP with the v2 schema
    (scope-aware, topic filter, weighted merge across collections).
    """

    async def test_memory_search_in_v2_only_tools(self):
        """memory_search is in the v2 plugin's V2_ONLY_TOOLS set."""
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_search" in V2_ONLY_TOOLS

    # test_memory_search_not_in_v1_registration removed (roadmap 8.6 — v1 plugin deleted)

    async def test_memory_search_registered_as_mcp_tool(self, populated_db):
        """memory_search appears as an MCP tool when v2 plugin tools are passed."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)

        # Simulate what embedded_mcp.py does: pass plugin tool definitions
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_names = {t.name for t in tools}
        assert "memory_search" in tool_names

    async def test_memory_search_schema_has_topic_filter(self, populated_db):
        """memory_search MCP tool schema includes the 'topic' parameter."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        schema = tool_map["memory_search"].inputSchema
        assert "topic" in schema["properties"], "memory_search must expose topic filter"
        assert "string" == schema["properties"]["topic"]["type"]

    async def test_memory_search_schema_has_scope(self, populated_db):
        """memory_search MCP tool schema includes the 'scope' parameter."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        schema = tool_map["memory_search"].inputSchema
        assert "scope" in schema["properties"], "memory_search must expose scope parameter"

    async def test_memory_search_schema_has_batch_queries(self, populated_db):
        """memory_search MCP tool schema supports batch queries via 'queries' array."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        schema = tool_map["memory_search"].inputSchema
        assert "queries" in schema["properties"], "memory_search must support batch queries"
        assert schema["properties"]["queries"]["type"] == "array"

    async def test_memory_search_delegates_to_command_handler(self, populated_db):
        """memory_search MCP tool delegates to CommandHandler.execute()."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        mock_handler = AsyncMock()
        mock_handler.execute.return_value = {
            "success": True,
            "project_id": "test-project",
            "query": "authentication patterns",
            "count": 2,
            "results": [
                {
                    "content": "OAuth best practices",
                    "score": 0.92,
                    "topic": "authentication",
                    "scope": "project",
                },
            ],
        }
        ctx = _make_mock_context(populated_db, test_bus, mock_handler)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        result = await server.call_tool(
            "memory_search",
            {
                "project_id": "test-project",
                "query": "authentication patterns",
                "topic": "authentication",
                "scope": "project_test-project",
            },
        )
        data = json.loads(result[0].text)
        mock_handler.execute.assert_called_once_with(
            "memory_search",
            {
                "project_id": "test-project",
                "query": "authentication patterns",
                "topic": "authentication",
                "scope": "project_test-project",
            },
        )
        assert data["success"] is True
        assert data["count"] == 2


class TestMemoryGetMCPTool:
    """Tests for memory_get exposure as an MCP tool (spec §7, roadmap 2.2.11).

    Verifies that memory_get is available via MCP with unified auto-routing
    schema (KV first, then semantic fallback).
    """

    async def test_memory_get_in_v2_only_tools(self):
        """memory_get is in the v2 plugin's V2_ONLY_TOOLS set."""
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_get" in V2_ONLY_TOOLS

    async def test_memory_get_registered_as_mcp_tool(self, populated_db):
        """memory_get appears as an MCP tool when v2 plugin tools are passed."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)

        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_names = {t.name for t in tools}
        assert "memory_get" in tool_names

    async def test_memory_get_schema_has_query_required(self, populated_db):
        """memory_get MCP tool schema requires 'query' parameter."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        schema = tool_map["memory_get"].inputSchema
        assert "query" in schema.get("required", [])

    async def test_memory_get_schema_has_expected_params(self, populated_db):
        """memory_get MCP tool schema exposes project_id, agent_type, topic, top_k."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        ctx = _make_mock_context(populated_db, test_bus)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        tools = await server.list_tools()
        tool_map = {t.name: t for t in tools}
        props = tool_map["memory_get"].inputSchema["properties"]
        assert "project_id" in props
        assert "agent_type" in props
        assert "topic" in props
        assert "top_k" in props

    async def test_memory_get_delegates_to_command_handler(self, populated_db):
        """memory_get MCP tool delegates to CommandHandler.execute()."""
        from src.event_bus import EventBus
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS, V2_ONLY_TOOLS

        test_bus = EventBus()
        mock_handler = AsyncMock()
        mock_handler.execute.return_value = {
            "success": True,
            "source": "kv",
            "query": "deploy_branch",
            "count": 1,
            "results": [
                {
                    "namespace": "project",
                    "key": "deploy_branch",
                    "value": "main",
                },
            ],
        }
        ctx = _make_mock_context(populated_db, test_bus, mock_handler)
        v2_tools = [d for d in TOOL_DEFINITIONS if d["name"] in V2_ONLY_TOOLS]
        server = _build_test_mcp_with_plugin_tools(populated_db, ctx, v2_tools)

        result = await server.call_tool(
            "memory_get",
            {
                "query": "deploy_branch",
                "project_id": "test-project",
            },
        )
        data = json.loads(result[0].text)
        mock_handler.execute.assert_called_once_with(
            "memory_get",
            {
                "query": "deploy_branch",
                "project_id": "test-project",
            },
        )
        assert data["success"] is True
        assert data["source"] == "kv"
