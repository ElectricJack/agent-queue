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
from src.cli.app import cli


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


def test_daemon_environment_appends_installed_user_executable_dirs(
    tmp_path, monkeypatch,
):
    """Non-login launches can still find user-installed MCP executables."""
    local_bin = tmp_path / ".local" / "bin"
    pnpm_bin = tmp_path / ".local" / "share" / "pnpm"
    local_bin.mkdir(parents=True)
    pnpm_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/venv/bin:/usr/bin")
    monkeypatch.setenv("CLAUDECODE", "1")

    env = daemon_mod._daemon_environment(home=str(tmp_path))

    assert env["PATH"].split(daemon_mod.os.pathsep) == [
        "/venv/bin",
        "/usr/bin",
        str(local_bin),
        str(pnpm_bin),
    ]
    assert "CLAUDECODE" not in env


def test_daemon_environment_does_not_duplicate_existing_path(tmp_path, monkeypatch):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"/usr/bin{daemon_mod.os.pathsep}{local_bin}")

    env = daemon_mod._daemon_environment(home=str(tmp_path))

    assert env["PATH"].split(daemon_mod.os.pathsep).count(str(local_bin)) == 1


def test_resolve_daemon_prefers_current_venv_entry_point(tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    local_daemon = venv_bin / "agent-queue"
    local_daemon.write_text("local")
    monkeypatch.setattr(daemon_mod.sys, "executable", str(venv_bin / "python"))

    with patch("shutil.which", return_value="/home/user/.local/bin/agent-queue"):
        resolved = daemon_mod._resolve_agent_queue_bin()

    assert resolved == str(local_daemon)


def test_resolve_daemon_falls_back_to_path_without_venv_entry_point(
    tmp_path, monkeypatch,
):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(daemon_mod.sys, "executable", str(venv_bin / "python"))

    with patch("shutil.which", return_value="/usr/local/bin/agent-queue"):
        resolved = daemon_mod._resolve_agent_queue_bin()

    assert resolved == "/usr/local/bin/agent-queue"
