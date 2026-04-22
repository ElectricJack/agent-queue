"""Tests for ToolRegistry -- tool categorization and on-demand loading."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.tools import ToolRegistry, _TOOL_CATEGORIES


def _make_tool(name: str, category: str | None = None) -> dict:
    """Create a minimal tool definition dict."""
    tool = {
        "name": name,
        "description": f"Tool: {name}",
        "input_schema": {"type": "object", "properties": {}},
    }
    if category:
        tool["_category"] = category
    return tool


# Plugin-contributed tools (moved from _TOOL_CATEGORIES to internal plugins).
# These are normally registered by aq-git, aq-files, aq-memory, aq-notes.
_PLUGIN_TOOLS = [
    _make_tool("get_git_status", "git"),
    _make_tool("git_commit", "git"),
    _make_tool("git_pull", "git"),
    _make_tool("git_push", "git"),
    _make_tool("git_create_branch", "git"),
    _make_tool("git_merge", "git"),
    _make_tool("git_create_pr", "git"),
    _make_tool("create_github_repo", "git"),
    _make_tool("generate_readme", "git"),
    _make_tool("git_changed_files", "git"),
    _make_tool("git_log", "git"),
    _make_tool("git_branch", "git"),
    _make_tool("git_checkout", "git"),
    _make_tool("git_diff", "git"),
    _make_tool("create_branch", "git"),
    _make_tool("checkout_branch", "git"),
    _make_tool("commit_changes", "git"),
    _make_tool("push_branch", "git"),
    _make_tool("merge_branch", "git"),
    _make_tool("read_file", "files"),
    _make_tool("write_file", "files"),
    _make_tool("edit_file", "files"),
    _make_tool("glob_files", "files"),
    _make_tool("grep", "files"),
    _make_tool("search_files", "files"),
    _make_tool("list_directory", "files"),
    _make_tool("run_command", "files"),
    _make_tool("memory_search", "memory"),
    _make_tool("memory_reindex", "memory"),
    _make_tool("memory_compact", "memory"),
    _make_tool("memory_stats", "memory"),
    _make_tool("edit_project_profile", "memory"),
    _make_tool("list_notes", "notes"),
    _make_tool("write_note", "notes"),
    _make_tool("read_note", "notes"),
    _make_tool("append_note", "notes"),
    _make_tool("delete_note", "notes"),
]


def _mock_plugin_registry():
    """Create a mock plugin registry that returns plugin-contributed tools."""
    mock = MagicMock()
    mock.get_all_tool_definitions.return_value = list(_PLUGIN_TOOLS)
    return mock


def _build_sample_tools() -> list[dict]:
    """Build a list covering all categorized tools + some core tools."""
    tools = []
    # Add all categorized tools
    for name in _TOOL_CATEGORIES:
        tools.append(_make_tool(name))
    # Add some core tools (not in _TOOL_CATEGORIES)
    for core_name in [
        "create_task",
        "list_tasks",
        "edit_task",
        "get_task",
    ]:
        tools.append(_make_tool(core_name))
    return tools


@pytest.fixture
def registry():
    """Registry initialized with sample tools + mock plugin registry."""
    reg = ToolRegistry(tools=_build_sample_tools())
    reg.set_plugin_registry(_mock_plugin_registry())
    return reg


def test_registry_has_core_tools(registry):
    core = registry.get_core_tools()
    core_names = {t["name"] for t in core}
    assert "create_task" in core_names
    assert "list_tasks" in core_names
    assert "edit_task" in core_names
    assert "get_task" in core_names
    # Navigation/core meta-tools synthesized by ToolRegistry._ensure_navigation_tools.
    assert "load_tools" in core_names
    assert "send_message" in core_names
    assert "reply_to_user" in core_names


def test_registry_has_categories(registry):
    categories = registry.get_categories()
    cat_names = {c["name"] for c in categories}
    assert cat_names == {
        "files",
        "git",
        "project",
        "agent",
        "memory",
        "notes",
        "system",
        "task",
        "plugin",
        "playbook",
    }

    for cat in categories:
        assert "name" in cat
        assert "description" in cat
        assert "tool_count" in cat
        assert isinstance(cat["tool_count"], int)
        assert cat["tool_count"] > 0


def test_get_category_tools(registry):
    git_tools = registry.get_category_tools("git")
    assert len(git_tools) > 0
    git_names = {t["name"] for t in git_tools}
    assert "git_push" in git_names
    assert "git_create_pr" in git_names
    # Core tools should NOT appear in categories
    assert "create_task" not in git_names


def test_get_unknown_category_returns_none(registry):
    result = registry.get_category_tools("nonexistent")
    assert result is None


def test_all_tools_returns_everything(registry):
    all_tools = registry.get_all_tools()
    all_names = {t["name"] for t in all_tools}
    assert "create_task" in all_names  # core
    assert "git_push" in all_names  # git category
    assert "create_project" in all_names  # project category


def test_no_duplicate_tool_names(registry):
    all_tools = registry.get_all_tools()
    names = [t["name"] for t in all_tools]
    assert len(names) == len(set(names)), (
        f"Duplicate tool names found: {[n for n in names if names.count(n) > 1]}"
    )


def test_category_tool_count_matches(registry):
    categories = registry.get_categories()
    for cat in categories:
        tools = registry.get_category_tools(cat["name"]) or []
        assert len(tools) == cat["tool_count"], (
            f"Category {cat['name']}: metadata says "
            f"{cat['tool_count']} tools but get_category_tools "
            f"returned {len(tools)}"
        )


def test_get_category_tool_names(registry):
    names = registry.get_category_tool_names("git")
    assert isinstance(names, list)
    assert "git_push" in names


# -------------------------------------------------------------------
# CommandHandler integration tests (browse_tools, load_tools, stubs)
# -------------------------------------------------------------------


def _get_command_handler():
    """Import CommandHandler."""
    from src.commands.handler import CommandHandler

    return CommandHandler


def _make_handler():
    """Build a CommandHandler with mocked orchestrator/config and plugin registry."""
    CommandHandler = _get_command_handler()
    orch = MagicMock()
    orch.db = AsyncMock()
    orch.config = MagicMock()
    orch.plugin_registry = _mock_plugin_registry()
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    return CommandHandler(orch, config)


def test_cmd_load_tools_valid_category():
    handler = _make_handler()
    result = asyncio.run(handler.execute("load_tools", {"category": "git"}))

    assert result["loaded"] == "git"
    assert "tools_added" in result
    assert "git_push" in result["tools_added"]
    assert "message" in result


def test_cmd_load_tools_invalid_category():
    handler = _make_handler()
    result = asyncio.run(handler.execute("load_tools", {"category": "nonexistent"}))
    assert "error" in result


def test_cmd_send_message_stub():
    handler = _make_handler()
    result = asyncio.run(
        handler.execute(
            "send_message",
            {
                "channel_id": "12345",
                "content": "Hello world",
            },
        )
    )
    # send_message needs Discord bot reference; without it, error
    assert "error" in result or "success" in result


def test_cmd_get_system_channel_requires_name():
    handler = _make_handler()
    result = asyncio.run(handler.execute("get_system_channel", {}))
    assert "error" in result
    assert "name is required" in result["error"]


def test_cmd_get_system_channel_no_bot():
    handler = _make_handler()
    handler.orchestrator._discord_bot = None
    result = asyncio.run(handler.execute("get_system_channel", {"name": "notifications"}))
    assert "error" in result
    assert "bot" in result["error"].lower() or "guild" in result["error"].lower()


def test_cmd_get_system_channel_unknown_key():
    handler = _make_handler()
    bot = MagicMock()
    bot._guild = MagicMock()
    bot._guild.text_channels = []
    bot.config.discord.channels = {"notifications": "notifications", "control": "control"}
    handler.orchestrator._discord_bot = bot
    result = asyncio.run(handler.execute("get_system_channel", {"name": "unknown"}))
    assert "error" in result
    assert "notifications" in result["error"]
    assert "control" in result["error"]


def test_cmd_get_system_channel_resolves_id():
    handler = _make_handler()
    ch = MagicMock()
    ch.name = "agent-questions"
    ch.id = 987654321
    bot = MagicMock()
    bot._guild = MagicMock()
    bot._guild.text_channels = [ch]
    bot.config.discord.channels = {"agent_questions": "agent-questions"}
    handler.orchestrator._discord_bot = bot
    result = asyncio.run(handler.execute("get_system_channel", {"name": "agent_questions"}))
    assert "error" not in result
    assert result["channel_id"] == "987654321"
    assert result["channel_name"] == "agent-questions"


def test_cmd_get_system_channel_missing_in_guild():
    handler = _make_handler()
    bot = MagicMock()
    bot._guild = MagicMock()
    bot._guild.text_channels = []  # no matching channel
    bot.config.discord.channels = {"agent_questions": "agent-questions"}
    handler.orchestrator._discord_bot = bot
    result = asyncio.run(handler.execute("get_system_channel", {"name": "agent_questions"}))
    assert "error" in result
    assert "not found" in result["error"]


def test_cmd_get_system_channel_notifications_aliases_to_channel():
    """After the config merge, 'notifications' is an alias for 'channel'."""
    handler = _make_handler()
    ch = MagicMock()
    ch.name = "control"
    ch.id = 11111
    bot = MagicMock()
    bot._guild = MagicMock()
    bot._guild.text_channels = [ch]
    # Post-merge config: only 'channel' and 'agent_questions' exist
    bot.config.discord.channels = {"channel": "control", "agent_questions": "agent-questions"}
    handler.orchestrator._discord_bot = bot

    result = asyncio.run(handler.execute("get_system_channel", {"name": "notifications"}))
    assert "error" not in result
    assert result["channel_id"] == "11111"


def test_cmd_get_system_channel_control_aliases_to_channel():
    """'control' is also an alias for 'channel'."""
    handler = _make_handler()
    ch = MagicMock()
    ch.name = "control"
    ch.id = 22222
    bot = MagicMock()
    bot._guild = MagicMock()
    bot._guild.text_channels = [ch]
    bot.config.discord.channels = {"channel": "control", "agent_questions": "agent-questions"}
    handler.orchestrator._discord_bot = bot

    result = asyncio.run(handler.execute("get_system_channel", {"name": "control"}))
    assert "error" not in result
    assert result["channel_id"] == "22222"


# -------------------------------------------------------------------
# Mutable tool set tests (chat() behavior)
# -------------------------------------------------------------------


def test_chat_starts_with_core_tools_only(registry):
    """Verify core tools are significantly fewer than all tools."""
    core_count = len(registry.get_core_tools())
    all_count = len(registry.get_all_tools())

    # Core should be significantly fewer than all
    assert core_count < all_count, f"Core ({core_count}) should be fewer than all ({all_count})"


def test_load_tools_expands_active_set(registry):
    """Verify that simulating load_tools adds category tools."""
    active_tools = {t["name"]: t for t in registry.get_core_tools()}

    # Simulate load_tools("git")
    git_tools = registry.get_category_tools("git")
    assert git_tools is not None
    for t in git_tools:
        active_tools[t["name"]] = t

    # Active set should now include git tools
    assert "git_push" in active_tools
    assert "create_task" in active_tools  # core still present


def test_load_tools_idempotent(registry):
    """Loading same category twice should not duplicate tools."""
    active_tools = {t["name"]: t for t in registry.get_core_tools()}
    initial_count = len(active_tools)

    # Load git twice
    git_tools = registry.get_category_tools("git")
    for t in git_tools:
        active_tools[t["name"]] = t
    count_after_first = len(active_tools)

    for t in git_tools:
        active_tools[t["name"]] = t
    count_after_second = len(active_tools)

    assert count_after_first == count_after_second
    assert count_after_first > initial_count


# -------------------------------------------------------------------
# Tool count preservation after split (Task 5)
# -------------------------------------------------------------------


def test_total_tool_count_preserved():
    """Verify no tools were lost in the split."""
    reg = ToolRegistry(tools=_build_sample_tools())
    reg.set_plugin_registry(_mock_plugin_registry())
    all_tools = reg.get_all_tools()
    all_names = {t["name"] for t in all_tools}

    # These are the navigation tools added by the registry
    expected_new_tools = {
        "load_tools",
        "send_message",
        "reply_to_user",
    }

    # Every original categorized tool should still exist
    for name in _TOOL_CATEGORIES:
        assert name in all_names, f"Tool {name} missing from registry"

    # New tools should be present
    for name in expected_new_tools:
        assert name in all_names, f"New tool {name} missing"

    # Core task tools should be present
    for name in [
        "create_task",
        "list_tasks",
        "edit_task",
        "get_task",
    ]:
        assert name in all_names, f"Core tool {name} missing"

    # Plugin tools should be present
    for name in ["git_push", "read_file", "memory_search"]:
        assert name in all_names, f"Plugin tool {name} missing"


# -------------------------------------------------------------------
# Compact prompt tests (Task 6)
# -------------------------------------------------------------------


def test_core_tools_are_compact(registry):
    """Core tools should be significantly fewer than all tools."""
    core = registry.get_core_tools()
    all_tools = registry.get_all_tools()

    # Core should be roughly 7-15 tools (4 task tools + navigation meta-tools).
    assert len(core) <= 20, f"Core has {len(core)} tools -- should be ~7"
    assert len(core) >= 5, f"Core has {len(core)} tools -- too few"
    # Core should be < 25% of all tools
    assert len(core) < len(all_tools) * 0.25


# -------------------------------------------------------------------
# Tool description quality tests (Task 7)
# -------------------------------------------------------------------


def _real_registry():
    """Create a ToolRegistry with real definitions + mock plugin tools."""
    from src.tools import _ALL_TOOL_DEFINITIONS

    reg = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    reg.set_plugin_registry(_mock_plugin_registry())
    return reg


def test_all_tools_have_descriptions():
    """Every tool in _ALL_TOOL_DEFINITIONS should have a non-empty description."""
    from src.tools import _ALL_TOOL_DEFINITIONS

    for tool in _ALL_TOOL_DEFINITIONS:
        assert "description" in tool, f"Tool {tool['name']} missing description"
        assert len(tool["description"]) > 10, (
            f"Tool {tool['name']} has too-short description: {tool['description']}"
        )


def test_all_tools_have_input_schema():
    """Every tool in _ALL_TOOL_DEFINITIONS should have an input_schema."""
    from src.tools import _ALL_TOOL_DEFINITIONS

    for tool in _ALL_TOOL_DEFINITIONS:
        assert "input_schema" in tool, f"Tool {tool['name']} missing input_schema"


def test_system_prompt_is_compact():
    """System prompt should be well under 500 lines with tiered tools."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder()
    builder.set_identity("chat-agent-system", {"workspace_dir": "/tmp/test"})
    prompt, _ = builder.build()

    line_count = len(prompt.split("\n"))
    assert line_count < 500, f"System prompt is {line_count} lines -- should be compact"


# -------------------------------------------------------------------
# Tool search tests (prompt-based category pre-loading)
# -------------------------------------------------------------------


def test_search_relevant_categories_git_query():
    """Git-related queries should return the git category."""
    registry = _real_registry()
    cats = registry.search_relevant_categories("commit and push my changes")
    assert "git" in cats


def test_search_relevant_categories_project_query():
    """Project management queries should return the project category."""
    registry = _real_registry()
    cats = registry.search_relevant_categories("create a new project with workspace")
    assert "project" in cats


def test_search_relevant_categories_files_query():
    """File-related queries should include the files category."""
    registry = _real_registry()
    # Use a higher cap since read_logs and compile_playbook also strongly
    # match "read ... file ... errors" — files ranks slightly below them.
    cats = registry.search_relevant_categories(
        "read the file and grep for errors", max_categories=5
    )
    assert "files" in cats


def test_search_relevant_categories_memory_query():
    """Memory-related queries should return the memory category."""
    registry = _real_registry()
    cats = registry.search_relevant_categories("reindex memory and compact notes")
    assert "memory" in cats


def test_search_relevant_categories_empty_query():
    """Empty query should return no categories."""
    registry = _real_registry()
    cats = registry.search_relevant_categories("")
    assert cats == []


def test_search_relevant_categories_max_limit():
    """Should return at most max_categories results."""
    registry = _real_registry()
    cats = registry.search_relevant_categories(
        "commit project files hooks memory agent system",
        max_categories=2,
    )
    assert len(cats) <= 2


def test_search_relevant_categories_respects_min_score():
    """Very unrelated queries should return few or no categories."""
    registry = _real_registry()
    cats = registry.search_relevant_categories("xyzzy plugh frobozz", min_score=0.5)
    assert len(cats) == 0


def test_tokenize_splits_underscores():
    """Tokenizer should split on underscores and filter short words."""
    tokens = ToolRegistry._tokenize("git_create_branch some_tool")
    assert "git" in tokens
    assert "create" in tokens
    assert "branch" in tokens
    assert "tool" in tokens


def test_search_with_sample_tools(registry):
    """Search should work with the sample tool set (minimal descriptions)."""
    # Sample tools have descriptions like "Tool: git_push"
    cats = registry.search_relevant_categories("push")
    # "push" appears in git_push's name → git category
    assert "git" in cats


# ------------------------------------------------------------------
# Tool schema compression
# ------------------------------------------------------------------


class TestCompressToolSchema:
    """Tests for compress_tool_schema() and compressed flag on getters."""

    def test_compress_strips_param_descriptions(self):
        tool = {
            "name": "my_tool",
            "description": "A tool that does something very specific and wonderful.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name of the thing to create",
                    },
                    "count": {
                        "type": "integer",
                        "description": "How many items to process",
                        "default": 10,
                    },
                },
                "required": ["name"],
            },
        }
        compressed = ToolRegistry.compress_tool_schema(tool)
        assert compressed["name"] == "my_tool"
        # Param descriptions should be stripped
        assert "description" not in compressed["input_schema"]["properties"]["name"]
        assert "description" not in compressed["input_schema"]["properties"]["count"]
        # Type and default preserved
        assert compressed["input_schema"]["properties"]["name"]["type"] == "string"
        assert compressed["input_schema"]["properties"]["count"]["default"] == 10
        # Required preserved
        assert compressed["input_schema"]["required"] == ["name"]

    def test_compress_truncates_long_description(self):
        tool = {
            "name": "verbose_tool",
            "description": "A" * 200,
            "input_schema": {"type": "object", "properties": {}},
        }
        compressed = ToolRegistry.compress_tool_schema(tool)
        assert len(compressed["description"]) <= 80

    def test_compress_keeps_first_sentence(self):
        tool = {
            "name": "multi_sentence",
            "description": "First sentence. Second sentence with more details.",
            "input_schema": {"type": "object", "properties": {}},
        }
        compressed = ToolRegistry.compress_tool_schema(tool)
        assert compressed["description"] == "First sentence."

    def test_compress_preserves_enums(self):
        tool = {
            "name": "enum_tool",
            "description": "Tool with enum.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["fast", "slow"],
                        "description": "Processing mode",
                    },
                },
            },
        }
        compressed = ToolRegistry.compress_tool_schema(tool)
        assert compressed["input_schema"]["properties"]["mode"]["enum"] == ["fast", "slow"]

    def test_get_core_tools_compressed_with_real_tools(self):
        """Compressed real tools should be significantly smaller."""
        reg = _real_registry()
        full = reg.get_core_tools(compressed=False)
        comp = reg.get_core_tools(compressed=True)
        assert len(full) == len(comp)
        import json

        full_size = len(json.dumps(full))
        comp_size = len(json.dumps(comp))
        assert comp_size < full_size
        # Should save at least 30%
        assert comp_size < full_size * 0.7

    def test_get_category_tools_compressed_with_real_tools(self):
        """Compressed real category tools should be smaller."""
        reg = _real_registry()
        full = reg.get_category_tools("git", compressed=False)
        comp = reg.get_category_tools("git", compressed=True)
        assert full is not None and comp is not None
        assert len(full) == len(comp)
        import json

        assert len(json.dumps(comp)) < len(json.dumps(full))


# ------------------------------------------------------------------
# Playbook category registration (Roadmap 5.5.7)
# ------------------------------------------------------------------

# Canonical list of playbook commands from spec §15.
_PLAYBOOK_COMMANDS = [
    "compile_playbook",
    "run_playbook",
    "dry_run_playbook",
    "show_playbook_graph",
    "list_playbooks",
    "list_playbook_runs",
    "inspect_playbook_run",
    "resume_playbook",
    "recover_workflow",
    "playbook_health",
    "playbook_graph_view",
    "get_playbook_source",
    "update_playbook_source",
    "create_playbook",
    "delete_playbook",
]


class TestPlaybookToolRegistration:
    """Verify all playbook commands from spec §15 are registered in the tool registry.

    Roadmap 5.5.7 — Register all playbook commands in CommandHandler via tool
    registry.  This test class validates the *complete* set as a cohesive unit,
    complementing the per-command tests that live alongside each command's own
    test file.
    """

    # -- Category mapping --------------------------------------------------

    def test_all_playbook_commands_in_category_map(self):
        """Every spec §15 command appears in _TOOL_CATEGORIES under 'playbook'."""
        from src.tools import _TOOL_CATEGORIES

        for cmd in _PLAYBOOK_COMMANDS:
            assert cmd in _TOOL_CATEGORIES, f"{cmd} missing from _TOOL_CATEGORIES"
            assert _TOOL_CATEGORIES[cmd] == "playbook", (
                f"{cmd} mapped to '{_TOOL_CATEGORIES[cmd]}' instead of 'playbook'"
            )

    def test_no_extra_playbook_commands(self):
        """No unexpected tools are mapped to the 'playbook' category."""
        from src.tools import _TOOL_CATEGORIES

        actual = {name for name, cat in _TOOL_CATEGORIES.items() if cat == "playbook"}
        expected = set(_PLAYBOOK_COMMANDS)
        extra = actual - expected
        assert not extra, f"Unexpected playbook-category tools: {extra}"

    # -- Tool definitions --------------------------------------------------

    def test_all_playbook_commands_have_definitions(self):
        """Every spec §15 command has a full tool definition."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        defined = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
        for cmd in _PLAYBOOK_COMMANDS:
            assert cmd in defined, f"{cmd} missing from _ALL_TOOL_DEFINITIONS"

    def test_all_playbook_definitions_have_description(self):
        """Every playbook tool has a non-trivial description."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        for tool in _ALL_TOOL_DEFINITIONS:
            if tool["name"] in _PLAYBOOK_COMMANDS:
                assert "description" in tool, f"{tool['name']} missing description"
                assert len(tool["description"]) > 20, (
                    f"{tool['name']} description too short: {tool['description']!r}"
                )

    def test_all_playbook_definitions_have_input_schema(self):
        """Every playbook tool has an input_schema with type=object."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        for tool in _ALL_TOOL_DEFINITIONS:
            if tool["name"] in _PLAYBOOK_COMMANDS:
                assert "input_schema" in tool, f"{tool['name']} missing input_schema"
                assert tool["input_schema"]["type"] == "object", (
                    f"{tool['name']} input_schema.type is not 'object'"
                )

    def test_required_params_present_in_schema(self):
        """Tools with required params have them defined in properties."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        for tool in _ALL_TOOL_DEFINITIONS:
            if tool["name"] in _PLAYBOOK_COMMANDS:
                schema = tool["input_schema"]
                required = schema.get("required", [])
                props = schema.get("properties", {})
                for param in required:
                    assert param in props, (
                        f"{tool['name']}: required param '{param}' not in properties"
                    )

    # -- ToolRegistry integration -----------------------------------------

    def test_playbook_category_exists_in_registry(self):
        """The 'playbook' category appears in registry.get_categories()."""
        reg = _real_registry()
        categories = reg.get_categories()
        cat_names = {c["name"] for c in categories}
        assert "playbook" in cat_names

    def test_playbook_category_has_correct_count(self):
        """The 'playbook' category reports exactly 7 tools."""
        reg = _real_registry()
        categories = reg.get_categories()
        playbook_cat = next(c for c in categories if c["name"] == "playbook")
        assert playbook_cat["tool_count"] == len(_PLAYBOOK_COMMANDS), (
            f"Expected {len(_PLAYBOOK_COMMANDS)} playbook tools, got {playbook_cat['tool_count']}"
        )

    def test_playbook_category_tools_match_spec(self):
        """get_category_tool_names('playbook') returns exactly the spec §15 set."""
        reg = _real_registry()
        names = reg.get_category_tool_names("playbook")
        assert names is not None
        assert set(names) == set(_PLAYBOOK_COMMANDS)

    def test_playbook_tools_not_in_core(self):
        """Playbook tools are on-demand, not in the core set."""
        reg = _real_registry()
        core_names = {t["name"] for t in reg.get_core_tools()}
        for cmd in _PLAYBOOK_COMMANDS:
            assert cmd not in core_names, (
                f"{cmd} should not be in core tools — it's in the playbook category"
            )

    def test_playbook_category_description(self):
        """The playbook CategoryMeta has a descriptive string."""
        from src.tools import CATEGORIES

        assert "playbook" in CATEGORIES
        meta = CATEGORIES["playbook"]
        assert "playbook" in meta.description.lower() or "compilation" in meta.description.lower()

    # -- Search integration ------------------------------------------------

    def test_search_finds_playbook_category(self):
        """A playbook-related query returns the 'playbook' category."""
        reg = _real_registry()
        cats = reg.search_relevant_categories("compile and run a playbook")
        assert "playbook" in cats

    def test_search_finds_playbook_for_resume_query(self):
        """A human-in-the-loop query returns the playbook category."""
        reg = _real_registry()
        cats = reg.search_relevant_categories("resume the paused playbook run")
        assert "playbook" in cats

    # -- CommandHandler routing (execute() dispatch) -----------------------

    def test_all_commands_have_handler_methods(self):
        """Every spec §15 command has a _cmd_* method on CommandHandler."""
        handler = _make_handler()
        for cmd in _PLAYBOOK_COMMANDS:
            method = getattr(handler, f"_cmd_{cmd}", None)
            assert method is not None, f"CommandHandler missing _cmd_{cmd} method"
            assert callable(method), f"_cmd_{cmd} is not callable"

    def test_execute_routes_unknown_playbook_command(self):
        """execute() returns error for a non-existent playbook command."""
        handler = _make_handler()
        result = asyncio.run(handler.execute("nonexistent_playbook_cmd", {}))
        assert "error" in result

    def test_load_tools_playbook_category(self):
        """load_tools('playbook') returns all playbook tool definitions."""
        handler = _make_handler()
        result = asyncio.run(handler.execute("load_tools", {"category": "playbook"}))
        assert result.get("loaded") == "playbook"
        assert "tools_added" in result
        loaded_names = set(result["tools_added"])
        assert loaded_names == set(_PLAYBOOK_COMMANDS), (
            f"Expected {set(_PLAYBOOK_COMMANDS)}, got {loaded_names}"
        )


# ------------------------------------------------------------------
# get_stuck_tasks — system-scoped helper for the system-health-check
# playbook.  Promoted from a multi-step supervisor procedure to a
# first-class tool so the compiler no longer needs to hallucinate the
# phantom ``system_monitor.get_stuck_tasks``.
# ------------------------------------------------------------------


class TestGetStuckTasksRegistration:
    """Verify ``get_stuck_tasks`` is a real, system-scoped tool."""

    def test_in_category_map(self):
        from src.tools import _TOOL_CATEGORIES

        assert _TOOL_CATEGORIES.get("get_stuck_tasks") == "system"

    def test_has_definition(self):
        from src.tools import _ALL_TOOL_DEFINITIONS

        names = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
        assert "get_stuck_tasks" in names

    def test_definition_has_expected_parameters(self):
        """Optional params with defaults documented, no required params."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        tool = next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == "get_stuck_tasks")
        props = tool["input_schema"]["properties"]

        for key in (
            "assigned_threshold_seconds",
            "in_progress_threshold_seconds",
            "now",
            "project_id",
        ):
            assert key in props, f"missing param: {key}"

        # None of the params are required — the defaults handle the common
        # "just tell me what's stuck" case.
        assert tool["input_schema"].get("required", []) == []

    def test_system_category_loads_get_stuck_tasks(self):
        """Loading the 'system' category surfaces the new tool."""
        reg = _real_registry()
        names = reg.get_category_tool_names("system")
        assert names is not None
        assert "get_stuck_tasks" in names
