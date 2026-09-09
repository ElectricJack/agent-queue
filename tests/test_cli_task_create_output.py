"""`aq task create` output contract (aq-surface design §4.1, §4.2.1).

Single-task creation used to print human text even under ``--json``: a client
had to scrape ``Task created: <id>`` or fail parsing *after* the task was
already persisted, and a failed parse invites a retry that creates a duplicate.
These tests pin the fix — one JSON document, the real id inside it, and exactly
one ``create_task`` request per invocation.
"""

from __future__ import annotations

import json
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.exceptions import CommandError


@pytest.fixture
def runner():
    return CliRunner()


def _mock_client(handler):
    """A mock ``CLIClient`` async context manager driven by *handler*."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=handler.handle)
    return client


def _created_payload(task_id="proj.7", title="A title"):
    return {
        "created": task_id,
        "success": True,
        "task_id": task_id,
        "gate_id": None,
        "status": "DEFINED",
        "title": title,
        "project_id": "proj",
    }


class _Recorder:
    """Records every command the CLI sends, so retries are visible."""

    def __init__(self, result):
        self.calls: list[tuple[str, dict]] = []
        self._result = result

    async def handle(self, command, args=None):
        self.calls.append((command, dict(args or {})))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    @property
    def creates(self):
        return [c for c in self.calls if c[0] == "create_task"]


def _invoke(runner, argv, recorder, env=None):
    from src.cli.app import cli

    with patch("src.cli.tasks._get_client", return_value=_mock_client(recorder)):
        return runner.invoke(cli, argv, env=env or {})


_ARGS = ["--project", "proj", "--title", "A title", "--description", "D"]


class TestSingleCreateJSON:
    def test_json_mode_prints_one_parseable_envelope_with_the_created_id(self, runner):
        rec = _Recorder(_created_payload())
        result = _invoke(runner, ["--json", "task", "create", *_ARGS], rec)

        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)  # one document, nothing else on stdout
        assert doc["schema_version"] == 1
        assert doc["data"]["created"] == "proj.7"
        assert doc["data"]["task_id"] == "proj.7"
        assert doc["data"]["status"] == "DEFINED"
        assert doc["data"]["title"] == "A title"
        assert "Task created" not in result.output

    def test_json_mode_sends_exactly_one_create_request(self, runner):
        """The parse succeeds, so a client has no reason to retry the write."""
        rec = _Recorder(_created_payload())
        result = _invoke(runner, ["--json", "task", "create", *_ARGS], rec)

        assert len(rec.creates) == 1
        json.loads(result.output)

    def test_brief_trims_to_the_task_created_projection(self, runner):
        from src.cli.envelope import BRIEF_PROJECTIONS

        rec = _Recorder(_created_payload())
        result = _invoke(runner, ["--json", "--brief", "task", "create", *_ARGS], rec)

        data = json.loads(result.output)["data"]
        assert set(data) == set(BRIEF_PROJECTIONS["task_created"])
        assert data["created"] == "proj.7"

    def test_legacy_escape_hatch_prints_the_raw_payload(self, runner):
        rec = _Recorder(_created_payload())
        result = _invoke(
            runner, ["--json", "task", "create", *_ARGS], rec, env={"AQ_JSON_LEGACY": "1"}
        )

        doc = json.loads(result.stdout)
        assert "schema_version" not in doc
        assert doc["created"] == "proj.7"

    def test_title_markup_survives_json_and_does_not_break_the_document(self, runner):
        rec = _Recorder(_created_payload(title="Fix [bold] parsing in {a: b}"))
        result = _invoke(
            runner,
            ["--json", "task", "create", "-p", "proj", "-t", "Fix [bold] parsing", "-d", "D"],
            rec,
        )

        assert json.loads(result.output)["data"]["title"] == "Fix [bold] parsing in {a: b}"

    def test_multiline_title_stays_inside_one_json_document(self, runner):
        rec = _Recorder(_created_payload(title="line one\nline two"))
        result = _invoke(runner, ["--json", "task", "create", *_ARGS], rec)

        assert json.loads(result.output)["data"]["title"] == "line one\nline two"

    def test_command_error_is_one_error_envelope_and_one_request(self, runner):
        rec = _Recorder(CommandError("create_task", "Project 'proj' not found"))
        result = _invoke(runner, ["--json", "task", "create", *_ARGS], rec)

        assert result.exit_code == 1
        doc = json.loads(result.stdout)
        assert doc["error"]["code"] == "command_error"
        assert "not found" in doc["error"]["message"]
        assert doc["data"] is None
        assert len(rec.creates) == 1


class TestSingleCreateHuman:
    def test_human_mode_still_reports_the_id_and_title(self, runner):
        rec = _Recorder(_created_payload())
        result = _invoke(runner, ["task", "create", *_ARGS], rec)

        assert result.exit_code == 0, result.output
        assert "Task created:" in result.output
        assert "proj.7" in result.output
        assert "A title" in result.output

    def test_human_mode_does_not_swallow_square_brackets_in_a_title(self, runner):
        """Rich reads ``[dim]`` as markup; an unescaped title lost its text."""
        rec = _Recorder(_created_payload(title="Fix [bold] parsing"))
        result = _invoke(runner, ["task", "create", *_ARGS], rec)

        assert "[bold]" in result.output

    def test_human_mode_prints_a_multiline_title(self, runner):
        rec = _Recorder(_created_payload(title="line one\nline two"))
        result = _invoke(runner, ["task", "create", *_ARGS], rec)

        assert "line one" in result.output
        assert "line two" in result.output


class TestWizardCancellation:
    def _invoke_cancelled(self, runner, argv):
        from src.cli.app import cli

        rec = _Recorder({"projects": [{"id": "proj"}]})
        with (
            patch("src.cli.tasks._get_client", return_value=_mock_client(rec)),
            patch("src.cli.menus.task_creation_wizard", return_value=None),
        ):
            return runner.invoke(cli, argv), rec

    def test_cancelled_wizard_is_still_one_json_document(self, runner):
        result, rec = self._invoke_cancelled(runner, ["--json", "task", "create"])

        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["data"]["cancelled"] is True
        assert doc["data"]["created"] is None
        assert rec.creates == []

    def test_cancelled_wizard_human_mode_says_so(self, runner):
        result, _ = self._invoke_cancelled(runner, ["task", "create"])

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()


class TestGraphCreateJSON:
    """`--graph` already routed through `emit()`; keep it that way."""

    def _graph_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump({"version": 1, "nodes": [{"key": "a", "title": "A"}]}, fh)
        return fh.name

    def test_graph_success_is_one_envelope_with_the_created_ids(self, runner):
        rec = _Recorder(
            {
                "parent_id": "epic",
                "parent_title": "Epic",
                "nodes": [{"key": "a", "task_id": "epic.1", "title": "A", "needs": []}],
                "warnings": [],
                "dry_run": False,
            }
        )
        result = _invoke(
            runner,
            ["--json", "task", "create", "-p", "proj", "--graph", self._graph_file()],
            rec,
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["parent_id"] == "epic"
        assert data["nodes"][0]["task_id"] == "epic.1"
        assert len([c for c in rec.calls if c[0] == "create_task_graph"]) == 1

    def test_graph_error_carries_its_findings_into_the_error_envelope(self, runner):
        rec = _Recorder(
            CommandError(
                "create_task_graph",
                "graph is invalid",
                {"errors": [{"rule": "cycle", "detail": "a -> a"}]},
            )
        )
        result = _invoke(
            runner,
            ["--json", "task", "create", "-p", "proj", "--graph", self._graph_file()],
            rec,
        )

        assert result.exit_code == 1
        doc = json.loads(result.stdout)
        assert doc["error"]["code"] == "command_error"
        assert doc["error"]["details"]["errors"][0]["rule"] == "cycle"
