"""Tests for PlaybookRun state machine — formal transition validation.

Tests the state machine defined in src/playbooks/state_machine.py and its
integration with PlaybookRunner (src/playbooks/runner.py).

Coverage:
- All valid transitions produce the correct target status
- Invalid transitions raise InvalidPlaybookRunTransition
- Terminal state detection
- Status-only validation (is_valid_playbook_run_transition)
- Runner integration: state machine is invoked on every status change
- Runner integration: status tracked correctly through full lifecycle
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.models import PlaybookRun, PlaybookRunEvent, PlaybookRunStatus
from src.playbooks.runner import PlaybookRunner
from src.playbooks.state_machine import (
    TERMINAL_STATUSES,
    VALID_PLAYBOOK_RUN_STATUS_TRANSITIONS,
    VALID_PLAYBOOK_RUN_TRANSITIONS,
    InvalidPlaybookRunTransition,
    is_terminal,
    is_valid_playbook_run_transition,
    playbook_run_transition,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supervisor():
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary.")
    return supervisor


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.create_playbook_run = AsyncMock()
    db.update_playbook_run = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    return db


@pytest.fixture
def event_data():
    return {"type": "test.event", "project_id": "proj-1"}


# ---------------------------------------------------------------------------
# State machine: valid transitions
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Every defined (status, event) pair produces the expected target."""

    def test_running_to_completed(self):
        target = playbook_run_transition(
            PlaybookRunStatus.RUNNING, PlaybookRunEvent.TERMINAL_REACHED
        )
        assert target == PlaybookRunStatus.COMPLETED

    def test_running_to_failed_node_failed(self):
        target = playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunEvent.NODE_FAILED)
        assert target == PlaybookRunStatus.FAILED

    def test_running_to_failed_transition_failed(self):
        target = playbook_run_transition(
            PlaybookRunStatus.RUNNING, PlaybookRunEvent.TRANSITION_FAILED
        )
        assert target == PlaybookRunStatus.FAILED

    def test_running_to_failed_graph_error(self):
        target = playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunEvent.GRAPH_ERROR)
        assert target == PlaybookRunStatus.FAILED

    def test_running_to_failed_on_budget_exceeded(self):
        target = playbook_run_transition(
            PlaybookRunStatus.RUNNING, PlaybookRunEvent.BUDGET_EXCEEDED
        )
        assert target == PlaybookRunStatus.FAILED

    def test_running_to_paused(self):
        target = playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunEvent.HUMAN_WAIT)
        assert target == PlaybookRunStatus.PAUSED

    def test_paused_to_running(self):
        target = playbook_run_transition(PlaybookRunStatus.PAUSED, PlaybookRunEvent.HUMAN_RESUMED)
        assert target == PlaybookRunStatus.RUNNING


class TestTransitionTableCompleteness:
    """The transition table covers all expected paths."""

    def test_total_transition_count(self):
        """Seven transitions are defined per the spec."""
        assert len(VALID_PLAYBOOK_RUN_TRANSITIONS) == 7

    def test_running_has_six_outgoing_transitions(self):
        """RUNNING can transition to COMPLETED, FAILED (x4), PAUSED."""
        running_transitions = [
            (s, e) for (s, e) in VALID_PLAYBOOK_RUN_TRANSITIONS if s == PlaybookRunStatus.RUNNING
        ]
        assert len(running_transitions) == 6

    def test_paused_has_one_outgoing_transition(self):
        """PAUSED can only transition to RUNNING (via HUMAN_RESUMED)."""
        paused_transitions = [
            (s, e) for (s, e) in VALID_PLAYBOOK_RUN_TRANSITIONS if s == PlaybookRunStatus.PAUSED
        ]
        assert len(paused_transitions) == 1

    def test_terminal_states_have_no_outgoing_transitions(self):
        """COMPLETED, FAILED, TIMED_OUT have no outgoing transitions."""
        for status in TERMINAL_STATUSES:
            outgoing = [(s, e) for (s, e) in VALID_PLAYBOOK_RUN_TRANSITIONS if s == status]
            assert outgoing == [], f"{status.value} should have no outgoing transitions"

    def test_all_events_are_used(self):
        """Every PlaybookRunEvent appears in at least one transition."""
        used_events = {e for (_, e) in VALID_PLAYBOOK_RUN_TRANSITIONS}
        all_events = set(PlaybookRunEvent)
        assert used_events == all_events


# ---------------------------------------------------------------------------
# State machine: invalid transitions
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Attempting undefined transitions raises InvalidPlaybookRunTransition."""

    def test_completed_cannot_transition(self):
        for event in PlaybookRunEvent:
            with pytest.raises(InvalidPlaybookRunTransition):
                playbook_run_transition(PlaybookRunStatus.COMPLETED, event)

    def test_failed_cannot_transition(self):
        for event in PlaybookRunEvent:
            with pytest.raises(InvalidPlaybookRunTransition):
                playbook_run_transition(PlaybookRunStatus.FAILED, event)

    def test_timed_out_cannot_transition(self):
        for event in PlaybookRunEvent:
            with pytest.raises(InvalidPlaybookRunTransition):
                playbook_run_transition(PlaybookRunStatus.TIMED_OUT, event)

    def test_paused_rejects_non_resume_events(self):
        non_resume_events = [e for e in PlaybookRunEvent if e != PlaybookRunEvent.HUMAN_RESUMED]
        for event in non_resume_events:
            with pytest.raises(InvalidPlaybookRunTransition):
                playbook_run_transition(PlaybookRunStatus.PAUSED, event)

    def test_running_rejects_human_resumed(self):
        """HUMAN_RESUMED only makes sense from PAUSED, not RUNNING."""
        with pytest.raises(InvalidPlaybookRunTransition):
            playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunEvent.HUMAN_RESUMED)

    def test_exception_carries_state_and_event(self):
        try:
            playbook_run_transition(PlaybookRunStatus.COMPLETED, PlaybookRunEvent.TERMINAL_REACHED)
        except InvalidPlaybookRunTransition as exc:
            assert exc.state == PlaybookRunStatus.COMPLETED
            assert exc.event == PlaybookRunEvent.TERMINAL_REACHED
            assert "completed" in str(exc)
            assert "TERMINAL_REACHED" in str(exc)


# ---------------------------------------------------------------------------
# Terminal state detection
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_completed_is_terminal(self):
        assert is_terminal(PlaybookRunStatus.COMPLETED)

    def test_failed_is_terminal(self):
        assert is_terminal(PlaybookRunStatus.FAILED)

    def test_timed_out_is_terminal(self):
        assert is_terminal(PlaybookRunStatus.TIMED_OUT)

    def test_running_is_not_terminal(self):
        assert not is_terminal(PlaybookRunStatus.RUNNING)

    def test_paused_is_not_terminal(self):
        assert not is_terminal(PlaybookRunStatus.PAUSED)


# ---------------------------------------------------------------------------
# Status-only validation (no event required)
# ---------------------------------------------------------------------------


class TestStatusOnlyValidation:
    def test_running_to_completed_is_valid(self):
        assert is_valid_playbook_run_transition(
            PlaybookRunStatus.RUNNING, PlaybookRunStatus.COMPLETED
        )

    def test_running_to_failed_is_valid(self):
        assert is_valid_playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunStatus.FAILED)

    def test_running_to_timed_out_is_no_longer_valid(self):
        """BUDGET_EXCEEDED now maps to FAILED, so RUNNING→TIMED_OUT is unused."""
        assert not is_valid_playbook_run_transition(
            PlaybookRunStatus.RUNNING, PlaybookRunStatus.TIMED_OUT
        )

    def test_running_to_paused_is_valid(self):
        assert is_valid_playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunStatus.PAUSED)

    def test_paused_to_running_is_valid(self):
        assert is_valid_playbook_run_transition(PlaybookRunStatus.PAUSED, PlaybookRunStatus.RUNNING)

    def test_completed_to_running_is_invalid(self):
        assert not is_valid_playbook_run_transition(
            PlaybookRunStatus.COMPLETED, PlaybookRunStatus.RUNNING
        )

    def test_failed_to_running_is_invalid(self):
        assert not is_valid_playbook_run_transition(
            PlaybookRunStatus.FAILED, PlaybookRunStatus.RUNNING
        )

    def test_paused_to_completed_is_invalid(self):
        """PAUSED must go through RUNNING first (via HUMAN_RESUMED)."""
        assert not is_valid_playbook_run_transition(
            PlaybookRunStatus.PAUSED, PlaybookRunStatus.COMPLETED
        )

    def test_derived_set_count(self):
        """The derived status transition set should have 4 unique pairs."""
        # running→completed, running→failed, running→paused, paused→running
        assert len(VALID_PLAYBOOK_RUN_STATUS_TRANSITIONS) == 4


# ---------------------------------------------------------------------------
# validate_transition helper
# ---------------------------------------------------------------------------


class TestValidateTransition:
    def test_valid_transition_returns_target(self):
        target = validate_transition(
            PlaybookRunStatus.RUNNING,
            PlaybookRunEvent.TERMINAL_REACHED,
            run_id="test-run",
        )
        assert target == PlaybookRunStatus.COMPLETED

    def test_invalid_transition_raises(self):
        with pytest.raises(InvalidPlaybookRunTransition):
            validate_transition(
                PlaybookRunStatus.COMPLETED,
                PlaybookRunEvent.TERMINAL_REACHED,
                run_id="test-run",
            )


# ---------------------------------------------------------------------------
# Runner integration: state machine drives status values
# ---------------------------------------------------------------------------


class TestRunnerStateMachineIntegration:
    """Verify that PlaybookRunner uses the state machine for all transitions."""

    async def test_successful_run_transitions_running_to_completed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Happy path: status goes running → completed."""
        graph = {
            "id": "sm-test",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        assert runner._status == PlaybookRunStatus.RUNNING
        result = await runner.run()
        assert result.status == "completed"
        assert runner._status == PlaybookRunStatus.COMPLETED

    async def test_failed_run_transitions_running_to_failed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Node execution error: status goes running → failed."""
        graph = {
            "id": "sm-fail",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.side_effect = RuntimeError("LLM error")
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "failed"
        assert runner._status == PlaybookRunStatus.FAILED

    async def test_budget_exceeded_transitions_running_to_failed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Budget exceeded: status goes running → failed (spec §6)."""
        graph = {
            "id": "sm-timeout",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "A.", "goto": "b"},
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "failed"
        assert runner._status == PlaybookRunStatus.FAILED

    async def test_paused_run_transitions_running_to_paused(
        self, mock_supervisor, mock_db, event_data
    ):
        """Human-in-the-loop: status goes running → paused."""
        graph = {
            "id": "sm-pause",
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
        mock_supervisor.chat.return_value = "Review result."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "paused"
        assert runner._status == PlaybookRunStatus.PAUSED

    async def test_no_entry_node_transitions_running_to_failed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Missing entry node: status goes running → failed (GRAPH_ERROR)."""
        graph = {
            "id": "sm-no-entry",
            "version": 1,
            "nodes": {
                "a": {"prompt": "A."},
                "b": {"prompt": "B."},
            },
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "failed"
        assert runner._status == PlaybookRunStatus.FAILED
        assert "entry" in result.error.lower()

    async def test_missing_node_transitions_running_to_failed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Node not found in graph: status goes running → failed (GRAPH_ERROR)."""
        graph = {
            "id": "sm-missing-node",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "nonexistent"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "failed"
        assert runner._status == PlaybookRunStatus.FAILED
        assert "nonexistent" in result.error

    async def test_transition_eval_failure_transitions_running_to_failed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Transition evaluation error: running → failed (TRANSITION_FAILED)."""
        graph = {
            "id": "sm-transition-fail",
            "version": 1,
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Scan.",
                    "transitions": [
                        {"when": "good", "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }
        # First call succeeds (node execution), second call fails (transition eval)
        call_count = 0

        async def chat_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Scan results."
            raise RuntimeError("LLM transition eval error")

        mock_supervisor.chat.side_effect = chat_side_effect
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)

        result = await runner.run()
        assert result.status == "failed"
        assert runner._status == PlaybookRunStatus.FAILED
        assert "Transition" in result.error


class TestRunnerResumeStateMachine:
    """Verify state machine integration in the resume() classmethod."""

    async def test_resume_transitions_paused_to_running_to_completed(
        self, mock_supervisor, mock_db, event_data
    ):
        """Resume: paused → running → completed."""
        graph = {
            "id": "sm-resume",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        # First run: pauses
        mock_supervisor.chat.return_value = "Review result."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        run_result = await runner.run()
        assert run_result.status == "paused"

        # Build the persisted DB record
        paused_run = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="sm-resume",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(runner.messages),
            node_trace=json.dumps(run_result.node_trace),
            tokens_used=run_result.tokens_used,
            started_at=1000.0,
            pinned_graph=json.dumps(graph),
        )

        # Resume with human input
        resume_result = await PlaybookRunner.resume(
            paused_run, graph, mock_supervisor, "Looks good!", db=mock_db
        )
        assert resume_result.status == "completed"

    async def test_resume_missing_current_node_fails(self, mock_supervisor, mock_db, event_data):
        """Resume with no current_node recorded: transitions to failed."""
        graph = {
            "id": "sm-resume-fail",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        paused_run = PlaybookRun(
            run_id="resume-fail-1",
            playbook_id="sm-resume-fail",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node=None,  # Missing!
            conversation_history="[]",
            node_trace="[]",
            started_at=1000.0,
        )

        result = await PlaybookRunner.resume(
            paused_run, graph, mock_supervisor, "Input", db=mock_db
        )
        assert result.status == "failed"
        assert "current_node" in result.error

    async def test_resume_missing_node_in_graph_fails(self, mock_supervisor, mock_db, event_data):
        """Resume with a node ID not in graph: transitions to failed."""
        graph = {
            "id": "sm-resume-missing",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        paused_run = PlaybookRun(
            run_id="resume-fail-2",
            playbook_id="sm-resume-missing",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="nonexistent",  # Not in graph
            conversation_history="[]",
            node_trace="[]",
            started_at=1000.0,
        )

        result = await PlaybookRunner.resume(
            paused_run, graph, mock_supervisor, "Input", db=mock_db
        )
        assert result.status == "failed"
        assert "nonexistent" in result.error

    async def test_resume_transition_eval_failure(self, mock_supervisor, mock_db, event_data):
        """Resume where transition evaluation fails: transitions to failed."""
        graph = {
            "id": "sm-resume-transition-fail",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "transitions": [
                        {"when": "approved", "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        paused_run = PlaybookRun(
            run_id="resume-transition-fail",
            playbook_id="sm-resume-transition-fail",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Event received."},
                    {"role": "user", "content": "Review."},
                    {"role": "assistant", "content": "Review result."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "review",
                        "started_at": 1000.0,
                        "completed_at": 1001.0,
                        "status": "completed",
                    },
                ]
            ),
            started_at=1000.0,
        )

        # Transition evaluation will fail
        mock_supervisor.chat.side_effect = RuntimeError("LLM error")

        result = await PlaybookRunner.resume(
            paused_run, graph, mock_supervisor, "Approve", db=mock_db
        )
        assert result.status == "failed"
        assert "Transition" in result.error


class TestRunnerStatusPersistedCorrectly:
    """Verify the persisted status values match the state machine output."""

    async def test_completed_status_persisted_via_enum(self, mock_supervisor, mock_db, event_data):
        """DB update uses the enum .value, not a hardcoded string."""
        graph = {
            "id": "persist-test",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Done."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        final_call = mock_db.update_playbook_run.call_args_list[-1]
        assert final_call.kwargs["status"] == "completed"

    async def test_failed_status_persisted_via_enum(self, mock_supervisor, mock_db, event_data):
        graph = {
            "id": "persist-fail",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.side_effect = RuntimeError("Error")
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        fail_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed":
                fail_call = call
                break
        assert fail_call is not None

    async def test_budget_exceeded_status_persisted_via_enum(
        self, mock_supervisor, mock_db, event_data
    ):
        graph = {
            "id": "persist-timeout",
            "version": 1,
            "max_tokens": 10,
            "nodes": {
                "a": {"entry": True, "prompt": "A.", "goto": "b"},
                "b": {"prompt": "B.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "x" * 200
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        budget_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "failed" and "token_budget_exceeded" in (
                call.kwargs.get("error") or ""
            ):
                budget_call = call
                break
        assert budget_call is not None

    async def test_paused_status_persisted_via_enum(self, mock_supervisor, mock_db, event_data):
        graph = {
            "id": "persist-pause",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_supervisor.chat.return_value = "Review."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        pause_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                pause_call = call
                break
        assert pause_call is not None


class TestRunnerStatusField:
    """Verify the runner's _status field accurately tracks state."""

    async def test_initial_status_is_running(self, mock_supervisor, event_data):
        graph = {
            "id": "status-field",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "done"},
                "done": {"terminal": True},
            },
        }
        runner = PlaybookRunner(graph, event_data, mock_supervisor)
        assert runner._status == PlaybookRunStatus.RUNNING

    async def test_status_after_each_transition(self, mock_supervisor, mock_db, event_data):
        """Full lifecycle: running → paused → running → completed."""
        graph = {
            "id": "full-lifecycle",
            "version": 1,
            "nodes": {
                "review": {
                    "entry": True,
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        # Phase 1: run until pause
        mock_supervisor.chat.return_value = "Review result."
        runner = PlaybookRunner(graph, event_data, mock_supervisor, db=mock_db)
        assert runner._status == PlaybookRunStatus.RUNNING

        result = await runner.run()
        assert runner._status == PlaybookRunStatus.PAUSED

        # Phase 2: resume to completion
        paused_run = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="full-lifecycle",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(runner.messages),
            node_trace=json.dumps(result.node_trace),
            tokens_used=result.tokens_used,
            started_at=1000.0,
            pinned_graph=json.dumps(graph),
        )

        resume_result = await PlaybookRunner.resume(
            paused_run, graph, mock_supervisor, "Approved!", db=mock_db
        )
        assert resume_result.status == "completed"
