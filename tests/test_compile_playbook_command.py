"""Tests for the compile_playbook command (roadmap 5.5.1).

Tests cover:
  (a) compile_playbook with valid markdown returns success and compiled metadata
  (b) compile_playbook with invalid markdown returns error with details
  (c) compile_playbook with file path reads from disk
  (d) compile_playbook with missing args returns helpful error
  (e) compile_playbook when playbook manager is unavailable returns error
  (f) compile_playbook force flag defaults to True for manual trigger
  (g) compile_playbook handles compilation exceptions gracefully
  (h) Tool registry includes compile_playbook definition
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VALID_COMPILED_NODES = {
    "nodes": {
        "start": {
            "entry": True,
            "prompt": "Do something.",
            "goto": "end",
        },
        "end": {"terminal": True},
    }
}


def _make_playbook_md(
    *,
    playbook_id: str = "test-playbook",
    triggers: str = "- git.commit",
    scope: str = "system",
    body: str = "# Test\n\nDo something then finish.",
) -> str:
    """Create a minimal playbook markdown string."""
    return f"""\
---
id: {playbook_id}
triggers:
  {triggers}
scope: {scope}
---

{body}
"""


SIMPLE_PLAYBOOK_MD = _make_playbook_md()


def _make_compiled_playbook(**overrides):
    """Create a mock CompiledPlaybook with realistic attributes."""
    from src.playbooks.models import CompiledPlaybook, PlaybookNode

    defaults = dict(
        id="test-playbook",
        version=1,
        source_hash="abcdef1234567890",
        triggers=["git.commit"],
        scope="system",
        nodes={
            "start": PlaybookNode(entry=True, prompt="Do something.", goto="end"),
            "end": PlaybookNode(terminal=True),
        },
    )
    defaults.update(overrides)
    return CompiledPlaybook(**defaults)


def _make_compilation_result(*, success=True, playbook=None, **overrides):
    """Create a mock CompilationResult."""
    from src.playbooks.compiler import CompilationResult

    if success and playbook is None:
        playbook = _make_compiled_playbook()

    defaults = dict(
        success=success,
        playbook=playbook,
        errors=[],
        source_hash="abcdef1234567890",
        retries_used=0,
        skipped=False,
    )
    defaults.update(overrides)
    return CompilationResult(**defaults)


def _make_handler(*, has_playbook_manager=True, compile_result=None):
    """Create a CommandHandler with a mock orchestrator."""
    mock_orch = MagicMock()
    mock_orch.db = AsyncMock()
    mock_config = MagicMock()

    if has_playbook_manager:
        pm = AsyncMock()
        if compile_result is None:
            compile_result = _make_compilation_result()
        pm.compile_playbook = AsyncMock(return_value=compile_result)
        mock_orch.playbook_manager = pm
    else:
        # Remove the attribute entirely so hasattr returns False
        if hasattr(mock_orch, "playbook_manager"):
            del mock_orch.playbook_manager
        mock_orch.playbook_manager = None

    handler = CommandHandler(mock_orch, mock_config)
    return handler


# ---------------------------------------------------------------------------
# Test: Success cases
# ---------------------------------------------------------------------------


class TestCompilePlaybookSuccess:
    """Tests for successful compilation via the command."""

    async def test_compile_with_inline_markdown(self):
        """compile_playbook with valid inline markdown returns success metadata."""
        handler = _make_handler()

        result = await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        assert result["compiled"] is True
        assert result["playbook_id"] == "test-playbook"
        assert result["version"] == 1
        assert result["source_hash"] == "abcdef1234567890"
        assert result["node_count"] == 2
        assert result["scope"] == "system"
        assert "git.commit" in [str(t) for t in result["triggers"]]

    async def test_compile_with_file_path(self, tmp_path):
        """compile_playbook with a file path reads the file and compiles."""
        md_file = tmp_path / "test.md"
        md_file.write_text(SIMPLE_PLAYBOOK_MD, encoding="utf-8")

        handler = _make_handler()
        result = await handler._cmd_compile_playbook({"path": str(md_file)})

        assert result["compiled"] is True
        assert result["playbook_id"] == "test-playbook"
        # Verify the manager was called with the file content
        handler.orchestrator.playbook_manager.compile_playbook.assert_awaited_once()
        call_args = handler.orchestrator.playbook_manager.compile_playbook.call_args
        assert call_args[0][0] == SIMPLE_PLAYBOOK_MD  # positional arg: markdown

    async def test_compile_passes_force_true_by_default(self):
        """Manual trigger defaults force=True."""
        handler = _make_handler()

        await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["force"] is True

    async def test_compile_force_false_when_explicit(self):
        """force=False can be explicitly set."""
        handler = _make_handler()

        await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD, "force": False})

        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["force"] is False

    async def test_compile_force_string_true(self):
        """force as string 'true' is converted to boolean True."""
        handler = _make_handler()

        await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD, "force": "true"})

        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["force"] is True

    async def test_compile_force_string_false(self):
        """force as string 'false' is converted to boolean False."""
        handler = _make_handler()

        await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD, "force": "false"})

        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["force"] is False

    async def test_compile_skipped_result(self):
        """When compilation is skipped (unchanged), response includes skipped=True."""
        result = _make_compilation_result(skipped=True)
        handler = _make_handler(compile_result=result)

        resp = await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        assert resp["compiled"] is True
        assert resp["skipped"] is True

    async def test_compile_passes_source_path(self, tmp_path):
        """The source_path kwarg is forwarded from the path arg."""
        md_file = tmp_path / "test.md"
        md_file.write_text(SIMPLE_PLAYBOOK_MD, encoding="utf-8")

        handler = _make_handler()
        await handler._cmd_compile_playbook({"path": str(md_file)})

        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["source_path"] == str(md_file)
        assert call_kwargs["rel_path"] == str(md_file)

    async def test_compile_inline_markdown_preferred_over_path(self, tmp_path):
        """When both markdown and path are given, inline markdown is used."""
        md_file = tmp_path / "test.md"
        md_file.write_text("file content", encoding="utf-8")

        handler = _make_handler()
        await handler._cmd_compile_playbook(
            {
                "markdown": SIMPLE_PLAYBOOK_MD,
                "path": str(md_file),
            }
        )

        # Should use inline markdown (stripped), not read from file
        call_args = handler.orchestrator.playbook_manager.compile_playbook.call_args
        assert call_args[0][0] == SIMPLE_PLAYBOOK_MD.strip()


# ---------------------------------------------------------------------------
# Test: Error cases
# ---------------------------------------------------------------------------


class TestCompilePlaybookErrors:
    """Tests for error handling in the compile_playbook command."""

    async def test_missing_both_markdown_and_path(self):
        """Returns error when neither markdown nor path is provided."""
        handler = _make_handler()

        result = await handler._cmd_compile_playbook({})

        assert "error" in result
        assert "markdown" in result["error"].lower() or "path" in result["error"].lower()

    async def test_empty_markdown_and_no_path(self):
        """Returns error when markdown is empty and no path given."""
        handler = _make_handler()

        result = await handler._cmd_compile_playbook({"markdown": "   "})

        assert "error" in result

    async def test_nonexistent_file_path(self):
        """Returns error when the file path doesn't exist."""
        handler = _make_handler()

        result = await handler._cmd_compile_playbook({"path": "/nonexistent/playbook.md"})

        assert "error" in result
        assert "not found" in result["error"].lower() or "File not found" in result["error"]

    async def test_no_playbook_manager(self):
        """Returns error when playbook manager is not initialised."""
        handler = _make_handler(has_playbook_manager=False)

        result = await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        assert "error" in result
        assert "not initialised" in result["error"].lower() or "not initial" in result["error"]

    async def test_compilation_failure_returns_errors(self):
        """Failed compilation returns error details."""
        fail_result = _make_compilation_result(
            success=False,
            playbook=None,
            errors=["Missing entry node", "Node 'x' has no transitions"],
            retries_used=2,
        )
        handler = _make_handler(compile_result=fail_result)

        result = await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        assert "error" in result
        assert result["errors"] == ["Missing entry node", "Node 'x' has no transitions"]
        assert result["retries_used"] == 2
        assert result["source_hash"] == "abcdef1234567890"

    async def test_compilation_exception_is_caught(self):
        """Exceptions during compilation are caught and returned as errors."""
        handler = _make_handler()
        handler.orchestrator.playbook_manager.compile_playbook = AsyncMock(
            side_effect=RuntimeError("LLM provider crashed")
        )

        result = await handler._cmd_compile_playbook({"markdown": SIMPLE_PLAYBOOK_MD})

        assert "error" in result
        assert "LLM provider crashed" in result["error"]


# ---------------------------------------------------------------------------
# Test: Tool registry
# ---------------------------------------------------------------------------


class TestToolRegistryIncludesCompilePlaybook:
    """Verify compile_playbook is properly registered in the tool registry."""

    def test_tool_definition_exists(self):
        """compile_playbook tool definition exists in _ALL_TOOL_DEFINITIONS."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        names = [t["name"] for t in _ALL_TOOL_DEFINITIONS]
        assert "compile_playbook" in names

    def test_tool_definition_has_schema(self):
        """compile_playbook tool definition has a valid input_schema."""
        from src.tools import _ALL_TOOL_DEFINITIONS

        tool = next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == "compile_playbook")
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "markdown" in schema["properties"]
        assert "path" in schema["properties"]
        assert "force" in schema["properties"]

    def test_tool_category_mapping(self):
        """compile_playbook is mapped to the playbook category."""
        from src.tools import _TOOL_CATEGORIES

        assert _TOOL_CATEGORIES["compile_playbook"] == "playbook"

    def test_tool_registry_category_includes_compile_playbook(self):
        """ToolRegistry includes compile_playbook in the playbook category."""
        from src.tools import ToolRegistry, _ALL_TOOL_DEFINITIONS

        registry = ToolRegistry(_ALL_TOOL_DEFINITIONS)
        tool_names = registry.get_category_tool_names("playbook")
        assert tool_names is not None
        assert "compile_playbook" in tool_names


# ---------------------------------------------------------------------------
# Test: execute() integration
# ---------------------------------------------------------------------------


class TestCompilePlaybookViaExecute:
    """Test that compile_playbook is callable through the execute() dispatcher."""

    async def test_execute_routes_to_compile_playbook(self):
        """execute('compile_playbook', ...) routes to the command handler."""
        handler = _make_handler()

        result = await handler.execute("compile_playbook", {"markdown": SIMPLE_PLAYBOOK_MD})

        assert result.get("compiled") is True or "error" not in result
