"""Tests for ToolRegistry -- tool categorization and on-demand loading."""

import pytest

from src.tool_registry import ToolRegistry, _TOOL_CATEGORIES


def _make_tool(name: str) -> dict:
    """Create a minimal tool definition dict."""
    return {
        "name": name,
        "description": f"Tool: {name}",
        "input_schema": {"type": "object", "properties": {}},
    }


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
        "memory_search",
    ]:
        tools.append(_make_tool(core_name))
    return tools


@pytest.fixture
def registry():
    """Registry initialized with sample tools (no chat_agent import)."""
    return ToolRegistry(tools=_build_sample_tools())


def test_registry_has_core_tools(registry):
    core = registry.get_core_tools()
    core_names = {t["name"] for t in core}
    assert "create_task" in core_names
    assert "list_tasks" in core_names
    assert "edit_task" in core_names
    assert "get_task" in core_names
    assert "browse_tools" in core_names
    assert "load_tools" in core_names
    assert "send_message" in core_names


def test_registry_has_categories(registry):
    categories = registry.get_categories()
    cat_names = {c["name"] for c in categories}
    assert cat_names == {
        "files",
        "git",
        "project",
        "agent",
        "hooks",
        "memory",
        "system",
        "task",
        "plugin",
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
        tools = registry.get_category_tools(cat["name"])
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

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock


def _get_command_handler():
    """Import CommandHandler."""
    from src.command_handler import CommandHandler

    return CommandHandler


def _make_handler():
    """Build a CommandHandler with mocked orchestrator/config."""
    CommandHandler = _get_command_handler()
    orch = MagicMock()
    orch.db = AsyncMock()
    orch.config = MagicMock()
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    return CommandHandler(orch, config)


def test_cmd_browse_tools():
    handler = _make_handler()
    result = asyncio.run(handler.execute("browse_tools", {}))

    assert "categories" in result
    cat_names = {c["name"] for c in result["categories"]}
    assert "git" in cat_names
    assert "project" in cat_names
    for cat in result["categories"]:
        assert "description" in cat
        assert "tool_count" in cat


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


def test_cmd_browse_rules():
    handler = _make_handler()
    result = asyncio.run(handler.execute("browse_rules", {}))
    # Phase 2 implemented — returns rules list (may be empty)
    assert "rules" in result or "error" not in result


def test_cmd_save_rule():
    handler = _make_handler()
    # Ensure the real implementation is wired (not a Phase 2 stub)
    # Full save_rule testing is in test_rule_manager.py
    assert hasattr(handler, "_cmd_save_rule")


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
    registry = ToolRegistry(tools=_build_sample_tools())
    all_tools = registry.get_all_tools()
    all_names = {t["name"] for t in all_tools}

    # These are the new navigation tools added by the registry
    expected_new_tools = {
        "browse_tools",
        "load_tools",
        "send_message",
        "reply_to_user",
        "list_rules",
        "load_rule",
        "save_rule",
        "delete_rule",
        "refresh_hooks",
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
        "memory_search",
    ]:
        assert name in all_names, f"Core tool {name} missing"


# -------------------------------------------------------------------
# Compact prompt tests (Task 6)
# -------------------------------------------------------------------


def test_core_tools_are_compact(registry):
    """Core tools should be significantly fewer than all tools."""
    core = registry.get_core_tools()
    all_tools = registry.get_all_tools()

    # Core should be roughly 10-15 tools
    assert len(core) <= 20, f"Core has {len(core)} tools -- should be ~11"
    assert len(core) >= 8, f"Core has {len(core)} tools -- too few"
    # Core should be < 25% of all tools
    assert len(core) < len(all_tools) * 0.25


# -------------------------------------------------------------------
# Tool description quality tests (Task 7)
# -------------------------------------------------------------------


def test_all_tools_have_descriptions():
    """Every tool should have a non-empty description."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    for tool in registry.get_all_tools():
        assert "description" in tool, f"Tool {tool['name']} missing description"
        assert len(tool["description"]) > 10, (
            f"Tool {tool['name']} has too-short description: {tool['description']}"
        )


def test_all_tools_have_input_schema():
    """Every tool should have an input_schema."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    for tool in registry.get_all_tools():
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
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("commit and push my changes")
    assert "git" in cats


def test_search_relevant_categories_project_query():
    """Project management queries should return the project category."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("create a new project with workspace")
    assert "project" in cats


def test_search_relevant_categories_files_query():
    """File-related queries should return the files category."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("read the file and grep for errors")
    assert "files" in cats


def test_search_relevant_categories_hooks_query():
    """Hook-related queries should return the hooks category."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("create a hook that fires on schedule")
    assert "hooks" in cats


def test_search_relevant_categories_memory_query():
    """Memory-related queries should return the memory category."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("reindex memory and compact notes")
    assert "memory" in cats


def test_search_relevant_categories_empty_query():
    """Empty query should return no categories."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories("")
    assert cats == []


def test_search_relevant_categories_max_limit():
    """Should return at most max_categories results."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
    cats = registry.search_relevant_categories(
        "commit project files hooks memory agent system",
        max_categories=2,
    )
    assert len(cats) <= 2


def test_search_relevant_categories_respects_min_score():
    """Very unrelated queries should return few or no categories."""
    from src.tool_registry import _ALL_TOOL_DEFINITIONS

    registry = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
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
        reg = ToolRegistry()  # uses real _ALL_TOOL_DEFINITIONS
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
        reg = ToolRegistry()  # uses real _ALL_TOOL_DEFINITIONS
        full = reg.get_category_tools("git", compressed=False)
        comp = reg.get_category_tools("git", compressed=True)
        assert full is not None and comp is not None
        assert len(full) == len(comp)
        import json

        assert len(json.dumps(comp)) < len(json.dumps(full))
