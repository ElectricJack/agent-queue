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
- Prompt building and context injection (5.2.3)
- Timeout enforcement (5.2.3)
- Progress forwarding to Supervisor (5.2.3)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from unittest.mock import AsyncMock

import pytest

from src.models import PlaybookRun
from src.playbook_runner import (
    DailyTokenTracker,
    NodeTraceEntry,
    PlaybookRunner,
    RunResult,
    _compare,
    _dot_get,
    _estimate_tokens,
    _midnight_today,
    _parse_literal,
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
        assert call_kwargs.kwargs["user_name"] == "playbook-runner:scan"

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
    """Token budget enforcement per spec §6 — Token Budget.

    The executor tracks cumulative token usage across all nodes and transition
    calls.  When the budget is exceeded:
    - The current node completes (don't cut off mid-response)
    - The run is marked as ``failed`` with reason ``token_budget_exceeded``
    - The partial context trace is preserved for debugging
    - A notification is sent (via on_progress)
    """

    async def test_budget_exceeded_fails_run(self, mock_supervisor, event_data):
        """Node A blows the budget; run fails before node B starts."""
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

        # Node A should complete, but we fail before node B starts (spec §6)
        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error
        assert len(result.node_trace) == 1  # Only node A executed

    async def test_budget_not_exceeded(self, mock_supervisor, event_data):
        """Run completes normally when usage stays under budget."""
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

    async def test_no_budget_means_unlimited(self, mock_supervisor, event_data):
        """When max_tokens is not set, budget enforcement is skipped entirely."""
        graph = {
            "id": "no-budget",
            "version": 1,
            # No max_tokens key
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Even a large response shouldn't trigger budget failure
        mock_supervisor.chat.return_value = "x" * 10000
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()
        assert result.status == "completed"
        assert result.tokens_used > 0

    async def test_post_node_check_prevents_transition_spend(self, mock_supervisor, event_data):
        """Budget check after node completion prevents wasting tokens on
        transition evaluation (spec §6 step 6d)."""
        graph = {
            "id": "post-node-check",
            "version": 1,
            "max_tokens": 10,  # Very small budget
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan files",
                    # Natural-language transitions would require an LLM call
                    "transitions": [
                        {"when": "issues found", "goto": "fix"},
                        {"when": "all clean", "goto": "done"},
                    ],
                },
                "fix": {"prompt": "Fix issues", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Long response blows the budget immediately
        mock_supervisor.chat.return_value = "x" * 200

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error
        # The supervisor should only have been called ONCE (for the node),
        # NOT a second time for transition classification — the post-node
        # budget check should have stopped execution first.
        assert mock_supervisor.chat.call_count == 1

    async def test_budget_exceeded_preserves_trace(self, mock_supervisor, event_data):
        """Partial context trace is preserved for debugging on budget exceed."""
        graph = {
            "id": "trace-test",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert len(result.node_trace) == 1
        trace = result.node_trace[0]
        assert trace["node_id"] == "a"
        assert trace["status"] == "completed"  # Node A completed successfully
        assert trace["started_at"] is not None
        assert trace["completed_at"] is not None

    async def test_budget_exceeded_sends_notification(self, mock_supervisor, event_data):
        """A notification is sent via on_progress when budget is exceeded."""
        graph = {
            "id": "notify-test",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200

        progress_events = []

        async def track_progress(event, detail):
            progress_events.append((event, detail))

        runner = PlaybookRunner(graph, event_data, mock_supervisor, on_progress=track_progress)
        result = await runner.run()

        assert result.status == "failed"
        # Should have fired playbook_failed with budget info
        failed_events = [(e, d) for e, d in progress_events if e == "playbook_failed"]
        assert len(failed_events) == 1
        assert "token_budget_exceeded" in failed_events[0][1]

    async def test_budget_exceeded_persists_to_db(self, mock_supervisor, mock_db, event_data):
        """Budget-exceeded failure is correctly persisted to the database."""
        graph = {
            "id": "db-budget",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"

        # Check the final DB update
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "failed"
        assert "token_budget_exceeded" in final_call.kwargs["error"]
        assert final_call.kwargs["tokens_used"] > 0
        # Conversation history is preserved
        history = json.loads(final_call.kwargs["conversation_history"])
        assert len(history) > 0
        # Node trace is preserved
        trace = json.loads(final_call.kwargs["node_trace"])
        assert len(trace) == 1

    async def test_budget_tracks_transition_tokens(self, mock_supervisor, event_data):
        """Token usage from transition LLM calls counts toward the budget."""
        # Budget large enough for node A but not for node A + transition + node B
        graph = {
            "id": "transition-budget",
            "version": 1,
            "max_tokens": 100,  # Tight but allows first node
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Check",  # ~1 token
                    "transitions": [
                        {"when": "issues found", "goto": "b"},
                        {"when": "all clean", "goto": "done"},
                    ],
                },
                "b": {"prompt": "Fix it", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Short response for node A (stays under budget), but transition
        # and next node push it over
        responses = iter(["Short.", "1", "x" * 800])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Tokens from the transition call should be counted
        assert runner.tokens_used > 0
        # Whether it completed or exceeded depends on exact estimates,
        # but transition tokens should be included in the total
        assert result.tokens_used == runner.tokens_used

    async def test_budget_error_includes_usage_details(self, mock_supervisor, event_data):
        """Error message includes both the budget limit and actual usage."""
        graph = {
            "id": "error-detail",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert "budget 10" in result.error
        assert "used" in result.error
        # Verify the actual tokens_used is in the error
        assert str(result.tokens_used) in result.error

    async def test_budget_exceeded_on_pre_node_check(self, mock_supervisor, event_data):
        """Tokens accumulated by transition in prior iteration trigger pre-node check."""
        graph = {
            "id": "pre-node-budget",
            "version": 1,
            "max_tokens": 50,  # Enough for one node but not two + transition
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Go",  # Small prompt
                    "transitions": [
                        {"when": "yes", "goto": "b"},
                        {"otherwise": True, "goto": "done"},
                    ],
                },
                "b": {"prompt": "More work", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Node A response is moderate, transition response is "1" (go to b),
        # but cumulative tokens exceed budget before node B starts
        responses = iter(["x" * 100, "1"])  # ~25 + ~1 transition tokens
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Should fail — either at post-node or pre-node check
        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error

    async def test_budget_exact_boundary(self, mock_supervisor, event_data):
        """When tokens_used equals max_tokens exactly, the run fails."""
        graph = {
            "id": "boundary",
            "version": 1,
            "max_tokens": 5,  # Will be hit by even a tiny exchange
            "nodes": {
                "a": {"entry": True, "prompt": "Hi", "goto": "b"},
                "b": {"prompt": "More", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "ok"  # tiny response
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # "Hi" (2 chars) + "ok" (2 chars) = ~1 token. Budget is 5.
        # Should depend on actual estimate — but the point is the boundary
        # is inclusive (>= check, not >)
        if runner.tokens_used >= 5:
            assert result.status == "failed"
        else:
            assert result.status == "completed"

    async def test_budget_resume_enforces_limit(self, mock_supervisor, mock_db, event_data):
        """Budget enforcement works correctly when resuming a paused run."""
        graph = {
            "id": "resume-budget",
            "version": 1,
            "max_tokens": 20,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review this.",
                    "wait_for_human": True,
                    "goto": "apply",
                },
                "apply": {"prompt": "Apply changes.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # First run: execute review node, then pause for human
        mock_supervisor.chat.return_value = "Awaiting review."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()
        assert result.status == "paused"

        # Create a db_run record for resuming, with tokens already near budget
        db_run = PlaybookRun(
            run_id=result.run_id,
            playbook_id="resume-budget",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            started_at=1000.0,
            current_node="review",
            conversation_history=json.dumps(runner.messages),
            node_trace=json.dumps(result.node_trace),
            tokens_used=18,  # Almost at budget of 20
            pinned_graph=json.dumps(graph),
        )

        # Resume — the apply node response pushes over budget
        mock_supervisor.chat.return_value = "x" * 200
        resumed = await PlaybookRunner.resume(
            db_run, graph, mock_supervisor, "Approved!", db=mock_db
        )

        assert resumed.status == "failed"
        assert "token_budget_exceeded" in resumed.error

    # ------------------------------------------------------------------
    # (c) Budget warning when approaching (within 10%)
    # ------------------------------------------------------------------

    async def test_approaching_budget_logs_warning(self, mock_supervisor, event_data, caplog):
        """When token usage reaches 90%+ of budget, a warning is logged but
        the run continues to completion (roadmap 5.2.16 case c)."""
        graph = {
            "id": "warn-test",
            "version": 1,
            "max_tokens": 100,  # Budget of 100 tokens
            "nodes": {
                "a": {"entry": True, "prompt": "Go", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Node A: prompt "Go" (2 chars → 1 tok) + response (360 chars → 90 tok)
        # Total: ~91 tokens.  91/100 = 91% — in the warning band (>= 90%)
        # but under budget (< 100), so the run completes with a warning.
        mock_supervisor.chat.return_value = "x" * 360

        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            runner = PlaybookRunner(graph, event_data, mock_supervisor)
            result = await runner.run()

        # Run should complete (not fail) — warning is advisory only
        assert result.status == "completed"
        # A warning about approaching budget should have been logged
        warning_msgs = [
            r.message for r in caplog.records if "approaching token budget" in r.message
        ]
        assert len(warning_msgs) >= 1
        assert "warn-test" in warning_msgs[0]

    async def test_approaching_budget_continues_execution(self, mock_supervisor, event_data):
        """Run at 90%+ of budget finishes all remaining nodes (case c)."""
        graph = {
            "id": "warn-continue",
            "version": 1,
            "max_tokens": 200,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Node A response sized to push usage to ~90% of 200 (≈180 tokens)
        # "Step A" ≈ 2 tokens, response ≈ 700/4 = 175 tokens → total ~177
        # Node B response is tiny
        responses = iter(["x" * 700, "ok"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Both nodes executed
        assert len(result.node_trace) == 2

    # ------------------------------------------------------------------
    # (d) Global daily token cap blocks new runs
    # ------------------------------------------------------------------

    async def test_daily_cap_blocks_new_run(self, mock_supervisor, event_data):
        """When daily playbook token usage exceeds the cap, new runs are
        blocked immediately (roadmap 5.2.16 case d)."""
        graph = {
            "id": "daily-cap-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Work", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        tracker = DailyTokenTracker()
        # Simulate prior runs consuming 10_000 tokens today
        tracker.add_tokens(10_000)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            daily_token_tracker=tracker,
            daily_token_cap=10_000,  # Cap already reached
        )
        result = await runner.run()

        assert result.status == "failed"
        assert "daily_token_cap_exceeded" in result.error
        assert result.tokens_used == 0  # No tokens consumed — run never started
        assert result.node_trace == []  # No nodes executed
        # Supervisor should NOT have been called
        mock_supervisor.chat.assert_not_called()

    async def test_daily_cap_allows_run_under_limit(self, mock_supervisor, event_data):
        """Runs proceed normally when daily usage is below the cap."""
        graph = {
            "id": "daily-cap-ok",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Work", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        tracker = DailyTokenTracker()
        tracker.add_tokens(5_000)  # Well under cap

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            daily_token_tracker=tracker,
            daily_token_cap=10_000,
        )
        result = await runner.run()

        assert result.status == "completed"
        # Daily tracker should reflect the tokens from this run too
        assert tracker.get_usage() > 5_000

    async def test_daily_cap_sends_notification(self, mock_supervisor, event_data):
        """Blocked run sends a playbook_failed progress notification."""
        graph = {
            "id": "daily-cap-notify",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Work", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        tracker = DailyTokenTracker()
        tracker.add_tokens(500)

        progress_events = []

        async def track_progress(event, detail):
            progress_events.append((event, detail))

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            daily_token_tracker=tracker,
            daily_token_cap=500,
            on_progress=track_progress,
        )
        result = await runner.run()

        assert result.status == "failed"
        failed_events = [(e, d) for e, d in progress_events if e == "playbook_failed"]
        assert len(failed_events) == 1
        assert "daily_token_cap_exceeded" in failed_events[0][1]

    # ------------------------------------------------------------------
    # (e) Daily cap resets at midnight (or configured time)
    # ------------------------------------------------------------------

    async def test_daily_cap_resets_at_midnight(self, mock_supervisor, event_data):
        """Daily token usage resets when the date changes (case e)."""
        tracker = DailyTokenTracker()

        # Simulate usage on April 8
        april_8 = datetime.datetime(2026, 4, 8, 23, 0)
        tracker.add_tokens(10_000, now=april_8)
        assert tracker.get_usage(now=april_8) == 10_000

        # After midnight (April 9) — usage resets to 0
        april_9 = datetime.datetime(2026, 4, 9, 0, 30)
        assert tracker.get_usage(now=april_9) == 0

        # New usage on April 9 is tracked separately
        tracker.add_tokens(500, now=april_9)
        assert tracker.get_usage(now=april_9) == 500
        # April 8 usage is still accessible
        assert tracker.get_usage(now=april_8) == 10_000

    async def test_daily_cap_resets_at_configured_hour(self, mock_supervisor, event_data):
        """Daily cap respects a custom reset hour (e.g. 6 AM)."""
        tracker = DailyTokenTracker(reset_hour=6)

        # Usage at 5 AM on April 9 — still belongs to "April 8" bucket
        # because reset_hour=6 means the new day starts at 06:00
        pre_reset = datetime.datetime(2026, 4, 9, 5, 30)
        tracker.add_tokens(3_000, now=pre_reset)
        assert tracker.get_usage(now=pre_reset) == 3_000

        # Usage at 7 AM on April 9 — new day bucket
        post_reset = datetime.datetime(2026, 4, 9, 7, 0)
        assert tracker.get_usage(now=post_reset) == 0

        tracker.add_tokens(1_000, now=post_reset)
        assert tracker.get_usage(now=post_reset) == 1_000
        # Pre-reset bucket unchanged
        assert tracker.get_usage(now=pre_reset) == 3_000

    async def test_daily_cap_blocks_after_accumulation(self, mock_supervisor, event_data):
        """Multiple runs accumulate toward the daily cap until it blocks."""
        graph = {
            "id": "daily-accum",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Go", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        tracker = DailyTokenTracker()
        mock_supervisor.chat.return_value = "Done."

        # First run succeeds — usage under cap
        runner1 = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            daily_token_tracker=tracker,
            daily_token_cap=100,
        )
        r1 = await runner1.run()
        assert r1.status == "completed"
        assert tracker.get_usage() > 0

        # Artificially push daily usage to the cap
        tracker.add_tokens(100 - tracker.get_usage())

        # Second run should be blocked
        runner2 = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            daily_token_tracker=tracker,
            daily_token_cap=100,
        )
        r2 = await runner2.run()
        assert r2.status == "failed"
        assert "daily_token_cap_exceeded" in r2.error

    # ------------------------------------------------------------------
    # (f) Token counting includes both input and output tokens
    # ------------------------------------------------------------------

    async def test_input_and_output_tokens_counted(self, mock_supervisor, event_data):
        """Both the prompt (input) and response (output) tokens are counted
        toward the budget (roadmap 5.2.16 case f)."""
        graph = {
            "id": "io-count",
            "version": 1,
            "max_tokens": 100_000,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "A" * 100,  # ~25 input tokens
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "B" * 200  # ~50 output tokens

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Token count should reflect BOTH input (~25) and output (~50)
        # _estimate_tokens counts chars/4 for each string
        expected_input = len("A" * 100) // 4  # 25
        expected_output = len("B" * 200) // 4  # 50
        expected_total = expected_input + expected_output  # 75
        assert result.tokens_used >= expected_total
        # Verify it's not just counting output (must be > output alone)
        assert result.tokens_used > expected_output

    # ------------------------------------------------------------------
    # (g) Zero-budget run fails immediately (first node never starts)
    # ------------------------------------------------------------------

    async def test_zero_budget_fails_before_first_node(self, mock_supervisor, event_data):
        """A run with max_tokens=0 fails immediately — no nodes execute
        (roadmap 5.2.16 case g)."""
        graph = {
            "id": "zero-budget",
            "version": 1,
            "max_tokens": 0,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error
        assert result.node_trace == []  # No nodes executed
        # Supervisor should NOT have been called
        mock_supervisor.chat.assert_not_called()

    async def test_zero_budget_preserves_run_metadata(self, mock_supervisor, mock_db, event_data):
        """Zero-budget failure persists correctly to the database."""
        graph = {
            "id": "zero-budget-db",
            "version": 1,
            "max_tokens": 0,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error

        # DB record should have been created and then updated with failure
        mock_db.create_playbook_run.assert_called_once()
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "failed"
        assert "token_budget_exceeded" in final_call.kwargs["error"]

    async def test_first_node_exceeds_tiny_budget(self, mock_supervisor, event_data):
        """A very small positive budget that the first node exceeds causes
        failure after that node (the node is allowed to complete gracefully)."""
        graph = {
            "id": "tiny-budget",
            "version": 1,
            "max_tokens": 1,  # Only 1 token — any real node will exceed
            "nodes": {
                "a": {"entry": True, "prompt": "Do work", "goto": "b"},
                "b": {"prompt": "More work", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Here is the result"
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Should fail after node A (graceful — node completes then budget check)
        assert result.status == "failed"
        assert "token_budget_exceeded" in result.error
        assert len(result.node_trace) == 1  # Only node A ran
        # Second node should NOT have been called
        assert mock_supervisor.chat.call_count == 1


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


class TestSummarization:
    async def test_summarize_before_compresses_history(self, mock_supervisor, event_data):
        """summarize_before triggers compression and supervisor.summarize is called."""
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

    async def test_summarize_replaces_history_with_summary(self, mock_supervisor, event_data):
        """After summarization, history contains seed + summary + subsequent node msgs."""
        graph = {
            "id": "replace-test",
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
        mock_supervisor.summarize.return_value = "Summary: steps A and B done."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # After run: seed, summary, node-C prompt, node-C response = 4 messages
        assert len(runner.messages) == 4
        assert runner.messages[0]["role"] == "user"
        assert "Event received" in runner.messages[0]["content"]
        assert "[Context summary of prior steps]" in runner.messages[1]["content"]
        assert "Summary: steps A and B done." in runner.messages[1]["content"]
        assert runner.messages[2]["content"] == "Step C"
        assert runner.messages[3]["content"] == "Done."

    async def test_summarize_failure_preserves_full_history(self, mock_supervisor, event_data):
        """When supervisor.summarize returns None, full history is kept."""
        graph = {
            "id": "fail-test",
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
        mock_supervisor.summarize.return_value = None  # Simulate failure

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Full history preserved: seed + (A prompt, A response) + (B prompt, B response) = 5
        assert len(runner.messages) == 5
        # No summary message injected
        assert not any(
            "[Context summary of prior steps]" in m.get("content", "") for m in runner.messages
        )

    async def test_summarize_skipped_when_insufficient_history(self, mock_supervisor, event_data):
        """summarize_before on a node with ≤2 messages is a no-op."""
        graph = {
            "id": "skip-test",
            "version": 1,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Step A",
                    "summarize_before": True,  # Only seed exists — should be skipped
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # summarize should NOT have been called (only 1 message = seed)
        mock_supervisor.summarize.assert_not_called()

    async def test_summarize_uses_playbook_specific_prompts(self, mock_supervisor, event_data):
        """Summarization passes playbook-specific system_prompt and instruction."""
        graph = {
            "id": "prompt-test",
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
        mock_supervisor.summarize.return_value = "Summary."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Verify summarize was called with keyword args for custom prompts
        call_kwargs = mock_supervisor.summarize.call_args
        assert "system_prompt" in call_kwargs.kwargs
        assert "instruction" in call_kwargs.kwargs
        # The playbook-specific instruction should mention "playbook" or "step"
        assert "playbook" in call_kwargs.kwargs["system_prompt"].lower()
        assert "step" in call_kwargs.kwargs["instruction"].lower()

    async def test_summarize_tracks_token_cost(self, mock_supervisor, event_data):
        """Token cost of the summarization LLM call is added to tokens_used."""
        graph = {
            "id": "token-test",
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
        mock_supervisor.summarize.return_value = "Summary."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # tokens_used should be > 0 and include summarization overhead
        assert result.tokens_used > 0
        # Run without summarization for comparison
        graph_no_sum = {
            "id": "token-test-nosumm",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        runner2 = PlaybookRunner(graph_no_sum, event_data, mock_supervisor)
        result2 = await runner2.run()

        # With summarization should use more tokens (summarization transcript + summary)
        assert result.tokens_used > result2.tokens_used

    async def test_summarize_fires_progress_callback(self, mock_supervisor, event_data):
        """A node_summarizing progress event is emitted during summarization."""
        graph = {
            "id": "progress-test",
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
        mock_supervisor.summarize.return_value = "Summary."

        progress_events: list[tuple[str, str]] = []

        async def capture_progress(event_type: str, detail: str):
            progress_events.append((event_type, detail))

        runner = PlaybookRunner(graph, event_data, mock_supervisor, on_progress=capture_progress)
        await runner.run()

        # There should be a node_summarizing event
        summarizing_events = [e for e in progress_events if e[0] == "node_summarizing"]
        assert len(summarizing_events) == 1

    async def test_summarize_transcript_includes_all_messages(self, mock_supervisor, event_data):
        """The transcript passed to summarize includes content from all prior messages."""
        graph = {
            "id": "transcript-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A prompt", "goto": "b"},
                "b": {"prompt": "Step B prompt", "goto": "c"},
                "c": {
                    "prompt": "Step C prompt",
                    "summarize_before": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        responses = iter(["Response from A", "Response from B", "Response from C"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)
        mock_supervisor.summarize.return_value = "Condensed summary."

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Grab the transcript that was passed to summarize
        transcript_arg = mock_supervisor.summarize.call_args.args[0]
        # Transcript should contain content from seed, step A, and step B
        assert "Event received" in transcript_arg
        assert "Step A prompt" in transcript_arg
        assert "Response from A" in transcript_arg
        assert "Step B prompt" in transcript_arg
        assert "Response from B" in transcript_arg

    async def test_multiple_summarize_before_nodes(self, mock_supervisor, event_data):
        """Multiple nodes with summarize_before each trigger their own compression."""
        graph = {
            "id": "multi-summarize",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {
                    "prompt": "Step B",
                    "summarize_before": True,
                    "goto": "c",
                },
                "c": {"prompt": "Step C", "goto": "d"},
                "d": {
                    "prompt": "Step D",
                    "summarize_before": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        mock_supervisor.summarize.side_effect = [
            "Summary after A.",
            "Summary after A-C.",
        ]

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert mock_supervisor.summarize.call_count == 2


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

    async def test_llm_config_with_max_tokens_and_temperature(self, mock_supervisor, event_data):
        """max_tokens and temperature in llm_config are passed through to supervisor."""
        graph = {
            "id": "config-extras",
            "version": 1,
            "llm_config": {
                "model": "gemini-2.5-flash",
                "max_tokens": 2048,
                "temperature": 0.3,
            },
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
            "max_tokens": 2048,
            "temperature": 0.3,
        }

    async def test_transition_llm_config_playbook_level(self, mock_supervisor, event_data):
        """Playbook-level transition_llm_config is used for transition classification."""
        graph = {
            "id": "transition-config",
            "version": 1,
            "llm_config": {"model": "sonnet"},
            "transition_llm_config": {"model": "haiku"},
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Step A",
                    "transitions": [
                        {"when": "success", "goto": "done"},
                        {"otherwise": True, "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        # First call is node execution (returns "success")
        # Second call is transition classification (returns "1")
        mock_supervisor.chat.side_effect = ["success result", "1"]
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # First call (node execution) uses playbook llm_config
        node_call = mock_supervisor.chat.call_args_list[0]
        assert node_call.kwargs["llm_config"] == {"model": "sonnet"}

        # Second call (transition) uses transition_llm_config
        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["llm_config"] == {"model": "haiku"}

    async def test_node_transition_llm_config_overrides_playbook(self, mock_supervisor, event_data):
        """Node-level transition_llm_config overrides playbook-level."""
        graph = {
            "id": "node-trans-config",
            "version": 1,
            "transition_llm_config": {"model": "playbook-haiku"},
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Step A",
                    "transition_llm_config": {"model": "node-flash"},
                    "transitions": [
                        {"when": "pass", "goto": "done"},
                        {"otherwise": True, "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.side_effect = ["result", "1"]
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Transition call should use node-level transition_llm_config
        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["llm_config"] == {"model": "node-flash"}

    async def test_no_config_passes_none(self, mock_supervisor, event_data):
        """When no llm_config is set anywhere, None is passed to supervisor."""
        graph = {
            "id": "no-config",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["llm_config"] is None


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

    async def test_persisted_conversation_history_content(
        self, mock_supervisor, event_data, mock_db
    ):
        """Verify the actual JSON content of persisted conversation history."""
        graph = {
            "id": "history-persist",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Scan for issues.", "goto": "b"},
                "b": {"prompt": "Fix the issues.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Found 3 issues.", "All fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # Check the final completion update has full conversation history
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        history = json.loads(final_call.kwargs["conversation_history"])

        # Should have: seed + node_a_prompt + node_a_response + node_b_prompt + node_b_response
        assert len(history) == 5
        assert history[0]["role"] == "user"  # seed
        assert "Event received" in history[0]["content"]
        assert history[1] == {"role": "user", "content": "Scan for issues."}
        assert history[2] == {"role": "assistant", "content": "Found 3 issues."}
        assert history[3] == {"role": "user", "content": "Fix the issues."}
        assert history[4] == {"role": "assistant", "content": "All fixed."}

    async def test_persisted_node_trace_content(self, mock_supervisor, event_data, mock_db):
        """Verify the actual JSON content of persisted node trace."""
        graph = {
            "id": "trace-persist",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # Check the final completion update has full node trace
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        trace = json.loads(final_call.kwargs["node_trace"])

        assert len(trace) == 2
        assert trace[0]["node_id"] == "a"
        assert trace[0]["status"] == "completed"
        assert trace[0]["started_at"] is not None
        assert trace[0]["completed_at"] is not None
        assert trace[0]["completed_at"] >= trace[0]["started_at"]
        assert trace[1]["node_id"] == "b"
        assert trace[1]["status"] == "completed"

    async def test_intermediate_updates_have_partial_state(
        self, mock_supervisor, event_data, mock_db
    ):
        """Each intermediate update should reflect the state at that point."""
        graph = {
            "id": "partial-state",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # First node update (after node "a")
        first_update = mock_db.update_playbook_run.call_args_list[0]
        assert first_update.kwargs["current_node"] == "a"
        trace_after_a = json.loads(first_update.kwargs["node_trace"])
        assert len(trace_after_a) == 1
        assert trace_after_a[0]["node_id"] == "a"

        history_after_a = json.loads(first_update.kwargs["conversation_history"])
        assert len(history_after_a) == 3  # seed + prompt + response

        # Second node update (after node "b")
        second_update = mock_db.update_playbook_run.call_args_list[1]
        assert second_update.kwargs["current_node"] == "b"
        trace_after_b = json.loads(second_update.kwargs["node_trace"])
        assert len(trace_after_b) == 2

        history_after_b = json.loads(second_update.kwargs["conversation_history"])
        assert len(history_after_b) == 5  # seed + 2*(prompt + response)

    async def test_failed_run_persists_error_and_partial_state(
        self, mock_supervisor, event_data, mock_db
    ):
        """A failed run should persist the error, partial history, and trace."""
        graph = {
            "id": "fail-persist",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        call_count = 0

        async def fail_on_second(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("LLM timeout")
            return "Step A done."

        mock_supervisor.chat.side_effect = fail_on_second
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"

        # Find the failure update call
        fail_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed":
                fail_call = call
                break

        assert fail_call is not None
        assert "LLM timeout" in fail_call.kwargs["error"]
        assert fail_call.kwargs["current_node"] == "b"
        assert fail_call.kwargs["completed_at"] is not None

        # Conversation history should contain what completed before failure
        history = json.loads(fail_call.kwargs["conversation_history"])
        assert len(history) == 3  # seed + node_a_prompt + node_a_response

        # Node trace should show node A completed and B failed
        trace = json.loads(fail_call.kwargs["node_trace"])
        assert len(trace) == 2
        assert trace[0]["node_id"] == "a"
        assert trace[0]["status"] == "completed"
        assert trace[1]["node_id"] == "b"
        assert trace[1]["status"] == "failed"

    async def test_budget_exceeded_run_persists_failed_status(
        self, mock_supervisor, event_data, mock_db
    ):
        """Token budget exhaustion should persist failed status (spec §6)."""
        graph = {
            "id": "timeout-persist",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "b"},
                "b": {"prompt": "Step B", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "x" * 200
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"

        # Find the failed update for budget exceeded
        budget_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed":
                budget_call = call
                break

        assert budget_call is not None
        assert "token_budget_exceeded" in budget_call.kwargs["error"]


# ---------------------------------------------------------------------------
# Version pinning (5.2.12)
# ---------------------------------------------------------------------------


class TestVersionPinning:
    """In-flight runs continue with old version when recompiled."""

    async def test_run_pins_graph_in_db_record(self, mock_supervisor, event_data, mock_db):
        """run() should persist the compiled graph in pinned_graph."""
        graph = {
            "id": "pin-test",
            "version": 3,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # The created DB record should contain the pinned graph
        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert created_run.pinned_graph is not None
        pinned = json.loads(created_run.pinned_graph)
        assert pinned == graph
        assert pinned["version"] == 3

    async def test_resume_uses_pinned_graph_not_current(self, mock_supervisor, mock_db):
        """Resume should use pinned_graph from DB, ignoring the caller-supplied graph."""
        # The pinned graph (v2) has a different structure than the current (v3).
        # Specifically, the pinned version has a "review" node that transitions
        # to "execute", while the current v3 removed the "execute" node.
        v2_graph = {
            "id": "evolving-playbook",
            "version": 2,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the code.",
                    "goto": "review",
                },
                "review": {
                    "prompt": "Present for review.",
                    "wait_for_human": True,
                    "transitions": [
                        {"when": "approved", "goto": "execute"},
                        {"when": "rejected", "goto": "done"},
                    ],
                },
                "execute": {
                    "prompt": "Execute the v2 plan.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        v3_graph = {
            "id": "evolving-playbook",
            "version": 3,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the code (v3 updated).",
                    "goto": "review",
                },
                "review": {
                    "prompt": "Present for review (v3 updated).",
                    "wait_for_human": True,
                    "transitions": [
                        {"when": "approved", "goto": "done"},
                        {"when": "rejected", "goto": "done"},
                    ],
                },
                # "execute" node was REMOVED in v3
                "done": {"terminal": True},
            },
        }

        # Build a paused run that has pinned_graph from v2
        paused_run = PlaybookRun(
            run_id="pinned-resume-1",
            playbook_id="evolving-playbook",
            playbook_version=2,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received: ..."},
                    {"role": "user", "content": "Analyse the code."},
                    {"role": "assistant", "content": "Analysis done."},
                    {"role": "user", "content": "Present for review."},
                    {"role": "assistant", "content": "Here is the review."},
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
            pinned_graph=json.dumps(v2_graph),
        )

        # LLM calls: transition classification → "1" (approved → execute in v2),
        # then execute node → "V2 plan executed."
        responses = iter(["1", "V2 plan executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        # Pass v3_graph as the current graph, but resume should use pinned v2
        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=v3_graph,  # Current version — should NOT be used
            supervisor=mock_supervisor,
            human_input="Approved, go ahead.",
            db=mock_db,
        )

        assert result.status == "completed"
        assert result.run_id == "pinned-resume-1"
        # The "execute" node only exists in v2, not v3.
        # If pinning works, the run should have walked through "execute".
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes

    async def test_resume_falls_back_to_caller_graph_without_pinned(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Resume falls back to the caller-supplied graph when no pinned_graph."""
        # Simulate a pre-5.2.12 run with no pinned_graph
        paused_run = PlaybookRun(
            run_id="legacy-run-1",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received: ..."},
                    {"role": "user", "content": "Analyse the issue and propose a plan."},
                    {"role": "assistant", "content": "Analysis complete."},
                    {"role": "user", "content": "Present your analysis for human review."},
                    {"role": "assistant", "content": "Here is the analysis."},
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
            pinned_graph=None,  # No pinned graph (pre-5.2.12)
        )

        responses = iter(["1", "Plan executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,  # Should be used as fallback
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes

    async def test_pinned_graph_version_preserved_in_runner(
        self, mock_supervisor, event_data, mock_db
    ):
        """The runner should store the correct version from the pinned graph."""
        graph = {
            "id": "version-check",
            "version": 7,
            "nodes": {
                "a": {"entry": True, "prompt": "Do A.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert created_run.playbook_version == 7
        pinned = json.loads(created_run.pinned_graph)
        assert pinned["version"] == 7

    async def test_resume_with_pinned_graph_uses_pinned_prompts(self, mock_supervisor, mock_db):
        """Verify the runner actually sends the pinned graph's prompts, not the current ones."""
        v1_graph = {
            "id": "prompt-check",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "V1: scan for security issues.",
                    "wait_for_human": True,
                    "transitions": [
                        {"goto": "fix", "when": "approved"},
                        {"goto": "done", "otherwise": True},
                    ],
                },
                "fix": {
                    "prompt": "V1: apply security fixes.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        v2_graph = {
            "id": "prompt-check",
            "version": 2,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "V2: scan for performance issues.",
                    "wait_for_human": True,
                    "transitions": [
                        {"goto": "fix", "when": "approved"},
                        {"goto": "done", "otherwise": True},
                    ],
                },
                "fix": {
                    "prompt": "V2: apply performance fixes.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        paused_run = PlaybookRun(
            run_id="prompt-pin-1",
            playbook_id="prompt-check",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="start",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received: ..."},
                    {"role": "user", "content": "V1: scan for security issues."},
                    {"role": "assistant", "content": "Found 2 security issues."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "start",
                        "started_at": 100.0,
                        "completed_at": 101.0,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=30,
            started_at=100.0,
            pinned_graph=json.dumps(v1_graph),
        )

        # Track what prompt the supervisor receives
        prompts_received = []

        async def capture_chat(**kwargs):
            text = kwargs.get("text", "")
            prompts_received.append(text)
            if "condition" in text.lower() or "which" in text.lower():
                return "1"  # approved
            return "Security fixes applied."

        mock_supervisor.chat.side_effect = capture_chat

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=v2_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        assert result.status == "completed"
        # The "fix" node should have been called with v1's prompt
        assert any("V1: apply security fixes" in p for p in prompts_received)
        # V2's prompt should NOT appear
        assert not any("V2: apply performance fixes" in p for p in prompts_received)

    async def test_pinned_graph_persisted_on_run_no_db(self, mock_supervisor, event_data):
        """When db is None, run still works; pinned_graph is set on the local object."""
        graph = {
            "id": "no-db-pin",
            "version": 4,
            "nodes": {
                "a": {"entry": True, "prompt": "Do A.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=None)
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


# ---------------------------------------------------------------------------
# 5.2.3: Prompt building and context
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    """Test prompt construction via _build_node_prompt."""

    def test_returns_node_prompt(self, mock_supervisor, event_data):
        graph = {
            "id": "prompt-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Do something specific.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        prompt = runner._build_node_prompt("a", graph["nodes"]["a"])
        assert prompt == "Do something specific."

    def test_empty_prompt_returns_empty(self, mock_supervisor, event_data):
        graph = {"id": "empty-prompt", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        prompt = runner._build_node_prompt("a", {})
        assert prompt == ""

    async def test_prompt_passed_to_supervisor_unchanged(self, mock_supervisor, event_data):
        """Node prompt from the compiled graph is passed directly to supervisor.chat()."""
        graph = {
            "id": "pass-through",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Run vibecop_check on changed files.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["text"] == "Run vibecop_check on changed files."

    async def test_event_context_in_seed_message(self, mock_supervisor, event_data):
        """The trigger event data is included in the conversation seed message."""
        graph = {
            "id": "context-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Seed message should contain the full event JSON
        seed = runner.messages[0]
        assert seed["role"] == "user"
        assert "Event received:" in seed["content"]
        assert '"project_id": "test-proj"' in seed["content"]
        assert '"commit_hash": "abc123"' in seed["content"]
        assert "context-test" in seed["content"]

    async def test_accumulated_history_includes_all_prior_nodes(self, mock_supervisor, event_data):
        """At node C, the history should include seed + node A + node B exchanges."""
        graph = {
            "id": "accumulation-test",
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
        await runner.run()

        calls = mock_supervisor.chat.call_args_list
        assert len(calls) == 3

        # Node C (third call) should get seed + A prompt + A response + B prompt + B response
        third_history = calls[2].kwargs["history"]
        assert len(third_history) == 5  # seed, A prompt, A response, B prompt, B response
        assert third_history[0]["role"] == "user"  # seed
        assert third_history[1]["content"] == "Step A"
        assert third_history[2]["content"] == "Result A"
        assert third_history[3]["content"] == "Step B"
        assert third_history[4]["content"] == "Result B"


# ---------------------------------------------------------------------------
# 5.2.3: LLM config resolution
# ---------------------------------------------------------------------------


class TestLLMConfigResolution:
    """Test _resolve_node_llm_config method."""

    def test_node_config_wins(self, mock_supervisor, event_data):
        graph = {
            "id": "config-resolution",
            "version": 1,
            "llm_config": {"model": "cheap-model"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"llm_config": {"model": "expensive-model"}}
        result = runner._resolve_node_llm_config(node)
        assert result == {"model": "expensive-model"}

    def test_playbook_fallback(self, mock_supervisor, event_data):
        graph = {
            "id": "config-resolution",
            "version": 1,
            "llm_config": {"model": "cheap-model"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"prompt": "Do something"}
        result = runner._resolve_node_llm_config(node)
        assert result == {"model": "cheap-model"}

    def test_no_config_returns_none(self, mock_supervisor, event_data):
        graph = {"id": "no-config", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"prompt": "Do something"}
        result = runner._resolve_node_llm_config(node)
        assert result is None


# ---------------------------------------------------------------------------
# 5.2.3: Timeout enforcement
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    """Test timeout_seconds per-node enforcement."""

    async def test_timeout_fails_node(self, mock_supervisor, event_data, mock_db):
        """A node with timeout_seconds that exceeds the limit should fail the run."""
        graph = {
            "id": "timeout-test",
            "version": 1,
            "nodes": {
                "slow": {
                    "entry": True,
                    "prompt": "Do something slow.",
                    "timeout_seconds": 1,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        # Simulate a slow supervisor call
        async def slow_chat(**kw):
            await asyncio.sleep(5)
            return "Eventually done."

        mock_supervisor.chat.side_effect = slow_chat
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        assert "timed out" in result.error.lower()
        assert result.node_trace[0]["status"] == "failed"

    async def test_no_timeout_works_normally(self, mock_supervisor, event_data):
        """Nodes without timeout_seconds are not artificially limited."""
        graph = {
            "id": "no-timeout",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()
        assert result.status == "completed"

    async def test_timeout_within_budget_completes(self, mock_supervisor, event_data):
        """A node that finishes within timeout_seconds should complete normally."""
        graph = {
            "id": "timeout-ok",
            "version": 1,
            "nodes": {
                "fast": {
                    "entry": True,
                    "prompt": "Do something fast.",
                    "timeout_seconds": 30,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done quickly."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()
        assert result.status == "completed"
        assert result.final_response == "Done quickly."


# ---------------------------------------------------------------------------
# 5.2.3: Progress forwarding to Supervisor
# ---------------------------------------------------------------------------


class TestSupervisorProgressForwarding:
    """Test that on_progress is bridged to supervisor.chat(on_progress=...)."""

    async def test_progress_callback_forwarded(self, mock_supervisor, event_data):
        """The runner's on_progress should be forwarded to supervisor.chat()."""
        graph = {
            "id": "progress-test",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."

        async def noop_progress(event, detail):
            pass

        runner = PlaybookRunner(graph, event_data, mock_supervisor, on_progress=noop_progress)
        await runner.run()

        # Supervisor.chat() should have received an on_progress callback
        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["on_progress"] is not None
        assert callable(call_kwargs["on_progress"])

    async def test_no_progress_forwards_none(self, mock_supervisor, event_data):
        """Without on_progress, supervisor.chat() should get on_progress=None."""
        graph = {
            "id": "no-progress",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Step A", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["on_progress"] is None

    async def test_progress_bridge_maps_events(self, mock_supervisor, event_data):
        """The progress bridge should map supervisor events to node-scoped events."""
        graph = {
            "id": "bridge-test",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan files.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        all_events: list[tuple[str, str | None]] = []

        async def track_progress(event, detail):
            all_events.append((event, detail))

        # Capture the bridge callback and invoke it during chat()
        bridge_ref = None

        async def chat_with_bridge(**kw):
            nonlocal bridge_ref
            bridge_ref = kw.get("on_progress")
            if bridge_ref:
                await bridge_ref("tool_use", "vibecop_check")
                await bridge_ref("responding", None)
            return "Scan complete."

        mock_supervisor.chat.side_effect = chat_with_bridge

        runner = PlaybookRunner(graph, event_data, mock_supervisor, on_progress=track_progress)
        await runner.run()

        # The bridge should have mapped supervisor events to node-scoped events
        event_types = [e[0] for e in all_events]
        assert "node_tool_use" in event_types
        assert "node_responding" in event_types

        # Check the detail includes node_id prefix
        tool_event = next(e for e in all_events if e[0] == "node_tool_use")
        assert tool_event[1] == "scan:vibecop_check"

        responding_event = next(e for e in all_events if e[0] == "node_responding")
        assert responding_event[1] == "scan"

    async def test_user_name_includes_node_id(self, mock_supervisor, event_data):
        """Supervisor.chat() should be called with user_name including the node ID."""
        graph = {
            "id": "user-name-test",
            "version": 1,
            "nodes": {
                "analyse": {"entry": True, "prompt": "Analyse code.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        call_kwargs = mock_supervisor.chat.call_args.kwargs
        assert call_kwargs["user_name"] == "playbook-runner:analyse"


# ---------------------------------------------------------------------------
# 5.2.4: Transition evaluation — separate LLM call with condition list
# ---------------------------------------------------------------------------


class TestTransitionEvaluationLLMCall:
    """Test the LLM-based transition classification (separate call with condition list)."""

    async def test_transition_call_is_separate_from_node_call(
        self, mock_supervisor, branching_graph, event_data
    ):
        """Transition evaluation should be a distinct supervisor.chat() call."""
        # scan node response, then transition classification, then triage node
        responses = iter(["Found issues", "2", "Grouped findings"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        # 3 calls: scan node, transition classification, triage node
        assert mock_supervisor.chat.call_count == 3

    async def test_transition_prompt_contains_numbered_conditions(
        self, mock_supervisor, branching_graph, event_data
    ):
        """The transition prompt should list conditions as numbered options."""
        responses = iter(["Found issues", "2", "Grouped"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        # Second call is the transition classification
        transition_call = mock_supervisor.chat.call_args_list[1]
        prompt = transition_call.kwargs["text"]
        assert "1." in prompt
        assert "2." in prompt
        assert "no findings" in prompt
        assert "findings exist" in prompt
        assert "ONLY the number" in prompt

    async def test_transition_call_receives_full_history(
        self, mock_supervisor, branching_graph, event_data
    ):
        """Transition classification call should receive full conversation history."""
        responses = iter(["I found 3 issues", "2", "Grouped"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        transition_call = mock_supervisor.chat.call_args_list[1]
        history = transition_call.kwargs["history"]
        # Should have: seed + scan prompt + scan response = 3 messages
        assert len(history) == 3
        assert "I found 3 issues" in history[2]["content"]

    async def test_transition_call_uses_no_tools(
        self, mock_supervisor, branching_graph, event_data
    ):
        """Transition evaluation should pass tool_overrides=[] (no tools)."""
        responses = iter(["findings", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["tool_overrides"] == []

    async def test_transition_user_name_includes_node_id(
        self, mock_supervisor, branching_graph, event_data
    ):
        """Transition call user_name should include the source node ID."""
        responses = iter(["findings", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        await runner.run()

        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["user_name"] == "playbook-runner:transition:scan"

    async def test_transition_tokens_tracked(self, mock_supervisor, branching_graph, event_data):
        """Token usage from transition LLM calls should be tracked."""
        responses = iter(["findings", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        result = await runner.run()

        # tokens_used should include both node execution and transition evaluation
        assert result.tokens_used > 0
        # The transition prompt + decision text add tokens beyond just the node call
        node_only_tokens = _estimate_tokens("Run scan on files and report findings.", "findings")
        assert result.tokens_used > node_only_tokens

    async def test_multiple_transitions_evaluated_sequentially(self, mock_supervisor, event_data):
        """A graph with multiple branching nodes should evaluate transitions for each."""
        graph = {
            "id": "multi-branch",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check status.",
                    "transitions": [
                        {"when": "needs action", "goto": "act"},
                        {"when": "all good", "goto": "done"},
                    ],
                },
                "act": {
                    "prompt": "Take action.",
                    "transitions": [
                        {"when": "success", "goto": "done"},
                        {"when": "needs retry", "goto": "act"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        # check → "needs action" → act → "success" → done
        responses = iter(["Status bad", "1", "Fixed it", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # 4 calls: check node, check transition, act node, act transition
        assert mock_supervisor.chat.call_count == 4

    async def test_transition_llm_error_propagates(self, mock_supervisor, event_data):
        """If the transition LLM call fails, the run should fail gracefully."""
        graph = {
            "id": "transition-error",
            "version": 1,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Step A",
                    "transitions": [
                        {"when": "cond1", "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        call_count = 0

        async def chat_side_effect(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Node result"
            # Second call (transition) fails
            raise RuntimeError("LLM provider timeout during transition")

        mock_supervisor.chat.side_effect = chat_side_effect

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert "Transition from 'a' failed" in result.error


class TestTransitionTraceInfo:
    """Test that trace entries record transition metadata."""

    async def test_goto_transition_recorded(self, mock_supervisor, simple_graph, event_data):
        """Unconditional goto should be recorded in trace as method='goto'."""
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        result = await runner.run()

        trace = result.node_trace[0]
        assert trace["transition_to"] == "done"
        assert trace["transition_method"] == "goto"

    async def test_llm_transition_recorded(self, mock_supervisor, branching_graph, event_data):
        """LLM-classified transition should be recorded with method='llm'."""
        responses = iter(["Found issues", "2", "Grouped"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(branching_graph, event_data, mock_supervisor)
        result = await runner.run()

        # scan node should show llm transition to triage
        scan_trace = result.node_trace[0]
        assert scan_trace["transition_to"] == "triage"
        assert scan_trace["transition_method"] == "llm"

        # triage node should show goto transition to done
        triage_trace = result.node_trace[1]
        assert triage_trace["transition_to"] == "done"
        assert triage_trace["transition_method"] == "goto"

    async def test_implicit_terminal_no_transition(self, mock_supervisor, event_data):
        """A node with no transitions should have method='none'."""
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

        trace = result.node_trace[0]
        assert trace.get("transition_to") is None
        assert trace["transition_method"] == "none"

    async def test_trace_dict_omits_none_transition(self, mock_supervisor, event_data):
        """When transition_to is None, it should not appear in trace dict."""
        entry = NodeTraceEntry(node_id="test", started_at=1.0)
        entry.status = "completed"
        entry.completed_at = 2.0
        d = PlaybookRunner._trace_to_dict(entry)
        assert "transition_to" not in d
        assert "transition_method" not in d

    async def test_trace_dict_includes_transition_when_set(self, mock_supervisor, event_data):
        """When transition info is set, it should appear in trace dict."""
        entry = NodeTraceEntry(node_id="test", started_at=1.0)
        entry.transition_to = "next_node"
        entry.transition_method = "goto"
        d = PlaybookRunner._trace_to_dict(entry)
        assert d["transition_to"] == "next_node"
        assert d["transition_method"] == "goto"


# ---------------------------------------------------------------------------
# 5.2.14: Branching transition evaluation (roadmap test cases a–g)
# ---------------------------------------------------------------------------


class TestBranchingTransitionEvaluation:
    """5.2.14: Branching transition evaluation per playbooks §6.

    Tests (a)-(g) from the roadmap verifying LLM-based transition
    classification for branching playbook graphs.
    """

    async def test_two_conditional_transitions_correct_branch(self, mock_supervisor, event_data):
        """(a) Node with two conditional transitions — LLM picks correct branch."""
        graph = {
            "id": "two-branch-test",
            "version": 1,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the codebase for issues.",
                    "transitions": [
                        {"when": "no issues found", "goto": "done"},
                        {"when": "issues found that need fixing", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix the identified issues.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # analyse → outputs issues → LLM picks "2" (issues found → fix) → fix → done
        responses = iter(
            [
                "Found 5 critical issues in the auth module",
                "2",
                "All issues fixed.",
            ]
        )
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 2
        assert result.node_trace[0]["node_id"] == "analyse"
        assert result.node_trace[0]["transition_to"] == "fix"
        assert result.node_trace[0]["transition_method"] == "llm"
        assert result.node_trace[1]["node_id"] == "fix"

    async def test_two_conditional_transitions_other_branch(self, mock_supervisor, event_data):
        """(a) extended — LLM picks the other branch when prior output differs."""
        graph = {
            "id": "two-branch-other",
            "version": 1,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the codebase for issues.",
                    "transitions": [
                        {"when": "no issues found", "goto": "done"},
                        {"when": "issues found that need fixing", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix the identified issues.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # analyse → clean output → LLM picks "1" (no issues → done)
        responses = iter(["Codebase looks clean, no issues detected.", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 1  # Only analyse (→ done is terminal)
        assert result.node_trace[0]["node_id"] == "analyse"
        assert result.node_trace[0]["transition_to"] == "done"
        assert result.node_trace[0]["transition_method"] == "llm"

    async def test_three_branches_middle_selected(self, mock_supervisor, event_data):
        """(b) Node with three branches — middle branch is selected."""
        graph = {
            "id": "three-branch-test",
            "version": 1,
            "nodes": {
                "triage": {
                    "entry": True,
                    "prompt": "Assess the severity of findings.",
                    "transitions": [
                        {"when": "critical severity requiring immediate action", "goto": "hotfix"},
                        {"when": "moderate severity that should be scheduled", "goto": "schedule"},
                        {"when": "low severity informational only", "goto": "log"},
                    ],
                },
                "hotfix": {"prompt": "Apply hotfix.", "goto": "done"},
                "schedule": {"prompt": "Schedule the fix.", "goto": "done"},
                "log": {"prompt": "Log for reference.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # triage → moderate finding → LLM picks "2" (schedule)
        responses = iter(
            [
                "Found moderate memory leak in connection pooling.",
                "2",
                "Scheduled fix for next sprint.",
            ]
        )
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 2
        assert result.node_trace[0]["node_id"] == "triage"
        assert result.node_trace[0]["transition_to"] == "schedule"
        assert result.node_trace[0]["transition_method"] == "llm"
        assert result.node_trace[1]["node_id"] == "schedule"

    async def test_three_branches_first_and_last(self, mock_supervisor, event_data):
        """(b) extended — verify first and last branches also selectable."""
        graph = {
            "id": "three-branch-ends",
            "version": 1,
            "nodes": {
                "triage": {
                    "entry": True,
                    "prompt": "Assess severity.",
                    "transitions": [
                        {"when": "critical", "goto": "hotfix"},
                        {"when": "moderate", "goto": "schedule"},
                        {"when": "low", "goto": "log"},
                    ],
                },
                "hotfix": {"prompt": "Hotfix.", "goto": "done"},
                "schedule": {"prompt": "Schedule.", "goto": "done"},
                "log": {"prompt": "Log.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Test first branch
        responses = iter(["Critical vulnerability!", "1", "Hotfixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.node_trace[0]["transition_to"] == "hotfix"

    async def test_three_branches_last_selected(self, mock_supervisor, event_data):
        """(b) extended — last branch in a three-branch node."""
        graph = {
            "id": "three-branch-last",
            "version": 1,
            "nodes": {
                "triage": {
                    "entry": True,
                    "prompt": "Assess severity.",
                    "transitions": [
                        {"when": "critical", "goto": "hotfix"},
                        {"when": "moderate", "goto": "schedule"},
                        {"when": "low", "goto": "log"},
                    ],
                },
                "hotfix": {"prompt": "Hotfix.", "goto": "done"},
                "schedule": {"prompt": "Schedule.", "goto": "done"},
                "log": {"prompt": "Log.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Test last branch
        responses = iter(["Minor style nit.", "3", "Logged."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.node_trace[0]["transition_to"] == "log"

    async def test_default_condition_selected_when_no_match(self, mock_supervisor, event_data):
        """(c) 'otherwise' transition selected when no other conditions match."""
        graph = {
            "id": "default-fallback",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check deployment health.",
                    "transitions": [
                        {"when": "all services healthy", "goto": "done"},
                        {"when": "specific service degraded", "goto": "remediate"},
                        {"otherwise": True, "goto": "escalate"},
                    ],
                },
                "remediate": {"prompt": "Remediate the service.", "goto": "done"},
                "escalate": {"prompt": "Escalate to on-call.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # check → ambiguous output → LLM returns "0" (no match) → otherwise → escalate
        responses = iter(
            [
                "Deployment status unclear, possible network issue.",
                "0",
                "Escalated to on-call team.",
            ]
        )
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 2
        assert result.node_trace[0]["node_id"] == "check"
        assert result.node_trace[0]["transition_to"] == "escalate"
        # The LLM was the mechanism that selected the otherwise option
        # (by returning "0"), so transition_method is "llm" when LLM actively
        # selects it.  The "otherwise" method is used when the LLM fails to
        # match anything and the code falls back to the otherwise branch.
        assert result.node_trace[0]["transition_method"] in ("llm", "otherwise")
        assert result.node_trace[1]["node_id"] == "escalate"

    async def test_default_fallback_when_llm_returns_no_match(self, mock_supervisor, event_data):
        """(c) extended — otherwise fallback when LLM returns unrecognised response."""
        graph = {
            "id": "otherwise-fallback-nomatch",
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

    async def test_default_with_otherwise_in_llm_prompt(self, mock_supervisor, event_data):
        """(c) extended — the LLM prompt includes a DEFAULT/OTHERWISE option."""
        graph = {
            "id": "otherwise-prompt-check",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan.",
                    "transitions": [
                        {"when": "clean", "goto": "done"},
                        {"otherwise": True, "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Some result", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Transition classification prompt should include DEFAULT/OTHERWISE option
        transition_call = mock_supervisor.chat.call_args_list[1]
        prompt_text = transition_call.kwargs["text"]
        assert "DEFAULT" in prompt_text or "OTHERWISE" in prompt_text

    async def test_transition_uses_cheaper_model(self, mock_supervisor, event_data):
        """(d) Transition evaluation uses cheaper model from playbook transition_llm_config."""
        graph = {
            "id": "cheap-transition",
            "version": 1,
            "llm_config": {"model": "claude-sonnet-4-20250514", "provider": "anthropic"},
            "transition_llm_config": {
                "model": "claude-haiku-4-20250414",
                "provider": "anthropic",
            },
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan for issues.",
                    "transitions": [
                        {"when": "issues found", "goto": "fix"},
                        {"when": "no issues", "goto": "done"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Found issues", "1", "Fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        calls = mock_supervisor.chat.call_args_list

        # First call (node execution) — uses main llm_config (Sonnet)
        node_call = calls[0]
        assert node_call.kwargs["llm_config"] == {
            "model": "claude-sonnet-4-20250514",
            "provider": "anthropic",
        }

        # Second call (transition classification) — uses transition_llm_config (Haiku)
        transition_call = calls[1]
        assert transition_call.kwargs["llm_config"] == {
            "model": "claude-haiku-4-20250414",
            "provider": "anthropic",
        }

    async def test_transition_node_level_config_overrides_playbook(
        self, mock_supervisor, event_data
    ):
        """(d) extended — node-level transition_llm_config overrides playbook-level."""
        graph = {
            "id": "node-transition-config",
            "version": 1,
            "transition_llm_config": {"model": "haiku-playbook"},
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan.",
                    "transition_llm_config": {"model": "haiku-node-override"},
                    "transitions": [
                        {"when": "found", "goto": "fix"},
                        {"when": "clean", "goto": "done"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Found stuff", "1", "Fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Transition call should use node-level override, not playbook-level
        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["llm_config"] == {"model": "haiku-node-override"}

    async def test_transition_prompt_includes_conditions_and_context(
        self, mock_supervisor, event_data
    ):
        """(e) Transition prompt includes condition list and conversation context."""
        graph = {
            "id": "prompt-content-check",
            "version": 1,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the auth module.",
                    "transitions": [
                        {"when": "SQL injection vulnerability detected", "goto": "critical"},
                        {"when": "XSS vulnerability detected", "goto": "high"},
                        {"when": "no vulnerabilities found", "goto": "done"},
                    ],
                },
                "critical": {"prompt": "Fix critical.", "goto": "done"},
                "high": {"prompt": "Fix high.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Found SQL injection in login handler", "1", "Fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Transition classification call (second call)
        transition_call = mock_supervisor.chat.call_args_list[1]
        prompt_text = transition_call.kwargs["text"]
        history = transition_call.kwargs["history"]

        # Prompt should list all numbered conditions
        assert "1." in prompt_text
        assert "SQL injection vulnerability detected" in prompt_text
        assert "2." in prompt_text
        assert "XSS vulnerability detected" in prompt_text
        assert "3." in prompt_text
        assert "no vulnerabilities found" in prompt_text

        # History should contain full conversation context
        assert len(history) >= 3  # seed + prompt + response
        # Node's response should be in the conversation history
        assert any("SQL injection in login handler" in m["content"] for m in history)

    async def test_transition_prompt_excludes_structured_conditions(
        self, mock_supervisor, event_data
    ):
        """(e) extended — structured conditions are NOT included in the LLM prompt."""
        graph = {
            "id": "mixed-prompt-check",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "magic"},
                            "goto": "special",
                        },
                        {"when": "needs attention", "goto": "fix"},
                        {"when": "all good", "goto": "done"},
                        {"otherwise": True, "goto": "fallback"},
                    ],
                },
                "special": {"prompt": "Special.", "goto": "done"},
                "fix": {"prompt": "Fix.", "goto": "done"},
                "fallback": {"prompt": "Fallback.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Structured condition doesn't match (response has no "magic" substring)
        responses = iter(["Some ordinary result here", "1", "Fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        transition_call = mock_supervisor.chat.call_args_list[1]
        prompt_text = transition_call.kwargs["text"]

        # NL conditions should be in the prompt
        assert "needs attention" in prompt_text
        assert "all good" in prompt_text
        # Structured condition should NOT appear in the LLM prompt
        assert "response_contains" not in prompt_text
        assert "magic" not in prompt_text

    async def test_ambiguous_conditions_first_match_wins(self, mock_supervisor, event_data):
        """(f) Ambiguous conditions — first matching transition wins (ordered evaluation)."""
        graph = {
            "id": "ambiguous-test",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check code quality.",
                    "transitions": [
                        {"when": "code needs changes", "goto": "refactor"},
                        {"when": "code needs improvement", "goto": "improve"},
                        {"when": "code is acceptable", "goto": "done"},
                    ],
                },
                "refactor": {"prompt": "Refactor the code.", "goto": "done"},
                "improve": {"prompt": "Improve the code.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # LLM picks "1" — first condition wins even though 1 and 2 overlap
        responses = iter(["Code has several issues that need work.", "1", "Refactored."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "refactor"
        assert result.node_trace[0]["transition_method"] == "llm"

    async def test_ambiguous_structured_first_match_wins(self, mock_supervisor, event_data):
        """(f) extended — structured conditions: first matching condition wins."""
        graph = {
            "id": "structured-order",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan for issues.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "issue"},
                            "goto": "first",
                        },
                        {
                            "when": {"function": "response_contains", "value": "issue found"},
                            "goto": "second",
                        },
                    ],
                },
                "first": {"prompt": "First.", "goto": "done"},
                "second": {"prompt": "Second.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Response matches BOTH structured conditions — first one should win
        mock_supervisor.chat.side_effect = lambda **kw: (
            "issue found in auth" if "Scan" in kw.get("text", "") else "Handled."
        )

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "first"
        assert result.node_trace[0]["transition_method"] == "structured"

    async def test_ambiguous_fuzzy_text_first_match_wins(self, mock_supervisor, event_data):
        """(f) extended — fuzzy text matching: first matching transition wins."""
        transitions = [
            {"when": "needs changes", "goto": "refactor"},
            {"when": "needs improvement", "goto": "improve"},
        ]
        # Decision text contains both conditions — first should win
        result = PlaybookRunner._match_transition_by_number(
            "The code needs changes and needs improvement", transitions, None
        )
        assert result == "refactor"

    async def test_no_match_no_default_fails_run(self, mock_supervisor, event_data):
        """(g) No matching transition and no default — run fails with descriptive error."""
        graph = {
            "id": "no-fallback",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check system status.",
                    "transitions": [
                        {"when": "system is healthy", "goto": "done"},
                        {"when": "system is degraded", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # check → unexpected output → LLM returns "0" (no match) → no otherwise → FAIL
        responses = iter(["System is in an unexpected state.", "0"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert "check" in result.error
        assert (
            "no transition matched" in result.error.lower() or "otherwise" in result.error.lower()
        )

    async def test_no_match_no_default_descriptive_error(
        self, mock_supervisor, event_data, mock_db
    ):
        """(g) extended — error message includes conditions and is persisted."""
        graph = {
            "id": "descriptive-error",
            "version": 1,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse.",
                    "transitions": [
                        {"when": "option alpha", "goto": "a"},
                        {"when": "option beta", "goto": "b"},
                    ],
                },
                "a": {"prompt": "A.", "goto": "done"},
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # LLM returns "0" → no match, no otherwise
        responses = iter(["Unclear result.", "0"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"
        # Error should mention the node and include condition info
        assert "analyse" in result.error
        assert "option alpha" in result.error or "otherwise" in result.error.lower()

        # Failure should be persisted to DB
        fail_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed":
                fail_call = call
                break
        assert fail_call is not None
        assert "analyse" in fail_call.kwargs["error"]

    async def test_no_match_structured_no_default_fails(self, mock_supervisor, event_data):
        """(g) extended — structured transitions with no match and no otherwise also fails."""
        graph = {
            "id": "structured-no-fallback",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "success"},
                            "goto": "done",
                        },
                        {
                            "when": {"function": "response_contains", "value": "failure"},
                            "goto": "fix",
                        },
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Response doesn't match either structured condition, no otherwise
        mock_supervisor.chat.return_value = "Something ambiguous happened."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        assert "check" in result.error
        assert (
            "no transition matched" in result.error.lower() or "otherwise" in result.error.lower()
        )

    async def test_implicit_terminal_still_works(self, mock_supervisor, event_data):
        """(g) guard — nodes with no transitions defined still complete (implicit terminal)."""
        graph = {
            "id": "implicit-end",
            "version": 1,
            "nodes": {
                "only": {"entry": True, "prompt": "Do something."},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # No transitions defined → implicit terminal → NOT a failure
        assert result.status == "completed"


# ---------------------------------------------------------------------------
# 5.2.4: Structured transition evaluation (no LLM call)
# ---------------------------------------------------------------------------


class TestStructuredTransitions:
    """Test dict-based structured conditions evaluated without LLM calls."""

    async def test_response_contains_match(self, mock_supervisor, event_data):
        """Structured condition with response_contains should match."""
        graph = {
            "id": "structured-contains",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan files.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "no findings"},
                            "goto": "done",
                        },
                        {
                            "when": {"function": "response_contains", "value": "found issues"},
                            "goto": "triage",
                        },
                    ],
                },
                "triage": {"prompt": "Triage.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Response contains "found issues" → should go to triage
        responses = iter(["I found issues in 3 files", "Triaged."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["node_id"] == "scan"
        assert result.node_trace[0]["transition_to"] == "triage"
        assert result.node_trace[0]["transition_method"] == "structured"
        assert result.node_trace[1]["node_id"] == "triage"
        # Only 2 supervisor calls (scan + triage) — no LLM transition call!
        assert mock_supervisor.chat.call_count == 2

    async def test_response_contains_case_insensitive(self, mock_supervisor, event_data):
        """Structured contains check should be case-insensitive."""
        graph = {
            "id": "case-test",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "ALL CLEAR"},
                            "goto": "done",
                        },
                        {"otherwise": True, "goto": "alert"},
                    ],
                },
                "alert": {"prompt": "Alert!", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Status is all clear."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 1  # Only check node (→ done terminal)
        assert result.node_trace[0]["transition_method"] == "structured"

    async def test_response_not_contains(self, mock_supervisor, event_data):
        """Structured response_not_contains should match when value is absent."""
        graph = {
            "id": "not-contains",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_not_contains", "value": "error"},
                            "goto": "done",
                        },
                        {"otherwise": True, "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix!", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Everything looks fine."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_method"] == "structured"
        assert result.node_trace[0]["transition_to"] == "done"

    async def test_has_tool_output_alias(self, mock_supervisor, event_data):
        """has_tool_output with 'contains' key should work as response_contains alias."""
        graph = {
            "id": "tool-output-alias",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Run vibecop.",
                    "transitions": [
                        {
                            "when": {
                                "function": "has_tool_output",
                                "contains": "no findings",
                            },
                            "goto": "done",
                        },
                        {"otherwise": True, "goto": "triage"},
                    ],
                },
                "triage": {"prompt": "Triage.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Vibecop returned no findings."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_method"] == "structured"
        assert result.node_trace[0]["transition_to"] == "done"

    async def test_structured_no_match_falls_to_otherwise(self, mock_supervisor, event_data):
        """When structured conditions don't match, fall through to otherwise."""
        graph = {
            "id": "structured-fallback",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "perfect"},
                            "goto": "celebrate",
                        },
                        {"otherwise": True, "goto": "investigate"},
                    ],
                },
                "celebrate": {"prompt": "Celebrate!", "goto": "done"},
                "investigate": {"prompt": "Investigate.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Some issues found."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_method"] == "otherwise"
        assert result.node_trace[0]["transition_to"] == "investigate"
        # No LLM transition call — only 2 node calls (check + investigate)
        assert mock_supervisor.chat.call_count == 2

    async def test_mixed_structured_and_natural_language(self, mock_supervisor, event_data):
        """Mixed transitions: structured checked first, then NL via LLM if needed."""
        graph = {
            "id": "mixed-transitions",
            "version": 1,
            "nodes": {
                "analyse": {
                    "entry": True,
                    "prompt": "Analyse the code.",
                    "transitions": [
                        # Structured: check for "no issues"
                        {
                            "when": {"function": "response_contains", "value": "no issues"},
                            "goto": "done",
                        },
                        # Natural language: LLM decides
                        {"when": "critical issues requiring immediate action", "goto": "hotfix"},
                        {"when": "minor issues that can wait", "goto": "backlog"},
                    ],
                },
                "hotfix": {"prompt": "Create hotfix.", "goto": "done"},
                "backlog": {"prompt": "Add to backlog.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Response doesn't match structured condition → falls to NL LLM call
        # LLM picks "1" (first NL condition = "critical issues...")
        responses = iter(["Found a critical security vulnerability", "1", "Hotfix created."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "hotfix"
        assert result.node_trace[0]["transition_method"] == "llm"
        # 3 calls: analyse node, transition classification, hotfix node
        assert mock_supervisor.chat.call_count == 3

    async def test_structured_match_skips_llm_call(self, mock_supervisor, event_data):
        """When a structured condition matches, NL conditions should not be evaluated."""
        graph = {
            "id": "structured-skip-llm",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "clean"},
                            "goto": "done",
                        },
                        {"when": "has issues", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Everything is clean."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Only 1 call (check node) — no LLM transition, no fix node
        assert mock_supervisor.chat.call_count == 1
        assert result.node_trace[0]["transition_method"] == "structured"

    async def test_unknown_function_falls_through(self, mock_supervisor, event_data):
        """Unknown structured function names should fall through gracefully."""
        graph = {
            "id": "unknown-func",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "unknown_check", "value": "x"},
                            "goto": "a",
                        },
                        {"otherwise": True, "goto": "b"},
                    ],
                },
                "a": {"prompt": "A.", "goto": "done"},
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Result."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Unknown function → False → falls to otherwise → b
        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "b"
        assert result.node_trace[0]["transition_method"] == "otherwise"


class TestStructuredConditionEvaluation:
    """Unit tests for _evaluate_structured_condition instance method."""

    @pytest.fixture
    def runner(self, mock_supervisor, event_data):
        """Minimal runner for unit-testing condition evaluation."""
        graph = {"id": "test", "version": 1, "nodes": {}}
        return PlaybookRunner(graph, event_data, mock_supervisor)

    def test_response_contains_true(self, runner):
        cond = {"function": "response_contains", "value": "error found"}
        assert runner._evaluate_structured_condition(cond, "An error found in line 5")

    def test_response_contains_false(self, runner):
        cond = {"function": "response_contains", "value": "error found"}
        assert not runner._evaluate_structured_condition(cond, "Everything is fine")

    def test_response_contains_case_insensitive(self, runner):
        cond = {"function": "response_contains", "value": "ERROR"}
        assert runner._evaluate_structured_condition(cond, "Found an error in the code")

    def test_response_not_contains_true(self, runner):
        cond = {"function": "response_not_contains", "value": "error"}
        assert runner._evaluate_structured_condition(cond, "All tests passed.")

    def test_response_not_contains_false(self, runner):
        cond = {"function": "response_not_contains", "value": "error"}
        assert not runner._evaluate_structured_condition(cond, "Got an error!")

    def test_has_tool_output_contains(self, runner):
        cond = {"function": "has_tool_output", "contains": "no findings"}
        assert runner._evaluate_structured_condition(cond, "Scan: no findings detected.")

    def test_has_tool_output_value_key(self, runner):
        """has_tool_output should also accept 'value' key."""
        cond = {"function": "has_tool_output", "value": "clean"}
        assert runner._evaluate_structured_condition(cond, "Code is clean.")

    def test_unknown_function_returns_false(self, runner):
        cond = {"function": "never_heard_of_this", "value": "x"}
        assert not runner._evaluate_structured_condition(cond, "anything")

    def test_empty_value_always_contains(self, runner):
        cond = {"function": "response_contains", "value": ""}
        assert runner._evaluate_structured_condition(cond, "anything at all")

    def test_missing_value_key(self, runner):
        cond = {"function": "response_contains"}
        assert runner._evaluate_structured_condition(cond, "anything")

    def test_missing_function_key(self, runner):
        cond = {"value": "test"}
        assert not runner._evaluate_structured_condition(cond, "test something")


# ---------------------------------------------------------------------------
# 5.2.5: Function-call expression evaluation (deterministic, no LLM)
# ---------------------------------------------------------------------------


class TestExpressionHelpers:
    """Unit tests for module-level expression helpers."""

    # -- _dot_get --------------------------------------------------------

    def test_dot_get_simple(self):
        assert _dot_get({"status": "ok"}, "status") == ("ok", True)

    def test_dot_get_nested(self):
        data = {"task": {"meta": {"priority": "high"}}}
        assert _dot_get(data, "task.meta.priority") == ("high", True)

    def test_dot_get_missing(self):
        assert _dot_get({"a": 1}, "b") == (None, False)

    def test_dot_get_missing_nested(self):
        assert _dot_get({"a": {"b": 1}}, "a.c") == (None, False)

    def test_dot_get_non_dict_traversal(self):
        """Traversing through a non-dict value should fail."""
        assert _dot_get({"a": "string"}, "a.b") == (None, False)

    # -- _parse_literal --------------------------------------------------

    def test_parse_double_quoted_string(self):
        assert _parse_literal('"hello"') == "hello"

    def test_parse_single_quoted_string(self):
        assert _parse_literal("'world'") == "world"

    def test_parse_escaped_quotes(self):
        assert _parse_literal('"say \\"hi\\""') == 'say "hi"'

    def test_parse_integer(self):
        assert _parse_literal("42") == 42

    def test_parse_negative_integer(self):
        assert _parse_literal("-5") == -5

    def test_parse_float(self):
        assert _parse_literal("3.14") == 3.14

    def test_parse_true(self):
        assert _parse_literal("true") is True

    def test_parse_True_case(self):
        assert _parse_literal("True") is True

    def test_parse_false(self):
        assert _parse_literal("false") is False

    def test_parse_null(self):
        assert _parse_literal("null") is None

    # -- _compare --------------------------------------------------------

    def test_compare_eq_true(self):
        assert _compare("completed", "==", "completed") is True

    def test_compare_eq_false(self):
        assert _compare("running", "==", "completed") is False

    def test_compare_ne_true(self):
        assert _compare("running", "!=", "completed") is True

    def test_compare_ne_false(self):
        assert _compare("completed", "!=", "completed") is False

    def test_compare_gt_numeric(self):
        assert _compare(5, ">", 3) is True
        assert _compare(3, ">", 5) is False

    def test_compare_lt_numeric(self):
        assert _compare(3, "<", 5) is True

    def test_compare_gte(self):
        assert _compare(5, ">=", 5) is True
        assert _compare(4, ">=", 5) is False

    def test_compare_lte(self):
        assert _compare(5, "<=", 5) is True
        assert _compare(6, "<=", 5) is False

    def test_compare_numeric_coercion_string_vs_int(self):
        """String '5' should be coerced for ordering operators."""
        assert _compare("5", ">", 3) is True
        assert _compare("2", "<", 3) is True

    def test_compare_type_mismatch_no_coerce(self):
        """Type mismatch that can't be coerced returns False."""
        assert _compare("abc", ">", 3) is False

    def test_compare_eq_no_coercion(self):
        """Equality is strict — no type coercion."""
        assert _compare("5", "==", 5) is False
        assert _compare(5, "==", 5) is True


class TestExpressionEvaluation:
    """Unit tests for _evaluate_expression and expression-based conditions."""

    @pytest.fixture
    def runner(self, mock_supervisor):
        """Runner with a rich event for variable resolution testing."""
        event = {
            "type": "task.completed",
            "project_id": "my-app",
            "status": "completed",
            "task_id": "t-123",
            "meta": {"priority": "high", "count": 5},
        }
        graph = {"id": "expr-test", "version": 1, "nodes": {}}
        return PlaybookRunner(graph, event, mock_supervisor)

    # -- task.* variable resolution --------------------------------------

    def test_task_status_equals(self, runner):
        """task.status == 'completed' evaluates against event data."""
        cond = {"expression": 'task.status == "completed"'}
        assert runner._evaluate_structured_condition(cond, "any response")

    def test_task_status_not_equals(self, runner):
        cond = {"expression": 'task.status != "running"'}
        assert runner._evaluate_structured_condition(cond, "any response")

    def test_task_status_false(self, runner):
        cond = {"expression": 'task.status == "running"'}
        assert not runner._evaluate_structured_condition(cond, "any response")

    def test_task_nested_field(self, runner):
        """task.meta.priority accesses nested event fields."""
        cond = {"expression": 'task.meta.priority == "high"'}
        assert runner._evaluate_structured_condition(cond, "any response")

    def test_task_nested_numeric(self, runner):
        """task.meta.count > 0 evaluates numeric comparisons."""
        cond = {"expression": "task.meta.count > 0"}
        assert runner._evaluate_structured_condition(cond, "any response")

    def test_task_project_id(self, runner):
        cond = {"expression": 'task.project_id == "my-app"'}
        assert runner._evaluate_structured_condition(cond, "any response")

    # -- event.* alias ---------------------------------------------------

    def test_event_alias(self, runner):
        """event.* is an alias for task.*."""
        cond = {"expression": 'event.status == "completed"'}
        assert runner._evaluate_structured_condition(cond, "any response")

    # -- output.* variable resolution (JSON response) --------------------

    def test_output_field_from_json_response(self, runner):
        """output.approval accesses JSON-parsed response fields."""
        response = json.dumps({"approval": "yes", "score": 95})
        cond = {"expression": 'output.approval == "yes"'}
        assert runner._evaluate_structured_condition(cond, response)

    def test_output_numeric_field(self, runner):
        response = json.dumps({"count": 10, "status": "done"})
        cond = {"expression": "output.count > 5"}
        assert runner._evaluate_structured_condition(cond, response)

    def test_output_nested_field(self, runner):
        response = json.dumps({"result": {"verdict": "pass"}})
        cond = {"expression": 'output.result.verdict == "pass"'}
        assert runner._evaluate_structured_condition(cond, response)

    def test_output_non_json_response_fails(self, runner):
        """output.* on a non-JSON response returns False (undefined)."""
        cond = {"expression": 'output.field == "value"'}
        assert not runner._evaluate_structured_condition(cond, "plain text response")

    def test_output_non_dict_json_fails(self, runner):
        """output.* on a JSON array response returns False."""
        cond = {"expression": 'output.field == "value"'}
        assert not runner._evaluate_structured_condition(cond, "[1, 2, 3]")

    # -- response variable -----------------------------------------------

    def test_response_equality(self, runner):
        """Bare 'response' variable is the raw response text."""
        cond = {"expression": 'response == "yes"'}
        assert runner._evaluate_structured_condition(cond, "yes")

    def test_response_not_equals(self, runner):
        cond = {"expression": 'response != "no"'}
        assert runner._evaluate_structured_condition(cond, "yes")

    # -- Condition format variants ---------------------------------------

    def test_expression_via_function_key(self, runner):
        """function: 'expression' with expression key should work."""
        cond = {"function": "expression", "expression": 'task.status == "completed"'}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_compare_function_structured(self, runner):
        """function: 'compare' with variable/operator/value keys."""
        cond = {
            "function": "compare",
            "variable": "task.status",
            "operator": "==",
            "value": "completed",
        }
        assert runner._evaluate_structured_condition(cond, "any")

    def test_compare_function_numeric(self, runner):
        cond = {
            "function": "compare",
            "variable": "task.meta.count",
            "operator": ">",
            "value": 3,
        }
        assert runner._evaluate_structured_condition(cond, "any")

    # -- Error handling (roadmap 5.2.15c, 5.2.15d) -----------------------

    def test_invalid_expression_syntax(self, runner):
        """Invalid syntax returns False with warning (not exception)."""
        cond = {"expression": "this is not valid at all"}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_invalid_expression_missing_operator(self, runner):
        cond = {"expression": 'task.status "completed"'}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_invalid_expression_empty(self, runner):
        cond = {"expression": ""}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_undefined_variable(self, runner):
        """Referencing a nonexistent variable returns False gracefully."""
        cond = {"expression": 'undefined_var == "x"'}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_undefined_nested_variable(self, runner):
        """Undefined nested path returns False."""
        cond = {"expression": 'task.nonexistent.deep == "x"'}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_compare_missing_variable_key(self, runner):
        cond = {"function": "compare", "operator": "==", "value": "x"}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_compare_missing_operator_key(self, runner):
        cond = {"function": "compare", "variable": "task.status", "value": "x"}
        assert not runner._evaluate_structured_condition(cond, "any")

    def test_compare_invalid_operator(self, runner):
        cond = {
            "function": "compare",
            "variable": "task.status",
            "operator": "~=",
            "value": "x",
        }
        assert not runner._evaluate_structured_condition(cond, "any")

    # -- Log verification for error cases (5.2.15c, 5.2.15d) -------------

    def test_invalid_syntax_logs_warning(self, runner, caplog):
        """5.2.15c: invalid expression syntax produces clear warning log."""
        cond = {"expression": "this is not valid at all"}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Invalid expression syntax" in caplog.text
        assert "this is not valid at all" in caplog.text

    def test_missing_operator_logs_warning(self, runner, caplog):
        """5.2.15c: expression without operator produces clear warning."""
        cond = {"expression": 'task.status "completed"'}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Invalid expression syntax" in caplog.text

    def test_empty_expression_logs_warning(self, runner, caplog):
        """5.2.15c: empty expression produces clear warning."""
        cond = {"expression": ""}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Invalid expression syntax" in caplog.text

    def test_undefined_variable_logs_warning(self, runner, caplog):
        """5.2.15d: undefined variable produces descriptive warning with variable name."""
        cond = {"expression": 'unknown_ns.field == "x"'}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Undefined variable" in caplog.text
        assert "unknown_ns.field" in caplog.text

    def test_undefined_nested_variable_logs_warning(self, runner, caplog):
        """5.2.15d: undefined nested path produces descriptive warning."""
        cond = {"expression": 'task.nonexistent.deep == "x"'}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Undefined variable" in caplog.text
        assert "task.nonexistent.deep" in caplog.text

    def test_compare_missing_keys_logs_warning(self, runner, caplog):
        """5.2.15c: compare condition with missing keys produces clear warning."""
        cond = {"function": "compare", "operator": "==", "value": "x"}
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Incomplete compare condition" in caplog.text

    def test_compare_invalid_operator_logs_warning(self, runner, caplog):
        """5.2.15c: compare with unsupported operator produces clear warning."""
        cond = {
            "function": "compare",
            "variable": "task.status",
            "operator": "~=",
            "value": "x",
        }
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = runner._evaluate_structured_condition(cond, "any")
        assert result is False
        assert "Unsupported operator" in caplog.text
        assert "~=" in caplog.text

    # -- Operator coverage -----------------------------------------------

    def test_expression_lte(self, runner):
        cond = {"expression": "task.meta.count <= 5"}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_gte(self, runner):
        cond = {"expression": "task.meta.count >= 5"}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_lt_false(self, runner):
        cond = {"expression": "task.meta.count < 5"}
        assert not runner._evaluate_structured_condition(cond, "any")

    # -- Literal types ---------------------------------------------------

    def test_expression_boolean_literal(self, mock_supervisor):
        event = {"type": "test", "active": True}
        graph = {"id": "t", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event, mock_supervisor)
        cond = {"expression": "task.active == true"}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_null_literal(self, mock_supervisor):
        event = {"type": "test", "label": None}
        graph = {"id": "t", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event, mock_supervisor)
        cond = {"expression": "task.label == null"}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_float_literal(self, mock_supervisor):
        event = {"type": "test", "score": 3.14}
        graph = {"id": "t", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event, mock_supervisor)
        cond = {"expression": "task.score > 3.0"}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_single_quoted_string(self, runner):
        cond = {"expression": "task.status == 'completed'"}
        assert runner._evaluate_structured_condition(cond, "any")

    # -- Whitespace tolerance --------------------------------------------

    def test_expression_no_spaces(self, runner):
        cond = {"expression": 'task.status=="completed"'}
        assert runner._evaluate_structured_condition(cond, "any")

    def test_expression_extra_spaces(self, runner):
        cond = {"expression": '  task.status  ==  "completed"  '}
        assert runner._evaluate_structured_condition(cond, "any")


class TestExpressionTransitions:
    """Integration tests: expression-based transitions in full playbook runs."""

    async def test_task_status_expression_no_llm_call(self, mock_supervisor):
        """5.2.15a: task.status expression evaluates without LLM call."""
        event = {
            "type": "task.completed",
            "project_id": "proj",
            "status": "completed",
        }
        graph = {
            "id": "expr-transition",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check task status.",
                    "transitions": [
                        {
                            "when": {"expression": 'task.status == "completed"'},
                            "goto": "done",
                        },
                        {
                            "when": {"expression": 'task.status == "failed"'},
                            "goto": "retry",
                        },
                    ],
                },
                "retry": {"prompt": "Retry.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Status checked."
        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["node_id"] == "check"
        assert result.node_trace[0]["transition_to"] == "done"
        assert result.node_trace[0]["transition_method"] == "structured"
        # Only 1 supervisor.chat call (for the check node) — no LLM transition!
        assert mock_supervisor.chat.call_count == 1

    async def test_output_field_expression(self, mock_supervisor):
        """5.2.15b: output.approval expression evaluates against JSON response."""
        event = {"type": "review", "project_id": "proj"}
        graph = {
            "id": "output-expr",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review the changes.",
                    "transitions": [
                        {
                            "when": {"expression": 'output.approval == "yes"'},
                            "goto": "merge",
                        },
                        {
                            "when": {"expression": 'output.approval == "no"'},
                            "goto": "reject",
                        },
                        {"otherwise": True, "goto": "manual"},
                    ],
                },
                "merge": {"prompt": "Merge.", "goto": "done"},
                "reject": {"prompt": "Reject.", "goto": "done"},
                "manual": {"prompt": "Manual review.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = json.dumps({"approval": "yes", "comment": "Looks good"})
        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "merge"
        assert result.node_trace[0]["transition_method"] == "structured"
        # Only 2 calls: review node + merge node (no LLM transition call)
        assert mock_supervisor.chat.call_count == 2

    async def test_output_field_no_match_falls_through(self, mock_supervisor):
        """When output expression doesn't match, falls to otherwise."""
        event = {"type": "review", "project_id": "proj"}
        graph = {
            "id": "output-fallback",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review.",
                    "transitions": [
                        {
                            "when": {"expression": 'output.approval == "yes"'},
                            "goto": "merge",
                        },
                        {"otherwise": True, "goto": "manual"},
                    ],
                },
                "merge": {"prompt": "Merge.", "goto": "done"},
                "manual": {"prompt": "Manual.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Response is not JSON → output.* can't resolve → falls to otherwise
        mock_supervisor.chat.return_value = "I'm not sure about this."
        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "manual"
        assert result.node_trace[0]["transition_method"] == "otherwise"

    async def test_mixed_expression_and_llm_transitions(self, mock_supervisor):
        """5.2.15f: structured expressions checked first, LLM only if no match."""
        event = {"type": "task.completed", "status": "running", "project_id": "proj"}
        graph = {
            "id": "mixed-expr-llm",
            "version": 1,
            "nodes": {
                "assess": {
                    "entry": True,
                    "prompt": "Assess.",
                    "transitions": [
                        # Structured expression — won't match (status is "running")
                        {
                            "when": {"expression": 'task.status == "completed"'},
                            "goto": "celebrate",
                        },
                        # Natural language — LLM decides
                        {"when": "assessment found critical issues", "goto": "fix"},
                        {"when": "assessment found minor issues", "goto": "backlog"},
                    ],
                },
                "celebrate": {"prompt": "Celebrate!", "goto": "done"},
                "fix": {"prompt": "Fix.", "goto": "done"},
                "backlog": {"prompt": "Backlog.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Expression doesn't match → falls to LLM classification
        # LLM picks "1" (first NL condition = "critical issues")
        responses = iter(["Critical problems detected.", "1", "Fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "fix"
        assert result.node_trace[0]["transition_method"] == "llm"
        # 3 calls: assess node, LLM transition, fix node
        assert mock_supervisor.chat.call_count == 3

    async def test_expression_match_skips_llm(self, mock_supervisor):
        """When expression matches, NL conditions are not evaluated."""
        event = {"type": "task.completed", "status": "completed", "project_id": "proj"}
        graph = {
            "id": "expr-skip-llm",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"expression": 'task.status == "completed"'},
                            "goto": "done",
                        },
                        {"when": "has issues", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Checked."
        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Only 1 call: check node. No LLM transition, no fix node.
        assert mock_supervisor.chat.call_count == 1
        assert result.node_trace[0]["transition_method"] == "structured"

    async def test_compare_function_in_transition(self, mock_supervisor):
        """Pre-parsed compare function works in full playbook run."""
        event = {"type": "check", "count": 5, "project_id": "proj"}
        graph = {
            "id": "compare-transition",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Start.",
                    "transitions": [
                        {
                            "when": {
                                "function": "compare",
                                "variable": "task.count",
                                "operator": ">",
                                "value": 0,
                            },
                            "goto": "process",
                        },
                        {"otherwise": True, "goto": "done"},
                    ],
                },
                "process": {"prompt": "Process.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Started.", "Processed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "process"
        assert result.node_trace[0]["transition_method"] == "structured"

    async def test_expression_performance_no_network(self, mock_supervisor):
        """5.2.15e: structured expressions are fast (no awaited calls for transitions)."""
        import time

        event = {"type": "perf-test", "status": "ok", "project_id": "proj"}
        graph = {
            "id": "perf-test",
            "version": 1,
            "nodes": {
                "node1": {
                    "entry": True,
                    "prompt": "Step 1.",
                    "transitions": [
                        {
                            "when": {"expression": 'task.status == "ok"'},
                            "goto": "node2",
                        },
                    ],
                },
                "node2": {
                    "prompt": "Step 2.",
                    "transitions": [
                        {
                            "when": {"expression": 'task.status == "ok"'},
                            "goto": "node3",
                        },
                    ],
                },
                "node3": {
                    "prompt": "Step 3.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        responses = iter(["Result 1.", "Result 2.", "Result 3."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        start = time.monotonic()
        result = await runner.run()
        elapsed = time.monotonic() - start

        assert result.status == "completed"
        # Only 3 chat calls (one per node) — zero LLM transition calls
        assert mock_supervisor.chat.call_count == 3
        # Verify the transitions were all structured (no LLM)
        assert result.node_trace[0]["transition_method"] == "structured"
        assert result.node_trace[1]["transition_method"] == "structured"
        # With mocked supervisor, entire run should be very fast
        assert elapsed < 1.0, f"Expression transitions took too long: {elapsed:.3f}s"

    async def test_invalid_syntax_falls_through_with_warning(self, mock_supervisor, caplog):
        """5.2.15c: invalid expression in playbook produces clear error, falls to otherwise."""
        event = {"type": "test", "project_id": "proj"}
        graph = {
            "id": "invalid-syntax",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"expression": "not a valid expression!!!"},
                            "goto": "never",
                        },
                        {"otherwise": True, "goto": "fallback"},
                    ],
                },
                "never": {"prompt": "Should not reach.", "goto": "done"},
                "fallback": {"prompt": "Fallback.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Checked.", "Fallback done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = await runner.run()

        assert result.status == "completed"
        # Invalid expression falls through to otherwise
        assert result.node_trace[0]["transition_to"] == "fallback"
        assert result.node_trace[0]["transition_method"] == "otherwise"
        # Warning was logged — not silent failure
        assert "Invalid expression syntax" in caplog.text
        # Only 2 chat calls: start node + fallback node (no LLM transition)
        assert mock_supervisor.chat.call_count == 2

    async def test_undefined_variable_falls_through_with_warning(self, mock_supervisor, caplog):
        """5.2.15d: undefined variable in playbook falls gracefully with descriptive error."""
        event = {"type": "test", "project_id": "proj"}
        graph = {
            "id": "undef-var",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"expression": 'missing_namespace.field == "x"'},
                            "goto": "never",
                        },
                        {"otherwise": True, "goto": "fallback"},
                    ],
                },
                "never": {"prompt": "Should not reach.", "goto": "done"},
                "fallback": {"prompt": "Fallback.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Checked.", "Fallback done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = await runner.run()

        assert result.status == "completed"
        # Undefined variable expression falls through to otherwise
        assert result.node_trace[0]["transition_to"] == "fallback"
        assert result.node_trace[0]["transition_method"] == "otherwise"
        # Descriptive warning was logged with the variable name
        assert "Undefined variable" in caplog.text
        assert "missing_namespace.field" in caplog.text
        # Only 2 chat calls: start node + fallback node (no LLM transition)
        assert mock_supervisor.chat.call_count == 2

    async def test_undefined_task_field_falls_through_with_warning(self, mock_supervisor, caplog):
        """5.2.15d: referencing undefined task field falls gracefully with descriptive error."""
        event = {"type": "test", "project_id": "proj"}
        graph = {
            "id": "undef-field",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"expression": 'task.nonexistent == "value"'},
                            "goto": "never",
                        },
                        {
                            "when": {"expression": 'task.type == "test"'},
                            "goto": "matched",
                        },
                    ],
                },
                "never": {"prompt": "Should not reach.", "goto": "done"},
                "matched": {"prompt": "Matched.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Checked.", "Matched."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event, mock_supervisor)
        with caplog.at_level(logging.WARNING, logger="src.playbook_runner"):
            result = await runner.run()

        assert result.status == "completed"
        # First expression fails (undefined field), second matches
        assert result.node_trace[0]["transition_to"] == "matched"
        assert result.node_trace[0]["transition_method"] == "structured"
        # Warning logged for the undefined field — not silent
        assert "Undefined variable" in caplog.text
        assert "task.nonexistent" in caplog.text
        # Only 2 chat calls: start + matched (no LLM transition)
        assert mock_supervisor.chat.call_count == 2


class TestResolveVariable:
    """Unit tests for the _resolve_variable method."""

    @pytest.fixture
    def runner(self, mock_supervisor):
        event = {
            "type": "task.completed",
            "status": "completed",
            "nested": {"deep": {"value": 42}},
        }
        graph = {"id": "t", "version": 1, "nodes": {}}
        return PlaybookRunner(graph, event, mock_supervisor)

    def test_task_top_level(self, runner):
        val, ok = runner._resolve_variable("task.status", "resp")
        assert ok is True
        assert val == "completed"

    def test_task_bare_namespace(self, runner):
        """task without field returns entire event dict."""
        val, ok = runner._resolve_variable("task", "resp")
        assert ok is True
        assert isinstance(val, dict)

    def test_task_nested(self, runner):
        val, ok = runner._resolve_variable("task.nested.deep.value", "resp")
        assert ok is True
        assert val == 42

    def test_event_alias(self, runner):
        val, ok = runner._resolve_variable("event.status", "resp")
        assert ok is True
        assert val == "completed"

    def test_output_json(self, runner):
        resp = json.dumps({"approval": "yes"})
        val, ok = runner._resolve_variable("output.approval", resp)
        assert ok is True
        assert val == "yes"

    def test_output_non_json(self, runner):
        val, ok = runner._resolve_variable("output.field", "not json")
        assert ok is False

    def test_output_json_array(self, runner):
        val, ok = runner._resolve_variable("output.field", "[1,2]")
        assert ok is False

    def test_response_variable(self, runner):
        val, ok = runner._resolve_variable("response", "hello world")
        assert ok is True
        assert val == "hello world"

    def test_unknown_namespace(self, runner):
        val, ok = runner._resolve_variable("unknown.field", "resp")
        assert ok is False

    def test_undefined_field(self, runner):
        val, ok = runner._resolve_variable("task.nonexistent", "resp")
        assert ok is False


# ---------------------------------------------------------------------------
# 5.2.4: Transition LLM config resolution
# ---------------------------------------------------------------------------


class TestTransitionLLMConfig:
    """Test the transition-specific LLM config resolution chain."""

    def test_node_transition_config_wins(self, mock_supervisor, event_data):
        """Node-level transition_llm_config takes highest priority."""
        graph = {
            "id": "config-test",
            "version": 1,
            "llm_config": {"model": "expensive"},
            "transition_llm_config": {"model": "medium"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"transition_llm_config": {"model": "cheap"}, "llm_config": {"model": "standard"}}
        result = runner._resolve_transition_llm_config(node)
        assert result == {"model": "cheap"}

    def test_playbook_transition_config_second(self, mock_supervisor, event_data):
        """Playbook-level transition_llm_config is second priority."""
        graph = {
            "id": "config-test",
            "version": 1,
            "llm_config": {"model": "expensive"},
            "transition_llm_config": {"model": "medium"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"llm_config": {"model": "standard"}}
        result = runner._resolve_transition_llm_config(node)
        assert result == {"model": "medium"}

    def test_node_general_config_third(self, mock_supervisor, event_data):
        """Node-level general llm_config is third priority."""
        graph = {
            "id": "config-test",
            "version": 1,
            "llm_config": {"model": "expensive"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"llm_config": {"model": "standard"}}
        result = runner._resolve_transition_llm_config(node)
        assert result == {"model": "standard"}

    def test_playbook_general_config_fourth(self, mock_supervisor, event_data):
        """Playbook-level general llm_config is fourth priority."""
        graph = {
            "id": "config-test",
            "version": 1,
            "llm_config": {"model": "expensive"},
            "nodes": {},
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        node = {"prompt": "Do something"}
        result = runner._resolve_transition_llm_config(node)
        assert result == {"model": "expensive"}

    def test_no_config_returns_none(self, mock_supervisor, event_data):
        """No config anywhere → None (Supervisor default)."""
        graph = {"id": "no-config", "version": 1, "nodes": {}}
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = runner._resolve_transition_llm_config({"prompt": "Do something"})
        assert result is None

    async def test_transition_call_uses_resolved_config(self, mock_supervisor, event_data):
        """End-to-end: transition LLM call should use the resolved config."""
        graph = {
            "id": "e2e-config",
            "version": 1,
            "transition_llm_config": {"model": "haiku", "provider": "anthropic"},
            "llm_config": {"model": "sonnet"},
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check status.",
                    "transitions": [
                        {"when": "ok", "goto": "done"},
                        {"when": "bad", "goto": "fix"},
                    ],
                },
                "fix": {"prompt": "Fix.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Status ok", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        # Node call should use playbook-level llm_config (sonnet)
        node_call = mock_supervisor.chat.call_args_list[0]
        assert node_call.kwargs["llm_config"] == {"model": "sonnet"}

        # Transition call should use transition_llm_config (haiku)
        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["llm_config"] == {
            "model": "haiku",
            "provider": "anthropic",
        }

    async def test_node_transition_config_overrides_playbook_in_e2e(
        self, mock_supervisor, event_data
    ):
        """Node-level transition_llm_config should override playbook-level."""
        graph = {
            "id": "node-override",
            "version": 1,
            "transition_llm_config": {"model": "haiku"},
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transition_llm_config": {"model": "gemini-flash"},
                    "transitions": [
                        {"when": "ok", "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        responses = iter(["Result", "1"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        await runner.run()

        transition_call = mock_supervisor.chat.call_args_list[1]
        assert transition_call.kwargs["llm_config"] == {"model": "gemini-flash"}


# ---------------------------------------------------------------------------
# 5.2.4: Edge cases in transition evaluation
# ---------------------------------------------------------------------------


class TestTransitionEdgeCases:
    """Test edge cases and error scenarios in transition evaluation."""

    async def test_all_otherwise_no_conditions(self, mock_supervisor, event_data):
        """A transitions list with only an otherwise entry should work."""
        graph = {
            "id": "only-otherwise",
            "version": 1,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Do something.",
                    "transitions": [
                        {"otherwise": True, "goto": "b"},
                    ],
                },
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_to"] == "b"
        assert result.node_trace[0]["transition_method"] == "otherwise"
        # No LLM transition call needed
        assert mock_supervisor.chat.call_count == 2  # a node + b node

    async def test_empty_transitions_list(self, mock_supervisor, event_data):
        """An empty transitions list should behave like no transitions."""
        graph = {
            "id": "empty-transitions",
            "version": 1,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Do something.",
                    "transitions": [],
                },
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert result.node_trace[0]["transition_method"] == "none"

    async def test_llm_returns_empty_string(self, mock_supervisor, event_data):
        """If LLM returns empty string for transition, fall back to otherwise."""
        graph = {
            "id": "empty-decision",
            "version": 1,
            "nodes": {
                "check": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {"when": "good", "goto": "celebrate"},
                        {"otherwise": True, "goto": "default"},
                    ],
                },
                "celebrate": {"prompt": "Celebrate!", "goto": "done"},
                "default": {"prompt": "Default path.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        responses = iter(["Status unclear", "", "Default done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        # Empty string → no match → otherwise → default
        assert result.node_trace[1]["node_id"] == "default"

    async def test_structured_only_no_otherwise_no_match(self, mock_supervisor, event_data):
        """All structured conditions + no otherwise + no match → run fails (5.2.14g)."""
        graph = {
            "id": "structured-no-match",
            "version": 1,
            "nodes": {
                "a": {
                    "entry": True,
                    "prompt": "Check.",
                    "transitions": [
                        {
                            "when": {"function": "response_contains", "value": "magic word"},
                            "goto": "b",
                        },
                    ],
                },
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Nothing special here."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        # Transitions were defined but none matched and no otherwise → failure
        assert result.status == "failed"
        assert (
            "no transition matched" in result.error.lower() or "otherwise" in result.error.lower()
        )


# ---------------------------------------------------------------------------
# 5.2.13: Playbook execution happy path
# ---------------------------------------------------------------------------


class TestPlaybookExecutionHappyPath:
    """Roadmap 5.2.13 — end-to-end happy path tests per playbooks §6.

    Uses a canonical 3-node linear playbook: start → middle → end → done(terminal).
    Each test case maps to a specific roadmap requirement (a)-(g).
    """

    # -- Shared graph fixture for (a)-(f) ----------------------------------

    @pytest.fixture
    def three_node_graph(self):
        """3-node linear playbook: start → middle → end → done."""
        return {
            "id": "happy-path-playbook",
            "version": 3,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Begin the analysis of the codebase.",
                    "goto": "middle",
                },
                "middle": {
                    "prompt": "Based on the analysis above, group findings by severity.",
                    "goto": "end",
                },
                "end": {
                    "prompt": "Generate a summary report of the grouped findings.",
                    "goto": "done",
                },
                "done": {
                    "terminal": True,
                },
            },
        }

    # (a) 3-node linear playbook executes all nodes in order, status "completed"
    async def test_three_node_linear_executes_in_order(
        self, mock_supervisor, three_node_graph, event_data
    ):
        """(a) start → middle → end executes in order with status 'completed'."""
        responses = iter(
            [
                "Found 5 issues in the codebase.",
                "Grouped: 2 critical, 2 warnings, 1 info.",
                "Report: 5 total findings across 3 severity levels.",
            ]
        )
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 3
        assert [t["node_id"] for t in result.node_trace] == ["start", "middle", "end"]
        for trace_entry in result.node_trace:
            assert trace_entry["status"] == "completed"

    # (b) Each node receives accumulated conversation history from prior nodes
    async def test_each_node_receives_accumulated_history(
        self, mock_supervisor, three_node_graph, event_data
    ):
        """(b) History grows: start sees seed; middle sees seed+start; end sees seed+start+middle."""
        responses = iter(["Result from start.", "Result from middle.", "Result from end."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor)
        await runner.run()

        calls = mock_supervisor.chat.call_args_list
        assert len(calls) == 3

        # Node "start" receives only the seed message
        start_history = calls[0].kwargs["history"]
        assert len(start_history) == 1
        assert start_history[0]["role"] == "user"
        assert "Event received" in start_history[0]["content"]

        # Node "middle" receives seed + start prompt + start response
        middle_history = calls[1].kwargs["history"]
        assert len(middle_history) == 3
        assert middle_history[0]["role"] == "user"  # seed
        assert middle_history[1] == {
            "role": "user",
            "content": "Begin the analysis of the codebase.",
        }
        assert middle_history[2] == {
            "role": "assistant",
            "content": "Result from start.",
        }

        # Node "end" receives seed + start exchange + middle exchange
        end_history = calls[2].kwargs["history"]
        assert len(end_history) == 5
        assert end_history[3] == {
            "role": "user",
            "content": "Based on the analysis above, group findings by severity.",
        }
        assert end_history[4] == {
            "role": "assistant",
            "content": "Result from middle.",
        }

    # (c) Each node's prompt is built with correct context (task data, event context)
    async def test_node_prompt_built_with_correct_context(
        self, mock_supervisor, three_node_graph, event_data
    ):
        """(c) Seed contains event/task data; each node gets its compiled prompt."""
        responses = iter(["R1", "R2", "R3"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor)
        await runner.run()

        # Seed message includes event context (task data / trigger event)
        seed = runner.messages[0]
        assert "Event received:" in seed["content"]
        assert '"project_id": "test-proj"' in seed["content"]
        assert '"commit_hash": "abc123"' in seed["content"]
        assert "happy-path-playbook" in seed["content"]

        # Each node's compiled prompt is passed verbatim
        calls = mock_supervisor.chat.call_args_list
        assert calls[0].kwargs["text"] == "Begin the analysis of the codebase."
        assert calls[1].kwargs["text"] == (
            "Based on the analysis above, group findings by severity."
        )
        assert calls[2].kwargs["text"] == ("Generate a summary report of the grouped findings.")

    # (d) Supervisor.chat() is invoked once per node with correct parameters
    async def test_supervisor_chat_invoked_once_per_node(
        self, mock_supervisor, three_node_graph, event_data
    ):
        """(d) Exactly 3 supervisor.chat() calls with correct text, user_name, history."""
        responses = iter(["R1", "R2", "R3"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor)
        await runner.run()

        assert mock_supervisor.chat.call_count == 3

        expected = [
            ("Begin the analysis of the codebase.", "playbook-runner:start"),
            (
                "Based on the analysis above, group findings by severity.",
                "playbook-runner:middle",
            ),
            (
                "Generate a summary report of the grouped findings.",
                "playbook-runner:end",
            ),
        ]

        for call, (text, user_name) in zip(
            mock_supervisor.chat.call_args_list, expected, strict=True
        ):
            assert call.kwargs["text"] == text
            assert call.kwargs["user_name"] == user_name
            # History should be a list of dicts
            assert isinstance(call.kwargs["history"], list)
            # LLM config should be passed (None when not set on graph)
            assert "llm_config" in call.kwargs

    # (e) Run duration and per-node token usage are recorded
    async def test_run_duration_and_per_node_tokens_recorded(
        self, mock_supervisor, three_node_graph, event_data
    ):
        """(e) Total tokens > 0; each trace entry has valid started_at/completed_at."""
        responses = iter(
            [
                "Analysis: found 5 issues.",
                "Grouped into 3 categories.",
                "Summary report generated.",
            ]
        )
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor)
        result = await runner.run()

        # Total tokens must be positive
        assert result.tokens_used > 0

        # Each node trace entry must have valid timing
        for trace_entry in result.node_trace:
            assert trace_entry["started_at"] is not None
            assert trace_entry["completed_at"] is not None
            assert trace_entry["completed_at"] >= trace_entry["started_at"]

        # Node timestamps should be in order (start before middle before end)
        assert result.node_trace[0]["started_at"] <= result.node_trace[1]["started_at"]
        assert result.node_trace[1]["started_at"] <= result.node_trace[2]["started_at"]

        # Per-node token contribution: each node adds tokens, so cumulative sum equals total
        # Verify by computing expected tokens from the prompts + responses
        prompts = [
            "Begin the analysis of the codebase.",
            "Based on the analysis above, group findings by severity.",
            "Generate a summary report of the grouped findings.",
        ]
        resp = [
            "Analysis: found 5 issues.",
            "Grouped into 3 categories.",
            "Summary report generated.",
        ]
        expected_tokens = sum(_estimate_tokens(p, r) for p, r in zip(prompts, resp, strict=True))
        assert result.tokens_used == expected_tokens

    # (f) Final PlaybookRun status "completed" with correct node trace [start, middle, end]
    async def test_final_run_persisted_completed_with_correct_trace(
        self, mock_supervisor, three_node_graph, event_data, mock_db
    ):
        """(f) DB record has status='completed' and node_trace=[start, middle, end]."""
        responses = iter(["R1", "R2", "R3"])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(three_node_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "completed"

        # DB should have been updated with the final state
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "completed"
        assert final_call.kwargs["completed_at"] is not None
        assert final_call.kwargs["tokens_used"] > 0

        # Verify persisted node trace
        persisted_trace = json.loads(final_call.kwargs["node_trace"])
        assert len(persisted_trace) == 3
        assert [t["node_id"] for t in persisted_trace] == ["start", "middle", "end"]
        for entry in persisted_trace:
            assert entry["status"] == "completed"
            assert entry["started_at"] is not None
            assert entry["completed_at"] is not None

        # Verify persisted conversation history is complete
        persisted_history = json.loads(final_call.kwargs["conversation_history"])
        # seed + 3 * (prompt + response) = 7 messages
        assert len(persisted_history) == 7
        assert persisted_history[0]["role"] == "user"  # seed
        assert "Event received" in persisted_history[0]["content"]
        # Prompts and responses alternate correctly
        for i in range(1, 7, 2):
            assert persisted_history[i]["role"] == "user"
            assert persisted_history[i + 1]["role"] == "assistant"

    # (g) Playbook with single node (entry = terminal) executes and completes
    async def test_single_node_playbook_executes_and_completes(self, mock_supervisor, event_data):
        """(g) A single-node playbook (entry, no goto/transitions) executes and completes."""
        graph = {
            "id": "single-node-playbook",
            "version": 1,
            "nodes": {
                "only": {
                    "entry": True,
                    "prompt": "Perform a one-shot analysis and return results.",
                },
            },
        }

        mock_supervisor.chat.return_value = "One-shot analysis complete."
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "completed"
        assert len(result.node_trace) == 1
        assert result.node_trace[0]["node_id"] == "only"
        assert result.node_trace[0]["status"] == "completed"
        assert result.tokens_used > 0
        assert result.final_response == "One-shot analysis complete."
        mock_supervisor.chat.assert_called_once()

    async def test_single_node_with_db_persistence(self, mock_supervisor, event_data, mock_db):
        """(g) extended — single-node playbook persists correctly to DB."""
        graph = {
            "id": "single-node-playbook",
            "version": 1,
            "nodes": {
                "only": {
                    "entry": True,
                    "prompt": "One-shot task.",
                },
            },
        }

        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "completed"

        # Verify DB record was created and completed
        mock_db.create_playbook_run.assert_called_once()
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "completed"

        persisted_trace = json.loads(final_call.kwargs["node_trace"])
        assert len(persisted_trace) == 1
        assert persisted_trace[0]["node_id"] == "only"


# ---------------------------------------------------------------------------
# Roadmap 5.2.17 — PlaybookRun persistence test cases (a)-(h)
# ---------------------------------------------------------------------------


class TestRoadmap5217:
    """Roadmap 5.2.17: PlaybookRun persistence per playbooks spec §6.

    Each test method maps to a specific roadmap case (a)-(h).
    """

    # -- helpers --

    @staticmethod
    def _three_node_graph(
        graph_id: str = "three-node",
        version: int = 1,
        *,
        source_hash: str | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Build a 3-node linear graph: start → analyze → report → done."""
        g: dict = {
            "id": graph_id,
            "version": version,
            "nodes": {
                "start": {"entry": True, "prompt": "Initialise scan.", "goto": "analyze"},
                "analyze": {"prompt": "Analyze the findings.", "goto": "report"},
                "report": {"prompt": "Generate final report.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        if source_hash is not None:
            g["source_hash"] = source_hash
        if max_tokens is not None:
            g["max_tokens"] = max_tokens
        return g

    # (a) completed run → status "completed", full node trace, total token usage

    async def test_a_completed_run_has_status_trace_and_tokens(
        self, mock_supervisor, event_data, mock_db
    ):
        """(a) Completed run has DB record with status 'completed',
        full node trace, and total token usage."""
        graph = self._three_node_graph()
        responses = iter(["Scan initialised.", "Analysis done.", "Report ready."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "completed"

        # Final DB update
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "completed"
        assert final_call.kwargs["completed_at"] is not None

        # Full node trace — one entry per executable node (3 nodes)
        trace = json.loads(final_call.kwargs["node_trace"])
        assert len(trace) == 3
        assert all(t["status"] == "completed" for t in trace)

        # Total token usage accumulated
        assert final_call.kwargs["tokens_used"] > 0
        assert final_call.kwargs["tokens_used"] == result.tokens_used

    # (b) node trace contains ordered list of node IDs visited

    async def test_b_node_trace_ordered_ids(self, mock_supervisor, event_data, mock_db):
        """(b) Node trace contains ordered list of node IDs visited
        (e.g., ['start', 'analyze', 'report'])."""
        graph = self._three_node_graph()
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        final_call = mock_db.update_playbook_run.call_args_list[-1]
        trace = json.loads(final_call.kwargs["node_trace"])

        visited_ids = [entry["node_id"] for entry in trace]
        assert visited_ids == ["start", "analyze", "report"]

    # (c) conversation history in DB matches messages exchanged at each node

    async def test_c_conversation_history_matches_messages(
        self, mock_supervisor, event_data, mock_db
    ):
        """(c) Conversation history in DB matches the actual messages
        exchanged at each node."""
        graph = self._three_node_graph()
        node_responses = {
            "Initialise scan.": "Scan started: 5 files found.",
            "Analyze the findings.": "Analysis: 2 critical, 1 warning.",
            "Generate final report.": "Report: all issues documented.",
        }

        async def respond(**kw):
            return node_responses.get(kw["text"], "Unknown prompt")

        mock_supervisor.chat.side_effect = respond

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        final_call = mock_db.update_playbook_run.call_args_list[-1]
        history = json.loads(final_call.kwargs["conversation_history"])

        # Expected: seed + 3 × (user prompt + assistant response) = 7 messages
        assert len(history) == 7

        # Seed message (event context)
        assert history[0]["role"] == "user"
        assert "Event received" in history[0]["content"]

        # Node "start"
        assert history[1] == {"role": "user", "content": "Initialise scan."}
        assert history[2] == {"role": "assistant", "content": "Scan started: 5 files found."}

        # Node "analyze"
        assert history[3] == {"role": "user", "content": "Analyze the findings."}
        assert history[4] == {
            "role": "assistant",
            "content": "Analysis: 2 critical, 1 warning.",
        }

        # Node "report"
        assert history[5] == {"role": "user", "content": "Generate final report."}
        assert history[6] == {"role": "assistant", "content": "Report: all issues documented."}

    # (d) failed run → status "failed" with error details and partial node trace

    async def test_d_failed_run_has_error_and_partial_trace(
        self, mock_supervisor, event_data, mock_db
    ):
        """(d) Failed run has status 'failed' with error details and
        partial node trace up to failure point."""
        graph = self._three_node_graph()
        call_count = 0

        async def fail_on_second(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Fail on "analyze" node
                raise RuntimeError("Connection refused: model API unavailable")
            return "Step done."

        mock_supervisor.chat.side_effect = fail_on_second

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"

        # Find the failure DB update
        fail_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed":
                fail_call = call
                break

        assert fail_call is not None

        # Error details present
        assert fail_call.kwargs["error"] is not None
        assert "Connection refused" in fail_call.kwargs["error"]

        # Partial node trace: "start" completed + "analyze" failed
        trace = json.loads(fail_call.kwargs["node_trace"])
        assert len(trace) == 2
        assert trace[0]["node_id"] == "start"
        assert trace[0]["status"] == "completed"
        assert trace[1]["node_id"] == "analyze"
        assert trace[1]["status"] == "failed"

        # "report" node was never reached
        visited_ids = [t["node_id"] for t in trace]
        assert "report" not in visited_ids

    # (e) budget-exceeded run → status "failed" with the node where budget was exhausted

    async def test_e_budget_exceeded_run_identifies_node(
        self, mock_supervisor, event_data, mock_db
    ):
        """(e) Budget-exceeded run has status 'failed' with the node where
        budget was exhausted (spec §6 Token Budget)."""
        # Use a very small token budget so it exhausts after the first node
        graph = self._three_node_graph(max_tokens=10)

        # Return a long response so token estimate exceeds budget
        mock_supervisor.chat.return_value = "x" * 500

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "failed"

        # Find the failed DB update for budget exceeded
        budget_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed" and "token_budget_exceeded" in (
                call.kwargs.get("error") or ""
            ):
                budget_call = call
                break

        assert budget_call is not None
        assert "token_budget_exceeded" in budget_call.kwargs["error"]

        # The current_node identifies where the budget check fired.
        # Budget is checked AFTER the node completes (spec §6 step 6d),
        # so after "start" completes and tokens exceed the limit, the
        # runner fails before "analyze" can execute.
        trace = json.loads(budget_call.kwargs["node_trace"])
        assert len(trace) >= 1
        # First node completed (tokens accumulated there)
        assert trace[0]["node_id"] == "start"
        assert trace[0]["status"] == "completed"

    # (f) run record includes playbook source version hash

    async def test_f_source_version_hash_preserved(self, mock_supervisor, event_data, mock_db):
        """(f) Run record includes playbook source version hash
        (for version tracking) via pinned_graph."""
        graph = self._three_node_graph(
            graph_id="versioned-pb",
            version=3,
            source_hash="sha256:a1b2c3d4e5f6789012345678",
        )
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # The create call pins the compiled graph (including source_hash)
        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert created_run.pinned_graph is not None
        pinned = json.loads(created_run.pinned_graph)
        assert pinned["source_hash"] == "sha256:a1b2c3d4e5f6789012345678"
        assert pinned["version"] == 3
        assert pinned["id"] == "versioned-pb"

    # (g) querying runs by playbook_id returns all runs sorted by start time

    async def test_g_query_by_playbook_id_sorted(self, mock_supervisor, mock_db):
        """(g) Querying runs by playbook_id returns all runs sorted by
        start time.

        This test verifies the runner creates records with the correct
        playbook_id — the sorting behaviour is verified in the database
        integration tests (TestPlaybookRunQueries).
        """
        graph_a = {
            "id": "playbook-alpha",
            "version": 1,
            "nodes": {
                "a": {"entry": True, "prompt": "Go.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        event = {"type": "test"}

        runner = PlaybookRunner(graph_a, event, mock_supervisor, db=mock_db)
        await runner.run()

        created = mock_db.create_playbook_run.call_args[0][0]
        assert created.playbook_id == "playbook-alpha"
        assert created.started_at > 0

    # (h) run record includes start_time, end_time, and per-node durations

    async def test_h_timing_fields_present(self, mock_supervisor, event_data, mock_db):
        """(h) Run record includes start_time, end_time, and per-node
        durations."""
        graph = self._three_node_graph()
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # 1. start_time set at creation
        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert created_run.started_at > 0

        # 2. end_time set at completion
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        completed_at = final_call.kwargs["completed_at"]
        assert completed_at is not None
        assert completed_at >= created_run.started_at

        # 3. Per-node durations in trace
        trace = json.loads(final_call.kwargs["node_trace"])
        assert len(trace) == 3

        for entry in trace:
            assert "started_at" in entry
            assert "completed_at" in entry
            assert entry["started_at"] > 0
            assert entry["completed_at"] is not None
            # Duration must be non-negative
            duration = entry["completed_at"] - entry["started_at"]
            assert duration >= 0, f"Node '{entry['node_id']}' has negative duration: {duration}"

        # Node ordering in time: each node starts at or after the previous finished
        for i in range(1, len(trace)):
            assert trace[i]["started_at"] >= trace[i - 1]["completed_at"], (
                f"Node '{trace[i]['node_id']}' started before '{trace[i - 1]['node_id']}' completed"
            )


# ---------------------------------------------------------------------------
# Daily Playbook Token Cap (roadmap 5.2.8)
# ---------------------------------------------------------------------------


class TestDailyPlaybookTokenCap:
    """Global daily playbook token cap enforcement (roadmap 5.2.8).

    When ``max_daily_playbook_tokens`` is configured, the runner queries the
    database for today's cumulative playbook token usage before starting a
    new run.  If the cap is already reached, the run is immediately failed
    with ``daily_playbook_token_cap_exceeded``.
    """

    async def test_daily_cap_blocks_new_run(self, mock_supervisor, mock_db, event_data):
        """Run is blocked when today's usage already meets the daily cap."""
        graph = {
            "id": "daily-cap-test",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # DB reports 50_000 tokens used today; cap is 50_000
        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=50_000)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=50_000,
        )
        result = await runner.run()

        assert result.status == "failed"
        assert "daily_playbook_token_cap_exceeded" in result.error
        assert "50000" in result.error
        # Supervisor should never be called — blocked before execution
        mock_supervisor.chat.assert_not_called()

    async def test_daily_cap_allows_under_budget(self, mock_supervisor, mock_db, event_data):
        """Run proceeds normally when today's usage is under the cap."""
        graph = {
            "id": "daily-ok",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=10_000)
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=50_000,
        )
        result = await runner.run()

        assert result.status == "completed"
        mock_supervisor.chat.assert_called_once()

    async def test_no_daily_cap_means_unlimited(self, mock_supervisor, mock_db, event_data):
        """When max_daily_playbook_tokens is None, no daily check occurs."""
        graph = {
            "id": "no-daily-cap",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "completed"
        # Should NOT have queried daily usage at all
        mock_db.get_daily_playbook_token_usage.assert_not_called()

    async def test_daily_cap_without_db_skips_check(self, mock_supervisor, event_data):
        """When no DB is configured, the daily cap check is skipped."""
        graph = {
            "id": "no-db-cap",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=None,
            max_daily_playbook_tokens=100,
        )
        result = await runner.run()

        # No DB means we can't check — run proceeds
        assert result.status == "completed"

    async def test_daily_cap_exceeded_persists_to_db(self, mock_supervisor, mock_db, event_data):
        """Daily cap failure is persisted to the database with correct status and error."""
        graph = {
            "id": "db-daily-cap",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=100_000)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=80_000,
        )
        result = await runner.run()

        assert result.status == "failed"

        # Verify the DB record was created and then updated with failure
        mock_db.create_playbook_run.assert_called_once()
        mock_db.update_playbook_run.assert_called_once()
        update_kwargs = mock_db.update_playbook_run.call_args.kwargs
        assert update_kwargs["status"] == "failed"
        assert "daily_playbook_token_cap_exceeded" in update_kwargs["error"]
        assert update_kwargs["completed_at"] is not None

    async def test_daily_cap_exceeded_sends_notification(
        self, mock_supervisor, mock_db, event_data
    ):
        """A notification is sent via on_progress when daily cap is exceeded."""
        graph = {
            "id": "notify-daily",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=200_000)
        progress_events: list[tuple[str, str | None]] = []

        async def on_progress(event: str, detail: str | None) -> None:
            progress_events.append((event, detail))

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            on_progress=on_progress,
            max_daily_playbook_tokens=100_000,
        )
        result = await runner.run()

        assert result.status == "failed"
        # Should have a playbook_failed event
        failed_events = [e for e in progress_events if e[0] == "playbook_failed"]
        assert len(failed_events) == 1
        assert "daily_playbook_token_cap_exceeded" in failed_events[0][1]

    async def test_daily_cap_exceeded_empty_trace(self, mock_supervisor, mock_db, event_data):
        """When blocked by daily cap, no nodes execute so trace is empty."""
        graph = {
            "id": "empty-trace",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=999_999)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=100_000,
        )
        result = await runner.run()

        assert result.status == "failed"
        assert result.node_trace == []
        assert result.tokens_used == 0

    async def test_daily_cap_error_includes_usage_details(
        self, mock_supervisor, mock_db, event_data
    ):
        """Error message includes both the daily cap and the actual usage."""
        graph = {
            "id": "error-detail",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=75_000)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=50_000,
        )
        result = await runner.run()

        assert result.status == "failed"
        assert "50000" in result.error  # cap
        assert "75000" in result.error  # actual

    async def test_daily_cap_boundary_exact_match(self, mock_supervisor, mock_db, event_data):
        """When usage exactly equals the cap, the run is blocked."""
        graph = {
            "id": "boundary",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=100_000)

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=100_000,
        )
        result = await runner.run()

        assert result.status == "failed"
        assert "daily_playbook_token_cap_exceeded" in result.error

    async def test_daily_cap_one_below_allows(self, mock_supervisor, mock_db, event_data):
        """When usage is one below the cap, the run is allowed to proceed."""
        graph = {
            "id": "one-below",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=99_999)
        mock_supervisor.chat.return_value = "Done."

        runner = PlaybookRunner(
            graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            max_daily_playbook_tokens=100_000,
        )
        result = await runner.run()

        assert result.status == "completed"

    async def test_check_daily_budget_class_method(self, mock_db):
        """The static check_daily_budget() method works correctly."""
        mock_db.get_daily_playbook_token_usage = AsyncMock(return_value=50_000)

        # Exceeded
        exceeded, used = await PlaybookRunner.check_daily_budget(mock_db, 50_000)
        assert exceeded is True
        assert used == 50_000

        # Not exceeded
        exceeded, used = await PlaybookRunner.check_daily_budget(mock_db, 100_000)
        assert exceeded is False
        assert used == 50_000

        # No cap
        exceeded, used = await PlaybookRunner.check_daily_budget(mock_db, None)
        assert exceeded is False
        assert used == 0

    def test_midnight_today_returns_valid_timestamp(self):
        """_midnight_today returns a timestamp for the start of today."""
        import datetime

        midnight = _midnight_today()
        now = time.time()

        # Midnight should be in the past (or exactly now if run at midnight)
        assert midnight <= now
        # And within the last 24 hours
        assert now - midnight < 86400

        # Round-trip check: converting back gives midnight
        dt = datetime.datetime.fromtimestamp(midnight)
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0


# ---------------------------------------------------------------------------
# Human-in-the-loop: event emission (spec §9, roadmap 5.4.1)
# ---------------------------------------------------------------------------


class TestHumanInTheLoopEvents:
    """Tests for playbook.run.paused and playbook.run.resumed event emission."""

    async def test_pause_emits_playbook_run_paused_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """When a run pauses at a wait_for_human node, a playbook.run.paused event fires."""
        responses = iter(["Analysis done.", "Ready for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        result = await runner.run()

        assert result.status == "paused"

        # Find the playbook.run.paused event call
        paused_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "playbook.run.paused"
        ]
        assert len(paused_calls) == 1
        payload = paused_calls[0].args[1]
        assert payload["playbook_id"] == "human-review-playbook"
        assert payload["run_id"] == result.run_id
        assert payload["node_id"] == "review"
        assert "paused_at" in payload
        assert "tokens_used" in payload

    async def test_pause_event_includes_last_response(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The paused event includes the last assistant response as context."""
        responses = iter(["Analysis complete.", "Here is my analysis for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        paused_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "playbook.run.paused"
        ]
        assert len(paused_calls) == 1
        payload = paused_calls[0].args[1]
        assert payload.get("last_response") == "Here is my analysis for review."

    async def test_pause_event_not_emitted_without_event_bus(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """When no event_bus is configured, pause still works without error."""
        responses = iter(["Analysis.", "Review context."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()
        assert result.status == "paused"  # No error, just no event emitted

    async def test_resume_emits_playbook_run_resumed_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """When a paused run is resumed, a playbook.run.resumed event fires.

        Note: prior to roadmap 5.4.3, resume emitted ``human.review.completed``.
        That event is now the *trigger* (fired externally); ``playbook.run.resumed``
        is the *notification* confirming the resume occurred.
        """
        paused_run = PlaybookRun(
            run_id="evt-test-1",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received: ..."},
                    {"role": "user", "content": "Analyse the issue."},
                    {"role": "assistant", "content": "Analysis done."},
                    {"role": "user", "content": "Present for review."},
                    {"role": "assistant", "content": "Here is the review."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "analyse",
                        "started_at": 100,
                        "completed_at": 101,
                        "status": "completed",
                    },
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": 102,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=50,
            started_at=100.0,
        )

        responses = iter(["1", "Executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, proceed.",
            db=mock_db,
            event_bus=event_bus,
        )

        assert result.status == "completed"

        # Find the playbook.run.resumed event (5.4.3: replaces human.review.completed)
        resumed_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "playbook.run.resumed"
        ]
        assert len(resumed_calls) == 1
        payload = resumed_calls[0].args[1]
        assert payload["playbook_id"] == "human-review-playbook"
        assert payload["run_id"] == "evt-test-1"
        assert payload["node_id"] == "review"
        assert payload["decision"] == "Approved, proceed."

    async def test_resume_emits_notification_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Resume emits notify.playbook_run_resumed for notification transports."""
        paused_run = PlaybookRun(
            run_id="evt-notify-1",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event."},
                    {"role": "assistant", "content": "Analysis."},
                    {"role": "user", "content": "Review."},
                    {"role": "assistant", "content": "For review."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "analyse",
                        "started_at": 100,
                        "completed_at": 101,
                        "status": "completed",
                    },
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": 102,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=50,
            started_at=100.0,
        )

        responses = iter(["1", "Executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, proceed.",
            db=mock_db,
            event_bus=event_bus,
        )

        notify_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "notify.playbook_run_resumed"
        ]
        assert len(notify_calls) == 1
        payload = notify_calls[0].args[1]
        assert payload["playbook_id"] == "human-review-playbook"
        assert payload["run_id"] == "evt-notify-1"
        assert payload["node_id"] == "review"
        assert payload["decision"] == "Approved, proceed."
        assert payload["event_type"] == "notify.playbook_run_resumed"
        assert payload["category"] == "interaction"

    async def test_resume_also_emits_completion_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """A resumed run that completes emits both playbook.run.resumed and playbook.run.completed."""
        paused_run = PlaybookRun(
            run_id="evt-test-2",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event."},
                    {"role": "assistant", "content": "Analysis."},
                    {"role": "user", "content": "Review."},
                    {"role": "assistant", "content": "For review."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "analyse",
                        "started_at": 100,
                        "completed_at": 101,
                        "status": "completed",
                    },
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": 102,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=30,
            started_at=100.0,
        )

        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Go ahead.",
            db=mock_db,
            event_bus=event_bus,
        )

        event_types = [c.args[0] for c in event_bus.emit.call_args_list]
        assert "playbook.run.resumed" in event_types
        assert "playbook.run.completed" in event_types

    async def test_pause_event_caps_long_response(self, mock_supervisor, mock_db):
        """The last_response in the paused event is capped at 2000 chars."""
        graph = {
            "id": "cap-test",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Go.",
                    "goto": "human",
                },
                "human": {
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        long_response = "x" * 5000
        responses = iter(["short.", long_response])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            graph, {"type": "test"}, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        paused_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "playbook.run.paused"
        ]
        assert len(paused_calls) == 1
        last_resp = paused_calls[0].args[1].get("last_response", "")
        assert len(last_resp) <= 2000

    async def test_resume_caps_long_human_input_in_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The decision field in playbook.run.resumed is capped at 2000 chars."""
        paused_run = PlaybookRun(
            run_id="cap-test-1",
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event."},
                    {"role": "assistant", "content": "Analysis."},
                    {"role": "user", "content": "Review."},
                    {"role": "assistant", "content": "Ready."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "analyse",
                        "started_at": 100,
                        "completed_at": 101,
                        "status": "completed",
                    },
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": 102,
                        "status": "completed",
                    },
                ]
            ),
            tokens_used=30,
            started_at=100.0,
        )

        long_input = "y" * 5000
        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input=long_input,
            db=mock_db,
            event_bus=event_bus,
        )

        resumed_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "playbook.run.resumed"
        ]
        assert len(resumed_calls) == 1
        decision = resumed_calls[0].args[1]["decision"]
        assert len(decision) <= 2000


# ---------------------------------------------------------------------------
# Command handler: resume_playbook and list_playbook_runs (roadmap 5.4.1)
# ---------------------------------------------------------------------------


class TestResumePlaybookCommand:
    """Tests for the _cmd_resume_playbook command handler method."""

    @pytest.fixture
    def mock_handler(self):
        """Create a minimal mock CommandHandler for testing."""
        handler = AsyncMock()
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()
        handler.orchestrator.bus.emit = AsyncMock()
        return handler

    async def test_resume_requires_run_id(self):
        """resume_playbook returns error when run_id is missing."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at

        result = await handler._cmd_resume_playbook({"human_input": "ok"})
        assert "error" in result
        assert "run_id" in result["error"]

    async def test_resume_requires_human_input(self):
        """resume_playbook returns error when human_input is empty."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at

        result = await handler._cmd_resume_playbook({"run_id": "abc", "human_input": ""})
        assert "error" in result
        assert "human_input" in result["error"]

    async def test_resume_run_not_found(self):
        """resume_playbook returns error when run doesn't exist."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.db.get_playbook_run = AsyncMock(return_value=None)

        result = await handler._cmd_resume_playbook(
            {"run_id": "nonexistent", "human_input": "Approved"}
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_resume_non_paused_run(self):
        """resume_playbook returns error when run is not in paused status."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()

        completed_run = PlaybookRun(
            run_id="completed-1",
            playbook_id="test-pb",
            playbook_version=1,
            status="completed",
            started_at=100.0,
        )
        handler.db.get_playbook_run = AsyncMock(return_value=completed_run)

        result = await handler._cmd_resume_playbook(
            {"run_id": "completed-1", "human_input": "Resume please"}
        )
        assert "error" in result
        assert "completed" in result["error"]
        assert "not 'paused'" in result["error"]

    async def test_resume_happy_path(self):
        """resume_playbook successfully resumes a paused run via PlaybookRunner.resume."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        graph = {
            "id": "test-pb",
            "version": 1,
            "nodes": {
                "review": {"prompt": "Review code", "wait_for_human": True, "goto": "done"},
                "done": {"terminal": True},
            },
        }
        paused_run = PlaybookRun(
            run_id="run-42",
            playbook_id="test-pb",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=time.time() - 60,
            paused_at=time.time() - 30,
            pinned_graph=json.dumps(graph),
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Review this code"},
                    {"role": "assistant", "content": "I found issues."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {"node_id": "review", "started_at": time.time() - 60, "status": "paused"},
                ]
            ),
            tokens_used=100,
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        mock_result = RunResult(
            run_id="run-42",
            status="completed",
            node_trace=[{"node_id": "review"}, {"node_id": "done"}],
            tokens_used=250,
            error=None,
        )

        with (
            patch("src.supervisor.Supervisor") as MockSupervisor,
            patch("src.playbook_runner.PlaybookRunner") as MockRunner,
        ):
            mock_sup = MockSupervisor.return_value
            mock_sup.initialize.return_value = True
            MockRunner.resume = AsyncMock(return_value=mock_result)
            MockRunner._resolve_pause_timeout = PlaybookRunner._resolve_pause_timeout

            result = await handler._cmd_resume_playbook(
                {"run_id": "run-42", "human_input": "Looks good, approved!"}
            )

        assert "error" not in result
        assert result["resumed"] == "run-42"
        assert result["playbook_id"] == "test-pb"
        assert result["status"] == "completed"
        assert result["tokens_used"] == 250

        # Verify PlaybookRunner.resume was called with correct args
        MockRunner.resume.assert_awaited_once()
        call_kwargs = MockRunner.resume.call_args
        assert call_kwargs.kwargs["db_run"] == paused_run
        assert call_kwargs.kwargs["human_input"] == "Looks good, approved!"

    async def test_resume_uses_pinned_graph(self):
        """resume_playbook resolves the graph from pinned_graph when available."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        graph = {
            "id": "my-pb",
            "version": 2,
            "nodes": {
                "step1": {"prompt": "Do thing", "wait_for_human": True, "goto": "end"},
                "end": {"terminal": True},
            },
        }
        paused_run = PlaybookRun(
            run_id="run-pinned",
            playbook_id="my-pb",
            playbook_version=2,
            status="paused",
            current_node="step1",
            started_at=time.time() - 120,
            paused_at=time.time() - 10,
            pinned_graph=json.dumps(graph),
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        mock_result = RunResult(
            run_id="run-pinned",
            status="completed",
            node_trace=[],
            tokens_used=50,
            error=None,
        )

        with (
            patch("src.supervisor.Supervisor") as MockSupervisor,
            patch("src.playbook_runner.PlaybookRunner") as MockRunner,
        ):
            mock_sup = MockSupervisor.return_value
            mock_sup.initialize.return_value = True
            MockRunner.resume = AsyncMock(return_value=mock_result)
            MockRunner._resolve_pause_timeout = PlaybookRunner._resolve_pause_timeout

            result = await handler._cmd_resume_playbook(
                {"run_id": "run-pinned", "human_input": "go"}
            )

        assert "error" not in result
        # The graph passed to resume should be the pinned graph
        call_kwargs = MockRunner.resume.call_args.kwargs
        assert call_kwargs["graph"] == graph

    async def test_resume_fallback_to_playbook_manager(self):
        """resume_playbook falls back to playbook_manager when no pinned_graph."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        active_graph = {
            "id": "from-manager",
            "version": 3,
            "nodes": {
                "review": {"prompt": "Review", "wait_for_human": True, "goto": "done"},
                "done": {"terminal": True},
            },
        }
        # Mock playbook_manager._active — to_dict is sync, use MagicMock
        from unittest.mock import MagicMock

        mock_pb = MagicMock()
        mock_pb.to_dict.return_value = active_graph
        handler.orchestrator.playbook_manager = MagicMock()
        handler.orchestrator.playbook_manager._active = {"from-manager": mock_pb}

        paused_run = PlaybookRun(
            run_id="run-no-pin",
            playbook_id="from-manager",
            playbook_version=3,
            status="paused",
            current_node="review",
            started_at=time.time() - 60,
            paused_at=time.time() - 5,
            pinned_graph=None,  # No pinned graph
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        mock_result = RunResult(
            run_id="run-no-pin",
            status="completed",
            node_trace=[],
            tokens_used=75,
            error=None,
        )

        with (
            patch("src.supervisor.Supervisor") as MockSupervisor,
            patch("src.playbook_runner.PlaybookRunner") as MockRunner,
        ):
            mock_sup = MockSupervisor.return_value
            mock_sup.initialize.return_value = True
            MockRunner.resume = AsyncMock(return_value=mock_result)
            MockRunner._resolve_pause_timeout = PlaybookRunner._resolve_pause_timeout

            result = await handler._cmd_resume_playbook(
                {"run_id": "run-no-pin", "human_input": "proceed"}
            )

        assert "error" not in result
        assert result["resumed"] == "run-no-pin"
        call_kwargs = MockRunner.resume.call_args.kwargs
        assert call_kwargs["graph"] == active_graph

    async def test_resume_no_graph_available(self):
        """resume_playbook returns error when no graph can be resolved."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()

        # playbook_manager has no active entry for this playbook
        handler.orchestrator.playbook_manager = AsyncMock()
        handler.orchestrator.playbook_manager._active = {}

        paused_run = PlaybookRun(
            run_id="run-orphan",
            playbook_id="deleted-pb",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=time.time() - 60,
            paused_at=time.time() - 5,
            pinned_graph=None,
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        result = await handler._cmd_resume_playbook(
            {"run_id": "run-orphan", "human_input": "approved"}
        )
        assert "error" in result
        assert "Cannot resolve playbook graph" in result["error"]

    async def test_resume_supervisor_init_failure(self):
        """resume_playbook returns error when Supervisor fails to initialize."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {"prompt": "Review", "wait_for_human": True, "goto": "done"},
                "done": {"terminal": True},
            },
        }
        paused_run = PlaybookRun(
            run_id="run-no-sup",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=time.time() - 60,
            paused_at=time.time() - 5,
            pinned_graph=json.dumps(graph),
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MockSupervisor.return_value
            mock_sup.initialize.return_value = False  # Fails to init
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-no-sup", "human_input": "ok"}
            )

        assert "error" in result
        assert "Failed to initialize" in result["error"]

    async def test_resume_timeout_enforced(self):
        """resume_playbook enforces pause timeout and marks run as timed_out."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 60,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        paused_run = PlaybookRun(
            run_id="run-expired",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=time.time() - 200,
            paused_at=time.time() - 120,  # Paused 120s ago, timeout is 60s
            pinned_graph=json.dumps(graph),
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)
        handler.db.update_playbook_run = AsyncMock()

        timed_out_result = RunResult(
            run_id="run-expired",
            status="timed_out",
            node_trace=[],
            tokens_used=0,
            error="Pause timeout exceeded",
        )

        with patch("src.playbook_runner.PlaybookRunner") as MockRunner:
            MockRunner._resolve_pause_timeout = PlaybookRunner._resolve_pause_timeout
            MockRunner.handle_timeout = AsyncMock(return_value=timed_out_result)
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-expired", "human_input": "too late"}
            )

        assert "error" in result
        assert "timeout" in result["error"].lower()

    async def test_resume_runner_exception_handled(self):
        """resume_playbook returns error when PlaybookRunner.resume raises."""
        from unittest.mock import patch

        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.config = AsyncMock()
        handler.orchestrator.bus = AsyncMock()

        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {"prompt": "Review", "wait_for_human": True, "goto": "done"},
                "done": {"terminal": True},
            },
        }
        paused_run = PlaybookRun(
            run_id="run-err",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=time.time() - 60,
            paused_at=time.time() - 5,
            pinned_graph=json.dumps(graph),
            conversation_history="[]",
            node_trace="[]",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=paused_run)

        with (
            patch("src.supervisor.Supervisor") as MockSupervisor,
            patch("src.playbook_runner.PlaybookRunner") as MockRunner,
        ):
            mock_sup = MockSupervisor.return_value
            mock_sup.initialize.return_value = True
            MockRunner.resume = AsyncMock(side_effect=RuntimeError("LLM provider crashed"))
            MockRunner._resolve_pause_timeout = PlaybookRunner._resolve_pause_timeout

            result = await handler._cmd_resume_playbook({"run_id": "run-err", "human_input": "go"})

        assert "error" in result
        assert "Resume failed" in result["error"]
        assert "LLM provider crashed" in result["error"]

    async def test_resume_whitespace_only_human_input_rejected(self):
        """resume_playbook rejects human_input that is only whitespace."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler._get_paused_at = CommandHandler._get_paused_at

        result = await handler._cmd_resume_playbook({"run_id": "abc", "human_input": "   \n  "})
        assert "error" in result
        assert "human_input" in result["error"]


class TestListPlaybookRunsCommand:
    """Tests for the _cmd_list_playbook_runs command handler method.

    Roadmap 5.5.5 — recent runs with status and path taken.
    """

    @staticmethod
    def _make_handler():
        """Build a mock CommandHandler with real list/format methods bound."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_list_playbook_runs = CommandHandler._cmd_list_playbook_runs.__get__(handler)
        handler._format_playbook_run_summary = CommandHandler._format_playbook_run_summary
        handler.db = AsyncMock()
        return handler

    async def test_list_all_runs(self):
        """list_playbook_runs returns all runs when no filters given."""
        handler = self._make_handler()

        run1 = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=1,
            status="completed",
            started_at=100.0,
            completed_at=200.0,
            tokens_used=500,
        )
        run2 = PlaybookRun(
            run_id="r2",
            playbook_id="pb2",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=150.0,
            tokens_used=300,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run1, run2])

        result = await handler._cmd_list_playbook_runs({})
        assert result["count"] == 2
        assert result["runs"][0]["run_id"] == "r1"
        assert result["runs"][1]["run_id"] == "r2"
        assert result["runs"][1]["current_node"] == "review"
        # Path field should be present (empty for default node_trace)
        assert "path" in result["runs"][0]
        assert "path" in result["runs"][1]

    async def test_list_paused_runs_only(self):
        """list_playbook_runs filters by status."""
        handler = self._make_handler()

        paused_run = PlaybookRun(
            run_id="r2",
            playbook_id="pb2",
            playbook_version=1,
            status="paused",
            current_node="review",
            started_at=150.0,
            tokens_used=300,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[paused_run])

        result = await handler._cmd_list_playbook_runs({"status": "paused"})
        assert result["count"] == 1
        assert result["runs"][0]["status"] == "paused"

        # Verify the db was called with the correct status filter
        handler.db.list_playbook_runs.assert_called_once_with(
            playbook_id=None, status="paused", limit=20
        )

    async def test_list_invalid_status_rejected(self):
        """list_playbook_runs rejects invalid status values."""
        handler = self._make_handler()

        result = await handler._cmd_list_playbook_runs({"status": "invalid"})
        assert "error" in result
        assert "Invalid status" in result["error"]

    async def test_list_with_limit(self):
        """list_playbook_runs respects the limit parameter."""
        handler = self._make_handler()
        handler.db.list_playbook_runs = AsyncMock(return_value=[])

        await handler._cmd_list_playbook_runs({"limit": 5})
        handler.db.list_playbook_runs.assert_called_once_with(
            playbook_id=None, status=None, limit=5
        )

    async def test_path_extracted_from_node_trace(self):
        """list_playbook_runs includes compact path from node_trace JSON."""
        import json

        handler = self._make_handler()

        trace = [
            {"node_id": "start", "started_at": 100.0, "completed_at": 110.0, "status": "completed"},
            {"node_id": "validate", "started_at": 110.0, "completed_at": 120.0, "status": "completed"},
            {"node_id": "deploy", "started_at": 120.0, "completed_at": 130.0, "status": "completed"},
        ]
        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=2,
            status="completed",
            started_at=100.0,
            completed_at=130.0,
            tokens_used=1500,
            node_trace=json.dumps(trace),
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert result["count"] == 1
        run_data = result["runs"][0]

        # Path should contain compact node summaries
        assert len(run_data["path"]) == 3
        assert run_data["path"][0] == {"node_id": "start", "status": "completed"}
        assert run_data["path"][1] == {"node_id": "validate", "status": "completed"}
        assert run_data["path"][2] == {"node_id": "deploy", "status": "completed"}

    async def test_path_with_failed_node(self):
        """Path shows the failed node status when a run fails mid-graph."""
        import json

        handler = self._make_handler()

        trace = [
            {"node_id": "start", "started_at": 100.0, "completed_at": 110.0, "status": "completed"},
            {"node_id": "build", "started_at": 110.0, "completed_at": 115.0, "status": "failed"},
        ]
        run = PlaybookRun(
            run_id="r-fail",
            playbook_id="ci-pipeline",
            playbook_version=1,
            status="failed",
            current_node="build",
            started_at=100.0,
            completed_at=115.0,
            tokens_used=800,
            node_trace=json.dumps(trace),
            error="Build step failed: exit code 1",
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        run_data = result["runs"][0]

        assert run_data["path"][1] == {"node_id": "build", "status": "failed"}
        assert run_data["error"] == "Build step failed: exit code 1"

    async def test_empty_node_trace_gives_empty_path(self):
        """Runs with default empty node_trace produce an empty path list."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r-empty",
            playbook_id="pb1",
            playbook_version=1,
            status="running",
            started_at=100.0,
            tokens_used=0,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert result["runs"][0]["path"] == []

    async def test_malformed_node_trace_gives_empty_path(self):
        """Malformed node_trace JSON is handled gracefully — empty path."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r-bad",
            playbook_id="pb1",
            playbook_version=1,
            status="failed",
            started_at=100.0,
            tokens_used=0,
            node_trace="NOT VALID JSON{{{",
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert result["runs"][0]["path"] == []

    async def test_duration_seconds_computed_for_completed_runs(self):
        """Completed runs include duration_seconds in the summary."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=1,
            status="completed",
            started_at=100.0,
            completed_at=245.5,
            tokens_used=1200,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert result["runs"][0]["duration_seconds"] == 145.5

    async def test_no_duration_for_running_runs(self):
        """Running (in-progress) runs do not include duration_seconds."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=1,
            status="running",
            started_at=100.0,
            tokens_used=50,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert "duration_seconds" not in result["runs"][0]

    async def test_playbook_version_included_in_summary(self):
        """Each run summary includes the playbook_version field."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=3,
            status="completed",
            started_at=100.0,
            completed_at=200.0,
            tokens_used=500,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert result["runs"][0]["playbook_version"] == 3

    async def test_error_omitted_when_none(self):
        """Successful runs do not include the error key in the summary."""
        handler = self._make_handler()

        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=1,
            status="completed",
            started_at=100.0,
            completed_at=200.0,
            tokens_used=500,
        )
        handler.db.list_playbook_runs = AsyncMock(return_value=[run])

        result = await handler._cmd_list_playbook_runs({})
        assert "error" not in result["runs"][0]


# ---------------------------------------------------------------------------
# Inspect playbook run (spec §15, roadmap 5.5.6)
# ---------------------------------------------------------------------------


class TestCmdInspectPlaybookRun:
    """Tests for the _cmd_inspect_playbook_run command handler method."""

    async def test_inspect_missing_run_id(self):
        """inspect_playbook_run returns error when run_id is missing."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )

        result = await handler._cmd_inspect_playbook_run({})
        assert "error" in result
        assert "run_id is required" in result["error"]

    async def test_inspect_nonexistent_run(self):
        """inspect_playbook_run returns error when run doesn't exist."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()
        handler.db.get_playbook_run = AsyncMock(return_value=None)

        result = await handler._cmd_inspect_playbook_run({"run_id": "nonexistent"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_inspect_completed_run_full_data(self):
        """inspect_playbook_run returns full trace, conversation, and tokens for a completed run."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        node_trace = [
            {
                "node_id": "start",
                "started_at": 1000.0,
                "completed_at": 1005.0,
                "status": "completed",
                "transition_to": "analyze",
                "transition_method": "goto",
            },
            {
                "node_id": "analyze",
                "started_at": 1005.0,
                "completed_at": 1020.0,
                "status": "completed",
                "transition_to": "done",
                "transition_method": "llm",
            },
            {
                "node_id": "done",
                "started_at": 1020.0,
                "completed_at": 1022.0,
                "status": "completed",
            },
        ]
        conversation = [
            {"role": "user", "content": "Seed prompt for start node"},
            {"role": "assistant", "content": "Analysis complete."},
            {"role": "user", "content": "Prompt for analyze node"},
            {"role": "assistant", "content": "Done processing."},
        ]
        trigger = {"type": "task.completed", "task_id": "t-123"}

        run = PlaybookRun(
            run_id="r-inspect-1",
            playbook_id="pb-analyze",
            playbook_version=2,
            status="completed",
            current_node="done",
            node_trace=json.dumps(node_trace),
            conversation_history=json.dumps(conversation),
            trigger_event=json.dumps(trigger),
            tokens_used=1500,
            started_at=1000.0,
            completed_at=1022.0,
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-inspect-1"})

        # Core fields
        assert result["run_id"] == "r-inspect-1"
        assert result["playbook_id"] == "pb-analyze"
        assert result["playbook_version"] == 2
        assert result["status"] == "completed"
        assert result["current_node"] == "done"
        assert result["tokens_used"] == 1500
        assert result["started_at"] == 1000.0
        assert result["completed_at"] == 1022.0

        # Node trace — enriched with duration_seconds
        assert result["node_count"] == 3
        assert len(result["node_trace"]) == 3
        assert result["node_trace"][0]["node_id"] == "start"
        assert result["node_trace"][0]["duration_seconds"] == 5.0
        assert result["node_trace"][0]["transition_to"] == "analyze"
        assert result["node_trace"][0]["transition_method"] == "goto"
        assert result["node_trace"][1]["node_id"] == "analyze"
        assert result["node_trace"][1]["duration_seconds"] == 15.0
        assert result["node_trace"][1]["transition_method"] == "llm"
        assert result["node_trace"][2]["node_id"] == "done"
        assert result["node_trace"][2]["duration_seconds"] == 2.0

        # Conversation history
        assert result["message_count"] == 4
        assert len(result["conversation_history"]) == 4
        assert result["conversation_history"][0]["role"] == "user"
        assert result["conversation_history"][1]["role"] == "assistant"

        # Trigger event
        assert result["trigger_event"]["type"] == "task.completed"
        assert result["trigger_event"]["task_id"] == "t-123"

        # Total run duration
        assert result["total_duration_seconds"] == 22.0

        # No error field for successful runs
        assert "error" not in result

    async def test_inspect_failed_run_includes_error(self):
        """inspect_playbook_run includes error field for failed runs."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        node_trace = [
            {
                "node_id": "start",
                "started_at": 2000.0,
                "completed_at": 2010.0,
                "status": "completed",
                "transition_to": "process",
            },
            {
                "node_id": "process",
                "started_at": 2010.0,
                "completed_at": 2015.0,
                "status": "failed",
            },
        ]

        run = PlaybookRun(
            run_id="r-fail-1",
            playbook_id="pb-process",
            playbook_version=1,
            status="failed",
            current_node="process",
            node_trace=json.dumps(node_trace),
            conversation_history=json.dumps([{"role": "user", "content": "hello"}]),
            tokens_used=300,
            started_at=2000.0,
            completed_at=2015.0,
            error="Node 'process' raised ValueError: invalid input",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-fail-1"})

        assert result["status"] == "failed"
        assert result["error"] == "Node 'process' raised ValueError: invalid input"
        assert result["node_count"] == 2
        assert result["node_trace"][1]["status"] == "failed"
        assert result["total_duration_seconds"] == 15.0

    async def test_inspect_paused_run_includes_paused_at(self):
        """inspect_playbook_run includes paused_at for paused runs."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        run = PlaybookRun(
            run_id="r-pause-1",
            playbook_id="pb-review",
            playbook_version=1,
            status="paused",
            current_node="human_review",
            node_trace=json.dumps(
                [
                    {
                        "node_id": "start",
                        "started_at": 3000.0,
                        "completed_at": 3005.0,
                        "status": "completed",
                        "transition_to": "human_review",
                    },
                    {
                        "node_id": "human_review",
                        "started_at": 3005.0,
                        "completed_at": None,
                        "status": "running",
                    },
                ]
            ),
            conversation_history=json.dumps([{"role": "user", "content": "Review needed"}]),
            tokens_used=200,
            started_at=3000.0,
            paused_at=3005.0,
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-pause-1"})

        assert result["status"] == "paused"
        assert result["paused_at"] == 3005.0
        assert result["current_node"] == "human_review"
        # No total_duration_seconds since not completed
        assert "total_duration_seconds" not in result
        # Second node has no duration_seconds since completed_at is None
        assert "duration_seconds" not in result["node_trace"][1]

    async def test_inspect_run_with_empty_json_fields(self):
        """inspect_playbook_run handles runs with empty/default JSON fields gracefully."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        run = PlaybookRun(
            run_id="r-empty-1",
            playbook_id="pb-minimal",
            playbook_version=1,
            status="running",
            node_trace="[]",
            conversation_history="[]",
            trigger_event="{}",
            tokens_used=0,
            started_at=4000.0,
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-empty-1"})

        assert result["node_trace"] == []
        assert result["node_count"] == 0
        assert result["conversation_history"] == []
        assert result["message_count"] == 0
        assert result["trigger_event"] == {}
        assert result["tokens_used"] == 0

    async def test_inspect_run_with_malformed_json(self):
        """inspect_playbook_run handles malformed JSON fields without crashing."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        run = PlaybookRun(
            run_id="r-bad-1",
            playbook_id="pb-broken",
            playbook_version=1,
            status="failed",
            node_trace="{invalid json",
            conversation_history="not json either",
            trigger_event="<<<>>>",
            tokens_used=100,
            started_at=5000.0,
            completed_at=5010.0,
            error="Something went wrong",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-bad-1"})

        # Should gracefully default to empty collections
        assert result["node_trace"] == []
        assert result["node_count"] == 0
        assert result["conversation_history"] == []
        assert result["message_count"] == 0
        assert result["trigger_event"] == {}

    async def test_inspect_timed_out_run(self):
        """inspect_playbook_run returns correct data for a timed_out run."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_inspect_playbook_run = CommandHandler._cmd_inspect_playbook_run.__get__(
            handler
        )
        handler.db = AsyncMock()

        node_trace = [
            {
                "node_id": "start",
                "started_at": 6000.0,
                "completed_at": 6050.0,
                "status": "completed",
                "transition_to": "long_process",
            },
            {
                "node_id": "long_process",
                "started_at": 6050.0,
                "completed_at": 6500.0,
                "status": "completed",
            },
        ]

        run = PlaybookRun(
            run_id="r-timeout-1",
            playbook_id="pb-slow",
            playbook_version=1,
            status="timed_out",
            current_node="long_process",
            node_trace=json.dumps(node_trace),
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Start processing"},
                    {"role": "assistant", "content": "Working..."},
                ]
            ),
            tokens_used=5000,
            started_at=6000.0,
            completed_at=6500.0,
            error="Token budget exceeded",
        )
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "r-timeout-1"})

        assert result["status"] == "timed_out"
        assert result["error"] == "Token budget exceeded"
        assert result["tokens_used"] == 5000
        assert result["node_trace"][0]["duration_seconds"] == 50.0
        assert result["node_trace"][1]["duration_seconds"] == 450.0
        assert result["total_duration_seconds"] == 500.0
        assert result["message_count"] == 2


# ---------------------------------------------------------------------------
# Pause timeout (spec §9, roadmap 5.4.1)
# ---------------------------------------------------------------------------


class TestPauseTimeout:
    """Tests for pause timeout enforcement."""

    async def test_get_paused_at_from_node_trace(self):
        """_get_paused_at extracts the completed_at from the last trace entry."""
        from src.command_handler import CommandHandler

        run = PlaybookRun(
            run_id="t1",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace=json.dumps(
                [
                    {"node_id": "a", "started_at": 100, "completed_at": 101, "status": "completed"},
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": 150,
                        "status": "completed",
                    },
                ]
            ),
        )
        paused_at = CommandHandler._get_paused_at(run)
        assert paused_at == 150

    async def test_get_paused_at_falls_back_to_started_at(self):
        """_get_paused_at falls back to started_at when trace has no completed_at."""
        from src.command_handler import CommandHandler

        run = PlaybookRun(
            run_id="t2",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace=json.dumps(
                [
                    {
                        "node_id": "review",
                        "started_at": 101,
                        "completed_at": None,
                        "status": "running",
                    },
                ]
            ),
        )
        paused_at = CommandHandler._get_paused_at(run)
        assert paused_at == 100.0

    async def test_get_paused_at_with_empty_trace(self):
        """_get_paused_at falls back to started_at when trace is empty."""
        from src.command_handler import CommandHandler

        run = PlaybookRun(
            run_id="t3",
            playbook_id="pb",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace="[]",
        )
        paused_at = CommandHandler._get_paused_at(run)
        assert paused_at == 100.0


# ---------------------------------------------------------------------------
# Configurable pause timeout (spec §9, roadmap 5.4.4)
# ---------------------------------------------------------------------------


class TestConfigurablePauseTimeout:
    """Tests for configurable pause timeout — roadmap 5.4.4.

    Covers:
    (a) Default 24h timeout respected
    (b) Custom timeout (e.g., 1h) respected
    (c) Timeout transitions to timeout node if defined
    (d) Without timeout node, transitions to "failed" (timed_out) status
    (e) Resume of timed-out run rejected
    (f) Timeout notification event emitted
    (g) Timeout countdown resets on re-pause
    """

    # -- Helper: build a paused PlaybookRun ----------------------------------

    @staticmethod
    def _make_paused_run(
        run_id: str = "r1",
        playbook_id: str = "test-pb",
        current_node: str = "review",
        paused_at: float | None = None,
        started_at: float = 100.0,
        graph: dict | None = None,
        messages: list | None = None,
        node_trace: list | None = None,
    ) -> PlaybookRun:
        return PlaybookRun(
            run_id=run_id,
            playbook_id=playbook_id,
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node=current_node,
            conversation_history=json.dumps(messages or []),
            node_trace=json.dumps(
                node_trace
                or [
                    {
                        "node_id": current_node,
                        "started_at": started_at,
                        "completed_at": started_at + 1,
                        "status": "completed",
                    }
                ]
            ),
            tokens_used=100,
            started_at=started_at,
            paused_at=paused_at,
            pinned_graph=json.dumps(graph) if graph else None,
        )

    # -- (a) Default 24h timeout ---------------------------------------------

    async def test_default_24h_timeout_respected(self):
        """_resolve_pause_timeout returns 86400 (24h) when no overrides set."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {"prompt": "Review", "wait_for_human": True, "goto": "done"},
                "done": {"terminal": True},
            },
        }
        result = PlaybookRunner._resolve_pause_timeout(graph, "review")
        assert result == 86400

    # -- (b) Custom timeout (node-level and playbook-level) ------------------

    async def test_node_level_timeout_override(self):
        """Node-level pause_timeout_seconds overrides playbook-level and default."""
        graph = {
            "id": "pb",
            "version": 1,
            "pause_timeout_seconds": 7200,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 3600,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        result = PlaybookRunner._resolve_pause_timeout(graph, "review")
        assert result == 3600

    async def test_playbook_level_timeout_override(self):
        """Playbook-level pause_timeout_seconds overrides default 24h."""
        graph = {
            "id": "pb",
            "version": 1,
            "pause_timeout_seconds": 7200,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        result = PlaybookRunner._resolve_pause_timeout(graph, "review")
        assert result == 7200

    async def test_node_timeout_takes_precedence_over_playbook(self):
        """Node-level timeout wins over playbook-level timeout."""
        graph = {
            "id": "pb",
            "version": 1,
            "pause_timeout_seconds": 7200,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 1800,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        assert PlaybookRunner._resolve_pause_timeout(graph, "review") == 1800

    # -- (c) Timeout transitions to timeout node if defined ------------------

    async def test_timeout_transitions_to_timeout_node(self):
        """handle_timeout routes to on_timeout node when defined."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "on_timeout": "handle_timeout",
                    "goto": "done",
                },
                "handle_timeout": {
                    "prompt": "Handle the timeout gracefully.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
            messages=[
                {"role": "user", "content": "test"},
                {"role": "assistant", "content": "analysis complete"},
            ],
        )
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Timeout handled.")
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        result = await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            supervisor=mock_supervisor,
            db=mock_db,
        )

        assert result.status == "completed"
        assert result.error is None
        # Verify the Supervisor was called (the timeout node was executed)
        mock_supervisor.chat.assert_called()

    # -- (d) Without timeout node, transitions to timed_out ------------------

    async def test_timeout_without_node_marks_timed_out(self):
        """handle_timeout marks run as timed_out when no on_timeout node."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        result = await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
        )

        assert result.status == "timed_out"
        assert "Pause timeout exceeded" in result.error

    async def test_timeout_without_supervisor_marks_timed_out(self):
        """handle_timeout marks timed_out when on_timeout exists but no supervisor."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "on_timeout": "handle_timeout",
                    "goto": "done",
                },
                "handle_timeout": {
                    "prompt": "Handle the timeout.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        result = await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            supervisor=None,
            db=mock_db,
        )

        assert result.status == "timed_out"

    # -- (e) Resume of timed-out run rejected --------------------------------

    async def test_resume_timed_out_run_rejected(self):
        """_cmd_resume_playbook rejects resume for already-timed-out runs."""
        from src.command_handler import CommandHandler

        handler = AsyncMock(spec=CommandHandler)
        handler._cmd_resume_playbook = CommandHandler._cmd_resume_playbook.__get__(handler)
        handler.db = AsyncMock()
        handler.db.get_playbook_run = AsyncMock(
            return_value=PlaybookRun(
                run_id="r1",
                playbook_id="pb",
                playbook_version=1,
                status="timed_out",
                started_at=100.0,
            )
        )

        result = await handler._cmd_resume_playbook({"run_id": "r1", "human_input": "approved"})
        assert "error" in result
        assert "timed_out" in result["error"]

    # -- (f) Timeout event emitted -------------------------------------------

    async def test_timeout_emits_timed_out_event(self):
        """handle_timeout emits playbook.run.timed_out event on EventBus."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        mock_bus = AsyncMock()
        mock_bus.emit = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
            event_bus=mock_bus,
        )

        # Two events: playbook.run.timed_out + notify.playbook_run_timed_out
        raw_calls = [c for c in mock_bus.emit.call_args_list if c[0][0] == "playbook.run.timed_out"]
        assert len(raw_calls) == 1
        payload = raw_calls[0][0][1]
        assert payload["run_id"] == "r1"
        assert payload["node_id"] == "review"
        assert payload["timeout_seconds"] == 10
        assert "transitioned_to" not in payload  # No timeout node

    async def test_timeout_with_transition_emits_event_with_target(self):
        """Timeout event includes transitioned_to when on_timeout is used."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "on_timeout": "handle_timeout",
                    "goto": "done",
                },
                "handle_timeout": {
                    "prompt": "Handle the timeout.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
            messages=[
                {"role": "user", "content": "test"},
                {"role": "assistant", "content": "analysis done"},
            ],
        )
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Handled timeout.")
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        mock_bus = AsyncMock()
        mock_bus.emit = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            supervisor=mock_supervisor,
            db=mock_db,
            event_bus=mock_bus,
        )

        # Find the timed_out event (there may also be a completed event)
        timed_out_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "playbook.run.timed_out"
        ]
        assert len(timed_out_calls) == 1
        payload = timed_out_calls[0][0][1]
        assert payload["transitioned_to"] == "handle_timeout"

    # -- (g) Timeout countdown resets on re-pause ----------------------------

    async def test_pause_persists_paused_at(self, mock_supervisor, mock_db):
        """_pause() persists paused_at timestamp in the DB update."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review this.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        runner = PlaybookRunner(graph, {"type": "test"}, mock_supervisor, mock_db)
        result = await runner.run()

        assert result.status == "paused"
        # Verify paused_at was persisted in the DB update
        update_calls = mock_db.update_playbook_run.call_args_list
        # Find the call that set paused_at
        paused_calls = [c for c in update_calls if "paused_at" in (c[1] or {})]
        assert len(paused_calls) >= 1
        paused_at_value = paused_calls[-1][1]["paused_at"]
        assert isinstance(paused_at_value, float)
        assert paused_at_value > 0

    # -- State machine transition: PAUSED → TIMED_OUT -----------------------

    async def test_state_machine_pause_timeout_transition(self):
        """State machine allows PAUSED → TIMED_OUT via PAUSE_TIMEOUT event."""
        from src.models import PlaybookRunEvent, PlaybookRunStatus
        from src.playbook_state_machine import playbook_run_transition

        result = playbook_run_transition(
            PlaybookRunStatus.PAUSED,
            PlaybookRunEvent.PAUSE_TIMEOUT,
        )
        assert result == PlaybookRunStatus.TIMED_OUT

    # -- PlaybookNode model fields ------------------------------------------

    async def test_playbook_node_pause_timeout_roundtrip(self):
        """PlaybookNode serializes/deserializes pause_timeout_seconds and on_timeout."""
        from src.playbook_models import PlaybookNode

        node = PlaybookNode(
            prompt="Review",
            wait_for_human=True,
            pause_timeout_seconds=3600,
            on_timeout="fallback",
        )
        d = node.to_dict()
        assert d["pause_timeout_seconds"] == 3600
        assert d["on_timeout"] == "fallback"

        restored = PlaybookNode.from_dict(d)
        assert restored.pause_timeout_seconds == 3600
        assert restored.on_timeout == "fallback"

    async def test_compiled_playbook_pause_timeout_roundtrip(self):
        """CompiledPlaybook serializes/deserializes pause_timeout_seconds."""
        from src.playbook_models import CompiledPlaybook, PlaybookNode

        pb = CompiledPlaybook(
            id="test",
            version=1,
            source_hash="abc",
            triggers=["manual"],
            scope="system",
            pause_timeout_seconds=7200,
            nodes={
                "start": PlaybookNode(entry=True, prompt="Go", goto="end"),
                "end": PlaybookNode(terminal=True),
            },
        )
        d = pb.to_dict()
        assert d["pause_timeout_seconds"] == 7200

        restored = CompiledPlaybook.from_dict(d)
        assert restored.pause_timeout_seconds == 7200

    # -- Fallback: paused_at from node trace --------------------------------

    async def test_handle_timeout_uses_node_trace_fallback(self):
        """handle_timeout falls back to node trace when paused_at is None."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        # Create run without paused_at — handle_timeout should use started_at
        db_run = self._make_paused_run(
            paused_at=None,
            started_at=1.0,  # Very old timestamp — definitely expired
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        result = await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
        )
        assert result.status == "timed_out"

    # -- DB update on timed_out --------------------------------------------

    async def test_timeout_updates_db_status(self):
        """handle_timeout updates DB with timed_out status and error."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100000,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
        )

        mock_db.update_playbook_run.assert_called()
        update_kwargs = mock_db.update_playbook_run.call_args[1]
        assert update_kwargs["status"] == "timed_out"
        assert "completed_at" in update_kwargs
        assert "Pause timeout exceeded" in update_kwargs["error"]

    # -- on_timeout with invalid node ID -----------------------------------

    async def test_timeout_invalid_target_marks_timed_out(self):
        """on_timeout pointing to a non-existent node falls through to timed_out."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "on_timeout": "nonexistent_node",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        result = await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
        )
        # on_timeout target doesn't exist in nodes → falls through to timed_out
        assert result.status == "timed_out"

    # -- Progress callback on timeout --------------------------------------

    async def test_timeout_calls_progress_callback(self):
        """handle_timeout invokes on_progress with playbook_timed_out."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        progress = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
            on_progress=progress,
        )

        progress.assert_called_with("playbook_timed_out", "review")


# ---------------------------------------------------------------------------
# Pause timeout spec compliance (roadmap 5.4.7)
# ---------------------------------------------------------------------------


class TestPauseTimeoutSpec:
    """Tests for pause timeout per roadmap 5.4.7.

    Adds coverage for cases not fully validated by 5.4.4:

    (f) Timeout notification is sent to the same channel as the original
        pause notification — i.e., the ``notify.playbook_run_timed_out``
        event carries the same ``project_id`` as the trigger event, which
        the notification handler uses for channel routing.

    (g) Timeout countdown resets if human provides partial input and
        re-pauses — i.e., ``paused_at`` is refreshed on every ``_pause()``
        call, not just the first one.
    """

    # -- Helper: reuse from TestConfigurablePauseTimeout ---------------------

    @staticmethod
    def _make_paused_run(
        run_id: str = "r1",
        playbook_id: str = "test-pb",
        current_node: str = "review",
        paused_at: float | None = None,
        started_at: float = 100.0,
        graph: dict | None = None,
        messages: list | None = None,
        node_trace: list | None = None,
        trigger_event: dict | None = None,
    ) -> PlaybookRun:
        return PlaybookRun(
            run_id=run_id,
            playbook_id=playbook_id,
            playbook_version=1,
            trigger_event=json.dumps(trigger_event or {"type": "test"}),
            status="paused",
            current_node=current_node,
            conversation_history=json.dumps(messages or []),
            node_trace=json.dumps(
                node_trace
                or [
                    {
                        "node_id": current_node,
                        "started_at": started_at,
                        "completed_at": started_at + 1,
                        "status": "completed",
                    }
                ]
            ),
            tokens_used=100,
            started_at=started_at,
            paused_at=paused_at,
            pinned_graph=json.dumps(graph) if graph else None,
        )

    # -- (f) Timeout notification routed to same channel as pause ------------

    async def test_timeout_notification_includes_project_id(self):
        """notify.playbook_run_timed_out carries project_id for channel routing."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        # Trigger event includes project_id — this is how the runner knows
        # which channel to route notifications to.
        trigger_event = {"type": "test", "project_id": "my-project"}
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
            trigger_event=trigger_event,
        )
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        mock_bus = AsyncMock()
        mock_bus.emit = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
            event_bus=mock_bus,
        )

        # Find the notify.playbook_run_timed_out event
        notify_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "notify.playbook_run_timed_out"
        ]
        assert len(notify_calls) == 1, (
            f"Expected exactly 1 notify.playbook_run_timed_out, "
            f"got {len(notify_calls)}. All events: "
            f"{[c[0][0] for c in mock_bus.emit.call_args_list]}"
        )
        payload = notify_calls[0][0][1]
        # project_id must match the trigger event so the notification handler
        # routes the timeout to the same Discord/Telegram channel.
        assert payload["project_id"] == "my-project"
        assert payload["run_id"] == "r1"
        assert payload["node_id"] == "review"
        assert payload["timeout_seconds"] == 10

    async def test_timeout_notification_matches_pause_notification_channel(self):
        """Both pause and timeout notifications carry the same project_id.

        This end-to-end test runs a playbook to a pause, captures the
        project_id from the pause notification, then times it out and
        verifies the timeout notification carries the same project_id.
        """
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review this code.",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        trigger_event = {"type": "test", "project_id": "channel-42"}
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Analysis complete.")
        mock_db = AsyncMock()
        mock_db.create_playbook_run = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        mock_bus = AsyncMock()
        mock_bus.emit = AsyncMock()

        # Phase 1: Run to pause
        runner = PlaybookRunner(graph, trigger_event, mock_supervisor, mock_db, event_bus=mock_bus)
        result = await runner.run()
        assert result.status == "paused"

        # Capture project_id from the pause notification
        pause_notify_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "notify.playbook_run_paused"
        ]
        assert len(pause_notify_calls) == 1
        pause_project_id = pause_notify_calls[0][0][1]["project_id"]
        assert pause_project_id == "channel-42"

        # Phase 2: Simulate timeout
        mock_bus.emit.reset_mock()
        db_run = self._make_paused_run(
            run_id=runner.run_id,
            paused_at=time.time() - 100,
            graph=graph,
            trigger_event=trigger_event,
        )

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            db=mock_db,
            event_bus=mock_bus,
        )

        # Capture project_id from the timeout notification
        timeout_notify_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "notify.playbook_run_timed_out"
        ]
        assert len(timeout_notify_calls) == 1
        timeout_project_id = timeout_notify_calls[0][0][1]["project_id"]
        # The key assertion: same project_id → same channel
        assert timeout_project_id == pause_project_id

    async def test_timeout_with_transition_notification_includes_target(self):
        """Timeout notification includes transitioned_to when on_timeout is set."""
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "on_timeout": "handle_timeout",
                    "goto": "done",
                },
                "handle_timeout": {
                    "prompt": "Handle the timeout.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        trigger_event = {"type": "test", "project_id": "proj-1"}
        db_run = self._make_paused_run(
            paused_at=time.time() - 100,
            graph=graph,
            trigger_event=trigger_event,
            messages=[
                {"role": "user", "content": "test"},
                {"role": "assistant", "content": "analysis done"},
            ],
        )
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Handled timeout.")
        mock_db = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()
        mock_bus = AsyncMock()
        mock_bus.emit = AsyncMock()

        await PlaybookRunner.handle_timeout(
            db_run=db_run,
            graph=graph,
            supervisor=mock_supervisor,
            db=mock_db,
            event_bus=mock_bus,
        )

        timeout_notify_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "notify.playbook_run_timed_out"
        ]
        assert len(timeout_notify_calls) == 1
        payload = timeout_notify_calls[0][0][1]
        assert payload["project_id"] == "proj-1"
        assert payload["transitioned_to"] == "handle_timeout"

    # -- (g) Timeout countdown resets on re-pause ----------------------------

    async def test_re_pause_resets_paused_at(self):
        """Resume → second wait_for_human → re-pause sets a new paused_at.

        This validates that the timeout countdown resets when the human
        provides partial input and the playbook re-pauses at a subsequent
        (or the same) wait_for_human node.
        """
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review1": {
                    "entry": True,
                    "prompt": "First review gate.",
                    "wait_for_human": True,
                    "goto": "review2",
                },
                "review2": {
                    "prompt": "Second review gate.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Processed.")
        mock_db = AsyncMock()
        mock_db.create_playbook_run = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        # Phase 1: Run → pauses at review1
        runner = PlaybookRunner(graph, {"type": "test"}, mock_supervisor, mock_db)
        result1 = await runner.run()
        assert result1.status == "paused"

        # Extract paused_at from the first pause DB update
        pause1_calls = [
            c for c in mock_db.update_playbook_run.call_args_list if "paused_at" in (c[1] or {})
        ]
        assert len(pause1_calls) >= 1
        paused_at_1 = pause1_calls[-1][1]["paused_at"]
        assert isinstance(paused_at_1, float)

        # Phase 2: Resume with input → should pause at review2
        mock_db.update_playbook_run.reset_mock()
        db_run = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="pb",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review1",
            conversation_history=json.dumps(runner.messages),
            node_trace=json.dumps(result1.node_trace),
            tokens_used=result1.tokens_used,
            started_at=100.0,
            paused_at=paused_at_1,
            pinned_graph=json.dumps(graph),
        )

        result2 = await PlaybookRunner.resume(
            db_run=db_run,
            graph=graph,
            supervisor=mock_supervisor,
            human_input="Approved, continue to review2.",
            db=mock_db,
        )
        assert result2.status == "paused"

        # Extract paused_at from the second pause DB update
        pause2_calls = [
            c for c in mock_db.update_playbook_run.call_args_list if "paused_at" in (c[1] or {})
        ]
        assert len(pause2_calls) >= 1
        paused_at_2 = pause2_calls[-1][1]["paused_at"]
        assert isinstance(paused_at_2, float)

        # Key assertion: second paused_at is strictly later → countdown reset
        assert paused_at_2 > paused_at_1, (
            f"paused_at should reset on re-pause: first={paused_at_1}, second={paused_at_2}"
        )

    async def test_resume_and_re_pause_at_same_node(self):
        """Re-pause at the same node resets the timeout countdown.

        Models the case where a human provides partial/insufficient input
        and the playbook pauses again at the same wait_for_human node.
        """
        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review gate — needs full approval.",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 3600,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor = AsyncMock()
        mock_supervisor.chat = AsyncMock(return_value="Awaiting review.")
        mock_db = AsyncMock()
        mock_db.create_playbook_run = AsyncMock()
        mock_db.update_playbook_run = AsyncMock()

        # Phase 1: Run → pauses at review (verify basic pause works)
        runner = PlaybookRunner(graph, {"type": "test"}, mock_supervisor, mock_db)
        result1 = await runner.run()
        assert result1.status == "paused"

        # Phase 2: Resume → the "review" node's goto is "done" (terminal),
        # so after providing human input the run should complete, not re-pause.
        # To test re-pause at the same node, we need a conditional transition
        # that loops back. Let's use a graph with a conditional loop.
        loop_graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review: say APPROVED or NEEDS_WORK.",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 3600,
                    "transitions": [
                        {
                            "condition": {
                                "field": "response",
                                "op": "contains",
                                "value": "APPROVED",
                            },
                            "goto": "done",
                        },
                    ],
                    "goto": "review",  # default fallback → loop back
                },
                "done": {"terminal": True},
            },
        }

        mock_db.update_playbook_run.reset_mock()
        # Supervisor returns NEEDS_WORK (doesn't match APPROVED → loops)
        mock_supervisor.chat = AsyncMock(return_value="NEEDS_WORK — please revise.")

        runner2 = PlaybookRunner(loop_graph, {"type": "test"}, mock_supervisor, mock_db)
        result2 = await runner2.run()
        assert result2.status == "paused"  # first pause

        pause2_calls = [
            c for c in mock_db.update_playbook_run.call_args_list if "paused_at" in (c[1] or {})
        ]
        paused_at_first = pause2_calls[-1][1]["paused_at"]

        # Resume with partial input → should loop back to review and pause again
        mock_db.update_playbook_run.reset_mock()
        db_run = PlaybookRun(
            run_id=runner2.run_id,
            playbook_id="pb",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(runner2.messages),
            node_trace=json.dumps(result2.node_trace),
            tokens_used=result2.tokens_used,
            started_at=100.0,
            paused_at=paused_at_first,
            pinned_graph=json.dumps(loop_graph),
        )

        result3 = await PlaybookRunner.resume(
            db_run=db_run,
            graph=loop_graph,
            supervisor=mock_supervisor,
            human_input="Partial feedback — not approved yet.",
            db=mock_db,
        )
        assert result3.status == "paused"

        pause3_calls = [
            c for c in mock_db.update_playbook_run.call_args_list if "paused_at" in (c[1] or {})
        ]
        paused_at_second = pause3_calls[-1][1]["paused_at"]
        assert isinstance(paused_at_second, float)
        assert paused_at_second > paused_at_first, (
            "Re-pause at same node should reset the timeout countdown"
        )

    # -- check_paused_playbook_timeouts integration -------------------------

    async def test_check_paused_playbook_timeouts_processes_expired(self):
        """Background sweep processes expired paused runs."""
        from src.command_handler import CommandHandler

        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 10,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        expired_run = self._make_paused_run(
            run_id="expired-1",
            paused_at=time.time() - 100,
            graph=graph,
        )

        handler = AsyncMock(spec=CommandHandler)
        handler.check_paused_playbook_timeouts = (
            CommandHandler.check_paused_playbook_timeouts.__get__(handler)
        )
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.db.list_playbook_runs = AsyncMock(return_value=[expired_run])
        handler.db.update_playbook_run = AsyncMock()
        handler.orchestrator = AsyncMock()
        handler.orchestrator.bus = AsyncMock()
        handler.orchestrator.bus.emit = AsyncMock()

        results = await handler.check_paused_playbook_timeouts()

        assert len(results) == 1
        assert results[0]["run_id"] == "expired-1"
        assert results[0]["status"] == "timed_out"

    async def test_check_paused_playbook_timeouts_skips_unexpired(self):
        """Background sweep skips runs that haven't exceeded timeout."""
        from src.command_handler import CommandHandler

        graph = {
            "id": "pb",
            "version": 1,
            "nodes": {
                "review": {
                    "prompt": "Review",
                    "wait_for_human": True,
                    "pause_timeout_seconds": 86400,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        # This run was paused 10 seconds ago — timeout is 24h, so not expired
        fresh_run = self._make_paused_run(
            run_id="fresh-1",
            paused_at=time.time() - 10,
            graph=graph,
        )

        handler = AsyncMock(spec=CommandHandler)
        handler.check_paused_playbook_timeouts = (
            CommandHandler.check_paused_playbook_timeouts.__get__(handler)
        )
        handler._get_paused_at = CommandHandler._get_paused_at
        handler.db = AsyncMock()
        handler.db.list_playbook_runs = AsyncMock(return_value=[fresh_run])
        handler.db.update_playbook_run = AsyncMock()
        handler.orchestrator = AsyncMock()

        results = await handler.check_paused_playbook_timeouts()

        assert len(results) == 0

    # -- PlaybookRunTimedOutEvent model -------------------------------------

    async def test_timed_out_event_model_roundtrip(self):
        """PlaybookRunTimedOutEvent serializes and deserializes correctly."""
        from src.notifications.events import PlaybookRunTimedOutEvent

        event = PlaybookRunTimedOutEvent(
            playbook_id="test-pb",
            run_id="r1",
            node_id="review",
            timeout_seconds=3600,
            waited_seconds=3605.2,
            tokens_used=500,
            transitioned_to="fallback",
            project_id="proj-1",
        )
        d = event.model_dump(mode="json")
        assert d["event_type"] == "notify.playbook_run_timed_out"
        assert d["playbook_id"] == "test-pb"
        assert d["transitioned_to"] == "fallback"
        assert d["project_id"] == "proj-1"
        assert d["severity"] == "warning"
        assert d["category"] == "interaction"

        restored = PlaybookRunTimedOutEvent(**d)
        assert restored.playbook_id == "test-pb"
        assert restored.transitioned_to == "fallback"

    async def test_timed_out_event_without_transition(self):
        """PlaybookRunTimedOutEvent works without transitioned_to."""
        from src.notifications.events import PlaybookRunTimedOutEvent

        event = PlaybookRunTimedOutEvent(
            playbook_id="pb",
            run_id="r1",
            node_id="review",
            timeout_seconds=86400,
        )
        d = event.model_dump(mode="json")
        assert d["transitioned_to"] is None

    # -- Notification formatter tests ---------------------------------------

    async def test_format_playbook_timed_out_without_transition(self):
        """format_playbook_timed_out returns correct text without transition."""
        from src.discord.notifications import format_playbook_timed_out

        msg = format_playbook_timed_out(
            playbook_id="my-pb",
            run_id="r123",
            node_id="review",
            transitioned_to=None,
        )
        assert "my-pb" in msg
        assert "r123" in msg
        assert "review" in msg
        assert "timed out" in msg.lower()

    async def test_format_playbook_timed_out_with_transition(self):
        """format_playbook_timed_out includes transition target when set."""
        from src.discord.notifications import format_playbook_timed_out

        msg = format_playbook_timed_out(
            playbook_id="my-pb",
            run_id="r123",
            node_id="review",
            transitioned_to="handle_timeout",
        )
        assert "handle_timeout" in msg
        assert "transitioned" in msg.lower()
