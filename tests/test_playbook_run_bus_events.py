"""Tests for notify.playbook_run_node_started / _node_completed bus events.

See pane spec §7.4/§13.6 (docs/superpowers/specs/
2026-08-22-pane-playbook-run-inspector-design.md) — these events let the
playbook-run-inspector pane get live node-level updates from the EventBus
instead of relying solely on polling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.playbooks.runner import PlaybookRunner


@pytest.fixture
def mock_supervisor():
    """A mock Supervisor with a controllable chat() return value."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary of prior steps.")
    return supervisor


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
def event_data():
    """Sample trigger event with project_id."""
    return {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}


class TestNodeLifecycleEventEmission:
    """notify.playbook_run_node_started / _node_completed (pane spec §7.4/§13.6)."""

    async def test_node_started_event_emitted(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        received = []
        event_bus.subscribe("notify.playbook_run_node_started", lambda d: received.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert len(received) >= 1
        assert received[0]["node_id"]
        assert received[0]["run_id"] == runner.run_id
        assert received[0]["playbook_id"] == "test-playbook"

    async def test_node_completed_event_emitted_with_status(
        self, mock_supervisor, simple_graph, event_data, event_bus
    ):
        received = []
        event_bus.subscribe("notify.playbook_run_node_completed", lambda d: received.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=event_bus)
        await runner.run()

        assert len(received) >= 1
        assert received[0]["status"] == "completed"

    async def test_node_events_omitted_without_bus(
        self, mock_supervisor, simple_graph, event_data
    ):
        runner = PlaybookRunner(simple_graph, event_data, mock_supervisor, event_bus=None)
        # Should not raise even with no event_bus configured.
        result = await runner.run()
        assert result.status == "completed"
