"""Tests for the aq-surface Phase S0 output contract.

Covers ``src/cli/envelope.py`` (``envelope()``, ``error_envelope()``, ``emit()``,
``BRIEF_PROJECTIONS``), the global ``--brief`` flag, ``AQ_JSON_LEGACY``, and the new
``aq task show|set|list|details`` / ``aq schema`` commands routed through ``emit()``.
See docs/specs/design/aq-surface.md §4, docs/specs/implementation/aq-surface.md §5.2, §10.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.envelope import (
    BRIEF_PROJECTIONS,
    SCHEMA_VERSION,
    apply_brief,
    emit,
    envelope,
    error_envelope,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clean_legacy_env(monkeypatch):
    """Make sure AQ_JSON_LEGACY never leaks between tests."""
    monkeypatch.delenv("AQ_JSON_LEGACY", raising=False)
    yield
    monkeypatch.delenv("AQ_JSON_LEGACY", raising=False)


def _mock_client(execute_results: dict):
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.close = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def mock_execute(command, args=None):
        result = execute_results.get(command, {})
        if isinstance(result, Exception):
            raise result
        return result

    mock_client.execute = AsyncMock(side_effect=mock_execute)
    return mock_client


# ---------------------------------------------------------------------------
# envelope() / error_envelope() — pure function shapes
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    def test_object_envelope_has_no_pagination(self):
        env = envelope({"id": "task-1"})
        assert env == {"schema_version": 1, "data": {"id": "task-1"}}
        assert "pagination" not in env

    def test_list_envelope_default_total_is_len(self):
        env = envelope([1, 2, 3])
        assert env["schema_version"] == SCHEMA_VERSION
        assert env["data"] == [1, 2, 3]
        assert env["pagination"] == {"returned": 3, "total": 3, "truncated": False}

    def test_list_envelope_explicit_total_truncated(self):
        env = envelope([1, 2], total=143)
        assert env["pagination"] == {"returned": 2, "total": 143, "truncated": True}

    def test_empty_list_envelope(self):
        env = envelope([])
        assert env["pagination"] == {"returned": 0, "total": 0, "truncated": False}

    def test_schema_version_is_stable_integer(self):
        assert isinstance(SCHEMA_VERSION, int)
        assert SCHEMA_VERSION == 1

    def test_error_envelope_shape(self):
        env = error_envelope("not_found", "Task 'x' not found")
        assert env == {
            "schema_version": 1,
            "error": {"code": "not_found", "message": "Task 'x' not found"},
            "data": None,
        }

    def test_envelope_json_serializable(self):
        env = envelope([{"id": "t1", "created_at": 1.0}], total=1)
        # Must round-trip through json.dumps without a custom encoder.
        assert json.loads(json.dumps(env)) == env


# ---------------------------------------------------------------------------
# BRIEF_PROJECTIONS — apply_brief() + completeness
# ---------------------------------------------------------------------------


class TestBriefProjections:
    def test_all_five_entities_from_design_spec_present(self):
        assert set(BRIEF_PROJECTIONS.keys()) == {
            "task",
            "session",
            "gate",
            "message",
            "workspace",
        }

    def test_task_brief_matches_design_table(self):
        assert BRIEF_PROJECTIONS["task"] == (
            "id",
            "title",
            "status",
            "priority",
            "project_id",
            "assigned_agent",
        )

    def test_task_brief_fields_are_real_keys_on_list_tasks_output(self):
        """`aq task list`'s brief projection must be addressable against the
        actual `_task_to_dict()` shape (src/commands/task_commands.py), not
        just the design doc — this is what `--brief --json` really returns.
        """
        from src.commands.task_commands import TaskCommandsMixin
        from src.models import Task

        t = Task(id="t1", project_id="p1", title="T", description="d")
        row = TaskCommandsMixin._task_to_dict(t)
        for field in BRIEF_PROJECTIONS["task"]:
            assert field in row, (
                f"BRIEF_PROJECTIONS['task'] field {field!r} missing from _task_to_dict output"
            )

    def test_task_brief_fields_are_real_keys_on_get_task_output(self):
        """Also addressable against `_cmd_get_task`'s (task_show's) shape."""
        import inspect

        from src.commands.task_commands import TaskCommandsMixin

        src = inspect.getsource(TaskCommandsMixin._cmd_get_task)
        for field in BRIEF_PROJECTIONS["task"]:
            key = "assigned_agent" if field == "assigned_agent" else field
            assert f'"{key}"' in src, f"{key!r} not assigned in _cmd_get_task"

    def test_apply_brief_trims_dict(self):
        full = {
            "id": "t1",
            "title": "Task",
            "status": "READY",
            "priority": 100,
            "project_id": "p1",
            "assigned_agent": None,
            "description": "long text nobody asked for",
        }
        trimmed = apply_brief(full, "task")
        assert trimmed == {
            "id": "t1",
            "title": "Task",
            "status": "READY",
            "priority": 100,
            "project_id": "p1",
            "assigned_agent": None,
        }
        assert "description" not in trimmed

    def test_apply_brief_trims_list(self):
        items = [{"id": "t1", "title": "A", "extra": 1}, {"id": "t2", "title": "B", "extra": 2}]
        trimmed = apply_brief(items, "task")
        assert all("extra" not in item for item in trimmed)
        assert [i["id"] for i in trimmed] == ["t1", "t2"]

    def test_apply_brief_unknown_entity_is_passthrough(self):
        data = {"a": 1}
        assert apply_brief(data, "nonexistent-entity") is data

    def test_apply_brief_none_entity_is_passthrough(self):
        data = [{"a": 1}]
        assert apply_brief(data, None) is data

    def test_apply_brief_missing_fields_default_to_none(self):
        trimmed = apply_brief({"id": "t1"}, "task")
        assert trimmed["title"] is None
        assert trimmed["id"] == "t1"


# ---------------------------------------------------------------------------
# emit() — the CLI output funnel (unit-level, using a bare Namespace ctx)
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self, obj):
        self.obj = obj


class TestEmit:
    def test_json_mode_prints_envelope(self, capsys):
        emit(_FakeCtx({"json": True}), {"id": "t1"})
        out = capsys.readouterr().out
        assert json.loads(out) == {"schema_version": 1, "data": {"id": "t1"}}

    def test_json_mode_with_total_paginates(self, capsys):
        emit(_FakeCtx({"json": True}), [{"id": "t1"}], total=5)
        out = json.loads(capsys.readouterr().out)
        assert out["pagination"] == {"returned": 1, "total": 5, "truncated": True}

    def test_json_brief_trims_before_envelope(self, capsys):
        emit(
            _FakeCtx({"json": True, "brief": True}),
            {
                "id": "t1",
                "title": "T",
                "status": "READY",
                "priority": 1,
                "project_id": "p",
                "assigned_agent": None,
                "description": "secret",
            },
            entity="task",
        )
        out = json.loads(capsys.readouterr().out)
        assert "description" not in out["data"]

    def test_human_mode_calls_render_with_untrimmed_data(self, capsys):
        seen = {}

        def _render(data):
            seen["data"] = data

        full = {
            "id": "t1",
            "title": "T",
            "status": "READY",
            "priority": 1,
            "project_id": "p",
            "assigned_agent": None,
            "description": "keep me",
        }
        emit(_FakeCtx({"json": False, "brief": True}), full, entity="task", render=_render)
        assert seen["data"] == full  # render always gets the untrimmed payload
        assert capsys.readouterr().out == ""

    def test_human_mode_without_render_falls_back_to_json_dump(self, capsys):
        emit(_FakeCtx({"json": False}), {"id": "t1"})
        out = capsys.readouterr().out
        assert json.loads(out) == {"id": "t1"}

    def test_legacy_env_prints_raw_payload_and_stderr_warning(self, capsys, monkeypatch):
        monkeypatch.setenv("AQ_JSON_LEGACY", "1")
        emit(_FakeCtx({"json": True}), {"id": "t1"})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"id": "t1"}
        assert "schema_version" not in captured.out
        assert "AQ_JSON_LEGACY" in captured.err

    def test_legacy_env_still_applies_brief(self, capsys, monkeypatch):
        monkeypatch.setenv("AQ_JSON_LEGACY", "1")
        emit(
            _FakeCtx({"json": True, "brief": True}),
            {
                "id": "t1",
                "title": "T",
                "status": "READY",
                "priority": 1,
                "project_id": "p",
                "assigned_agent": None,
                "description": "secret",
            },
            entity="task",
        )
        out = json.loads(capsys.readouterr().out)
        assert "description" not in out

    def test_missing_obj_defaults_to_human_mode(self, capsys):
        emit(_FakeCtx(None), {"id": "t1"}, render=lambda d: print("rendered"))
        assert capsys.readouterr().out.strip() == "rendered"


# ---------------------------------------------------------------------------
# --brief global flag wiring
# ---------------------------------------------------------------------------


class TestGlobalBriefFlag:
    def test_brief_flag_sets_ctx_obj(self, runner):
        import click

        from src.cli.app import cli

        @cli.command("_probe_brief", hidden=True)
        @click.pass_context
        def _probe(ctx):
            click.echo(str(ctx.obj.get("brief")))

        try:
            result = runner.invoke(cli, ["--brief", "_probe_brief"])
            assert result.exit_code == 0
            assert result.output.strip() == "True"

            result2 = runner.invoke(cli, ["_probe_brief"])
            assert result2.exit_code == 0
            assert result2.output.strip() == "False"
        finally:
            cli.commands.pop("_probe_brief", None)


# ---------------------------------------------------------------------------
# aq task list|show|set|details — routed through emit()
# ---------------------------------------------------------------------------


class TestTaskShowSetListDetailsCLI:
    def test_list_json_envelope_shape(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "list_tasks": {
                    "tasks": [
                        {
                            "id": "task-1",
                            "project_id": "proj",
                            "title": "Test task",
                            "status": "IN_PROGRESS",
                            "priority": 100,
                            "task_type": "feature",
                            "assigned_agent": "ws-1",
                        }
                    ],
                    "total": 1,
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "task", "list"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == 1
        assert payload["data"][0]["id"] == "task-1"
        assert payload["pagination"] == {"returned": 1, "total": 1, "truncated": False}

    def test_list_brief_json_trims_fields(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "list_tasks": {
                    "tasks": [
                        {
                            "id": "task-1",
                            "project_id": "proj",
                            "title": "Test task",
                            "status": "IN_PROGRESS",
                            "priority": 100,
                            "task_type": "feature",
                            "assigned_agent": "ws-1",
                            "pr_url": None,
                            "requires_approval": False,
                        }
                    ],
                    "total": 1,
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "--brief", "task", "list"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"] == [
            {
                "id": "task-1",
                "title": "Test task",
                "status": "IN_PROGRESS",
                "priority": 100,
                "project_id": "proj",
                "assigned_agent": "ws-1",
            }
        ]

    def test_list_human_mode_renders_table_not_json(self, runner):
        """Regression guard: hand-crafted `task list` must not lose the
        Rich-table behavior the auto-generated `list_tasks` command had.
        """
        from src.cli.app import cli

        mock = _mock_client(
            {
                "list_tasks": {
                    "tasks": [
                        {
                            "id": "task-1",
                            "project_id": "proj",
                            "title": "Test task",
                            "status": "IN_PROGRESS",
                            "priority": 100,
                            "task_type": "feature",
                            "assigned_agent": "ws-1",
                        }
                    ],
                    "total": 1,
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "list"])
        assert result.exit_code == 0, result.output
        assert "Test task" in result.output
        assert '"task-1"' not in result.output

    def test_show_json_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "task_show": {
                    "id": "task-1",
                    "project_id": "proj",
                    "title": "Test task",
                    "status": "IN_PROGRESS",
                    "priority": 100,
                    "description": "d",
                    "depends_on": [],
                    "blocks": [],
                    "context": [],
                    "labels": ["urgent"],
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "task", "show", "task-1"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["labels"] == ["urgent"]
        assert "pagination" not in payload  # single-object envelope

    def test_show_human_mode_renders_panel(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "task_show": {
                    "id": "task-1",
                    "project_id": "proj",
                    "title": "Test task",
                    "status": "IN_PROGRESS",
                    "priority": 100,
                    "description": "d",
                    "depends_on": [],
                    "blocks": [],
                    "context": [],
                    "labels": ["urgent"],
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "show", "task-1"])
        assert result.exit_code == 0, result.output
        assert "Test task" in result.output
        assert "urgent" in result.output

    def test_details_is_an_alias_of_show(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "task_show": {
                    "id": "task-1",
                    "project_id": "proj",
                    "title": "Aliased task",
                    "status": "IN_PROGRESS",
                    "priority": 100,
                    "description": "d",
                    "depends_on": [],
                    "blocks": [],
                    "context": [],
                    "labels": [],
                }
            }
        )
        with patch("src.cli.tasks._get_client", return_value=mock):
            show_result = runner.invoke(cli, ["task", "show", "task-1"])
            details_result = runner.invoke(cli, ["task", "details", "task-1"])
        assert show_result.exit_code == details_result.exit_code == 0
        assert "Aliased task" in show_result.output
        assert "Aliased task" in details_result.output

    def test_set_sends_expected_args(self, runner):
        from src.cli.app import cli

        captured = {}

        async def mock_execute(command, args=None):
            if command == "task_set":
                captured.update(args or {})
                return {"id": "task-1", "fields_changed": list((args or {}).keys())}
            return {}

        mock = _mock_client({})
        mock.execute = AsyncMock(side_effect=mock_execute)

        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task",
                    "set",
                    "task-1",
                    "--branch",
                    "feat/x",
                    "--pr-url",
                    "https://example/pr/1",
                    "--note",
                    "progress",
                    "--label",
                    "+urgent",
                    "--label",
                    "-stale",
                    "--meta",
                    "owner=alice",
                ],
            )
        assert result.exit_code == 0, result.output
        assert captured["task_id"] == "task-1"
        assert captured["branch"] == "feat/x"
        assert captured["pr_url"] == "https://example/pr/1"
        assert captured["note"] == "progress"
        assert captured["labels_add"] == ["urgent"]
        assert captured["labels_remove"] == ["stale"]
        assert captured["meta"] == {"owner": "alice"}

    def test_set_json_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client({"task_set": {"id": "task-1", "fields_changed": ["branch_name"]}})
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "task", "set", "task-1", "--branch", "feat/x"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == 1
        assert payload["data"]["fields_changed"] == ["branch_name"]

    def test_set_meta_rejects_missing_equals(self, runner):
        from src.cli.app import cli

        mock = _mock_client({})
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(cli, ["task", "set", "task-1", "--meta", "not-a-kv-pair"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# aq schema
# ---------------------------------------------------------------------------


class TestSchemaCLI:
    def test_schema_json_envelope(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "get_schema": {
                    "schema_version": 1,
                    "enums": {"task_status": ["DEFINED", "READY"]},
                }
            }
        )
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["--json", "schema"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == 1
        assert payload["data"]["enums"]["task_status"] == ["DEFINED", "READY"]

    def test_schema_human_mode_renders_table(self, runner):
        from src.cli.app import cli

        mock = _mock_client(
            {
                "get_schema": {
                    "schema_version": 1,
                    "enums": {"task_status": ["DEFINED", "READY"]},
                }
            }
        )
        with patch("src.cli.agent_surface._get_client", return_value=mock):
            result = runner.invoke(cli, ["schema"])
        assert result.exit_code == 0, result.output
        assert "task_status" in result.output
        assert "DEFINED" in result.output


# ---------------------------------------------------------------------------
# AQ_API_URL / AQ_API_TOKEN plumbing (CLIClient)
# ---------------------------------------------------------------------------


class TestCLIClientEnvPlumbing:
    def test_aq_api_url_takes_priority(self, monkeypatch):
        monkeypatch.setenv("AQ_API_URL", "http://example:9999")
        monkeypatch.setenv("AGENT_QUEUE_API_URL", "http://legacy:1111")
        from src.cli.client import _resolve_api_url

        assert _resolve_api_url() == "http://example:9999"

    def test_agent_queue_api_url_is_a_fallback(self, monkeypatch):
        monkeypatch.delenv("AQ_API_URL", raising=False)
        monkeypatch.setenv("AGENT_QUEUE_API_URL", "http://legacy:1111")
        from src.cli.client import _resolve_api_url

        assert _resolve_api_url() == "http://legacy:1111"

    def test_token_from_env_is_picked_up(self, monkeypatch):
        monkeypatch.setenv("AQ_API_TOKEN", "aqs_test123")
        from src.cli.client import CLIClient

        client = CLIClient(base_url="http://x")
        assert client._token == "aqs_test123"

    def test_explicit_token_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("AQ_API_TOKEN", "aqs_env")
        from src.cli.client import CLIClient

        client = CLIClient(base_url="http://x", token="aqs_explicit")
        assert client._token == "aqs_explicit"

    def test_no_token_means_no_auth_header_field(self, monkeypatch):
        monkeypatch.delenv("AQ_API_TOKEN", raising=False)
        from src.cli.client import CLIClient

        client = CLIClient(base_url="http://x")
        assert client._token is None
