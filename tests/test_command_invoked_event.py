"""Tests for the ``command.invoked`` bus event.

See docs/superpowers/plans/2026-08-21-dv2-phase5-observability.md ("Phase 5
Follow-up: ``command.invoked`` WS events") for the motivation.  The event is
emitted by :meth:`CommandHandler.execute` after every dispatch (success or
failure) and carries a redacted args summary plus duration + ok/error, so the
dashboard "supervisor is thinking…" bubble can render live tool-call chips.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.commands.handler import _summarize_args


def _find_command_invoked(bus_emit: AsyncMock) -> list[dict]:
    """Return the payloads of every ``command.invoked`` call on *bus_emit*."""
    frames: list[dict] = []
    for call in bus_emit.await_args_list:
        args, _ = call.args, call.kwargs
        if args and args[0] == "command.invoked":
            payload = args[1] if len(args) > 1 else {}
            frames.append(payload)
    return frames


@pytest.mark.asyncio
async def test_command_invoked_emitted_on_success(command_handler_factory):
    ch = await command_handler_factory()

    # Create a project so ``project_show`` has something to succeed on — the
    # project_id is not what we care about; we just need a call that reaches
    # a real ``_cmd_*`` and returns without an ``error`` key.
    created = await ch.execute("create_project", {"name": "invoked-test"})
    assert "error" not in created, created

    ch.orchestrator.bus.emit.reset_mock()
    result = await ch.execute("list_projects", {})
    assert "error" not in result, result

    frames = _find_command_invoked(ch.orchestrator.bus.emit)
    assert len(frames) == 1, frames
    payload = frames[0]
    assert payload["command"] == "list_projects"
    assert payload["ok"] is True
    assert payload["error"] is None
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
    # Local scope (no _scope injected) → id fields null
    assert payload["session_id"] is None
    assert payload["task_id"] is None
    assert payload["project_id"] is None


@pytest.mark.asyncio
async def test_command_invoked_emitted_on_failure(command_handler_factory):
    ch = await command_handler_factory()
    ch.orchestrator.bus.emit.reset_mock()

    # ``task_show`` requires ``task_id`` — a missing/unknown one produces an
    # error result rather than a raised exception in most builds; either way
    # ``ok`` must be False and ``error`` must be a short string.
    await ch.execute("task_show", {"task_id": "nonexistent-id"})

    frames = _find_command_invoked(ch.orchestrator.bus.emit)
    assert len(frames) == 1, frames
    payload = frames[0]
    assert payload["command"] == "task_show"
    assert payload["ok"] is False
    assert isinstance(payload["error"], str)
    assert len(payload["error"]) <= 200
    assert "Traceback" not in payload["error"]


@pytest.mark.asyncio
async def test_command_invoked_gate_disabled_suppresses_emission(command_handler_factory):
    ch = await command_handler_factory()
    ch.config.events.command_invoked_enabled = False
    ch.orchestrator.bus.emit.reset_mock()

    await ch.execute("list_projects", {})

    frames = _find_command_invoked(ch.orchestrator.bus.emit)
    assert frames == []


@pytest.mark.asyncio
async def test_command_invoked_args_summary_redacts_body(command_handler_factory):
    ch = await command_handler_factory()
    ch.orchestrator.bus.emit.reset_mock()

    payload_body = "a" * 500
    # Unknown command still routes through execute → still emits.
    await ch.execute("no_such_command_xyz", {"body": payload_body, "task_id": "t-1"})

    frames = _find_command_invoked(ch.orchestrator.bus.emit)
    assert len(frames) == 1, frames
    payload = frames[0]
    summary = payload["args_summary"]
    assert payload_body not in summary, "raw body must never appear on the wire"
    assert "redacted" in summary
    assert "task_id=t-1" in summary
    assert len(summary) <= 200


# ---------------------------------------------------------------------------
# _summarize_args unit tests
# ---------------------------------------------------------------------------


def test_summarize_args_empty():
    assert _summarize_args("x", None) == ""
    assert _summarize_args("x", {}) == ""


def test_summarize_args_passthrough_and_redaction():
    got = _summarize_args(
        "task_close",
        {
            "task_id": "task-abc",
            "body": "z" * 300,
            "api_key": "shhh",
            "note": "short note",
        },
    )
    assert "task_id=task-abc" in got
    assert "body=<redacted len=300>" in got
    assert "api_key=<redacted len=4>" in got
    assert "note=short note" in got


def test_summarize_args_long_string_truncated():
    long_val = "b" * 200
    got = _summarize_args("x", {"payload": long_val})
    assert long_val not in got
    assert "payload=<...len=200>" in got


def test_summarize_args_max_length():
    args = {f"k{i}": f"v{i}" * 5 for i in range(50)}
    got = _summarize_args("x", args)
    assert len(got) <= 200


def test_summarize_args_drops_scope():
    got = _summarize_args("x", {"_scope": {"session_id": "s1"}, "task_id": "t1"})
    assert "_scope" not in got
    assert "task_id=t1" in got
