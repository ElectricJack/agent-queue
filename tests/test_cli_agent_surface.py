"""CLI tests for ``aq prime``, ``aq handoff``, ``aq inbox`` (Phase S1).

See docs/specs/implementation/aq-surface.md §5.3, §10.1: "aq inbox --inject
exits 0 on daemon-down and on timeout; aq prime --hook-json envelope".
Patches ``src.cli.agent_surface._get_client`` — hand-crafted CLI commands
import ``_get_client`` at module scope, so tests must patch it there (see
the patch-target note in the implementation spec's §10.0).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # ``AQ_API_TOKEN`` is here because these tests may themselves be running
    # inside a session that exports one; a leaked token silently flips the
    # tokened/untokened branch under test.
    for var in (
        "AQ_TASK_ID", "AQ_SESSION_ID", "AQ_STARTUP_PROMPT_DELIVERED", "AQ_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _mock_client(execute_results: dict):
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.close = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    calls: list[tuple[str, dict]] = []

    async def mock_execute(command, args=None):
        calls.append((command, args or {}))
        result = execute_results.get(command, {})
        if isinstance(result, Exception):
            raise result
        return result

    mock_client.execute = AsyncMock(side_effect=mock_execute)
    mock_client.calls = calls
    return mock_client


# ---------------------------------------------------------------------------
# aq prime
# ---------------------------------------------------------------------------


class TestPrimeCLI:
    def test_plain_mode_prints_body(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"success": True, "body": "## Task\n\nhello"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        assert "## Task" in result.output
        assert mock.calls == [("prime", {"task_id": "task-1"})]

    def test_task_id_falls_back_to_env(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_TASK_ID", "env-task")
        mock = _mock_client({"prime": {"success": True, "body": "body text"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime"])
        assert result.exit_code == 0, result.output
        assert mock.calls == [("prime", {"task_id": "env-task"})]

    def test_explicit_task_id_wins_over_env(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_TASK_ID", "env-task")
        mock = _mock_client({"prime": {"success": True, "body": "body"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            runner.invoke(cli, ["prime", "--task-id", "explicit-task"])
        assert mock.calls == [("prime", {"task_id": "explicit-task"})]

    def test_json_mode_wraps_full_result_in_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "prime": {
                    "success": True,
                    "body": "hello",
                    "sections": [],
                    "source": "default",
                    "tokens_est": 1,
                }
            }
        )
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "prime", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == 1
        assert payload["data"]["body"] == "hello"

    def test_hook_json_wraps_body_as_claude_session_start_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"success": True, "body": "primed body"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1", "--hook-json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "primed body",
            }
        }

    def test_hook_format_claude_matches_hook_json(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"success": True, "body": "primed body"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1", "--hook-format", "claude"])
        payload = json.loads(result.output)
        assert payload["hookSpecificOutput"]["additionalContext"] == "primed body"

    def test_hook_format_unknown_harness_is_plain_text(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"success": True, "body": "primed body"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1", "--hook-format", "codex"])
        assert result.output.strip() == "primed body"

    def test_suppressed_when_startup_prompt_already_delivered(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_STARTUP_PROMPT_DELIVERED", "1")
        mock = _mock_client({"prime": {"success": True, "body": "should not appear"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1", "--hook-json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["hookSpecificOutput"]["additionalContext"] == ""
        # Suppression must short-circuit before any daemon call.
        assert mock.calls == []

    def test_not_suppressed_in_plain_mode_even_if_delivered(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_STARTUP_PROMPT_DELIVERED", "1")
        mock = _mock_client({"prime": {"success": True, "body": "still prints"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        assert "still prints" in result.output
        assert mock.calls == [("prime", {"task_id": "task-1"})]

    def test_error_result_in_hook_mode_wraps_error_message(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"error": "Task 'nope' not found"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "nope", "--hook-json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "not found" in payload["hookSpecificOutput"]["additionalContext"]


class TestAgentMessageCLI:
    def test_uses_positional_target_and_body(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"agent_message": {"message_id": "msg-1", "state": "queued"}})
        with patch("src.cli.agent_messages._get_client", return_value=mock):
            result = runner.invoke(cli, ["agent", "message", "task-1", "stop the suite"])
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            ("agent_message", {"target": "task-1", "body": "stop the suite", "all_running": False})
        ]

    def test_broadcast_accepts_body_without_a_target(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"agent_message": {"count": 2, "recipients": []}})
        with patch("src.cli.agent_messages._get_client", return_value=mock):
            result = runner.invoke(cli, ["agent", "message", "--all-running", "stop the suite"])
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            ("agent_message", {"body": "stop the suite", "all_running": True})
        ]


# ---------------------------------------------------------------------------
# aq handoff
# ---------------------------------------------------------------------------


class TestHandoffCLI:
    def test_sends_subject_detail_and_task_id(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
        mock = _mock_client(
            {"task_handoff": {"success": True, "handoff_id": "h1", "restart_requested": True}}
        )
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(
                cli, ["handoff", "--task-id", "task-1", "partial fix", "stopped early"]
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            (
                "task_handoff",
                {
                    "auto": False,
                    "task_id": "task-1",
                    "subject": "partial fix",
                    "detail": "stopped early",
                },
            )
        ]

    def test_auto_flag_forwarded(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
        mock = _mock_client(
            {"task_handoff": {"success": True, "handoff_id": "h1", "restart_requested": False}}
        )
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            runner.invoke(cli, ["handoff", "--auto", "--task-id", "task-1"])
        assert mock.calls == [("task_handoff", {"auto": True, "task_id": "task-1"})]

    def test_task_id_and_session_id_fall_back_to_env(self, runner, monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_TASK_ID", "env-task")
        monkeypatch.setenv("AQ_SESSION_ID", "env-session")
        monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
        mock = _mock_client(
            {"task_handoff": {"success": True, "handoff_id": "h1", "restart_requested": True}}
        )
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            runner.invoke(cli, ["handoff"])
        assert mock.calls == [
            ("task_handoff", {"auto": False, "task_id": "env-task", "session_id": "env-session"})
        ]

    def test_json_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {"task_handoff": {"success": True, "handoff_id": "h1", "restart_requested": True}}
        )
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "handoff", "--task-id", "task-1"])
        payload = json.loads(result.output)
        assert payload["data"]["handoff_id"] == "h1"
        assert payload["data"]["restart_requested"] is True


# ---------------------------------------------------------------------------
# aq inbox --inject — hook safety with no resolvable recipient.
#
# This module no longer defines ``inbox``: its Phase S1 no-op stub collided
# with the real ``aq inbox`` in ``src/cli/messages.py`` on the shared click
# root group, so which one an interpreter got depended on import order (see
# tests/test_cli_module_entry.py). These tests always ran against the
# surviving ``messages.py`` command — ``app.py`` imports this module first,
# so ``messages.py`` won — and they still pin the property the stub existed
# for: a stale hook file calling ``aq inbox --inject`` from a session with
# no ``AQ_SESSION_ID``/``AQ_TASK_ID`` exits 0, prints nothing, and never
# reaches the daemon.
# ---------------------------------------------------------------------------


class TestInboxCLI:
    def test_inject_prints_nothing_and_exits_zero(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["inbox", "--inject"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_plain_inbox_also_exits_zero(self, runner):
        from src.cli.app import cli

        result = runner.invoke(cli, ["inbox"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_never_touches_the_daemon(self, runner):
        """With no recipient to resolve, the hook path builds no CLIClient."""
        from src.cli.app import cli

        with patch("src.cli.messages._get_client") as get_client:
            result = runner.invoke(cli, ["inbox", "--inject"])
        assert result.exit_code == 0
        get_client.assert_not_called()


# ---------------------------------------------------------------------------
# aq subagent event — the SubagentStart / SubagentStop receiver
# ---------------------------------------------------------------------------


class TestSubagentEventCLI:
    """Two hard rules: never block the agent, never fail in its face."""

    CLAUDE_START = json.dumps({
        "session_id": "harness-session", "cwd": "/repo",
        "transcript_path": "/t.jsonl", "hook_event_name": "SubagentStart",
        "agent_id": "agent_017Kx", "agent_type": "Explore",
    })
    CODEX_STOP = json.dumps({
        "session_id": "harness-session", "turn_id": "turn-9", "cwd": "/repo",
        "hook_event_name": "SubagentStop", "agent_id": "child-1",
        "agent_type": "default", "agent_transcript_path": "/child.jsonl",
        "last_assistant_message": "42",
    })

    def _run(self, runner, stdin, results=None, env=None):
        from src.cli.app import cli

        client = _mock_client(
            results if results is not None else {"subagent_event": {"success": True}}
        )
        with patch("src.cli.agent_surface._get_client", return_value=client):
            result = runner.invoke(
                cli, ["subagent", "event", "--hook-json"], input=stdin, env=env or {},
            )
        return result, client

    def test_a_claude_start_is_forwarded_as_a_start(self, runner):
        result, client = self._run(runner, self.CLAUDE_START)
        assert result.exit_code == 0
        command, args = client.calls[0]
        assert command == "subagent_event"
        assert args["event"] == "start"
        assert args["subagent_id"] == "agent_017Kx"
        assert args["agent_type"] == "Explore"

    def test_a_codex_stop_carries_its_turn(self, runner):
        _result, client = self._run(runner, self.CODEX_STOP)
        _command, args = client.calls[0]
        assert args["event"] == "stop"
        assert args["subagent_id"] == "child-1"
        assert args["turn_id"] == "turn-9"

    def test_the_bearer_token_names_the_session_not_the_environment(self, runner):
        """Regression: sending both made any disagreement a hard rejection.

        The hook inherits ``AQ_SESSION_ID`` from the session's env, but the
        daemon derives the session from the token's own scope.  Sending the
        env value as well turned a mismatch into ``out of scope: session_id
        mismatch`` — a dropped count instead of a recorded one.  Found by
        running a real Claude session against a real daemon.
        """
        _result, client = self._run(
            runner, self.CLAUDE_START,
            env={"AQ_API_TOKEN": "aqs_x", "AQ_SESSION_ID": "s-env"},
        )
        _command, args = client.calls[0]
        assert "session_id" not in args

    def test_an_untokened_local_call_still_names_its_session(self, runner):
        _result, client = self._run(
            runner, self.CLAUDE_START, env={"AQ_SESSION_ID": "s-env"},
        )
        _command, args = client.calls[0]
        assert args["session_id"] == "s-env"

    @pytest.mark.parametrize(
        "stdin", ["", "not json", json.dumps({"hook_event_name": "Stop"})]
    )
    def test_a_payload_that_is_not_a_subagent_event_is_dropped_silently(self, runner, stdin):
        result, client = self._run(runner, stdin)
        assert result.exit_code == 0
        assert client.calls == []
        assert result.output == ""

    def test_a_daemon_that_is_down_does_not_stop_the_subagent(self, runner):
        result, _client = self._run(
            runner, self.CLAUDE_START,
            results={"subagent_event": RuntimeError("connection refused")},
        )
        # Exit 2 would block the sub-agent from starting; exit 1 would print
        # an error into the agent's pane.  A missed count is neither.
        assert result.exit_code == 0
        assert result.output == ""

    def test_a_command_error_is_also_swallowed(self, runner):
        from src.cli.exceptions import CommandError

        result, _client = self._run(
            runner, self.CLAUDE_START,
            results={"subagent_event": CommandError("subagent_event", "out of scope")},
        )
        assert result.exit_code == 0
