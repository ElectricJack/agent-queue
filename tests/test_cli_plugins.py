"""Contract tests for the ``aq plugin`` CLI layer (api-cli plan 19).

CLI layer only (plan X4): the PluginClient and loader are faked; what is
under test is argument validation, exact client calls, and that failures
exit nonzero without rendering success.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner


def _plugin_client(*, plugin: dict | None = None, fail: str | None = None):
    """Fake PluginClient context manager.

    ``fail`` names one method that raises instead of succeeding.
    """
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_plugin = AsyncMock(return_value=plugin)
    client.list_plugins = AsyncMock(return_value=[])
    for method in ("update_plugin", "create_plugin", "delete_plugin",
                   "delete_plugin_data_all"):
        mock = AsyncMock()
        if fail == method:
            mock.side_effect = RuntimeError("db unavailable")
        setattr(client, method, mock)
    return client


@pytest.fixture
def runner():
    return CliRunner()


def test_plugin_lifecycle_validates_options_and_prints_operation_errors(
    runner, tmp_path,
):
    """Plan 19: exact calls on success; nonzero + no success text on failure."""
    from src.cli.app import cli

    # -- enable / disable write exactly one status update ------------------
    for command, status in (("enable", "installed"), ("disable", "disabled")):
        client = _plugin_client()
        with patch("src.cli.plugins._get_plugin_client", return_value=client):
            result = runner.invoke(cli, ["plugin", command, "my-plugin"])
        assert result.exit_code == 0, result.output
        client.update_plugin.assert_awaited_once_with("my-plugin", status=status)
        assert f"'my-plugin' {command}d" in result.output

    # -- enable failure: nonzero, error text, no success rendering ---------
    client = _plugin_client(fail="update_plugin")
    with patch("src.cli.plugins._get_plugin_client", return_value=client):
        result = runner.invoke(cli, ["plugin", "enable", "my-plugin"])
    assert result.exit_code == 1
    assert "Enable failed" in result.output and "db unavailable" in result.output
    assert "enabled." not in result.output

    # -- update / reload on an unknown plugin: nonzero, nothing written ----
    for command, label in (("update", "Update failed"), ("reload", "Reload failed")):
        client = _plugin_client(plugin=None)
        with patch("src.cli.plugins._get_plugin_client", return_value=client):
            result = runner.invoke(cli, ["plugin", command, "ghost"])
        assert result.exit_code == 1, (command, result.output)
        assert label in result.output and "not found" in result.output
        client.update_plugin.assert_not_awaited()

    # -- remove: unknown plugin refuses; known plugin deletes data + row ---
    client = _plugin_client(plugin=None)
    with patch("src.cli.plugins._get_plugin_client", return_value=client):
        result = runner.invoke(cli, ["plugin", "remove", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output
    client.delete_plugin.assert_not_awaited()

    client = _plugin_client(
        plugin={"id": "my-plugin", "install_path": str(tmp_path / "gone")},
    )
    with patch("src.cli.plugins._get_plugin_client", return_value=client):
        result = runner.invoke(cli, ["plugin", "remove", "my-plugin", "--yes"])
    assert result.exit_code == 0, result.output
    client.delete_plugin_data_all.assert_awaited_once_with("my-plugin")
    client.delete_plugin.assert_awaited_once_with("my-plugin")
    assert "removed" in result.output

    # -- config: malformed KEY=VALUE stops before any write ----------------
    client = _plugin_client(plugin={"id": "my-plugin", "config": "{}"})
    with patch("src.cli.plugins._get_plugin_client", return_value=client):
        result = runner.invoke(cli, ["plugin", "config", "my-plugin", "not-an-assignment"])
    assert result.exit_code == 1
    assert "Invalid format" in result.output
    client.update_plugin.assert_not_awaited()

    # -- config set merges into the stored JSON ----------------------------
    client = _plugin_client(plugin={"id": "my-plugin", "config": '{"keep": "old"}'})
    with patch("src.cli.plugins._get_plugin_client", return_value=client):
        result = runner.invoke(cli, ["plugin", "config", "my-plugin", "level=high"])
    assert result.exit_code == 0, result.output
    client.update_plugin.assert_awaited_once_with(
        "my-plugin", config=json.dumps({"keep": "old", "level": "high"}),
    )

    # -- install: loader result is persisted verbatim ----------------------
    install_result = {
        "name": "fresh", "version": "1.2.3", "source_rev": "abc123",
        "install_path": str(tmp_path / "fresh"),
        "default_config": {"a": 1}, "permissions": ["files"],
    }
    client = _plugin_client()
    with patch("src.cli.plugins._get_plugin_client", return_value=client), \
         patch("src.plugins.loader.install_plugin_from_url",
               AsyncMock(return_value=install_result)):
        result = runner.invoke(
            cli, ["plugin", "install", "https://example.test/repo.git", "-b", "main"],
        )
    assert result.exit_code == 0, result.output
    client.create_plugin.assert_awaited_once_with(
        plugin_id="fresh", version="1.2.3",
        source_url="https://example.test/repo.git", source_rev="abc123",
        source_branch="main", install_path=install_result["install_path"],
        status="installed", config=json.dumps({"a": 1}),
        permissions=json.dumps(["files"]),
    )
    assert "Installed plugin 'fresh' v1.2.3" in result.output

    # -- install failure: nonzero, no DB row created -----------------------
    client = _plugin_client()
    with patch("src.cli.plugins._get_plugin_client", return_value=client), \
         patch("src.plugins.loader.install_plugin_from_url",
               AsyncMock(side_effect=ValueError("reserved plugin name"))):
        result = runner.invoke(cli, ["plugin", "install", "https://example.test/x.git"])
    assert result.exit_code == 1
    assert "Installation failed" in result.output and "reserved plugin name" in result.output
    client.create_plugin.assert_not_awaited()
