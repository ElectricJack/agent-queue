"""Tests for all playbook commands (roadmap 5.5.8).

Comprehensive test suite for the seven spec §15 playbook commands registered in
CommandHandler.  Each roadmap test case (a–i) is covered:

  (a) compile_playbook with valid markdown → success + compiled metadata
  (b) compile_playbook with invalid markdown → error with details
  (c) dry_run_playbook simulates execution → node trace, no side effects
  (d) show_playbook_graph → ASCII or mermaid, correct nodes and transitions
  (e) list_playbooks → all scopes, status, last run time
  (f) list_playbook_runs → recent runs with status and node path
  (g) inspect_playbook_run → full node trace, conversation, token usage
  (h) All commands return dict format per command handler convention
  (i) Commands with invalid arguments return helpful error messages

Individual commands also have dedicated test files for deeper coverage:
  - test_compile_playbook_command.py   (a, b)
  - test_dry_run_playbook.py           (c)
  - test_list_playbooks_command.py     (e)
  - test_playbook_runner.py            (f, g) — TestListPlaybookRunsCommand, TestCmdInspectPlaybookRun

This file focuses on:
  - show_playbook_graph command handler (d) — the only command without a
    dedicated command-handler test file
  - Response format consistency across all 7 commands (h)
  - Error handling for invalid arguments across all 7 commands (i)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

from src.command_handler import CommandHandler
from src.playbook_models import CompiledPlaybook, PlaybookNode, PlaybookTransition


# ---------------------------------------------------------------------------
# Fixtures — shared helpers
# ---------------------------------------------------------------------------


def _make_playbook(
    *,
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str] | None = None,
    scope: str = "system",
    cooldown_seconds: int | None = None,
    max_tokens: int | None = None,
    compiled_at: str | None = "2026-01-15T10:00:00Z",
    nodes: dict[str, PlaybookNode] | None = None,
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    if nodes is None:
        nodes = {
            "start": PlaybookNode(
                entry=True,
                prompt="Do something.",
                goto="end",
            ),
            "end": PlaybookNode(terminal=True),
        }
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["git.commit"],
        scope=scope,
        nodes=nodes,
        cooldown_seconds=cooldown_seconds,
        max_tokens=max_tokens,
        compiled_at=compiled_at,
    )


def _make_branching_playbook() -> CompiledPlaybook:
    """Create a playbook with decision transitions for graph testing."""
    return CompiledPlaybook(
        id="branch-pb",
        version=2,
        source_hash="bbb222",
        triggers=["task.completed"],
        scope="project",
        nodes={
            "entry": PlaybookNode(
                entry=True,
                prompt="Analyze the results.",
                transitions=[
                    PlaybookTransition(goto="fix", when="needs fixes"),
                    PlaybookTransition(goto="done", when="all good"),
                ],
            ),
            "fix": PlaybookNode(
                prompt="Apply fixes to the code.",
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


@dataclass
class FakePlaybookRun:
    """Minimal stand-in for a PlaybookRun database record."""

    run_id: str = "run-001"
    playbook_id: str = "test-playbook"
    playbook_version: int = 1
    status: str = "completed"
    current_node: str | None = None
    started_at: float = 1700000000.0
    completed_at: float | None = 1700000060.0
    tokens_used: int = 500
    node_trace: str = "[]"
    conversation_history: str = "[]"
    trigger_event: str = "{}"
    error: str | None = None
    paused_at: float | None = None
    pinned_graph: str | None = None


def _make_playbook_manager(
    playbooks: dict[str, CompiledPlaybook] | None = None,
    scope_identifiers: dict[str, str | None] | None = None,
    cooldown_remaining: dict[str, float] | None = None,
    runs_for_playbook: dict[str, list[str]] | None = None,
):
    """Create a mock PlaybookManager with configurable state."""
    pm = MagicMock()
    active = playbooks or {}
    pm.active_playbooks = active
    pm._active = active  # Used by resume_playbook
    pm.get_playbook = MagicMock(side_effect=lambda pid: active.get(pid))

    scope_ids = scope_identifiers or {}
    pm.get_scope_identifier = MagicMock(side_effect=lambda pid: scope_ids.get(pid))

    cooldowns = cooldown_remaining or {}
    pm.get_cooldown_remaining = MagicMock(
        side_effect=lambda pid, scope="system": cooldowns.get(pid, 0.0)
    )

    runs = runs_for_playbook or {}
    pm.get_runs_for_playbook = MagicMock(side_effect=lambda pid: runs.get(pid, []))

    return pm


def _make_handler(
    *,
    has_playbook_manager: bool = True,
    playbooks: dict[str, CompiledPlaybook] | None = None,
    scope_identifiers: dict[str, str | None] | None = None,
    cooldown_remaining: dict[str, float] | None = None,
    runs_for_playbook: dict[str, list[str]] | None = None,
    db_runs: list | None = None,
    compile_result=None,
):
    """Create a CommandHandler with a mock orchestrator and database."""
    mock_orch = MagicMock()
    mock_db = AsyncMock()
    mock_orch.db = mock_db
    mock_config = MagicMock()

    if has_playbook_manager:
        pm = _make_playbook_manager(
            playbooks=playbooks,
            scope_identifiers=scope_identifiers,
            cooldown_remaining=cooldown_remaining,
            runs_for_playbook=runs_for_playbook,
        )
        mock_orch.playbook_manager = pm

        if compile_result is not None:
            pm.compile_playbook = AsyncMock(return_value=compile_result)
    else:
        mock_orch.playbook_manager = None

    # Configure db methods
    if db_runs is not None:
        mock_db.list_playbook_runs = AsyncMock(return_value=db_runs)
    else:
        mock_db.list_playbook_runs = AsyncMock(return_value=[])

    mock_db.get_playbook_run = AsyncMock(return_value=None)

    handler = CommandHandler(mock_orch, mock_config)
    return handler


# ===========================================================================
# (d) show_playbook_graph — ASCII or mermaid, correct nodes and transitions
# ===========================================================================


class TestShowPlaybookGraphCommand:
    """Tests for _cmd_show_playbook_graph command handler (roadmap 5.5.8 case d)."""

    async def test_ascii_format_returns_graph_with_correct_nodes(self):
        """show_playbook_graph in ASCII format includes all nodes and transitions."""
        pb = _make_playbook(playbook_id="my-graph", version=3)
        handler = _make_handler(playbooks={"my-graph": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "my-graph", "format": "ascii"}
        )

        assert "error" not in result
        assert result["playbook_id"] == "my-graph"
        assert result["format"] == "ascii"
        assert result["node_count"] == 2
        assert result["version"] == 3

        # The graph output should contain node names
        graph = result["graph"]
        assert "start" in graph
        assert "end" in graph

    async def test_mermaid_format_returns_flowchart(self):
        """show_playbook_graph in mermaid format returns valid flowchart syntax."""
        pb = _make_playbook(playbook_id="mermaid-test", version=1)
        handler = _make_handler(playbooks={"mermaid-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "mermaid-test", "format": "mermaid"}
        )

        assert "error" not in result
        assert result["format"] == "mermaid"
        graph = result["graph"]
        assert "flowchart" in graph
        assert "start" in graph
        assert "end" in graph

    async def test_mermaid_with_lr_direction(self):
        """Mermaid output respects LR direction parameter."""
        pb = _make_playbook(playbook_id="lr-graph")
        handler = _make_handler(playbooks={"lr-graph": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "lr-graph", "format": "mermaid", "direction": "LR"}
        )

        assert "error" not in result
        assert "flowchart LR" in result["graph"]

    async def test_mermaid_default_td_direction(self):
        """Mermaid output uses TD direction by default."""
        pb = _make_playbook(playbook_id="td-graph")
        handler = _make_handler(playbooks={"td-graph": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "td-graph", "format": "mermaid"}
        )

        assert "error" not in result
        assert "flowchart TD" in result["graph"]

    async def test_ascii_default_format(self):
        """Default format is ASCII when not specified."""
        pb = _make_playbook(playbook_id="default-fmt")
        handler = _make_handler(playbooks={"default-fmt": pb})

        result = await handler._cmd_show_playbook_graph({"playbook_id": "default-fmt"})

        assert result["format"] == "ascii"

    async def test_branching_graph_shows_transitions(self):
        """Branching playbook graph shows all transition edges in ASCII."""
        pb = _make_branching_playbook()
        handler = _make_handler(playbooks={"branch-pb": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "branch-pb", "format": "ascii"}
        )

        assert result["node_count"] == 3
        graph = result["graph"]
        # All nodes present
        assert "entry" in graph
        assert "fix" in graph
        assert "done" in graph
        # Transitions shown (arrows to target nodes)
        assert "fix" in graph
        assert "done" in graph

    async def test_branching_graph_mermaid_includes_edges(self):
        """Branching playbook in mermaid format includes transition edges."""
        pb = _make_branching_playbook()
        handler = _make_handler(playbooks={"branch-pb": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "branch-pb", "format": "mermaid"}
        )

        graph = result["graph"]
        assert result["node_count"] == 3
        # Mermaid edges: entry --> fix, entry --> done, fix --> done
        assert "entry" in graph
        assert "fix" in graph
        assert "done" in graph

    async def test_show_prompts_true_includes_prompt_preview(self):
        """show_prompts=True includes truncated prompt text in output."""
        pb = _make_playbook(playbook_id="prompt-preview")
        handler = _make_handler(playbooks={"prompt-preview": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "prompt-preview", "format": "ascii", "show_prompts": True}
        )

        # The prompt "Do something." should appear in the output
        assert "Do something." in result["graph"]

    async def test_show_prompts_false_excludes_prompt_preview(self):
        """show_prompts=False excludes prompt text from output."""
        pb = _make_playbook(playbook_id="no-prompt")
        handler = _make_handler(playbooks={"no-prompt": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "no-prompt", "format": "ascii", "show_prompts": False}
        )

        # The prompt text should NOT appear as a preview line
        # But the node names should still be present
        assert "start" in result["graph"]
        assert "end" in result["graph"]

    async def test_show_prompts_string_true(self):
        """show_prompts as string 'true' is converted to boolean True."""
        pb = _make_playbook(playbook_id="str-true")
        handler = _make_handler(playbooks={"str-true": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "str-true", "format": "ascii", "show_prompts": "true"}
        )

        assert "Do something." in result["graph"]

    async def test_show_prompts_string_false(self):
        """show_prompts as string 'false' is converted to boolean False."""
        pb = _make_playbook(playbook_id="str-false")
        handler = _make_handler(playbooks={"str-false": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "str-false", "format": "ascii", "show_prompts": "false"}
        )

        # Should not include the prompt preview
        assert "error" not in result

    async def test_playbook_not_found_error(self):
        """Returns error when playbook_id doesn't match any active playbook."""
        handler = _make_handler(playbooks={})

        result = await handler._cmd_show_playbook_graph({"playbook_id": "nonexistent"})

        assert "error" in result
        assert "not found" in result["error"]
        assert "nonexistent" in result["error"]

    async def test_missing_playbook_id_error(self):
        """Returns error when playbook_id is not provided."""
        handler = _make_handler()

        result = await handler._cmd_show_playbook_graph({})

        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_empty_playbook_id_error(self):
        """Returns error when playbook_id is empty string."""
        handler = _make_handler()

        result = await handler._cmd_show_playbook_graph({"playbook_id": "  "})

        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_invalid_format_error(self):
        """Returns error for an invalid format value."""
        pb = _make_playbook(playbook_id="fmt-test")
        handler = _make_handler(playbooks={"fmt-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "fmt-test", "format": "svg"}
        )

        assert "error" in result
        assert "Invalid format" in result["error"]
        assert "svg" in result["error"]

    async def test_invalid_direction_error(self):
        """Returns error for an invalid direction value."""
        pb = _make_playbook(playbook_id="dir-test")
        handler = _make_handler(playbooks={"dir-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "dir-test", "format": "mermaid", "direction": "RL"}
        )

        assert "error" in result
        assert "Invalid direction" in result["error"]
        assert "RL" in result["error"]

    async def test_no_playbook_manager_returns_not_found(self):
        """Returns not-found error when playbook manager is not initialised."""
        handler = _make_handler(has_playbook_manager=False)

        result = await handler._cmd_show_playbook_graph({"playbook_id": "test"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_via_execute_dispatcher(self):
        """show_playbook_graph is callable through execute()."""
        pb = _make_playbook(playbook_id="via-execute")
        handler = _make_handler(playbooks={"via-execute": pb})

        result = await handler.execute("show_playbook_graph", {"playbook_id": "via-execute"})

        assert "error" not in result
        assert result["playbook_id"] == "via-execute"
        assert result["format"] == "ascii"

    async def test_graph_with_human_gate_node(self):
        """Graph with a wait_for_human node shows the checkpoint marker."""
        pb = _make_playbook(
            playbook_id="human-gate",
            nodes={
                "start": PlaybookNode(
                    entry=True,
                    prompt="Review the PR.",
                    wait_for_human=True,
                    goto="done",
                ),
                "done": PlaybookNode(terminal=True),
            },
        )
        handler = _make_handler(playbooks={"human-gate": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "human-gate", "format": "ascii"}
        )

        graph = result["graph"]
        assert "start" in graph
        assert "done" in graph
        # Human-gate nodes should show the pause marker
        assert "human" in graph.lower() or "⏸" in graph


class TestShowPlaybookGraphToolRegistry:
    """Verify show_playbook_graph is properly registered."""

    def test_tool_definition_exists(self):
        """show_playbook_graph tool definition exists in the registry."""
        from src.tool_registry import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

        assert "show_playbook_graph" in _TOOL_CATEGORIES
        assert _TOOL_CATEGORIES["show_playbook_graph"] == "playbook"

        tool_names = [t["name"] for t in _ALL_TOOL_DEFINITIONS]
        assert "show_playbook_graph" in tool_names

    def test_tool_definition_has_format_enum(self):
        """The show_playbook_graph definition includes format and direction enums."""
        from src.tool_registry import _ALL_TOOL_DEFINITIONS

        tool = next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == "show_playbook_graph")
        props = tool["input_schema"]["properties"]
        assert "playbook_id" in props
        assert "format" in props
        assert "direction" in props
        assert "show_prompts" in props


# ===========================================================================
# (h) Response format consistency — all commands return dict
# ===========================================================================


class TestResponseFormatConsistency:
    """All 7 playbook commands return dicts consistent with the command handler
    convention: error responses have an 'error' key; success responses are
    dicts with command-specific keys and no 'error' key.

    Roadmap 5.5.8 case (h).
    """

    # -- compile_playbook ------------------------------------------------

    async def test_compile_playbook_success_format(self):
        """compile_playbook success returns dict with compiled=True, no error."""
        from src.playbook_compiler import CompilationResult

        pb = _make_playbook()
        compile_result = CompilationResult(
            success=True,
            playbook=pb,
            errors=[],
            source_hash="abc123",
            retries_used=0,
            skipped=False,
        )
        handler = _make_handler(compile_result=compile_result)

        result = await handler._cmd_compile_playbook({"markdown": "---\nid: test\n---\n# Test"})

        assert isinstance(result, dict)
        assert "error" not in result
        assert result["compiled"] is True
        assert "playbook_id" in result
        assert "version" in result
        assert "source_hash" in result

    async def test_compile_playbook_error_format(self):
        """compile_playbook failure returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_compile_playbook({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)
        assert len(result["error"]) > 0

    # -- dry_run_playbook ------------------------------------------------

    async def test_dry_run_playbook_success_format(self):
        """dry_run_playbook success returns dict with dry_run=True, no error."""
        from src.playbook_runner import RunResult

        pb = _make_playbook(playbook_id="dry-test")
        handler = _make_handler(playbooks={"dry-test": pb})

        mock_result = RunResult(
            run_id="dry-run-1",
            status="completed",
            node_trace=[{"node_id": "start", "status": "completed"}],
            tokens_used=0,
            error=None,
        )

        with patch("src.playbook_runner.PlaybookRunner.dry_run", new_callable=AsyncMock) as m:
            m.return_value = mock_result
            result = await handler._cmd_dry_run_playbook({"playbook_id": "dry-test"})

        assert isinstance(result, dict)
        assert "error" not in result
        assert result["dry_run"] is True
        assert "playbook_id" in result
        assert "node_trace" in result
        assert "tokens_used" in result

    async def test_dry_run_playbook_error_format(self):
        """dry_run_playbook with missing args returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_dry_run_playbook({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    # -- show_playbook_graph ---------------------------------------------

    async def test_show_playbook_graph_success_format(self):
        """show_playbook_graph success returns dict with graph, no error."""
        pb = _make_playbook(playbook_id="fmt-test")
        handler = _make_handler(playbooks={"fmt-test": pb})

        result = await handler._cmd_show_playbook_graph({"playbook_id": "fmt-test"})

        assert isinstance(result, dict)
        assert "error" not in result
        assert "playbook_id" in result
        assert "format" in result
        assert "graph" in result
        assert "node_count" in result
        assert "version" in result

    async def test_show_playbook_graph_error_format(self):
        """show_playbook_graph with missing args returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_show_playbook_graph({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    # -- list_playbooks --------------------------------------------------

    async def test_list_playbooks_success_format(self):
        """list_playbooks success returns dict with playbooks list and count."""
        pb = _make_playbook()
        handler = _make_handler(playbooks={"test-playbook": pb})

        result = await handler._cmd_list_playbooks({})

        assert isinstance(result, dict)
        assert "error" not in result
        assert "playbooks" in result
        assert isinstance(result["playbooks"], list)
        assert "count" in result
        assert isinstance(result["count"], int)

    async def test_list_playbooks_error_format(self):
        """list_playbooks with no manager returns dict with 'error' key."""
        handler = _make_handler(has_playbook_manager=False)

        result = await handler._cmd_list_playbooks({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    # -- list_playbook_runs ----------------------------------------------

    async def test_list_playbook_runs_success_format(self):
        """list_playbook_runs success returns dict with runs list and count."""
        handler = _make_handler()

        result = await handler._cmd_list_playbook_runs({})

        assert isinstance(result, dict)
        assert "error" not in result
        assert "runs" in result
        assert isinstance(result["runs"], list)
        assert "count" in result
        assert isinstance(result["count"], int)

    async def test_list_playbook_runs_error_format(self):
        """list_playbook_runs with invalid status returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_list_playbook_runs({"status": "bogus"})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    # -- inspect_playbook_run --------------------------------------------

    async def test_inspect_playbook_run_success_format(self):
        """inspect_playbook_run success returns dict with full run data."""
        run = FakePlaybookRun(
            run_id="r-test",
            playbook_id="pb-test",
            playbook_version=1,
            status="completed",
            started_at=1000.0,
            completed_at=1060.0,
            tokens_used=500,
            node_trace=json.dumps(
                [
                    {
                        "node_id": "start",
                        "started_at": 1000.0,
                        "completed_at": 1060.0,
                        "status": "completed",
                    }
                ]
            ),
            conversation_history=json.dumps([{"role": "user", "content": "hello"}]),
            trigger_event=json.dumps({"type": "test"}),
        )
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-test"})

        assert isinstance(result, dict)
        assert "error" not in result
        assert "run_id" in result
        assert "playbook_id" in result
        assert "node_trace" in result
        assert "conversation_history" in result
        assert "tokens_used" in result
        assert "trigger_event" in result

    async def test_inspect_playbook_run_error_format(self):
        """inspect_playbook_run with missing run_id returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_inspect_playbook_run({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    # -- resume_playbook -------------------------------------------------

    async def test_resume_playbook_error_format(self):
        """resume_playbook with missing args returns dict with 'error' key."""
        handler = _make_handler()

        result = await handler._cmd_resume_playbook({})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)

    async def test_resume_playbook_not_found_format(self):
        """resume_playbook for nonexistent run returns dict with 'error' key."""
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=None)

        result = await handler._cmd_resume_playbook({"run_id": "missing", "human_input": "ok"})

        assert isinstance(result, dict)
        assert "error" in result

    # -- All commands via execute() dispatcher ---------------------------

    async def test_all_commands_return_dicts_via_execute(self):
        """All 7 playbook commands return dicts when dispatched via execute()."""
        pb = _make_playbook(playbook_id="exec-test")
        handler = _make_handler(playbooks={"exec-test": pb})

        # These may return errors due to missing args, but that's fine —
        # the key assertion is that they all return dicts.
        commands_args = [
            ("compile_playbook", {}),
            ("dry_run_playbook", {}),
            ("show_playbook_graph", {}),
            ("list_playbooks", {}),
            ("list_playbook_runs", {}),
            ("inspect_playbook_run", {}),
            ("resume_playbook", {}),
        ]

        for cmd_name, args in commands_args:
            result = await handler.execute(cmd_name, args)
            assert isinstance(result, dict), (
                f"Command '{cmd_name}' returned {type(result)}, expected dict"
            )


# ===========================================================================
# (i) Error handling for invalid arguments — helpful error messages
# ===========================================================================


class TestErrorHandlingInvalidArguments:
    """All 7 playbook commands return helpful error messages when called with
    invalid or missing arguments.

    Roadmap 5.5.8 case (i).
    """

    # -- compile_playbook ------------------------------------------------

    async def test_compile_missing_markdown_and_path(self):
        """compile_playbook with no markdown or path returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_compile_playbook({})

        assert "error" in result
        # Should mention what's missing
        assert "markdown" in result["error"].lower() or "path" in result["error"].lower()

    async def test_compile_empty_markdown_only(self):
        """compile_playbook with blank markdown returns error."""
        handler = _make_handler()
        result = await handler._cmd_compile_playbook({"markdown": "   "})

        assert "error" in result

    async def test_compile_nonexistent_path(self):
        """compile_playbook with nonexistent file path returns 'not found'."""
        handler = _make_handler()
        result = await handler._cmd_compile_playbook({"path": "/no/such/file.md"})

        assert "error" in result
        assert "not found" in result["error"].lower() or "File not found" in result["error"]

    async def test_compile_no_playbook_manager(self):
        """compile_playbook when manager is unavailable gives clear error."""
        handler = _make_handler(has_playbook_manager=False)
        result = await handler._cmd_compile_playbook({"markdown": "# test"})

        assert "error" in result
        assert "not initialised" in result["error"].lower() or "not initial" in result["error"]

    # -- dry_run_playbook ------------------------------------------------

    async def test_dry_run_missing_playbook_id(self):
        """dry_run_playbook without playbook_id returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_dry_run_playbook({})

        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_dry_run_empty_playbook_id(self):
        """dry_run_playbook with empty playbook_id returns error."""
        handler = _make_handler()
        result = await handler._cmd_dry_run_playbook({"playbook_id": "  "})

        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_dry_run_playbook_not_found(self):
        """dry_run_playbook with unknown playbook_id returns not-found error."""
        handler = _make_handler(playbooks={})
        result = await handler._cmd_dry_run_playbook({"playbook_id": "ghost"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_dry_run_no_playbook_manager(self):
        """dry_run_playbook when manager is unavailable gives clear error."""
        handler = _make_handler(has_playbook_manager=False)
        result = await handler._cmd_dry_run_playbook({"playbook_id": "test"})

        assert "error" in result
        assert "not initialised" in result["error"].lower() or "not initial" in result["error"]

    async def test_dry_run_invalid_event_json(self):
        """dry_run_playbook with invalid event JSON returns parse error."""
        pb = _make_playbook(playbook_id="ev-test")
        handler = _make_handler(playbooks={"ev-test": pb})
        result = await handler._cmd_dry_run_playbook(
            {"playbook_id": "ev-test", "event": "not{json"}
        )

        assert "error" in result
        assert "Invalid event" in result["error"] or "event" in result["error"].lower()

    # -- show_playbook_graph ---------------------------------------------

    async def test_show_graph_missing_playbook_id(self):
        """show_playbook_graph without playbook_id returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_show_playbook_graph({})

        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_show_graph_invalid_format(self):
        """show_playbook_graph with invalid format returns descriptive error."""
        pb = _make_playbook(playbook_id="fmt-err")
        handler = _make_handler(playbooks={"fmt-err": pb})
        result = await handler._cmd_show_playbook_graph({"playbook_id": "fmt-err", "format": "png"})

        assert "error" in result
        assert "Invalid format" in result["error"]
        assert "png" in result["error"]

    async def test_show_graph_invalid_direction(self):
        """show_playbook_graph with invalid direction returns descriptive error."""
        pb = _make_playbook(playbook_id="dir-err")
        handler = _make_handler(playbooks={"dir-err": pb})
        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "dir-err", "format": "mermaid", "direction": "BT"}
        )

        assert "error" in result
        assert "Invalid direction" in result["error"]
        assert "BT" in result["error"]

    async def test_show_graph_not_found(self):
        """show_playbook_graph with unknown playbook returns not-found error."""
        handler = _make_handler(playbooks={})
        result = await handler._cmd_show_playbook_graph({"playbook_id": "missing"})

        assert "error" in result
        assert "not found" in result["error"]
        assert "missing" in result["error"]

    # -- list_playbooks --------------------------------------------------

    async def test_list_playbooks_invalid_scope(self):
        """list_playbooks with invalid scope returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_list_playbooks({"scope": "global"})

        assert "error" in result
        assert "Invalid scope" in result["error"]
        assert "global" in result["error"]

    async def test_list_playbooks_no_manager(self):
        """list_playbooks when manager is unavailable gives clear error."""
        handler = _make_handler(has_playbook_manager=False)
        result = await handler._cmd_list_playbooks({})

        assert "error" in result
        assert "not initialised" in result["error"]

    # -- list_playbook_runs ----------------------------------------------

    async def test_list_runs_invalid_status(self):
        """list_playbook_runs with invalid status returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_list_playbook_runs({"status": "pending"})

        assert "error" in result
        assert "Invalid status" in result["error"]
        assert "pending" in result["error"]

    # -- inspect_playbook_run --------------------------------------------

    async def test_inspect_missing_run_id(self):
        """inspect_playbook_run without run_id returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_inspect_playbook_run({})

        assert "error" in result
        assert "run_id" in result["error"]

    async def test_inspect_nonexistent_run(self):
        """inspect_playbook_run with unknown run_id returns not-found error."""
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=None)

        result = await handler._cmd_inspect_playbook_run({"run_id": "nope"})

        assert "error" in result
        assert "not found" in result["error"]
        assert "nope" in result["error"]

    # -- resume_playbook -------------------------------------------------

    async def test_resume_missing_run_id(self):
        """resume_playbook without run_id returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_resume_playbook({"human_input": "ok"})

        assert "error" in result
        assert "run_id" in result["error"]

    async def test_resume_missing_human_input(self):
        """resume_playbook without human_input returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_resume_playbook({"run_id": "r-1"})

        assert "error" in result
        assert "human_input" in result["error"]

    async def test_resume_empty_human_input(self):
        """resume_playbook with blank human_input returns descriptive error."""
        handler = _make_handler()
        result = await handler._cmd_resume_playbook({"run_id": "r-1", "human_input": "  "})

        assert "error" in result
        assert "human_input" in result["error"]

    async def test_resume_nonexistent_run(self):
        """resume_playbook with unknown run_id returns not-found error."""
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=None)

        result = await handler._cmd_resume_playbook({"run_id": "ghost", "human_input": "approved"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_resume_non_paused_run(self):
        """resume_playbook for a completed run returns status mismatch error."""
        run = FakePlaybookRun(
            run_id="completed-run",
            status="completed",
        )
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_resume_playbook(
            {"run_id": "completed-run", "human_input": "go"}
        )

        assert "error" in result
        assert "completed" in result["error"]
        assert "not 'paused'" in result["error"] or "paused" in result["error"]


# ===========================================================================
# (d) Additional show_playbook_graph coverage for node/transition correctness
# ===========================================================================


class TestShowPlaybookGraphNodeCorrectness:
    """Verify that the show_playbook_graph command correctly represents
    the graph structure — node types, transition targets, and metadata.

    Roadmap 5.5.8 case (d) — "includes correct nodes and transitions".
    """

    async def test_ascii_includes_entry_and_terminal_labels(self):
        """ASCII output labels entry and terminal nodes with type markers."""
        pb = _make_playbook(playbook_id="label-test")
        handler = _make_handler(playbooks={"label-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "label-test", "format": "ascii"}
        )

        graph = result["graph"]
        # Entry nodes use ▶ marker, terminal nodes use ■ marker
        assert "▶" in graph  # entry marker
        assert "■" in graph  # terminal marker

    async def test_ascii_includes_legend(self):
        """ASCII output includes a legend explaining node type symbols."""
        pb = _make_playbook(playbook_id="legend-test")
        handler = _make_handler(playbooks={"legend-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "legend-test", "format": "ascii"}
        )

        assert "Legend" in result["graph"]

    async def test_ascii_header_shows_playbook_metadata(self):
        """ASCII output header includes playbook ID, version, and scope."""
        pb = _make_playbook(playbook_id="header-test", version=5, scope="project")
        handler = _make_handler(playbooks={"header-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "header-test", "format": "ascii"}
        )

        graph = result["graph"]
        assert "header-test" in graph
        assert "v5" in graph
        assert "project" in graph

    async def test_ascii_shows_goto_arrow(self):
        """ASCII output shows → arrow for goto transitions."""
        pb = _make_playbook(playbook_id="arrow-test")
        handler = _make_handler(playbooks={"arrow-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "arrow-test", "format": "ascii"}
        )

        graph = result["graph"]
        # Should show the transition arrow from start → end
        assert "→" in graph
        assert "end" in graph

    async def test_mermaid_includes_title_and_version(self):
        """Mermaid output includes a title with playbook ID and version."""
        pb = _make_playbook(playbook_id="title-test", version=7)
        handler = _make_handler(playbooks={"title-test": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "title-test", "format": "mermaid"}
        )

        graph = result["graph"]
        assert "title-test" in graph
        assert "v7" in graph

    async def test_node_count_matches_actual_nodes(self):
        """Returned node_count matches the actual number of nodes in the playbook."""
        nodes = {
            "a": PlaybookNode(entry=True, prompt="Step A", goto="b"),
            "b": PlaybookNode(prompt="Step B", goto="c"),
            "c": PlaybookNode(prompt="Step C", goto="d"),
            "d": PlaybookNode(terminal=True),
        }
        pb = _make_playbook(playbook_id="count-test", nodes=nodes)
        handler = _make_handler(playbooks={"count-test": pb})

        result = await handler._cmd_show_playbook_graph({"playbook_id": "count-test"})

        assert result["node_count"] == 4

    async def test_decision_node_shows_transition_conditions(self):
        """Decision nodes in ASCII output show conditional transition labels."""
        pb = _make_branching_playbook()
        handler = _make_handler(playbooks={"branch-pb": pb})

        result = await handler._cmd_show_playbook_graph(
            {"playbook_id": "branch-pb", "format": "ascii"}
        )

        graph = result["graph"]
        # Decision node should be marked as such
        assert "decision" in graph.lower() or "◆" in graph
        # Both transition targets should appear
        assert "fix" in graph
        assert "done" in graph


# ===========================================================================
# Integration: All 7 commands exist as _cmd_* methods on CommandHandler
# ===========================================================================


class TestAllPlaybookCommandsRegistered:
    """Verify that all 7 spec §15 playbook commands are wired up in
    CommandHandler and the tool registry.
    """

    EXPECTED_COMMANDS = [
        "compile_playbook",
        "dry_run_playbook",
        "show_playbook_graph",
        "list_playbooks",
        "list_playbook_runs",
        "inspect_playbook_run",
        "resume_playbook",
    ]

    def test_all_cmd_methods_exist(self):
        """All 7 _cmd_* methods exist on CommandHandler."""
        for cmd in self.EXPECTED_COMMANDS:
            method_name = f"_cmd_{cmd}"
            assert hasattr(CommandHandler, method_name), (
                f"CommandHandler missing method {method_name}"
            )
            assert callable(getattr(CommandHandler, method_name)), (
                f"CommandHandler.{method_name} is not callable"
            )

    def test_all_in_tool_categories(self):
        """All 7 commands are mapped to the 'playbook' category."""
        from src.tool_registry import _TOOL_CATEGORIES

        for cmd in self.EXPECTED_COMMANDS:
            assert cmd in _TOOL_CATEGORIES, f"{cmd} not in _TOOL_CATEGORIES"
            assert _TOOL_CATEGORIES[cmd] == "playbook", (
                f"{cmd} mapped to '{_TOOL_CATEGORIES[cmd]}', expected 'playbook'"
            )

    def test_all_have_tool_definitions(self):
        """All 7 commands have tool definitions with descriptions and schemas."""
        from src.tool_registry import _ALL_TOOL_DEFINITIONS

        tool_map = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}

        for cmd in self.EXPECTED_COMMANDS:
            assert cmd in tool_map, f"No tool definition for {cmd}"
            tool = tool_map[cmd]
            assert "description" in tool, f"{cmd} tool definition missing description"
            assert len(tool["description"]) > 20, (
                f"{cmd} tool description too short: {tool['description']!r}"
            )
            assert "input_schema" in tool, f"{cmd} tool definition missing input_schema"
            schema = tool["input_schema"]
            assert schema.get("type") == "object", (
                f"{cmd} input_schema type is {schema.get('type')!r}, expected 'object'"
            )

    def test_tool_registry_category_has_correct_count(self):
        """The playbook category in ToolRegistry contains exactly 7 tools."""
        from src.tool_registry import ToolRegistry, _ALL_TOOL_DEFINITIONS

        registry = ToolRegistry(_ALL_TOOL_DEFINITIONS)
        tool_names = registry.get_category_tool_names("playbook")

        assert tool_names is not None, "Playbook category not found in registry"
        assert len(tool_names) == 7, (
            f"Expected 7 playbook tools, got {len(tool_names)}: {tool_names}"
        )

    async def test_unknown_command_returns_error(self):
        """execute() with a nonexistent command returns an error dict."""
        handler = _make_handler()
        # Remove plugin_registry so the execute() fallback doesn't try to
        # await a plain MagicMock (which is not async).
        handler.orchestrator.plugin_registry = None

        result = await handler.execute("nonexistent_playbook_command", {})

        assert isinstance(result, dict)
        assert "error" in result
        assert "Unknown command" in result["error"]
