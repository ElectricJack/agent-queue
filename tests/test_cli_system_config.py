"""Contract tests for the hand-crafted system-config CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


def _client(results):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(side_effect=lambda command, args: results[command])
    return client


@pytest.fixture
def runner():
    return CliRunner()


def test_config_set_nested_scalar_dry_run_does_not_write_and_reports_effective_change(runner):
    """Dropping dotted YAML parsing would send a different update to the daemon."""
    from src.cli.app import cli

    client = _client(
        {
            "get_config": {"config": {"scheduling": {"enabled": False}}},
            "update_config": {"dry_run": True},
        }
    )
    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(
            cli, ["system", "config", "set", "scheduling.enabled=true", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    assert "would set" in result.output
    assert client.execute.await_args_list[1].args == (
        "update_config",
        {"section": "scheduling", "data": {"enabled": True}, "dry_run": True},
    )


def test_config_set_rejects_malformed_assignment_without_touching_config(runner):
    """Malformed keys must stop before any daemon read or write."""
    from src.cli.app import cli

    client = _client({})
    with patch("src.cli.system_config._get_client", return_value=client):
        result = runner.invoke(cli, ["system", "config", "set", "not-dotted=true"])

    assert result.exit_code != 0
    assert "KEY must be dotted" in result.output
    client.execute.assert_not_awaited()
