"""Contract tests for daemon start failure reporting (api-cli plan 20).

``start_daemon`` launches external state (a subprocess, Docker, Postgres);
these tests prove the unsafe preconditions each refuse distinctly and that
no daemon subprocess is ever spawned when a precondition fails.  All probes
are patched — nothing here touches Docker or a real process.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import src.cli.daemon as daemon_mod


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def no_popen(monkeypatch):
    """Fail loudly if any code path tries to spawn the daemon process."""
    popen = MagicMock(side_effect=AssertionError("subprocess must not be spawned"))
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", popen)
    return popen


def test_daemon_start_reports_docker_or_subprocess_failure_without_claiming_success(
    runner, tmp_path, monkeypatch, no_popen,
):
    """Plan 20: each unsafe precondition refuses distinctly, spawns nothing."""
    from src.cli.app import cli

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(tmp_path / "daemon.lock"))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(tmp_path / "daemon.pid"))
    monkeypatch.setattr(daemon_mod, "LOG_PATH", str(tmp_path / "daemon.log"))

    # -- Missing config: refuse before anything else -----------------------
    result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 1, result.output
    assert "Config not found" in result.output
    assert "started" not in result.output.lower()

    config_path.write_text("database:\n  url: postgresql://localhost/aq\n")

    # -- Already running: succeed idempotently without a second spawn ------
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=4242):
        result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    assert "4242" in result.output

    # -- Postgres configured, Docker down and unstartable ------------------
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=None), \
         patch.object(daemon_mod, "_is_docker_running", return_value=False), \
         patch.object(daemon_mod, "_start_docker_desktop", return_value=False):
        result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 1, result.output
    assert "Could not start Docker" in result.output

    # -- Docker up but compose fails to start the container ----------------
    compose_fail = MagicMock(returncode=1, stderr="no such service: postgres")
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=None), \
         patch.object(daemon_mod, "_is_docker_running", return_value=True), \
         patch.object(daemon_mod, "_is_container_running", return_value=False), \
         patch.object(daemon_mod, "_find_compose_file", return_value="/x/docker-compose.yml"), \
         patch.object(daemon_mod.subprocess, "run", return_value=compose_fail):
        result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 1, result.output
    assert "Failed to start PostgreSQL container" in result.output
    assert "no such service" in result.output

    # -- A concurrent start holds the lock ---------------------------------
    (tmp_path / "daemon.lock").mkdir()
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=None), \
         patch.object(daemon_mod, "_config_uses_postgres", return_value=False):
        result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 1, result.output
    assert "Another start is in progress" in result.output

    # No branch above may have attempted to spawn the daemon.
    no_popen.assert_not_called()
