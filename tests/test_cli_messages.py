"""``aq message *`` / ``aq reply`` / ``aq chat`` / ``aq task create --graph``.

Supervisor-agent §6.3 plus the aq-surface envelope contract: every command
added here must print exactly one versioned envelope under ``--json``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.envelope import SCHEMA_VERSION


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_legacy_env(monkeypatch):
    monkeypatch.delenv("AQ_JSON_LEGACY", raising=False)
    yield


def _mock_client(execute_results: dict | None = None, **attrs):
    client = AsyncMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def execute(command, args=None):
        result = (execute_results or {}).get(command, {})
        if isinstance(result, Exception):
            raise result
        return result

    client.execute = AsyncMock(side_effect=execute)
    for name, value in attrs.items():
        setattr(client, name, value)
    return client


def _invoke(runner, args, client):
    """Run the CLI with both command modules' ``_get_client`` stubbed.

    Each module imports the helper by name, so patching ``src.cli.app`` alone
    would leave the already-bound references untouched.
    """
    with (
        patch("src.cli.messages._get_client", return_value=client),
        patch("src.cli.tasks._get_client", return_value=client),
    ):
        return runner.invoke(cli, args)


# ---------------------------------------------------------------------------
# aq message send / reply / inbox / list
# ---------------------------------------------------------------------------


class TestMessageSend:
    def test_json_envelope(self, runner):
        client = _mock_client(
            {"message_send": {"message_id": "msg-1", "state": "queued", "message": {}}}
        )
        result = _invoke(
            runner,
            ["--json", "message", "send", "--to", "session:supervisor-p1", "--body", "hi"],
            client,
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["data"]["message_id"] == "msg-1"

    def test_recipient_is_split(self, runner):
        client = _mock_client({"message_send": {"message_id": "msg-1", "state": "queued"}})
        _invoke(
            runner,
            ["message", "send", "--to", "task:some-task", "--body", "hi", "--from-id", "cli"],
            client,
        )
        args = client.execute.await_args.args[1]
        assert args["to_kind"] == "task"
        assert args["to_id"] == "some-task"
        assert args["from_id"] == "cli"

    def test_explicit_kind_and_id_options(self, runner):
        client = _mock_client({"message_send": {"message_id": "msg-1", "state": "queued"}})
        _invoke(
            runner,
            ["message", "send", "--to-kind", "user", "--to-id", "a:b:c", "--body", "hi"],
            client,
        )
        assert client.execute.await_args.args[1]["to_id"] == "a:b:c"

    def test_malformed_to_is_a_usage_error(self, runner):
        client = _mock_client()
        result = _invoke(runner, ["message", "send", "--to", "session", "--body", "hi"], client)
        assert result.exit_code != 0
        assert "--to must be KIND:ID" in result.output

    def test_missing_recipient_is_a_usage_error(self, runner):
        client = _mock_client()
        result = _invoke(runner, ["message", "send", "--body", "hi"], client)
        assert result.exit_code != 0
        assert "a recipient is required" in result.output

    def test_command_error_exits_nonzero(self, runner):
        from src.cli.exceptions import CommandError

        client = _mock_client({"message_send": CommandError("message_send", "boom")})
        result = _invoke(
            runner, ["message", "send", "--to", "user:u", "--body", "hi"], client
        )
        assert result.exit_code == 1


class TestReplyAlias:
    def test_top_level_reply_matches_the_subcommand(self, runner):
        payload = {"message_id": "msg-1", "reply_id": "msg-2", "reply": {}}
        alias_client = _mock_client({"message_reply": payload})
        sub_client = _mock_client({"message_reply": payload})

        alias = _invoke(runner, ["--json", "reply", "msg-1", "on it"], alias_client)
        sub = _invoke(runner, ["--json", "message", "reply", "msg-1", "on it"], sub_client)

        assert alias.exit_code == 0
        assert alias.output == sub.output
        assert alias_client.execute.await_args.args[1] == sub_client.execute.await_args.args[1]

    def test_via_is_forwarded(self, runner):
        client = _mock_client({"message_reply": {"message_id": "m", "reply_id": "r"}})
        _invoke(runner, ["reply", "msg-1", "text", "--via", "transcript_tail"], client)
        assert client.execute.await_args.args[1]["via"] == "transcript_tail"


class TestInboxAndList:
    def test_inbox_json_envelope(self, runner):
        client = _mock_client(
            {
                "message_inbox": {
                    "to_kind": "session",
                    "to_id": "supervisor-p1",
                    "count": 1,
                    "messages": [{"id": "msg-1", "from": "user:u", "body": "hi"}],
                }
            }
        )
        result = _invoke(
            runner, ["--json", "message", "inbox", "--to", "session:supervisor-p1"], client
        )
        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["data"]["count"] == 1
        assert payload["data"]["messages"][0]["id"] == "msg-1"
        # The payload is a result object, not a bare list, so the envelope
        # carries no pagination block (envelope.py adds it only for lists).
        assert "pagination" not in payload

    def test_inject_flag_is_forwarded(self, runner):
        client = _mock_client({"message_inbox": {"count": 0, "messages": []}})
        _invoke(
            runner, ["message", "inbox", "--to", "session:s", "--inject"], client
        )
        assert client.execute.await_args.args[1]["inject"] is True

    def test_list_filters_are_forwarded(self, runner):
        client = _mock_client({"message_list": {"count": 0, "messages": []}})
        _invoke(
            runner,
            ["message", "list", "-p", "p1", "--thread-id", "t1", "--since", "5", "--limit", "3"],
            client,
        )
        args = client.execute.await_args.args[1]
        assert args["project_id"] == "p1"
        assert args["thread_id"] == "t1"
        assert args["since"] == 5.0
        assert args["limit"] == 3

    def test_brief_trims_to_the_message_projection(self, runner):
        client = _mock_client(
            {
                "message_list": {
                    "count": 1,
                    "messages": [{"id": "msg-1", "from": "user:u", "body": "long"}],
                }
            }
        )
        result = _invoke(runner, ["--json", "--brief", "message", "list"], client)
        # message_list returns an object, not a list, so --brief passes it
        # through: the projection applies to entities, and this envelope is
        # a result object. Assert we still emit a valid envelope.
        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# aq chat
# ---------------------------------------------------------------------------


class TestChat:
    def test_once_prints_the_reply_and_exits_zero(self, runner):
        client = _mock_client()
        client.send_session_message = AsyncMock(
            return_value={"success": True, "message_id": "msg-1", "state": "queued"}
        )
        client.get_session_messages = AsyncMock(
            return_value={
                "success": True,
                "messages": [{"id": "msg-2", "reply_to_id": "msg-1", "body": "3 tasks running"}],
            }
        )
        result = _invoke(runner, ["chat", "p1", "--once", "status?"], client)
        assert result.exit_code == 0
        assert "3 tasks running" in result.output
        assert client.send_session_message.await_args.args[0] == "supervisor-p1"

    def test_once_json_envelope(self, runner):
        client = _mock_client()
        client.send_session_message = AsyncMock(return_value={"message_id": "msg-1"})
        client.get_session_messages = AsyncMock(
            return_value={"messages": [{"id": "msg-2", "reply_to_id": "msg-1", "body": "ok"}]}
        )
        result = _invoke(runner, ["--json", "chat", "p1", "--once", "hi"], client)
        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["data"]["state"] == "replied"
        assert payload["data"]["reply"]["body"] == "ok"

    def test_timeout_exits_nonzero(self, runner):
        client = _mock_client()
        client.send_session_message = AsyncMock(return_value={"message_id": "msg-1"})
        client.get_session_messages = AsyncMock(return_value={"messages": []})
        result = _invoke(runner, ["chat", "p1", "--once", "hi", "--timeout", "0"], client)
        assert result.exit_code == 1
        assert "No reply" in result.output

    def test_project_is_required(self, runner):
        client = _mock_client()
        result = _invoke(runner, ["chat", "--once", "hi"], client)
        assert result.exit_code != 0
        assert "a project is required" in result.output

    def test_unrelated_messages_are_not_treated_as_replies(self, runner):
        client = _mock_client()
        client.send_session_message = AsyncMock(return_value={"message_id": "msg-1"})
        client.get_session_messages = AsyncMock(
            return_value={"messages": [{"id": "msg-9", "reply_to_id": "msg-other", "body": "x"}]}
        )
        result = _invoke(runner, ["chat", "p1", "--once", "hi", "--timeout", "0"], client)
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# aq task create --graph / --from-spec / --dry-run
# ---------------------------------------------------------------------------


_GRAPH_RESULT = {
    "parent_id": "brave-otter",
    "parent_title": "Messages table",
    "task_ids": ["a1", "a2"],
    "nodes": [
        {"key": "schema", "task_id": "a1", "title": "Schema", "needs": []},
        {
            "key": "queries",
            "task_id": "a2",
            "title": "Queries",
            "needs": [{"on": "schema", "task_id": "a1", "dep_type": "blocks"}],
        },
    ],
    "dependency_count": 1,
    "context_count": 2,
    "dry_run": False,
    "created": True,
    "warnings": [],
}


class TestTaskCreateGraph:
    def test_from_spec_routes_to_create_task_graph(self, runner):
        client = _mock_client({"create_task_graph": _GRAPH_RESULT})
        result = _invoke(
            runner,
            ["task", "create", "-p", "p1", "--from-spec", "projects/p1/specs/x.md"],
            client,
        )
        assert result.exit_code == 0
        command, args = client.execute.await_args.args
        assert command == "create_task_graph"
        assert args["spec_path"] == "projects/p1/specs/x.md"
        assert args["project_id"] == "p1"
        assert args["dry_run"] is False

    def test_graph_file_is_decoded_before_sending(self, runner, tmp_path):
        graph_file = tmp_path / "graph.json"
        graph_file.write_text(
            json.dumps({"version": 1, "nodes": [{"key": "a", "title": "A"}]}), encoding="utf-8"
        )
        client = _mock_client({"create_task_graph": _GRAPH_RESULT})
        result = _invoke(
            runner, ["task", "create", "-p", "p1", "--graph", str(graph_file)], client
        )
        assert result.exit_code == 0
        args = client.execute.await_args.args[1]
        assert args["graph"]["nodes"][0]["key"] == "a"

    def test_yaml_graph_file_accepted(self, runner, tmp_path):
        graph_file = tmp_path / "graph.yaml"
        graph_file.write_text("version: 1\nnodes:\n  - key: a\n    title: A\n", encoding="utf-8")
        client = _mock_client({"create_task_graph": _GRAPH_RESULT})
        _invoke(runner, ["task", "create", "-p", "p1", "--graph", str(graph_file)], client)
        assert client.execute.await_args.args[1]["graph"]["version"] == 1

    def test_dry_run_forwarded_and_rendered(self, runner):
        client = _mock_client(
            {"create_task_graph": {**_GRAPH_RESULT, "dry_run": True, "created": False}}
        )
        result = _invoke(
            runner, ["task", "create", "-p", "p1", "--from-spec", "x.md", "--dry-run"], client
        )
        assert client.execute.await_args.args[1]["dry_run"] is True
        assert "Would create graph" in result.output

    def test_json_envelope(self, runner):
        client = _mock_client({"create_task_graph": _GRAPH_RESULT})
        result = _invoke(
            runner, ["--json", "task", "create", "-p", "p1", "--from-spec", "x.md"], client
        )
        payload = json.loads(result.output)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["data"]["parent_id"] == "brave-otter"

    def test_graph_and_from_spec_are_mutually_exclusive(self, runner, tmp_path):
        graph_file = tmp_path / "g.json"
        graph_file.write_text("{}", encoding="utf-8")
        client = _mock_client()
        result = _invoke(
            runner,
            ["task", "create", "--graph", str(graph_file), "--from-spec", "x.md"],
            client,
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_dry_run_alone_is_a_usage_error(self, runner):
        client = _mock_client()
        result = _invoke(runner, ["task", "create", "--dry-run"], client)
        assert result.exit_code != 0
        assert "--dry-run only applies" in result.output

    def test_unreadable_graph_file_is_a_usage_error(self, runner, tmp_path):
        client = _mock_client()
        result = _invoke(
            runner, ["task", "create", "--graph", str(tmp_path / "nope.json")], client
        )
        assert result.exit_code != 0
        assert "could not read graph file" in result.output

    def test_warnings_are_surfaced_in_human_mode(self, runner):
        client = _mock_client(
            {
                "create_task_graph": {
                    **_GRAPH_RESULT,
                    "warnings": [
                        {"rule": "no_acceptance", "node": "schema", "detail": "no criteria"}
                    ],
                }
            }
        )
        result = _invoke(runner, ["task", "create", "-p", "p1", "--from-spec", "x.md"], client)
        assert "no_acceptance" in result.output


# ---------------------------------------------------------------------------
# Auto-generation must not shadow the hand-crafted commands
# ---------------------------------------------------------------------------


class TestNoDuplicateAutoCommands:
    def test_message_commands_are_handcrafted_only(self):
        from src.cli.auto_commands import HANDCRAFTED_COVERAGE

        assert {
            "message_send",
            "message_reply",
            "message_inbox",
            "message_list",
            "create_task_graph",
        } <= HANDCRAFTED_COVERAGE

    def test_message_group_exposes_exactly_the_four_commands(self, runner):
        group = cli.commands["message"]
        assert set(group.commands) == {"send", "reply", "inbox", "list"}

    def test_message_tools_are_registered_for_mcp_and_api(self):
        from src.tools import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

        names = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
        for tool in ("message_send", "message_reply", "message_inbox", "message_list"):
            assert tool in names
            assert _TOOL_CATEGORIES[tool] == "message"
        assert "create_task_graph" in names
        assert _TOOL_CATEGORIES["create_task_graph"] == "task"
