"""Tests for playbook.run.completed and playbook.run.failed event emission.

Roadmap 5.3.6 — Emit ``playbook.run.completed`` and ``playbook.run.failed``
events per playbooks spec Section 7 (Event System).

Tests verify:
- Events are emitted on the EventBus at the right lifecycle points
- Payloads contain all required fields per event_schemas.py
- Optional fields (project_id, tokens_used, duration_seconds) are included
- Events are NOT emitted when no EventBus is configured
- Failure events include failed_at_node and error details
- Resume paths also emit events correctly
- EventBus errors don't crash the runner
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.models import PlaybookRun
from src.playbooks.runner import PlaybookRunner


# ---------------------------------------------------------------------------
# Fixtures
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
def event_bus():
    """A real EventBus instance (validation disabled for test simplicity)."""
    return EventBus(validate_events=False)


@pytest.fixture
def simple_graph():
    """A minimal 2-node linear playbook: scan -> done."""
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
def failing_graph():
    """A graph that references a missing node to trigger failure."""
    return {
        "id": "fail-playbook",
        "version": 1,
        "nodes": {
            "start": {
                "entry": True,
                "prompt": "Begin.",
                "goto": "missing_node",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def no_entry_graph():
    """A graph with no entry node."""
    return {
        "id": "no-entry-playbook",
        "version": 1,
        "nodes": {
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def event_data():
    """Sample trigger event with project_id."""
    return {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}


@pytest.fixture
def event_data_no_project():
    """Sample trigger event without project_id."""
    return {"type": "timer.5m", "tick_time": 1234567890}


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
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


# ---------------------------------------------------------------------------
# Completed event emission
# ---------------------------------------------------------------------------


class TestCompletedEventEmission:
    """Tests for playbook.run.completed event."""

    async def test_completed_event_emitted_on_success(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Successful run emits playbook.run.completed with required fields."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "completed"
        assert len(captured) == 1

        payload = captured[0]
        assert payload["playbook_id"] == "test-playbook"
        assert payload["run_id"] == runner.run_id
        assert payload["_event_type"] == "playbook.run.completed"

    async def test_completed_event_includes_final_context(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Completed event includes final_context from the last response."""
        mock_supervisor.chat = AsyncMock(return_value="Final analysis complete.")
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert captured[0]["final_context"] == "Final analysis complete."

    async def test_completed_event_includes_project_id(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Completed event includes project_id from trigger event."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert captured[0]["project_id"] == "test-proj"

    async def test_completed_event_omits_project_id_when_absent(
        self, mock_supervisor, simple_graph, event_data_no_project, event_bus
    ):
        """Completed event does not include project_id when trigger lacks one."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(
            simple_graph, event_data_no_project, mock_supervisor, event_bus=event_bus
        )
        await runner.run()

        assert "project_id" not in captured[0]

    async def test_completed_event_includes_tokens_used(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Completed event includes tokens_used."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert "tokens_used" in captured[0]
        assert isinstance(captured[0]["tokens_used"], int)

    async def test_completed_event_includes_duration(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Completed event includes duration_seconds."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert "duration_seconds" in captured[0]
        assert captured[0]["duration_seconds"] >= 0

    async def test_no_event_without_bus(self, mock_supervisor, simple_graph, event_data):
        """No event is emitted when event_bus is None."""
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor)
        result = await runner.run()

        # Should complete without errors — no bus, no emission
        assert result.status == "completed"

    async def test_completed_event_with_db(
        self, mock_supervisor, mock_db, simple_graph, event_data, event_bus
    ):
        """Completed event is emitted even when DB persistence is active."""
        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(
            simple_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        assert len(captured) == 1
        assert captured[0]["playbook_id"] == "test-playbook"


# ---------------------------------------------------------------------------
# Failed event emission
# ---------------------------------------------------------------------------


class TestFailedEventEmission:
    """Tests for playbook.run.failed event."""

    async def test_failed_event_on_missing_node(
        self, mock_supervisor, failing_graph, event_data, event_bus
    ):
        """Failed event emitted when graph references a non-existent node."""
        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(failing_graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "failed"
        assert len(captured) == 1

        payload = captured[0]
        assert payload["playbook_id"] == "fail-playbook"
        assert payload["run_id"] == runner.run_id
        assert payload["_event_type"] == "playbook.run.failed"
        assert "missing_node" in payload.get("error", "")

    async def test_failed_event_on_no_entry_node(
        self, mock_supervisor, no_entry_graph, event_data, event_bus
    ):
        """Failed event emitted when no entry node exists."""
        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "failed"
        assert len(captured) == 1

        payload = captured[0]
        assert payload["failed_at_node"] == "<unknown>"
        assert "No entry node" in payload.get("error", "")

    async def test_failed_event_includes_failed_at_node(
        self, mock_supervisor, event_data, event_bus
    ):
        """Failed event includes the node where execution failed."""
        mock_supervisor.chat = AsyncMock(side_effect=RuntimeError("LLM error"))

        graph = {
            "id": "node-fail-playbook",
            "version": 1,
            "nodes": {
                "step1": {
                    "entry": True,
                    "prompt": "Do something.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "failed"
        assert len(captured) == 1
        assert captured[0]["failed_at_node"] == "step1"

    async def test_failed_event_includes_project_id(
        self, mock_supervisor, no_entry_graph, event_data, event_bus
    ):
        """Failed event includes project_id from trigger event."""
        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert captured[0]["project_id"] == "test-proj"

    async def test_failed_event_includes_error(
        self, mock_supervisor, no_entry_graph, event_data, event_bus
    ):
        """Failed event includes error message."""
        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert "error" in captured[0]
        assert len(captured[0]["error"]) > 0

    async def test_failed_event_includes_tokens_and_duration(
        self, mock_supervisor, no_entry_graph, event_data, event_bus
    ):
        """Failed event includes tokens_used and duration_seconds."""
        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert "tokens_used" in captured[0]
        assert "duration_seconds" in captured[0]
        assert captured[0]["duration_seconds"] >= 0

    async def test_failed_event_on_token_budget_exceeded(
        self, mock_supervisor, event_data, event_bus
    ):
        """Failed event emitted when token budget is exceeded."""
        graph = {
            "id": "budget-playbook",
            "version": 1,
            "max_tokens": 1,  # Very low budget to trigger failure
            "nodes": {
                "step1": {
                    "entry": True,
                    "prompt": "Do a lot of work that uses many tokens.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        # Simulate token usage that exceeds budget
        mock_supervisor.chat = AsyncMock(return_value="A" * 100)

        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()

        assert result.status == "failed"
        assert "token_budget_exceeded" in (result.error or "")
        assert len(captured) == 1
        assert "token_budget_exceeded" in captured[0].get("error", "")

    async def test_no_failed_event_without_bus(self, mock_supervisor, no_entry_graph, event_data):
        """No failed event when event_bus is None."""
        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor)
        result = await runner.run()

        assert result.status == "failed"
        # No crash — event_bus is None

    async def test_failed_event_on_daily_cap_preflight(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """Failed event emitted on daily token cap pre-flight rejection."""
        from src.playbooks.runner import DailyTokenTracker

        tracker = DailyTokenTracker()
        tracker.add_tokens(1000)  # Already used 1000

        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(
            simple_graph,
            event_data,
            mock_supervisor,
            event_bus=event_bus,
            daily_token_tracker=tracker,
            daily_token_cap=500,  # Cap is 500, but 1000 already used
        )
        result = await runner.run()

        assert result.status == "failed"
        assert "daily_token_cap_exceeded" in (result.error or "")
        assert len(captured) == 1
        assert captured[0]["playbook_id"] == "test-playbook"
        assert captured[0]["failed_at_node"] == "<unknown>"


# ---------------------------------------------------------------------------
# Resume event emission
# ---------------------------------------------------------------------------


class TestResumeEventEmission:
    """Tests for event emission during resume() paths."""

    async def test_resume_completion_emits_completed_event(
        self, mock_supervisor, mock_db, human_review_graph, event_data, event_bus
    ):
        """Resumed run that completes emits playbook.run.completed."""
        # First, run to pause
        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        paused_result = await runner.run()
        assert paused_result.status == "paused"

        # Build a PlaybookRun for resume
        db_run = PlaybookRun(
            run_id=paused_result.run_id,
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node="review",
            conversation_history=json.dumps(runner.messages),
            node_trace=json.dumps(paused_result.node_trace),
            tokens_used=paused_result.tokens_used,
            started_at=time.time() - 10,
            pinned_graph=json.dumps(human_review_graph),
        )

        captured = []
        event_bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        result = await PlaybookRunner.resume(
            db_run,
            human_review_graph,
            mock_supervisor,
            "Looks good, approved!",
            db=mock_db,
            event_bus=event_bus,
        )

        assert result.status == "completed"
        assert len(captured) == 1
        assert captured[0]["playbook_id"] == "human-review-playbook"
        assert captured[0]["run_id"] == paused_result.run_id

    async def test_resume_failure_emits_failed_event(
        self, mock_supervisor, mock_db, event_data, event_bus
    ):
        """Resumed run that fails (no current_node) emits playbook.run.failed."""
        db_run = PlaybookRun(
            run_id="resume-fail-123",
            playbook_id="test-playbook",
            playbook_version=1,
            trigger_event=json.dumps(event_data),
            status="paused",
            current_node=None,  # Missing — will cause resume to fail
            conversation_history="[]",
            node_trace="[]",
            tokens_used=0,
            started_at=time.time() - 10,
        )

        graph = {
            "id": "test-playbook",
            "version": 1,
            "nodes": {"done": {"terminal": True}},
        }

        captured = []
        event_bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        result = await PlaybookRunner.resume(
            db_run,
            graph,
            mock_supervisor,
            "input",
            db=mock_db,
            event_bus=event_bus,
        )

        assert result.status == "failed"
        assert len(captured) == 1
        assert captured[0]["playbook_id"] == "test-playbook"
        assert "Cannot resume" in captured[0].get("error", "")


# ---------------------------------------------------------------------------
# EventBus error resilience
# ---------------------------------------------------------------------------


class TestEventBusResilience:
    """Ensure runner doesn't crash when EventBus subscribers fail."""

    async def test_bus_subscriber_error_does_not_crash_runner(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        """If a subscriber raises, the runner still completes."""

        async def bad_handler(data):
            raise RuntimeError("subscriber exploded")

        event_bus.subscribe("playbook.run.completed", bad_handler)

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        # The runner's _emit_bus_event wraps in try/except, but the EventBus
        # itself may propagate. The test verifies it doesn't crash.
        # Note: EventBus calls handlers sequentially, so the exception will
        # propagate through emit(). Our _emit_bus_event catches it.
        result = await runner.run()
        assert result.status == "completed"

    async def test_failed_bus_error_does_not_crash_runner(
        self, mock_supervisor, no_entry_graph, event_data, event_bus
    ):
        """If a subscriber raises on failure event, the runner still fails cleanly."""

        async def bad_handler(data):
            raise RuntimeError("subscriber exploded")

        event_bus.subscribe("playbook.run.failed", bad_handler)

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=event_bus)
        result = await runner.run()
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestEventSchemaCompliance:
    """Verify emitted payloads pass schema validation."""

    async def test_completed_payload_passes_validation(
        self, mock_supervisor, simple_graph, event_data
    ):
        """Completed event payload satisfies the schema."""
        from src.event_schemas import validate_event

        # Use a validating bus
        bus = EventBus(env="dev", validate_events=True)
        captured = []
        bus.subscribe("playbook.run.completed", lambda d: captured.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=bus)
        result = await runner.run()
        assert result.status == "completed"

        # If we got here without EventValidationError, the schema is valid
        assert len(captured) == 1

        # Double-check with explicit validation (strip _event_type meta field)
        payload = {k: v for k, v in captured[0].items() if not k.startswith("_")}
        errors = validate_event("playbook.run.completed", payload)
        assert errors == []

    async def test_failed_payload_passes_validation(
        self, mock_supervisor, no_entry_graph, event_data
    ):
        """Failed event payload satisfies the schema."""
        from src.event_schemas import validate_event

        bus = EventBus(env="dev", validate_events=True)
        captured = []
        bus.subscribe("playbook.run.failed", lambda d: captured.append(d))

        runner = PlaybookRunner(no_entry_graph, event_data, mock_supervisor, event_bus=bus)
        result = await runner.run()
        assert result.status == "failed"

        assert len(captured) == 1

        payload = {k: v for k, v in captured[0].items() if not k.startswith("_")}
        errors = validate_event("playbook.run.failed", payload)
        assert errors == []


# ---------------------------------------------------------------------------
# Notification event classes
# ---------------------------------------------------------------------------


class TestNotificationEventModels:
    """Verify PlaybookRunCompletedEvent and PlaybookRunFailedEvent models."""

    def test_completed_event_defaults(self):
        from src.notifications.events import PlaybookRunCompletedEvent

        evt = PlaybookRunCompletedEvent(playbook_id="pb-1", run_id="run-1")
        assert evt.event_type == "notify.playbook_run_completed"
        assert evt.category == "system"
        assert evt.severity == "info"
        assert evt.playbook_id == "pb-1"
        assert evt.run_id == "run-1"
        assert evt.final_context is None
        assert evt.tokens_used == 0
        assert evt.duration_seconds == 0.0

    def test_completed_event_with_all_fields(self):
        from src.notifications.events import PlaybookRunCompletedEvent

        evt = PlaybookRunCompletedEvent(
            playbook_id="pb-1",
            run_id="run-1",
            project_id="proj-1",
            final_context="All checks passed.",
            tokens_used=5000,
            duration_seconds=12.5,
        )
        assert evt.project_id == "proj-1"
        assert evt.final_context == "All checks passed."
        assert evt.tokens_used == 5000
        assert evt.duration_seconds == 12.5

    def test_failed_event_defaults(self):
        from src.notifications.events import PlaybookRunFailedEvent

        evt = PlaybookRunFailedEvent(playbook_id="pb-1", run_id="run-1", failed_at_node="step2")
        assert evt.event_type == "notify.playbook_run_failed"
        assert evt.severity == "error"
        assert evt.category == "system"
        assert evt.playbook_id == "pb-1"
        assert evt.run_id == "run-1"
        assert evt.failed_at_node == "step2"
        assert evt.error == ""
        assert evt.tokens_used == 0

    def test_failed_event_with_all_fields(self):
        from src.notifications.events import PlaybookRunFailedEvent

        evt = PlaybookRunFailedEvent(
            playbook_id="pb-1",
            run_id="run-1",
            project_id="proj-1",
            failed_at_node="step2",
            error="LLM timeout",
            tokens_used=3000,
            duration_seconds=45.0,
        )
        assert evt.project_id == "proj-1"
        assert evt.error == "LLM timeout"
        assert evt.tokens_used == 3000
        assert evt.duration_seconds == 45.0

    def test_completed_event_serialization(self):
        from src.notifications.events import PlaybookRunCompletedEvent

        evt = PlaybookRunCompletedEvent(
            playbook_id="pb-1",
            run_id="run-1",
            tokens_used=1000,
        )
        data = evt.model_dump(mode="json")
        assert data["event_type"] == "notify.playbook_run_completed"
        assert data["playbook_id"] == "pb-1"
        assert data["tokens_used"] == 1000

    def test_failed_event_serialization(self):
        from src.notifications.events import PlaybookRunFailedEvent

        evt = PlaybookRunFailedEvent(
            playbook_id="pb-1",
            run_id="run-1",
            failed_at_node="analyze",
            error="Graph error",
        )
        data = evt.model_dump(mode="json")
        assert data["event_type"] == "notify.playbook_run_failed"
        assert data["failed_at_node"] == "analyze"
        assert data["error"] == "Graph error"
