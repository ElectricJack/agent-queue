"""Delete and its deprecated alias share choices, preview and one confirmation."""

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("AQ_TASK_ID", "AQ_SESSION_ID", "AQ_CLAIM_EPOCH"):
        monkeypatch.delenv(key, raising=False)


def client():
    mock = AsyncMock()
    mock.__aenter__.return_value = mock
    mock.execute.return_value = {
        "success": True, "deleted": "epic", "disposition": "deleted", "branches": "keep",
        "task_ids": ["epic"], "tasks": [{"id": "epic", "title": "Epic", "status": "READY"}],
        "branch_details": [], "warnings": [], "audit_id": 123,
    }
    return mock


@pytest.mark.parametrize("name", ["delete", "remove"])
def test_confirm_once_after_preview_with_explicit_options(name):
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        result = CliRunner().invoke(cli, ["task", name, "epic", "--recursive", "--branches",
                                          "delete", "--reason", "Superseded", "--stop-running"],
                                    input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("[y/N]") == 1
    assert mock.execute.await_count == 2
    assert mock.execute.await_args_list[0].args == ("delete_task", {
        "task_id": "epic", "recursive": True, "branches": "delete", "stop_running": True,
        "reason": "Superseded", "confirmed": False,
    })
    assert mock.execute.await_args.args[1]["confirmed"] is True


def test_cancel_reads_preview_without_mutation():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        cancelled = CliRunner().invoke(cli, ["task", "delete", "epic", "--task-only",
                                             "--branches", "keep", "--reason", ""], input="n\n")
    assert cancelled.exit_code != 0
    assert mock.execute.await_count == 1
    assert mock.execute.await_args.args[1]["confirmed"] is False


def test_interactive_scope_branch_and_optional_reason():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        result = CliRunner().invoke(cli, ["task", "delete", "epic"],
                                    input="recursive\ndelete\n\ny\n")
    assert result.exit_code == 0, result.output
    assert mock.execute.await_args.args[1]["recursive"] is True
    assert mock.execute.await_args.args[1]["branches"] == "delete"
    assert mock.execute.await_args.args[1]["reason"] == ""
    assert result.output.count("[y/N]") == 1


@pytest.mark.parametrize("name", ["delete", "remove"])
def test_yes_defaults_to_single_task_keep_without_reason(name):
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock) as get_client:
        result = CliRunner().invoke(cli, ["--api-url", "http://localhost:9876", "--json",
                                         "task", name, "epic", "--yes"])
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert mock.execute.await_args.args[1] == {
        "task_id": "epic", "recursive": False, "branches": "keep", "stop_running": False,
        "reason": "", "confirmed": True,
    }
    get_client.assert_called_once_with("http://localhost:9876")


def test_preview_never_prompts_or_confirms():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        result = CliRunner().invoke(cli, ["task", "delete", "epic", "--recursive", "--preview"])
    assert result.exit_code == 0, result.output
    assert mock.execute.await_count == 1
    assert mock.execute.await_args.args[1]["confirmed"] is False
