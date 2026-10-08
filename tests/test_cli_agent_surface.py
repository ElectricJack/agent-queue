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

    def test_no_bundle_means_no_delivery_receipt(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"prime": {"success": True, "body": "## Task\n\nhello"}})
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        assert [command for command, _ in mock.calls] == ["prime"]

    def test_prepared_bundle_is_acknowledged_after_the_body_is_written(self, runner,
                                                                      monkeypatch):
        from src.cli.app import cli

        monkeypatch.setenv("AQ_SESSION_ID", "sess-1")
        monkeypatch.setenv("AQ_CLAIM_EPOCH", "3")
        mock = _mock_client({
            "prime": {"success": True, "body": "## Task\n\nhello", "context_bundle": {
                "bundle_id": "bundle-1", "content_sha256": "a" * 64,
            }},
            "knowledge_context_deliver": {"success": True, "delivery_id": "d-1"},
        })
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            ("prime", {"task_id": "task-1"}),
            ("knowledge_context_deliver", {
                "bundle_id": "bundle-1",
                "transport": "startup_prompt",
                "idempotency_key": "startup_prompt:bundle-1:startup:sess-1:3",
                "rendered_sha256": "a" * 64,
                "state": "delivered",
                "claim_epoch": 3,
            }),
        ]

    def test_hook_mode_acknowledges_the_envelope_transport(self, runner):
        from src.cli.app import cli

        mock = _mock_client({
            "prime": {"success": True, "body": "hello", "context_bundle": {
                "bundle_id": "bundle-1", "content_sha256": "b" * 64,
            }},
            "knowledge_context_deliver": {"success": True},
        })
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            # A task (non-pool) session has no claim fence to send.
            patch("src.cli.agent_surface.read_claim_epoch", return_value=None),
        ):
            result = runner.invoke(cli, ["prime", "--hook-json", "--task-id", "task-1"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["hookSpecificOutput"]["additionalContext"] == "hello"
        delivery = dict(mock.calls[1][1])
        assert delivery["transport"] == "hook_envelope"
        assert delivery["idempotency_key"] == "hook_envelope:bundle-1:startup:no-session"
        assert "claim_epoch" not in delivery

    def test_an_unrecorded_receipt_is_unknown_not_a_read(self, runner):
        from src.cli.app import cli

        mock = _mock_client({
            "prime": {"success": True, "body": "hello", "context_bundle": {
                "bundle_id": "bundle-1", "content_sha256": "c" * 64,
            }},
            "knowledge_context_deliver": RuntimeError("daemon unreachable"),
        })
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["prime", "--task-id", "task-1"])
        # The payload still reached the agent, and both receipt attempts are
        # visible: nothing here claims the model read anything.
        assert "hello" in result.output
        assert [args["state"] for _, args in mock.calls[1:]] == ["delivered", "unknown"]


class TestTaskCloseCLI:
    def test_forwards_repeatable_deliverable_unmet_reasons(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_close": {"success": True, "task_id": "task-1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(
                cli,
                [
                    "task", "close", "task-1", "--outcome", "pass",
                    "--deliverable-unmet", "docs: deferred with approval",
                    "--deliverable-unmet", "flag: not required by this package",
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            (
                "task_close",
                {
                    "task_id": "task-1",
                    "outcome": "pass",
                    "deliverable_unmet": [
                        "docs: deferred with approval", "flag: not required by this package"
                    ],
                },
            )
        ]


    def test_forwards_skip_open_subtasks(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_close": {"success": True, "task_id": "task-1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(
                cli,
                ["task", "close", "task-1", "--outcome", "pass", "--skip-open-subtasks"],
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            ("task_close", {"task_id": "task-1", "outcome": "pass", "skip_open_subtasks": True})
        ]

    def test_forwards_abandon_children(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_close": {"success": True, "task_id": "task-1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(
                cli, ["task", "close", "task-1", "--outcome", "pass", "--abandon-children"]
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            ("task_close", {"task_id": "task-1", "outcome": "pass", "abandon_children": True})
        ]

    def test_forwards_both_close_override_flags(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_close": {"success": True, "task_id": "task-1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(
                cli,
                [
                    "task", "close", "task-1", "--outcome", "pass", "--abandon-children",
                    "--skip-open-subtasks",
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            (
                "task_close",
                {
                    "task_id": "task-1",
                    "outcome": "pass",
                    "abandon_children": True,
                    "skip_open_subtasks": True,
                },
            )
        ]

    def test_omits_close_override_flags_when_not_asked(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_close": {"success": True, "task_id": "task-1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=None),
        ):
            result = runner.invoke(cli, ["task", "close", "task-1", "--outcome", "pass"])
        assert result.exit_code == 0, result.output
        assert "skip_open_subtasks" not in mock.calls[0][1]
        assert "abandon_children" not in mock.calls[0][1]


class TestTaskCreateCLI:
    def test_forwards_json_deliverable_declarations(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"create_task": {"created": "task-1", "title": "Task"}})
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task", "create", "--project", "p1", "--title", "Task", "--description", "body",
                    "--deliverable", '{"id":"module","kind":"file","target":"src/module.py"}',
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock.calls[0][0] == "create_task"
        assert mock.calls[0][1]["deliverables"] == [
            {"id": "module", "kind": "file", "target": "src/module.py"}
        ]

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
            result = runner.invoke(cli, ["prime", "--task-id", "task-1", "--hook-format", "other"])
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

    def test_reply_to_is_forwarded(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"agent_message": {"message_id": "msg-2", "state": "queued"}})
        with patch("src.cli.agent_messages._get_client", return_value=mock):
            result = runner.invoke(
                cli, ["agent", "message", "task-1", "filed", "--reply-to", "msg-1"]
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            (
                "agent_message",
                {"target": "task-1", "body": "filed", "all_running": False, "reply_to": "msg-1"},
            )
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

    def test_bare_hook_never_asks_for_consumed_mail(self, runner):
        """The consumed re-read is opt-in: without the flag the hook asks for
        exactly what it always asked for, so consumed bodies cannot re-enter
        the context on every later prompt (fair-impact-65)."""
        from src.cli.app import cli

        with patch("src.cli.messages._get_client") as get_client:
            result = runner.invoke(cli, ["inbox", "--inject"])
        assert result.exit_code == 0
        assert get_client.call_count == 0

    def test_include_consumed_survives_hook_safety(self, runner):
        """The flag is accepted on the hook form and still exits 0 with no
        recipient — a stale hook file passing it must not break the prompt."""
        from src.cli.app import cli

        with patch("src.cli.messages._get_client") as get_client:
            result = runner.invoke(cli, ["inbox", "--include-consumed"])
        assert result.exit_code == 0
        assert result.output == ""
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


class TestStructuredHandoffCLI:
    def test_structured_options_reach_handler(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_handoff": {"success": True, "handoff_id": "h1"}})
        with (
            patch("src.cli.agent_surface._get_client", return_value=mock),
            patch("src.cli.agent_surface.resolve_claim_epoch", return_value=9),
        ):
            result = runner.invoke(
                cli,
                [
                    "handoff",
                    "--auto",
                    "--task-id",
                    "task-1",
                    "--session-id",
                    "session-1",
                    "--schema-version",
                    "1",
                    "--goal",
                    "Resume",
                    "--completed",
                    "Edited",
                    "--completed",
                    "Linted",
                    "--next-step",
                    "Test",
                    "--waiting-for",
                    "slot",
                    "--file",
                    "a.py",
                    "--decision",
                    "Keep API",
                    "--do-not-repeat",
                    "Serial suite",
                    "--uncertainty",
                    "Timing",
                    "--idempotency-key",
                    "one",
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock.calls == [
            (
                "task_handoff",
                {
                    "auto": True,
                    "task_id": "task-1",
                    "session_id": "session-1",
                    "schema_version": 1,
                    "goal": "Resume",
                    "completed": ["Edited", "Linted"],
                    "next_step": "Test",
                    "waiting_for": "slot",
                    "files": ["a.py"],
                    "decisions": ["Keep API"],
                    "do_not_repeat": ["Serial suite"],
                    "uncertainties": ["Timing"],
                    "idempotency_key": "one",
                    "claim_epoch": 9,
                },
            )
        ]


@pytest.fixture
def pool_handoff_api(tmp_path, monkeypatch):
    """Keep the real CLI transport, bearer middleware, scope and command fences."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import httpx
    from fastapi import FastAPI

    from src.api import dependencies
    from src.api.auth import RequestScope
    from src.api.execute import router
    from src.api.middleware import TokenAuthMiddleware
    from src.cli.client import CLIClient
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.models import AgentProfile, Project, SessionRecord, Task

    session = SessionRecord(
        id="pool-session", project_id="agent-queue", task_id="held-task",
        profile_id="worker", harness="codex", provider="openai", name="pool",
        lifecycle="pool", work_dir="", epoch="e1", instance_token="instance-1",
        started_at=1, state="running", last_claim_epoch=7,
    )
    task = Task(
        id="held-task", project_id="agent-queue", title="Held", description="", claim_epoch=7,
    )
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.get_task = AsyncMock(return_value=task)
    db.get_project = AsyncMock(return_value=Project(id="agent-queue", name="Agent Queue"))
    db.get_profile = AsyncMock(return_value=AgentProfile(
        id="worker", name="Worker", aq_commands=["task_handoff"],
        harness_tools=[], plugin_tools=[],
    ))
    db.count_task_subtasks = AsyncMock(return_value={task.id: (0, 0)})
    db.list_jobs = AsyncMock(return_value=[])
    db.list_agent_waits = AsyncMock(return_value=[])
    db.get_gates_for_task = AsyncMock(return_value=[])
    db.add_task_handoff = AsyncMock(return_value=("handoff-1", True))
    bus = AsyncMock()
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.security.capability_enforcement = "enforce"
    handler = CommandHandler(
        orchestrator=SimpleNamespace(db=db, bus=bus, plugin_registry=None), config=config,
    )
    scope = RequestScope(
        kind="session", session_id=session.id, project_id=session.project_id, task_id=None,
    )
    token_store = SimpleNamespace(validate=AsyncMock(return_value=scope))
    monkeypatch.setattr(dependencies, "_token_store", token_store)
    monkeypatch.setattr(dependencies, "_command_handler", handler)
    monkeypatch.setattr(dependencies, "_require_session_token", True)
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(TokenAuthMiddleware)
    app.dependency_overrides[dependencies.get_command_handler] = lambda: handler

    def client_factory(_api_url=None):
        client = CLIClient(base_url="http://aq.test")

        async def connect():
            client._http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=client._base_url,
                headers={"Authorization": f"Bearer {client._token}"},
            )

        client.connect = connect
        return client

    monkeypatch.setattr("src.cli.agent_surface._get_client", client_factory)
    monkeypatch.setenv("AQ_API_TOKEN", "aqs_pool-test")
    monkeypatch.setenv("AQ_SESSION_ID", session.id)
    monkeypatch.setenv("AQ_PROJECT_ID", session.project_id)
    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    monkeypatch.chdir(tmp_path)
    claim = tmp_path / ".aq" / "claim.json"
    claim.parent.mkdir()
    claim.write_text(json.dumps({
        "task_id": task.id, "session_id": session.id, "claim_epoch": task.claim_epoch,
    }))
    return SimpleNamespace(db=db, bus=bus, token_store=token_store, client=client_factory, claim=claim)


@pytest.mark.parametrize("explicit_task", [False, True])
def test_pool_handoff_cli_records_checkpoint_through_scoped_api(runner, pool_handoff_api, explicit_task):
    from src.cli.app import cli

    evidence = "aq test tests/test_handoffs.py: passed"
    args = [
        "--json", "handoff", "--auto", "--goal", "Finish the fix",
        "--next-step", "Publish", "--constraint", "Keep session authorization",
        "--evidence", evidence,
    ]
    if explicit_task:
        args += ["--task-id", "held-task"]
    result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["restart_requested"] is False
    pool_handoff_api.token_store.validate.assert_awaited_once_with("aqs_pool-test")
    stored = pool_handoff_api.db.add_task_handoff.await_args
    assert stored.args == ("held-task",)
    assert stored.kwargs["claim_epoch"] == 7
    assert stored.kwargs["session_id"] == "pool-session"
    note = json.loads(stored.kwargs["content"])
    assert note["agent"]["goal"] == "Finish the fix"
    assert note["agent"]["next_step"] == "Publish"
    assert note["agent"]["constraints"] == ["Keep session authorization"]
    assert note["agent"]["evidence"] == [evidence]
    assert note["facts"]["task_id"] == "held-task"
    assert note["facts"]["claim_epoch"] == 7
    assert note["facts"]["session_id"] == "pool-session"
    assert not any(call.args[0] == "session.restart_requested"
                   for call in pool_handoff_api.bus.emit.await_args_list)


@pytest.mark.parametrize(("extra", "error", "exit_code"), [
    (["--task-id", "foreign-task"], "does not hold task foreign-task", 1),
    (["--session-id", "foreign-session"], "session_id mismatch", 4),
    (["--claim-epoch", "6"], "claim epoch 6 is not current", 1),
])
def test_pool_handoff_cli_preserves_identity_and_epoch_fences(
    runner, pool_handoff_api, extra, error, exit_code,
):
    from src.cli.app import cli

    result = runner.invoke(cli, [
        "--json", "handoff", "--auto", "--goal", "Resume", *extra,
    ])
    assert result.exit_code == exit_code, result.output
    assert error in result.output
    pool_handoff_api.db.add_task_handoff.assert_not_awaited()


def test_pool_handoff_cli_requires_claim_epoch(runner, pool_handoff_api):
    from src.cli.app import cli

    pool_handoff_api.claim.unlink()
    result = runner.invoke(cli, [
        "--json", "handoff", "--auto", "--goal", "Resume",
    ])
    assert result.exit_code == 1, result.output
    assert "claim_epoch is required for pool sessions" in result.output
    pool_handoff_api.db.add_task_handoff.assert_not_awaited()


def test_pool_handoff_cli_preserves_legacy_restart_request(runner, pool_handoff_api):
    from src.cli.app import cli

    result = runner.invoke(cli, ["--json", "handoff", "Partial fix", "Run the area checks"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["restart_requested"] is True
    stored = pool_handoff_api.db.add_task_handoff.await_args
    assert stored.args == ("held-task",)
    note = json.loads(stored.kwargs["content"])
    assert note["agent"]["subject"] == "Partial fix"
    assert note["agent"]["detail"] == "Run the area checks"
    assert any(call.args[0] == "session.restart_requested"
               for call in pool_handoff_api.bus.emit.await_args_list)


def test_pool_handoff_api_rejects_foreign_project(pool_handoff_api):
    from src.cli.app import _run
    from src.cli.exceptions import ScopeDeniedError

    async def submit():
        async with pool_handoff_api.client() as client:
            await client.execute("task_handoff", {
                "auto": True, "task_id": "held-task", "project_id": "foreign-project",
                "claim_epoch": 7, "goal": "Resume",
            })

    with pytest.raises(ScopeDeniedError, match="project_id mismatch"):
        _run(submit())
    pool_handoff_api.db.add_task_handoff.assert_not_awaited()


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("source", ["resume", "compact"])
def test_compaction_hook_reprime_bypasses_startup_suppression(runner, monkeypatch, harness, source):
    from src.cli.app import cli

    monkeypatch.setenv("AQ_STARTUP_PROMPT_DELIVERED", "1")
    mock = _mock_client({"prime": {"success": True, "body": "Saved continuation and human gate"}})
    with patch("src.cli.agent_surface._get_client", return_value=mock):
        result = runner.invoke(
            cli, ["prime", "--task-id", "task-1", "--hook-format", harness],
            input=json.dumps({"hook_event_name": "SessionStart", "source": source}),
        )
    assert result.exit_code == 0, result.output
    assert len(mock.calls) == 1
    assert json.loads(result.output)["hookSpecificOutput"]["additionalContext"] == (
        "Saved continuation and human gate")


def test_handoff_cli_preserves_constraints_and_exact_evidence(runner):
    from src.cli.app import cli

    mock = _mock_client({"task_handoff": {"success": True}})
    with patch("src.cli.agent_surface._get_client", return_value=mock):
        result = runner.invoke(cli, [
            "handoff", "--auto", "--task-id", "task-1", "--constraint", "Await human gate",
            "--evidence", "aq test tests/test_x.py: failed; raw /tmp/failure.log",
        ])
    assert result.exit_code == 0, result.output
    assert mock.calls[0][1]["constraints"] == ["Await human gate"]
    assert mock.calls[0][1]["evidence"] == [
        "aq test tests/test_x.py: failed; raw /tmp/failure.log"]
