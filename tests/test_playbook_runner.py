"""Tests for PlaybookRunner — graph walker with conversation history.

Tests the core execution model from docs/specs/design/playbooks.md §6:
- Linear graph walking through nodes
- Conversation history accumulation across nodes
- Unconditional ``goto`` transitions
- Conditional transitions via LLM classification
- Token budget enforcement
- Context summarization (``summarize_before``)
- Human-in-the-loop pause and resume
- Per-node LLM config overrides
- Error handling (missing nodes, failed LLM calls)
- DB persistence of run state
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.models import PlaybookRun
from src.playbook_runner import (
    NodeTraceEntry,
    PlaybookRunner,
    _estimate_tokens,
)


# ---------------------------------------------------------------------------
# Fixtures — mock Supervisor and DB
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supervisor():
    """A mock Supervisor with a controllable chat() return value."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary of prior steps.")
    return supervisor


@pytest.fixture
def mock_db():
    """A mock database backend for PlaybookRun persistence."""
    db = AsyncMock()
    db.create_playbook_run = AsyncMock()
    db.update_playbook_run = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    return db


@pytest.fixture
def simple_graph():
    """A minimal 2-node linear playbook: scan → done."""
    return {
        "id": "test-playbook",
        "version": 1,
        "nodes": {
            "scan": {
                "entry": True,
                "prompt": "Run scan on files.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def branching_graph():
    """A 4-node graph with conditional transitions."""
    return {
        "id": "branching-playbook",
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
                "prompt": "Group findings by severity.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def human_review_graph():
    """A graph with a wait_for_human node."""
    return {
        "id": "human-review-playbook",
        "version": 1,
        "nodes": {
            "analyse": {
                "entry": True,
                "prompt": "Analyse the issue and propose a plan.",
                "goto": "review",
            },
            "review": {
                "prompt": "Present your analysis for human review.",
                "wait_for_human": True,
                "transitions": [
                    {"when": "approved", "goto": "execute"},
                    {"when": "rejected", "goto": "done"},
                ],
            },
            "execute": {
                "prompt": "Execute the approved plan.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def event_data():
    """Sample trigger event."""
    return {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_basic_estimate(self):
        assert _estimate_tokens("hello world") == 2  # 11 chars / 4 ≈ 2

    def test_empty_string(self):
        assert _estimate_tokens("") == 1  # min 1

    def test_multiple_texts(self):
        result = _estimate_tokens("hello", "world")
        assert result == 2  # 10 chars / 4 ≈ 2

    def test_none_text_ignored(self):
        result = _estimate_tokens("hello", None, "world")
        assert result == 2


# ---------------------------------------------------------------------------
# Simple linear execution
# ---------------------------------------------------------------------------


class TestLinearExecution:
    """Test basic graph walking: entry → action → terminal."""

    async def test_simple_two_node_graph(self, mock_supervisor, simple_graph, event_data):
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.run_id == runner.run_id
        assert len(result.node_trace) == 1  # Only "scan" is executed (done is terminal)
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[0]["status"] == "completed"

    async def test_supervisor_called_with_node_prompt(
        self, mock_supervisor, simple_graph, event_data
    ):
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        await runner.run()

        # Supervisor should be called once for the "scan" node
        mock_supervisor.chat.assert_called_once()
        call_kwargs = mock_supervisor.chat.call_args
        assert call_kwargs.kwargs["text"] == "Run scan on files."
        assert call_kwargs.kwargs["user_name"] == "playbook-runner"

    async def test_conversation_history_accumulates(
        self, mock_supervisor, simple_graph, event_data
    ):
        mock_supervisor.chat.return_value = "Scan complete, no issues found."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        await runner.run()

        # Should have: seed message + scan prompt + scan response = 3 messages
        assert len(runner.messages) == 3
        assert runner.messages[0]["role"] == "user"  # Seed
        assert "Event received" in runner.messages[0]["content"]
        assert runner.messages[1]["role"] == "user"  # Node prompt
        assert runner.messages[1]["content"] == "Run scan on files."
        assert runner.messages[2]["role"] == "assistant"  # Response
        assert runner.messages[2]["content"] == "Scan complete, no issues found."

    async def test_tokens_tracked(self, mock_supervisor, simple_graph, event_data):
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.tokens_used > 0
        assert runner.tokens_used == result.tokens_used

    async def test_three_node_linear(self, mock_supervisor, event_data):
        """Test a linear chain: a → b → c → done."""
        graph = {
            "id": "three-step",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "c"},
                "c": {"prompt": "Step C", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Result A", "Result B", "Result C"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 3
        assert [t["node_id"] for t in result.node_trace] == ["a", "b", "c"]
        assert mock_supervisor.chat.call_count == 3

    async def test_history_passed_to_supervisor(self, mock_supervisor, event_data):
        """Each node call should receive the accumulated history."""
        graph = {
            "id": "history-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Second call (node B) should receive history including node A's exchange
        calls = mock_supervisor.chat.call_args_list
        assert len(calls) == 2

        # First call gets seed message as history
        first_history = calls[0].kwargs["history"]
        assert len(first_history) == 1  # Just the seed

        # Second call gets seed + node A prompt + node A response
        second_history = calls[1].kwargs["history"]
        assert len(second_history) == 3


# ---------------------------------------------------------------------------
# Conditional transitions
# ---------------------------------------------------------------------------


class TestConditionalTransitions:
    """Test branching paths via LLM transition evaluation."""

    async def test_goto_branch(self, mock_supervisor, branching_graph, event_data):
        """When the LLM picks 'findings exist', it should go to triage."""
        # First call: scan node → "I found 3 issues"
        # Second call: transition classification → "2" (findings exist → triage)
        # Third call: triage node → "Grouped by severity"
        responses = iter(["I found 3 issues", "2", "Grouped by severity"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 2  # scan + triage
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[1]["node_id"] == "triage"

    async def test_transition_to_terminal(self, mock_supervisor, branching_graph, event_data):
        """When LLM picks 'no findings', it should go directly to done."""
        responses = iter(["No issues found", "1"])  # 1 = "no findings" → done
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 1  # Only scan
        assert result.node_trace[0]["node_id"] == "scan"

    async def test_otherwise_fallback(self, mock_supervisor, event_data):
        """An ``otherwise`` transition is used when no condition matches."""
        graph = {
            "id": "otherwise-test",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check status.",
                    "transitions": [
                        {"when": "status is green", "goto": "celebrate"},
                        {"otherwise": True, "goto": "investigate"},
                    ],
                },
                "celebrate": {"prompt": "Celebrate!", "goto": "done"},
                "investigate": {"prompt": "Investigate!", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # LLM returns "0" → no condition matched → otherwise → investigate
        responses = iter(["Status unclear", "0", "Looking into it"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 2
        assert result.node_trace[1]["node_id"] == "investigate"

    async def test_transition_classification_uses_no_tools(
        self, mock_supervisor, branching_graph, event_data
    ):
        """The transition LLM call should pass tool_overrides=[] (no tools)."""
        responses = iter(["findings", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        # The second call should be the transition classification
        calls = mock_supervisor.chat.call_args_list
        assert len(calls) >= 2
        transition_call = calls[1]
        assert transition_call.kwargs.get("tool_overrides") == []


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


class TestTokenBudget:
    async def test_budget_exceeded_fails_run(self, mock_supervisor, event_data):
        graph = {
            "id": "budget-test",
            "version": 1,
            "max_tokens": 10,  # Very small budget
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Return a long response to blow the budget on node A
        mock_supervisor.chat.return_value = "x" * 200

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Node A should complete, but we fail before node B starts
        assert result.status == "timed_out"
        assert "Token budget exceeded" in result.error
        assert len(result.node_trace) == 1  # Only node A

    async def test_budget_not_exceeded(self, mock_supervisor, event_data):
        graph = {
            "id": "budget-ok",
            "version": 1,
            "max_tokens": 100000,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()
        assert result.status == "completed"


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


class TestSummarization:
    async def test_summarize_before_compresses_history(self, mock_supervisor, event_data):
        graph = {
            "id": "summarize-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "c"},
                "c": {
                    "prompt": "Step C",
                    "summarize_before": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        mock_supervisor.summarize.return_value = "Prior: A and B completed."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Summarize should have been called once (before node C)
        mock_supervisor.summarize.assert_called_once()

    async def test_summarize_preserves_seed(self, mock_supervisor, event_data):
        """After summarization, the seed message should still be present."""
        graph = {
            "id": "seed-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {
                    "prompt": "Step B",
                    "summarize_before": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        mock_supervisor.summarize.return_value = "Prior steps summary."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # After summarization + node B execution, we should have:
        # seed, summary, node B prompt, node B response
        # Check that first message is still the seed
        assert "Event received" in runner.messages[0]["content"]


# ---------------------------------------------------------------------------
# Human-in-the-loop
# ---------------------------------------------------------------------------


class TestHumanInTheLoop:
    async def test_wait_for_human_pauses(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        responses = iter(["Analysis: the code has issues.", "Review context presented."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "paused"
        assert len(result.node_trace) == 2  # analyse + review
        assert result.node_trace[1]["node_id"] == "review"

    async def test_paused_run_persisted(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        responses = iter(["Analysis done.", "Ready for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # DB should be updated with paused status
        update_calls = mock_db.update_playbook_run.call_args_list
        # Find the paused update call
        paused_call = None
        for call in update_calls:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None
        assert paused_call.kwargs["current_node"] == "review"
        assert "conversation_history" in paused_call.kwargs

    async def test_resume_continues_from_paused_node(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Resume a paused run with human approval → execute → done."""
        # Build a paused PlaybookRun record
        paused_run = PlaybookRun(
            run_id="paused-run-1",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received: ..."},
                    {"role": "user", "content": "Analyse the issue and propose a plan."},
                    {"role": "assistant", "content": "Analysis complete."},
                    {"role": "user", "content": "Present your analysis for human review."},
                    {"role": "assistant", "content": "Here is the analysis for review."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "analyse",
                        "started_at": 100.0,
                        "completed_at": 101.0,
                        "status": "completed",
                    },
                    {
                        "node_id": "review",
                        "started_at": 101.0,
                        "completed_at": 102.0,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=50,
            started_at=100.0,
        )

        # LLM calls: transition classification → "1" (approved → execute),
        # then execute node → "Plan executed."
        responses = iter(["1", "Plan executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, go ahead.",
            db=mock_db,
        )

        assert result.status == "completed"
        assert result.run_id == "paused-run-1"
        # Should have executed the "execute" node
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes


# ---------------------------------------------------------------------------
# LLM config overrides
# ---------------------------------------------------------------------------


class TestLLMConfigOverrides:
    async def test_playbook_level_llm_config(self, mock_supervisor, event_data):
        graph = {
            "id": "config-test",
            "version": 1,
            "llm_config": {"model": "gemini-2.5-flash", "provider": "gemini"},
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["llm_config"] == {
            "model": "gemini-2.5-flash",
            "provider": "gemini",
        }

    async def test_node_level_llm_config_overrides_playbook(self, mock_supervisor, event_data):
        graph = {
            "id": "node-config-test",
            "version": 1,
            "llm_config": {"model": "gemini-2.5-flash"},
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Step A",
                    "llm_config": {"model": "claude-sonnet-4-20250514"},
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        # Node-level config should override playbook-level
        assert call_kwargs["llm_config"] == {"model": "claude-sonnet-4-20250514"}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_missing_entry_node(self, mock_supervisor, event_data, mock_db):
        graph = {
            "id": "no-entry",
            "version": 1,
            "nodes": {
                "done": {"terminal": True},
            },
        }

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        assert "No entry node" in result.error

    async def test_missing_target_node(self, mock_supervisor, event_data, mock_db):
        graph = {
            "id": "bad-goto",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "nonexistent"},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        assert "nonexistent" in result.error

    async def test_supervisor_error_fails_node(self, mock_supervisor, event_data, mock_db):
        graph = {
            "id": "error-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.side_effect = RuntimeError("LLM provider down")
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        assert "LLM provider down" in result.error
        # Node trace should show the failed node
        assert result.node_trace[0]["status"] == "failed"

    async def test_no_transitions_implicit_terminal(self, mock_supervisor, event_data):
        """A node with no transitions, no goto, and no terminal is implicitly terminal."""
        graph = {
            "id": "implicit-end",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Do something."},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


class TestDBPersistence:
    async def test_run_creates_db_record(self, mock_supervisor, simple_graph, event_data, mock_db):
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        mock_db.create_playbook_run.assert_called_once()
        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert isinstance(created_run, PlaybookRun)
        assert created_run.playbook_id == "test-playbook"
        assert created_run.status == "running"

    async def test_run_updates_after_each_node(self, mock_supervisor, event_data, mock_db):
        graph = {
            "id": "multi-node",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "A", "goto": "b"},
                "b": {"prompt": "B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # At minimum: 1 update per node + 1 final completion update
        assert mock_db.update_playbook_run.call_count >= 3

    async def test_completed_run_has_final_state(
        self, mock_supervisor, simple_graph, event_data, mock_db
    ):
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # The last update should be the completion
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "completed"
        assert final_call.kwargs["completed_at"] is not None

    async def test_no_db_still_works(self, mock_supervisor, simple_graph, event_data):
        """Runner should work fine without a DB (db=None)."""
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        result = await runner.run()
        assert result.status == "completed"


# ---------------------------------------------------------------------------
# Progress callbacks
# ---------------------------------------------------------------------------


class TestProgressCallbacks:
    async def test_progress_events_emitted(self, mock_supervisor, simple_graph, event_data):
        mock_supervisor.chat.return_value = "Done."
        progress_events: list[tuple[str, str | None]] = []

        async def on_progress(event: str, detail: str | None):
            progress_events.append((event, detail))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, on_progress=on_progress)
        await runner.run()

        event_types = [e[0] for e in progress_events]
        assert "playbook_started" in event_types
        assert "node_started" in event_types
        assert "node_completed" in event_types
        assert "playbook_completed" in event_types


# ---------------------------------------------------------------------------
# Transition matching helpers
# ---------------------------------------------------------------------------


class TestTransitionMatching:
    def test_numeric_match(self):
        transitions = [
            {"when": "option A", "goto": "a"},
            {"when": "option B", "goto": "b"},
        ]
        result = PlaybookRunner._match_transition_by_number("1", transitions, None)
        assert result == "a"

        result = PlaybookRunner._match_transition_by_number("2", transitions, None)
        assert result == "b"

    def test_zero_returns_otherwise(self):
        transitions = [{"when": "option A", "goto": "a"}]
        result = PlaybookRunner._match_transition_by_number("0", transitions, "fallback")
        assert result == "fallback"

    def test_fuzzy_text_match(self):
        transitions = [
            {"when": "no findings", "goto": "done"},
            {"when": "findings exist", "goto": "triage"},
        ]
        result = PlaybookRunner._match_transition_by_number(
            "The answer is: findings exist", transitions, None
        )
        assert result == "triage"

    def test_no_match_returns_none(self):
        transitions = [{"when": "specific thing", "goto": "target"}]
        result = PlaybookRunner._match_transition_by_number(
            "something unrelated", transitions, None
        )
        assert result is None

    def test_embedded_number(self):
        """LLM might say 'I think it is condition 2.' instead of just '2'."""
        transitions = [
            {"when": "option A", "goto": "a"},
            {"when": "option B", "goto": "b"},
        ]
        result = PlaybookRunner._match_transition_by_number(
            "I think it is condition 2.", transitions, None
        )
        assert result == "b"


# ---------------------------------------------------------------------------
# NodeTraceEntry
# ---------------------------------------------------------------------------


class TestNodeTraceEntry:
    def test_defaults(self):
        entry = NodeTraceEntry(node_id="test", started_at=1.0)
        assert entry.status == "running"
        assert entry.completed_at is None

    def test_trace_to_dict(self):
        entry = NodeTraceEntry(
            node_id="scan",
            started_at=100.0,
            completed_at=101.5,
            status="completed",
        )
        d = PlaybookRunner._trace_to_dict(entry)
        assert d == {
            "node_id": "scan",
            "started_at": 100.0,
            "completed_at": 101.5,
            "status": "completed",
        }
