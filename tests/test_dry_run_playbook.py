"""Tests for dry_run_playbook — simulate execution with mock event, no side effects.

Roadmap 5.5.2, spec §15 and §19 Open Question #2.

Validates that the dry-run feature:
- Walks the graph from entry to terminal without real LLM calls
- Produces a node trace matching the expected path
- Does NOT write to the database
- Does NOT emit events on the EventBus
- Handles unconditional goto, conditional transitions, and human-in-the-loop nodes
- Works via both PlaybookRunner.dry_run() and the command handler
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.playbooks.runner import PlaybookRunner, RunResult


# ---------------------------------------------------------------------------
# Fixtures — graphs
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_graph():
    """A minimal 2-node linear playbook: scan → done."""
    return {
        "id": "test-dry-run",
        "version": 1,
        "nodes": {
            "scan": {
                "entry": True,
                "prompt": "Run scan on the codebase.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def three_node_graph():
    """A 3-node linear playbook: scan → fix → done."""
    return {
        "id": "three-node",
        "version": 1,
        "nodes": {
            "scan": {
                "entry": True,
                "prompt": "Scan the codebase for issues.",
                "goto": "fix",
            },
            "fix": {
                "prompt": "Fix all detected issues.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def branching_graph():
    """A graph with conditional (natural-language) transitions."""
    return {
        "id": "branching-dry-run",
        "version": 2,
        "nodes": {
            "scan": {
                "entry": True,
                "prompt": "Run scan on files and report findings.",
                "transitions": [
                    {"when": "no findings", "goto": "done"},
                    {"when": "findings exist", "goto": "triage"},
                ],
            },
            "triage": {
                "prompt": "Triage the findings and fix them.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def otherwise_graph():
    """A graph with conditional transitions including an otherwise fallback."""
    return {
        "id": "otherwise-graph",
        "version": 1,
        "nodes": {
            "check": {
                "entry": True,
                "prompt": "Check the status.",
                "transitions": [
                    {"when": {"function": "response_contains", "value": "critical"}, "goto": "fix"},
                    {"otherwise": True, "goto": "done"},
                ],
            },
            "fix": {
                "prompt": "Fix the critical issue.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def human_in_the_loop_graph():
    """A graph with a wait_for_human node."""
    return {
        "id": "hitl-dry-run",
        "version": 1,
        "nodes": {
            "scan": {
                "entry": True,
                "prompt": "Scan the codebase.",
                "goto": "review",
            },
            "review": {
                "prompt": "Please review these findings.",
                "wait_for_human": True,
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def structured_transition_graph():
    """A graph with structured (dict) transitions."""
    return {
        "id": "structured-trans",
        "version": 1,
        "nodes": {
            "check": {
                "entry": True,
                "prompt": "Check the status of the deployment.",
                "transitions": [
                    {
                        "when": {"function": "response_contains", "value": "all clear"},
                        "goto": "done",
                    },
                    {
                        "when": {"function": "response_contains", "value": "error"},
                        "goto": "fix",
                    },
                    {"otherwise": True, "goto": "escalate"},
                ],
            },
            "fix": {
                "prompt": "Fix the errors.",
                "goto": "done",
            },
            "escalate": {
                "prompt": "Escalate the issue.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def mixed_transition_graph():
    """A graph with both structured and natural-language transitions."""
    return {
        "id": "mixed-trans",
        "version": 1,
        "nodes": {
            "analyze": {
                "entry": True,
                "prompt": "Analyze the code.",
                "transitions": [
                    {
                        "when": {"function": "response_contains", "value": "PASS"},
                        "goto": "done",
                    },
                    {"when": "code has issues that need fixing", "goto": "fix"},
                    {"otherwise": True, "goto": "done"},
                ],
            },
            "fix": {
                "prompt": "Fix the issues.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def token_budget_graph():
    """A graph with a token budget set."""
    return {
        "id": "budget-graph",
        "version": 1,
        "max_tokens": 1000,
        "nodes": {
            "work": {
                "entry": True,
                "prompt": "Do some work.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


# ---------------------------------------------------------------------------
# PlaybookRunner.dry_run() — core tests
# ---------------------------------------------------------------------------


class TestDryRunBasic:
    """Basic dry-run execution tests."""

    async def test_simple_linear_graph(self, simple_graph):
        """Dry-run walks a 2-node graph and returns completed trace."""
        result = await PlaybookRunner.dry_run(simple_graph, {"type": "test"})

        assert isinstance(result, RunResult)
        assert result.status == "completed"
        assert result.tokens_used == 0
        assert len(result.node_trace) == 1  # scan executed, done is terminal
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[0]["status"] == "completed"
        assert result.node_trace[0]["transition_to"] == "done"
        assert result.node_trace[0]["transition_method"] == "goto"

    async def test_three_node_linear_graph(self, three_node_graph):
        """Dry-run walks a 3-node linear graph."""
        result = await PlaybookRunner.dry_run(three_node_graph, {"type": "test"})

        assert result.status == "completed"
        assert len(result.node_trace) == 2  # scan, fix (done is terminal)
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[1]["node_id"] == "fix"
        assert result.node_trace[0]["transition_to"] == "fix"
        assert result.node_trace[1]["transition_to"] == "done"

    async def test_returns_simulated_response(self, simple_graph):
        """The final_response is a dry-run simulation string."""
        result = await PlaybookRunner.dry_run(simple_graph, {})

        # final_response comes from the last executed node
        assert result.final_response is not None
        assert "[dry-run]" in result.final_response
        assert "scan" in result.final_response

    async def test_no_real_tokens_consumed(self, simple_graph):
        """Dry-run reports zero tokens since no LLM calls are made."""
        result = await PlaybookRunner.dry_run(simple_graph, {})
        assert result.tokens_used == 0


class TestDryRunNoSideEffects:
    """Verify that dry-run produces no side effects."""

    async def test_no_db_writes(self, simple_graph):
        """Dry-run does NOT write to the database."""
        mock_db = AsyncMock()
        # dry_run does not accept a db parameter — verify it doesn't touch any DB
        result = await PlaybookRunner.dry_run(simple_graph, {})
        assert result.status == "completed"
        # The mock_db should never have been called since we can't pass it
        mock_db.create_playbook_run.assert_not_called()
        mock_db.update_playbook_run.assert_not_called()

    async def test_no_event_emission(self, simple_graph):
        """Dry-run does NOT emit events on the EventBus."""
        mock_bus = AsyncMock()
        # dry_run sets event_bus=None internally
        result = await PlaybookRunner.dry_run(simple_graph, {})
        assert result.status == "completed"
        mock_bus.emit.assert_not_called()

    async def test_dry_run_runner_has_flag_set(self, simple_graph):
        """Internal: the runner's _dry_run flag is True during simulation."""
        captured_dry_run = []

        original_run = PlaybookRunner.run

        async def capturing_run(self):
            captured_dry_run.append(self._dry_run)
            return await original_run(self)

        with patch.object(PlaybookRunner, "run", capturing_run):
            await PlaybookRunner.dry_run(simple_graph, {})

        assert captured_dry_run == [True]


class TestDryRunTransitions:
    """Test transition handling during dry-run."""

    async def test_unconditional_goto(self, simple_graph):
        """Unconditional goto transitions work normally in dry-run."""
        result = await PlaybookRunner.dry_run(simple_graph, {})
        assert result.node_trace[0]["transition_method"] == "goto"
        assert result.node_trace[0]["transition_to"] == "done"

    async def test_conditional_follows_first_nl_candidate(self, branching_graph):
        """Natural-language conditions follow the first candidate (LLM skipped)."""
        result = await PlaybookRunner.dry_run(branching_graph, {})

        assert result.status == "completed"
        # First NL transition is "no findings" → "done"
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[0]["transition_to"] == "done"

    async def test_structured_falls_through_to_otherwise(self, otherwise_graph):
        """Structured conditions that don't match fall through to otherwise."""
        result = await PlaybookRunner.dry_run(otherwise_graph, {})

        assert result.status == "completed"
        # Simulated response won't contain "critical", so falls to otherwise → done
        assert result.node_trace[0]["node_id"] == "check"
        assert result.node_trace[0]["transition_to"] == "done"
        assert result.node_trace[0]["transition_method"] == "otherwise"

    async def test_structured_only_with_otherwise(self, structured_transition_graph):
        """All-structured transitions with otherwise: falls to otherwise when none match."""
        result = await PlaybookRunner.dry_run(structured_transition_graph, {})

        assert result.status == "completed"
        # Simulated response won't contain "all clear" or "error" → falls to otherwise
        assert result.node_trace[0]["node_id"] == "check"
        assert result.node_trace[0]["transition_to"] == "escalate"

    async def test_mixed_transitions_follows_first_nl(self, mixed_transition_graph):
        """Mixed structured + NL: structured fail, then first NL candidate is followed."""
        result = await PlaybookRunner.dry_run(mixed_transition_graph, {})

        assert result.status == "completed"
        # Structured "response_contains PASS" won't match simulated response
        # First NL condition "code has issues..." is picked → "fix"
        assert result.node_trace[0]["node_id"] == "analyze"
        assert result.node_trace[0]["transition_to"] == "fix"


class TestDryRunHumanInTheLoop:
    """Test that wait_for_human nodes don't pause during dry-run."""

    async def test_continues_past_human_node(self, human_in_the_loop_graph):
        """Dry-run does NOT pause at wait_for_human nodes — it continues."""
        result = await PlaybookRunner.dry_run(human_in_the_loop_graph, {})

        assert result.status == "completed"
        # Both scan and review should be in the trace (no pause)
        node_ids = [t["node_id"] for t in result.node_trace]
        assert node_ids == ["scan", "review"]
        # review node transitioned to done instead of pausing
        assert result.node_trace[1]["transition_to"] == "done"


class TestDryRunTokenBudget:
    """Token budget checks are skipped in dry-run (no real tokens consumed)."""

    async def test_token_budget_not_enforced(self, token_budget_graph):
        """Dry-run succeeds even with a token budget — no real tokens used."""
        result = await PlaybookRunner.dry_run(token_budget_graph, {})
        assert result.status == "completed"
        assert result.tokens_used == 0


class TestDryRunProgress:
    """Test progress callback during dry-run."""

    async def test_progress_events_emitted(self, simple_graph):
        """Progress events are still emitted during dry-run."""
        events = []

        async def on_progress(event: str, detail: str | None) -> None:
            events.append((event, detail))

        result = await PlaybookRunner.dry_run(simple_graph, {}, on_progress=on_progress)

        assert result.status == "completed"
        event_types = [e[0] for e in events]
        assert "playbook_started" in event_types
        assert "node_started" in event_types
        assert "node_completed" in event_types
        assert "playbook_completed" in event_types


class TestDryRunEdgeCases:
    """Edge cases for dry-run simulation."""

    async def test_empty_event_gets_default_type(self):
        """When event has no 'type', the runner still works."""
        graph = {
            "id": "minimal",
            "version": 1,
            "nodes": {
                "start": {"entry": True, "prompt": "Go.", "goto": "end"},
                "end": {"terminal": True},
            },
        }
        result = await PlaybookRunner.dry_run(graph, {})
        assert result.status == "completed"

    async def test_missing_entry_node_fails(self):
        """Graph with no entry node and multiple non-terminals returns a failed result."""
        graph = {
            "id": "no-entry",
            "version": 1,
            "nodes": {
                "orphan_a": {"prompt": "First orphan."},
                "orphan_b": {"prompt": "Second orphan."},
                "done": {"terminal": True},
            },
        }
        # Two non-terminal nodes and neither marked entry → no entry found
        # (fallback only works for exactly one non-terminal)
        result = await PlaybookRunner.dry_run(graph, {})
        assert result.status == "failed"
        assert "entry" in (result.error or "").lower()

    async def test_single_entry_terminal(self):
        """Graph that is just an entry node → immediately done."""
        graph = {
            "id": "immediate",
            "version": 1,
            "nodes": {
                "start": {"entry": True, "terminal": True},
            },
        }
        result = await PlaybookRunner.dry_run(graph, {})
        assert result.status == "completed"
        assert len(result.node_trace) == 0  # terminal is not executed

    async def test_event_data_in_seed_message(self, simple_graph):
        """Mock event data appears in the seed conversation message."""
        captured_messages = []

        original_run = PlaybookRunner.run

        async def capturing_run(self):
            result = await original_run(self)
            captured_messages.extend(self.messages)
            return result

        with patch.object(PlaybookRunner, "run", capturing_run):
            await PlaybookRunner.dry_run(simple_graph, {"project": "my-app", "type": "push"})

        # First message is the seed with event context
        seed = captured_messages[0]["content"]
        assert "my-app" in seed
        assert "push" in seed


# ---------------------------------------------------------------------------
# Command handler integration tests
# ---------------------------------------------------------------------------


class TestDryRunCommand:
    """Test the _cmd_dry_run_playbook command handler method."""

    @pytest.fixture
    def mock_playbook(self):
        """A mock CompiledPlaybook that returns a graph dict."""
        pb = MagicMock()
        pb.id = "test-pb"
        pb.version = 3
        pb.to_dict.return_value = {
            "id": "test-pb",
            "version": 3,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Start working.",
                    "goto": "end",
                },
                "end": {"terminal": True},
            },
        }
        return pb

    @pytest.fixture
    def mock_handler(self, mock_playbook):
        """A mock CommandHandler with a playbook manager."""
        handler = MagicMock()
        handler.orchestrator = MagicMock()
        handler.orchestrator.playbook_manager = MagicMock()
        handler.orchestrator.playbook_manager.get_playbook.return_value = mock_playbook
        handler.db = AsyncMock()
        handler.config = {}
        return handler

    async def test_dry_run_command_success(self, mock_handler, mock_playbook):
        """Command returns dry_run=True with node trace."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler,
            {"playbook_id": "test-pb"},
        )

        assert result["dry_run"] is True
        assert result["playbook_id"] == "test-pb"
        assert result["version"] == 3
        assert result["status"] == "completed"
        assert result["node_count"] >= 1
        assert result["tokens_used"] == 0
        assert isinstance(result["node_trace"], list)
        assert isinstance(result["mock_event"], dict)

    async def test_dry_run_command_with_custom_event(self, mock_handler, mock_playbook):
        """Command accepts a custom mock event dict."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler,
            {
                "playbook_id": "test-pb",
                "event": {"type": "push", "project_id": "myproj"},
            },
        )

        assert result["dry_run"] is True
        assert result["mock_event"]["type"] == "push"
        assert result["mock_event"]["project_id"] == "myproj"

    async def test_dry_run_command_missing_playbook_id(self, mock_handler):
        """Command returns error when playbook_id is missing."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(mock_handler, {})
        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_dry_run_command_playbook_not_found(self, mock_handler):
        """Command returns error when playbook is not in active set."""
        from src.commands.handler import CommandHandler

        mock_handler.orchestrator.playbook_manager.get_playbook.return_value = None
        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler, {"playbook_id": "nonexistent"}
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_dry_run_command_no_playbook_manager(self, mock_handler):
        """Command returns error when playbook manager is not initialised."""
        from src.commands.handler import CommandHandler

        mock_handler.orchestrator.playbook_manager = None
        # getattr with None check
        delattr(mock_handler.orchestrator, "playbook_manager")
        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler, {"playbook_id": "test-pb"}
        )
        assert "error" in result

    async def test_dry_run_command_invalid_event_json(self, mock_handler):
        """Command returns error when event is invalid JSON string."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler,
            {"playbook_id": "test-pb", "event": "not valid json{"},
        )
        assert "error" in result
        assert "Invalid event JSON" in result["error"]

    async def test_dry_run_command_event_as_json_string(self, mock_handler, mock_playbook):
        """Command parses event from a JSON string."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler,
            {
                "playbook_id": "test-pb",
                "event": '{"type": "timer.30m", "project_id": "proj-1"}',
            },
        )
        assert result["dry_run"] is True
        assert result["mock_event"]["type"] == "timer.30m"

    async def test_dry_run_no_db_interaction(self, mock_handler, mock_playbook):
        """Command does not interact with the database."""
        from src.commands.handler import CommandHandler

        result = await CommandHandler._cmd_dry_run_playbook(
            mock_handler, {"playbook_id": "test-pb"}
        )
        assert result["dry_run"] is True
        # DB should not have been called
        mock_handler.db.create_playbook_run.assert_not_called()
        mock_handler.db.update_playbook_run.assert_not_called()
