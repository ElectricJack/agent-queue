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
import json
from unittest.mock import AsyncMock

import pytest

from src.models import PlaybookRun
from src.playbook_runner import (
    NodeTraceEntry,
    PlaybookRunner,
    _compare,
    _dot_get,
    _estimate_tokens,
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
            "[Context summary of prior steps]" in m.get("content", "")
            for m in runner.messages
        )

    async def test_summarize_skipped_when_insufficient_history(
        self, mock_supervisor, event_data
    ):
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

        runner = PlaybookRunner(
            graph, event_data, mock_supervisor, on_progress=capture_progress
        )
        await runner.run()

        # There should be a node_summarizing event
        summarizing_events = [e for e in progress_events if e[0] == "node_summarizing"]
        assert len(summarizing_events) == 1

    async def test_summarize_transcript_includes_all_messages(
        self, mock_supervisor, event_data
    ):
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

    async def test_timed_out_run_persists_status(self, mock_supervisor, event_data, mock_db):
        """Token budget exhaustion should persist timed_out status."""
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

        assert result.status == "timed_out"

        # Find the timed_out update
        timeout_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "timed_out":
                timeout_call = call
                break

        assert timeout_call is not None
        assert "Token budget exceeded" in timeout_call.kwargs["error"]


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

        mock_supervisor.chat.return_value = json.dumps(
            {"approval": "yes", "comment": "Looks good"}
        )
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
        """All structured conditions + no otherwise + no match → None (implicit end)."""
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

        assert result.status == "completed"
        # transition_to is None → omitted from dict; transition_method is "none"
        assert "transition_to" not in result.node_trace[0]
        assert result.node_trace[0]["transition_method"] == "none"


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
        responses = iter([
            "Found 5 issues in the codebase.",
            "Grouped: 2 critical, 2 warnings, 1 info.",
            "Report: 5 total findings across 3 severity levels.",
        ])
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
        assert calls[2].kwargs["text"] == (
            "Generate a summary report of the grouped findings."
        )

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
        responses = iter([
            "Analysis: found 5 issues.",
            "Grouped into 3 categories.",
            "Summary report generated.",
        ])
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
    async def test_single_node_playbook_executes_and_completes(
        self, mock_supervisor, event_data
    ):
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

    async def test_single_node_with_db_persistence(
        self, mock_supervisor, event_data, mock_db
    ):
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
