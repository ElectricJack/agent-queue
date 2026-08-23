"""CLI wiring tests for `aq stream *` — exercises the Click commands against
a stubbed CLIClient so no real daemon is required."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from src.cli.app import cli


def test_stream_start_invokes_client_with_argv_after_dashdash():
    runner = CliRunner()
    fake_result = {"stream_id": "abc123", "status": "running"}
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.start_stream = AsyncMock(return_value=fake_result)
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(
            cli, ["stream", "start", "--title", "Running pytest",
                  "--session-id", "supervisor-global", "--cwd", "/tmp",
                  "--", "pytest", "tests/", "-x"],
        )
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    client.start_stream.assert_awaited_once_with(
        ["pytest", "tests/", "-x"], "/tmp",
        title="Running pytest", session_id="supervisor-global", project_id=None,
    )


def test_stream_kill_invokes_client():
    runner = CliRunner()
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.kill_stream = AsyncMock(return_value={"stream_id": "abc123", "status": "killed"})
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(cli, ["stream", "kill", "abc123"])
    assert result.exit_code == 0, result.output
    client.kill_stream.assert_awaited_once_with("abc123")


def test_stream_tail_invokes_client():
    runner = CliRunner()
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.tail_stream = AsyncMock(return_value={"frames": [], "status": "running", "exit_code": None})
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(cli, ["stream", "tail", "abc123"])
    assert result.exit_code == 0, result.output
    client.tail_stream.assert_awaited_once_with("abc123", after_seq=-1)
