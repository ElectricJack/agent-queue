"""Dispatch-boundary tests for :meth:`src.commands.handler.CommandHandler.execute`.

``execute()`` is the single cross-surface command path (docs/specs/command-handler.md).
These tests cover the branches that were uncovered as a group: the plugin
fallback (success and exception), the ``command.invoked`` emission that rides on
each, terminal-input redaction, and the guarantee that the server-injected
``_scope`` envelope never reaches a ``_cmd_*`` body (test-coverage plan,
commands 18–20).
"""

from __future__ import annotations

import logging

import pytest


def _invoked_payloads(bus_emit) -> list[dict]:
    return [
        call.args[1]
        for call in bus_emit.await_args_list
        if call.args and call.args[0] == "command.invoked"
    ]


class _StubPluginRegistry:
    """Registry stub exposing just the plugin-fallback surface ``execute`` uses."""

    def __init__(self, command_name: str, handler):
        self._name = command_name
        self._handler = handler
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def get_command(self, name: str):
        return self._handler if name == self._name else None

    def record_success(self, name: str) -> None:
        self.successes.append(name)

    async def record_failure(self, name: str, error: str) -> None:
        self.failures.append((name, error))


# ---------------------------------------------------------------------------
# 18: plugin fallback success
# ---------------------------------------------------------------------------


async def test_execute_plugin_fallback_records_success_and_emits_sanitized_invocation(
    command_handler_factory,
):
    handler = await command_handler_factory()
    seen_args: list[dict] = []

    async def _plugin_command(args: dict) -> dict:
        seen_args.append(args)
        return {"ok": True, "note": "from plugin"}

    registry = _StubPluginRegistry("notes.write", _plugin_command)
    handler.orchestrator.plugin_registry = registry
    handler.orchestrator.bus.emit.reset_mock()

    result = await handler.execute(
        "notes.write",
        {"task_id": "t1", "body": "hello", "api_key": "sk-super-secret", "_scope": {}},
    )

    assert result == {"ok": True, "note": "from plugin"}
    # The plugin's own bucket name (before the dot) is what gets credited.
    assert registry.successes == ["notes"]
    assert registry.failures == []
    # The handler never sees the trust envelope.
    assert "_scope" not in seen_args[0]

    payloads = _invoked_payloads(handler.orchestrator.bus.emit)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["command"] == "notes.write"
    assert payload["ok"] is True
    assert payload["error"] is None
    assert isinstance(payload["duration_ms"], int)
    # Args summary is redacted: no raw secret, no _scope.
    assert "sk-super-secret" not in payload["args_summary"]
    assert "api_key=<redacted len=15>" in payload["args_summary"]
    assert "task_id=t1" in payload["args_summary"]
    assert "_scope" not in payload["args_summary"]


# ---------------------------------------------------------------------------
# 19: plugin fallback exception
# ---------------------------------------------------------------------------


async def test_execute_plugin_exception_records_failure_and_returns_error(
    command_handler_factory,
):
    handler = await command_handler_factory()

    async def _plugin_command(args: dict) -> dict:
        raise RuntimeError("plugin blew up")

    registry = _StubPluginRegistry("notes.write", _plugin_command)
    handler.orchestrator.plugin_registry = registry
    handler.orchestrator.bus.emit.reset_mock()

    result = await handler.execute("notes.write", {"body": "hello"})

    assert result == {"error": "Plugin command failed: plugin blew up"}
    assert registry.successes == []
    assert registry.failures == [("notes", "plugin blew up")]

    payloads = _invoked_payloads(handler.orchestrator.bus.emit)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    # The event carries the exception class, not the raw message.
    assert payloads[0]["error"] == "Plugin command failed: RuntimeError"


async def test_execute_returns_unknown_command_when_no_handler_or_plugin(
    command_handler_factory,
):
    handler = await command_handler_factory()
    handler.orchestrator.plugin_registry = _StubPluginRegistry("other.cmd", None)
    handler.orchestrator.bus.emit.reset_mock()

    result = await handler.execute("no_such_command", {})

    assert result == {"error": "Unknown command: no_such_command"}
    payloads = _invoked_payloads(handler.orchestrator.bus.emit)
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"] == "Unknown command: no_such_command"


async def test_execute_wraps_a_raising_builtin_handler_in_the_error_convention(
    command_handler_factory,
):
    handler = await command_handler_factory()

    async def _boom(args: dict) -> dict:
        raise ValueError("handler exploded")

    handler._cmd_probe_boom = _boom  # type: ignore[attr-defined]
    handler.orchestrator.bus.emit.reset_mock()

    result = await handler.execute("probe_boom", {})

    assert result == {"error": "handler exploded"}
    payloads = _invoked_payloads(handler.orchestrator.bus.emit)
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"] == "ValueError: handler exploded"


# ---------------------------------------------------------------------------
# 20: redaction + scope stripping
# ---------------------------------------------------------------------------


async def test_execute_redacts_session_input_in_logs_and_removes_scope_from_handler_args(
    command_handler_factory, caplog
):
    handler = await command_handler_factory()
    secret = "hunter2-terminal-secret"
    seen: list[dict] = []

    async def _fake_session_input(args: dict) -> dict:
        seen.append(dict(args))
        return {"success": True}

    handler._cmd_session_input = _fake_session_input  # type: ignore[attr-defined]
    handler.orchestrator.bus.emit.reset_mock()

    with caplog.at_level(logging.DEBUG, logger="src.commands.handler"):
        result = await handler.execute(
            "session_input",
            {"session_id": "s1", "input": secret, "_scope": {"session_id": "s1"}},
        )

    assert result == {"success": True}

    # The literal keystrokes never reach the logs...
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in log_text
    assert "<redacted>" in log_text
    assert "session_id" in log_text

    # ...but the real handler still gets them.
    assert seen[0]["input"] == secret
    # ...and never gets the server-injected trust envelope.
    assert "_scope" not in seen[0]

    # session_input is excluded from the command.invoked feed entirely.
    assert _invoked_payloads(handler.orchestrator.bus.emit) == []


async def test_execute_exposes_scope_via_current_scope_not_handler_args(
    command_handler_factory,
):
    handler = await command_handler_factory()
    seen: list[tuple[dict, object]] = []

    async def _probe(args: dict) -> dict:
        seen.append((dict(args), handler._current_scope))
        return {"success": True}

    handler._cmd_probe_scope = _probe  # type: ignore[attr-defined]
    scope = {"session_id": "s1", "task_id": "t1", "project_id": "p1"}

    await handler.execute("probe_scope", {"task_id": "t1", "_scope": scope})

    args, current = seen[0]
    assert "_scope" not in args
    assert args == {"task_id": "t1"}
    assert current == scope

    # Scope does not leak into the next dispatch.
    await handler.execute("probe_scope", {"task_id": "t1"})
    assert seen[1][1] is None


@pytest.mark.parametrize("flag", [True, False])
async def test_command_invoked_emission_honours_the_config_flag(command_handler_factory, flag):
    handler = await command_handler_factory()

    async def _probe(args: dict) -> dict:
        return {"success": True}

    handler._cmd_probe_flag = _probe  # type: ignore[attr-defined]
    handler.config.events.command_invoked_enabled = flag
    handler.orchestrator.bus.emit.reset_mock()

    await handler.execute("probe_flag", {})

    assert bool(_invoked_payloads(handler.orchestrator.bus.emit)) is flag
