"""Tests for NodeTraceEntry.output / .error population (pane spec §5.3.1)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.playbooks.runner import NodeTraceEntry, PlaybookRunner


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


def _make_supervisor(chat_response: str = "Hi there!"):
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value=chat_response)
    supervisor.summarize = AsyncMock(return_value="Summary of prior steps.")
    supervisor.config = SimpleNamespace(chat_provider=SimpleNamespace(playbook_max_tokens=2048))
    return supervisor


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
    supervisor = _make_supervisor("Hi there!")

    runner = PlaybookRunner(graph=graph, event={}, supervisor=supervisor, db=None)
    result = await runner.run()

    assert result.status == "completed"
    assert len(runner.node_trace) == 1
    assert runner.node_trace[0].output == "Hi there!"


@pytest.mark.asyncio
async def test_dry_run_populates_output():
    graph = _make_graph()
    supervisor = _make_supervisor()

    runner = PlaybookRunner(graph=graph, event={}, supervisor=supervisor, db=None)
    runner._dry_run = True
    result = await runner.run()

    assert result.status == "completed"
    assert runner.node_trace[0].output is not None
    assert "dry-run" in runner.node_trace[0].output
