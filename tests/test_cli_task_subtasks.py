"""CLI verbs for task subtasks — args sent, task-id fallback, literal rendering."""

from unittest.mock import AsyncMock, patch
import json

import pytest

from click.testing import CliRunner

from src.cli.app import cli


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("AQ_TASK_ID", "AQ_SESSION_ID", "AQ_CLAIM_EPOCH"):
        monkeypatch.delenv(key, raising=False)


def client_for(result):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = result
    return client


def test_subtasks_lists_with_task_id_positional():
    client = client_for(
        {
            "task_id": "t-1",
            "subtasks": [{"ordinal": 1, "status": "pending", "title": "[red]literal[/red]"}],
            "total": 1,
            "settled": 0,
        }
    )
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtasks", "t-1"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_subtasks", {"task_id": "t-1"})
    assert "[red]literal[/red]" in result.output
    assert "0/1 settled" in result.output


def test_subtasks_omits_task_id_with_no_flag_and_no_env():
    client = client_for({"task_id": "t-2", "subtasks": [], "total": 0, "settled": 0})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtasks"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_subtasks", {})


def test_subtasks_falls_back_to_aq_task_id(monkeypatch):
    monkeypatch.setenv("AQ_TASK_ID", "env-task")
    client = client_for({"task_id": "env-task", "subtasks": [], "total": 0, "settled": 0})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtasks"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_subtasks", {"task_id": "env-task"})


def test_subtask_add_repeatable_title_and_context_on_last():
    client = client_for({"task_id": "t-1", "subtasks": [{"id": "s1"}, {"id": "s2"}]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json", "task", "subtask-add", "t-1",
                "--title", "First", "--title", "Second", "--context", "extra",
            ],
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_add",
        {
            "task_id": "t-1",
            "subtasks": [{"title": "First"}, {"title": "Second", "context": "extra"}],
        },
    )


def test_subtask_add_from_file(tmp_path):
    payload = tmp_path / "subtasks.json"
    payload.write_text(json.dumps([{"title": "From file", "context": "c"}]))
    client = client_for({"task_id": "t-1", "subtasks": [{"id": "s1"}]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["task", "subtask-add", "t-1", "--from-file", str(payload)]
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_add",
        {"task_id": "t-1", "subtasks": [{"title": "From file", "context": "c"}]},
    )


def test_subtask_add_requires_title_or_file():
    result = CliRunner().invoke(cli, ["task", "subtask-add", "t-1"])
    assert result.exit_code != 0


def test_subtask_add_uses_claim_file_epoch(tmp_path):
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text('{"claim_epoch": 9}')
    client = client_for({"task_id": "t-1", "subtasks": [{"id": "s1"}]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["task", "subtask-add", "t-1", "--title", "Held"]
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_add",
        {"task_id": "t-1", "subtasks": [{"title": "Held"}], "claim_epoch": 9},
    )


def test_subtask_add_explicit_claim_epoch_wins_over_claim_file(tmp_path):
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text('{"claim_epoch": 9}')
    client = client_for({"task_id": "t-1", "subtasks": [{"id": "s1"}]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            ["task", "subtask-add", "t-1", "--title", "Held", "--claim-epoch", "42"],
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_add",
        {"task_id": "t-1", "subtasks": [{"title": "Held"}], "claim_epoch": 42},
    )


def test_subtask_show_uses_task_option():
    client = client_for(
        {
            "subtask": {
                "ordinal": 2,
                "title": "[red]literal[/red]",
                "status": "pending",
                "note": None,
                "context": "detail",
            }
        }
    )
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtask-show", "2", "--task", "t-1"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_subtask_get", {"ordinal": 2, "task_id": "t-1"})
    assert "[red]literal[/red]" in result.output
    assert "detail" in result.output


def test_subtask_done_sends_status_and_claim_epoch(tmp_path):
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text('{"claim_epoch": 5}')
    client = client_for({"subtask": {"ordinal": 1, "status": "done"}, "total": 1, "settled": 1})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtask-done", "1", "--task", "t-1"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_update", {"ordinal": 1, "task_id": "t-1", "status": "done", "claim_epoch": 5}
    )


def test_subtask_start_sends_in_progress_status():
    client = client_for({"subtask": {"ordinal": 1, "status": "in_progress"}, "total": 1, "settled": 0})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "subtask-start", "1", "--task", "t-1"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_update", {"ordinal": 1, "task_id": "t-1", "status": "in_progress"}
    )


def test_subtask_skip_requires_note_and_sends_skipped_status():
    missing = CliRunner().invoke(cli, ["task", "subtask-skip", "1", "--task", "t-1"])
    assert missing.exit_code != 0

    client = client_for({"subtask": {"ordinal": 1, "status": "skipped"}, "total": 1, "settled": 1})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["task", "subtask-skip", "1", "--task", "t-1", "--note", "not needed"]
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_subtask_update",
        {"ordinal": 1, "task_id": "t-1", "status": "skipped", "note": "not needed"},
    )
