"""Tests for notify.playbook_run_node_started / _node_completed bus events.

See pane spec §7.4/§13.6 (docs/superpowers/specs/
2026-08-22-pane-playbook-run-inspector-design.md) — these events let the
playbook-run-inspector pane get live node-level updates from the EventBus
instead of relying solely on polling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.event_bus import EventBus
from src.llm import LLMClient, LLMRunResult
from src.llm.fake import FakeProvider
from src.playbooks.runner import PlaybookRunner
from src.playbooks.services import PlaybookServices


@pytest.fixture
def mock_services():
    """PlaybookServices with a controllable llm.run_tools() return value."""
    services = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    services.llm = MagicMock()
    services.llm.run_tools = AsyncMock(
        return_value=LLMRunResult(text="Done.", transcript=[], turns=1, stopped_by="done")
    )
    services.llm.complete = AsyncMock(return_value=MagicMock(text="1", tool_calls=[]))
    return services


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
        self, mock_services, simple_graph, event_data, event_bus
    ):
        received = []
        event_bus.subscribe("notify.playbook_run_node_started", lambda d: received.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_services, event_bus=event_bus)
        await runner.run()

        assert len(received) >= 1
        assert received[0]["node_id"]
        assert received[0]["run_id"] == runner.run_id
        assert received[0]["playbook_id"] == "test-playbook"

    async def test_node_completed_event_emitted_with_status(
        self, mock_services, simple_graph, event_data, event_bus
    ):
        received = []
        event_bus.subscribe("notify.playbook_run_node_completed", lambda d: received.append(d))

        runner = PlaybookRunner(simple_graph, event_data, mock_services, event_bus=event_bus)
        await runner.run()

        assert len(received) >= 1
        assert received[0]["status"] == "completed"

    async def test_node_events_omitted_without_bus(
        self, mock_services, simple_graph, event_data
    ):
        runner = PlaybookRunner(simple_graph, event_data, mock_services, event_bus=None)
        # Should not raise even with no event_bus configured.
        result = await runner.run()
        assert result.status == "completed"
