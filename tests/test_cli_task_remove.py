"""The operator removes a subtree with one explicit confirmation."""

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
        "success": True, "removed": "epic", "disposition": "archived", "branches": "keep",
    }
    return mock


def test_remove_confirms_once_and_sends_reason():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        result = CliRunner().invoke(cli, ["task", "remove", "epic", "--reason", "Superseded"],
                                    input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("[y/N]") == 1
    assert "branches" in result.output.lower()
    mock.execute.assert_awaited_once_with("remove_task", {
        "task_id": "epic", "confirmed": True, "reason": "Superseded",
    })


def test_remove_cancel_and_blank_reason_never_mutate():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock):
        cancelled = CliRunner().invoke(cli, ["task", "remove", "epic", "--reason", "Dead"],
                                       input="n\n")
        blank = CliRunner().invoke(cli, ["task", "remove", "epic", "--reason", "  ", "--yes"])
    assert cancelled.exit_code != 0
    assert blank.exit_code != 0
    mock.execute.assert_not_awaited()


def test_remove_yes_is_explicit_noninteractive_confirmation():
    mock = client()
    with patch("src.cli.tasks._get_client", return_value=mock) as get_client:
        result = CliRunner().invoke(cli, [
            "--api-url", "http://localhost:9876", "--json", "task", "remove", "epic",
            "--reason", "Dead", "--yes",
        ])
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert mock.execute.await_args.args[1]["confirmed"] is True
    get_client.assert_called_once_with("http://localhost:9876")
