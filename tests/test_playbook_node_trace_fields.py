"""Tests for NodeTraceEntry.output / .error population (pane spec §5.3.1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm import LLMClient, LLMRunResult
from src.llm.fake import FakeProvider
from src.playbooks.runner import NodeTraceEntry, PlaybookRunner
from src.playbooks.services import PlaybookServices


def _make_graph() -> dict:
    """A minimal single-node terminal playbook graph."""
    return {
        "id": "trace-test",
        "version": 1,
        "nodes": {
            "start": {
                "entry": True,
                "prompt": "Say hi.",
                "terminal": True,
            },
        },
    }


def _make_services(response_text: str = "Hi there!"):
    services = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    services.llm = MagicMock()
    services.llm.run_tools = AsyncMock(
        return_value=LLMRunResult(text=response_text, transcript=[], turns=1, stopped_by="done")
    )
    services.llm.complete = AsyncMock(return_value=MagicMock(text="1", tool_calls=[]))
    return services


def test_node_trace_entry_has_new_optional_fields():
    entry = NodeTraceEntry(node_id="n1", started_at=1.0)
    assert entry.command is None
    assert entry.args_summary is None
    assert entry.output is None
    assert entry.error is None


def test_trace_to_dict_omits_unset_new_fields():
    entry = NodeTraceEntry(node_id="n1", started_at=1.0, completed_at=2.0, status="completed")
    d = PlaybookRunner._trace_to_dict(entry)
    assert "command" not in d
    assert "args_summary" not in d
    assert "output" not in d
    assert "error" not in d


def test_trace_to_dict_includes_output_when_set():
    entry = NodeTraceEntry(
        node_id="n1", started_at=1.0, completed_at=2.0, status="completed",
        output="the response text",
    )
    d = PlaybookRunner._trace_to_dict(entry)
    assert d["output"] == "the response text"


def test_trace_to_dict_includes_error_when_set():
    entry = NodeTraceEntry(
        node_id="n1", started_at=1.0, completed_at=2.0, status="failed",
        error="boom",
    )
    d = PlaybookRunner._trace_to_dict(entry)
    assert d["error"] == "boom"


@pytest.mark.asyncio
async def test_execute_node_populates_output(monkeypatch):
    """After a node executes successfully, its trace entry's .output holds
    the node's response text."""
    graph = _make_graph()
    services = _make_services("Hi there!")

    runner = PlaybookRunner(graph=graph, event={}, services=services, db=None)
    result = await runner.run()

    assert result.status == "completed"
    assert len(runner.node_trace) == 1
    assert runner.node_trace[0].output == "Hi there!"


@pytest.mark.asyncio
async def test_dry_run_populates_output():
    graph = _make_graph()
    services = _make_services()

    runner = PlaybookRunner(graph=graph, event={}, services=services, db=None)
    runner._dry_run = True
    result = await runner.run()

    assert result.status == "completed"
    assert runner.node_trace[0].output is not None
    assert "dry-run" in runner.node_trace[0].output
