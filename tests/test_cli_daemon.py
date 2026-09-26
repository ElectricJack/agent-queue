"""Contract tests for daemon start failure reporting (api-cli plan 20).

``start_daemon`` launches external state (a subprocess, Docker, Postgres);
these tests prove the unsafe preconditions each refuse distinctly and that
no daemon subprocess is ever spawned when a precondition fails.  All probes
are patched — nothing here touches Docker or a real process.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import src.cli.daemon as daemon_mod
from src.cli.app import cli
from src.sessions.env import AQ_MARKER_KEYS, DAEMON_ENV_STRIP_KEYS, DB_ISOLATION_KEYS


@pytest.fixture(autouse=True)
def _pg_backend():
    """Daemon lifecycle probes are mocked; never allocate a real test database."""


@pytest.fixture(autouse=True)
def _no_dashboard_server(monkeypatch):
    """`aq start` also starts the dashboard server, whose PID file is the real
    ~/.agent-queue one; tests/test_cli_dashboard_server.py covers that half."""
    monkeypatch.setattr("src.cli.dashboard.ensure_dashboard_server", lambda: None)


@pytest.fixture(autouse=True)
def _private_daemon_home(tmp_path, monkeypatch):
    """`aq stop` records, and `aq start` clears, ``daemon.stopped`` in CONFIG_DIR;
    never let a test touch the operator's (their auto-restart service reads it)."""
    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _operator_environment(monkeypatch):
    """Lifecycle tests model an operator shell, never this worker's parent env."""
    for key in DAEMON_ENV_STRIP_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("start", ["--no-dashboard", "--no-dashboard-server"]),
        ("stop", ["--no-dashboard-server"]),
        ("restart", ["--no-dashboard", "--no-dashboard-server"]),
    ],
)
@pytest.mark.parametrize(
    "worker_environment",
    [
        {"AQ_SESSION_KIND": "pool"},
        {"AQ_SESSION_KIND": "task"},
        {"AQ_DB_SCOPE": "worker", "AQ_SESSION_ID": "worker-session"},
    ],
    ids=["pool", "task", "worker-db-scope"],
)
def test_worker_sessions_cannot_manage_the_daemon(
    runner, monkeypatch, command, arguments, worker_environment,
):
    """Every lifecycle command refuses before launching or stopping anything."""
    for key, value in worker_environment.items():
        monkeypatch.setenv(key, value)
    actions = {
        "start": MagicMock(return_value=True),
        "stop": MagicMock(return_value=True),
        "sessions": MagicMock(return_value=0),
        "after_start": MagicMock(),
    }
    monkeypatch.setattr(daemon_mod, "start_daemon", actions["start"])
    monkeypatch.setattr(daemon_mod, "stop_daemon", actions["stop"])
    monkeypatch.setattr(daemon_mod, "stop_agent_sessions", actions["sessions"])
    monkeypatch.setattr(daemon_mod, "_after_daemon_started", actions["after_start"])

    result = runner.invoke(cli, [command, *arguments])

    assert result.exit_code == 10, result.output
    assert "worker must never manage the operator's daemon" in result.output
    for action in actions.values():
        action.assert_not_called()


@pytest.mark.parametrize("polluted", [False, True], ids=["clean", "polluted-supervisor"])
def test_start_scrubs_session_environment_before_launch(
    runner, tmp_path, monkeypatch, polluted,
):
    """A non-worker parent cannot lend daemon children its AQ session state."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    for name, path in {
        "CONFIG_PATH": config_path,
        "CONFIG_DIR": tmp_path,
        "LOCK_DIR": tmp_path / "lock",
        "PID_FILE": tmp_path / "pid",
        "LOG_PATH": tmp_path / "log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(path))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_config_uses_postgres", lambda: False)
    monkeypatch.setattr(daemon_mod, "_resolve_agent_queue_bin", lambda: "agent-queue")
    monkeypatch.setattr("src.cli.client._resolve_api_url", lambda: "http://daemon.test")
    monkeypatch.setattr(daemon_mod.os, "kill", lambda *args: None)

    child_environment = {}

    def popen(*args, **kwargs):
        child_environment.update(kwargs["env"])
        return SimpleNamespace(pid=123)

    class HealthyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", popen)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: HealthyResponse())
    if polluted:
        for key in DAEMON_ENV_STRIP_KEYS:
            monkeypatch.setenv(key, "daemon" if key == "AQ_DB_SCOPE" else "polluted")
        monkeypatch.setenv("AQ_SESSION_KIND", "supervisor")

    result = runner.invoke(cli, ["start", "--no-dashboard", "--no-dashboard-server"])

    assert result.exit_code == 0, result.output
    assert all(key not in child_environment for key in DAEMON_ENV_STRIP_KEYS)
    assert ("AQ_DB_SCOPE" in result.output) is polluted


@pytest.mark.parametrize("status, expected", [(503, True), (500, False)])
def test_start_preserves_degraded_daemon_but_rejects_other_http_errors(
    tmp_path, monkeypatch, status, expected,
):
    import itertools
    import urllib.error
    import urllib.request
    from types import SimpleNamespace

    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    for name, path in {
        "CONFIG_PATH": config_path,
        "CONFIG_DIR": tmp_path,
        "LOCK_DIR": tmp_path / "lock",
        "PID_FILE": tmp_path / "pid",
        "LOG_PATH": tmp_path / "log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(path))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_config_uses_postgres", lambda: False)
    monkeypatch.setattr(daemon_mod, "_resolve_agent_queue_bin", lambda: "agent-queue")
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=123))
    signals = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda pid, sig: signals.append(sig))
    ticks = itertools.count(step=15)
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda _: None)

    def health_error(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost/health", status, "health", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", health_error)
    assert daemon_mod.start_daemon() is expected
    assert (tmp_path / "pid").exists() is expected
    assert any(sig != 0 for sig in signals) is (not expected)


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
    # The database probe is patched, not left to the box: on a machine that
    # happens to run PostgreSQL on 5432 the Docker branch is never reached.
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=None), \
         patch.object(daemon_mod, "_database_reachable", return_value=False), \
         patch.object(daemon_mod, "_find_compose_file", return_value="/x/docker-compose.yml"), \
         patch.object(daemon_mod, "_is_docker_running", return_value=False), \
         patch.object(daemon_mod, "_start_docker_desktop", return_value=False):
        result = runner.invoke(cli, ["start", "--no-dashboard"])
    assert result.exit_code == 1, result.output
    assert "Could not start Docker" in result.output

    # -- Docker up but compose fails to start the container ----------------
    inspect_missing = SimpleNamespace(
        returncode=1, stderr="Error: No such container: aq-postgres",
    )
    compose_fail = SimpleNamespace(returncode=1, stderr="no such service: postgres")
    with patch.object(daemon_mod, "_find_daemon_pid", return_value=None), \
         patch.object(daemon_mod, "_database_reachable", return_value=False), \
         patch.object(daemon_mod, "_is_docker_running", return_value=True), \
         patch.object(daemon_mod, "_is_container_running", return_value=False), \
         patch.object(daemon_mod, "_find_compose_file", return_value="/x/docker-compose.yml"), \
         patch.object(daemon_mod.subprocess, "run", side_effect=[inspect_missing, compose_fail]):
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


def test_stopped_postgres_container_starts_without_compose_up(monkeypatch):
    compose_file = "/x/docker-compose.yml"
    monkeypatch.setattr(daemon_mod, "_find_compose_file", lambda: compose_file)
    monkeypatch.setattr(daemon_mod, "_is_docker_running", lambda: True)
    monkeypatch.setattr(daemon_mod, "_is_container_running", lambda _: False)
    run = MagicMock(side_effect=[
        SimpleNamespace(returncode=0, stderr=""),  # existing container
        SimpleNamespace(returncode=0, stderr=""),  # docker start
        SimpleNamespace(returncode=0),  # pg_isready
    ])
    monkeypatch.setattr(daemon_mod.subprocess, "run", run)

    assert daemon_mod._ensure_docker_postgres() is True
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0] == ["docker", "container", "inspect", "aq-postgres"]
    assert commands[1] == ["docker", "start", "aq-postgres"]
    assert not any("up" in command for command in commands)


def test_missing_postgres_container_uses_compose_without_recreation(monkeypatch):
    compose_file = "/x/docker-compose.yml"
    monkeypatch.setattr(daemon_mod, "_find_compose_file", lambda: compose_file)
    monkeypatch.setattr(daemon_mod, "_is_docker_running", lambda: True)
    monkeypatch.setattr(daemon_mod, "_is_container_running", lambda _: False)
    run = MagicMock(side_effect=[
        SimpleNamespace(returncode=1, stderr="Error: No such container: aq-postgres"),
        SimpleNamespace(returncode=0, stderr=""),  # compose up
        SimpleNamespace(returncode=0),  # pg_isready
    ])
    monkeypatch.setattr(daemon_mod.subprocess, "run", run)

    assert daemon_mod._ensure_docker_postgres() is True
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[1] == [
        "docker", "compose", "-f", compose_file,
        "up", "-d", "--no-recreate", "postgres",
    ]


def test_postgres_inspection_error_never_runs_compose_up(monkeypatch):
    monkeypatch.setattr(daemon_mod, "_find_compose_file", lambda: "/x/docker-compose.yml")
    monkeypatch.setattr(daemon_mod, "_is_docker_running", lambda: True)
    monkeypatch.setattr(daemon_mod, "_is_container_running", lambda _: False)
    run = MagicMock(return_value=SimpleNamespace(returncode=1, stderr="permission denied"))
    monkeypatch.setattr(daemon_mod.subprocess, "run", run)

    assert daemon_mod._ensure_docker_postgres() is False
    run.assert_called_once()


def test_start_and_stop_ignore_dashboard_server_when_daemon_pid_file_is_missing(
    runner, tmp_path, monkeypatch,
):
    """A dashboard server has the config path in argv but is not the daemon."""
    import urllib.request

    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    for name, path in {
        "CONFIG_PATH": config_path,
        "CONFIG_DIR": tmp_path,
        "LOCK_DIR": tmp_path / "daemon.lock",
        "PID_FILE": tmp_path / "daemon.pid",
        "LOG_PATH": tmp_path / "daemon.log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(path))

    dashboard_pid = 7123
    dashboard_argv = (
        "/home/operator/dev/agent-queue2/.venv/bin/python3 -m "
        f"src.dashboard_server --config {config_path}"
    )

    def pgrep(command, **kwargs):
        assert command[:2] == ["pgrep", "-f"]
        if re.search(command[2], dashboard_argv):
            return MagicMock(returncode=0, stdout=f"{dashboard_pid}\n")
        return MagicMock(returncode=1, stdout="")

    proc = MagicMock(pid=12345)
    health = MagicMock()
    health.__enter__.return_value.status = 200
    kill = MagicMock()
    monkeypatch.setattr(daemon_mod.subprocess, "run", pgrep)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(daemon_mod, "_config_uses_postgres", lambda: False)
    monkeypatch.setattr(daemon_mod, "_resolve_agent_queue_bin", lambda: "agent-queue")
    monkeypatch.setattr(daemon_mod.os, "kill", kill)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: health)

    started = runner.invoke(cli, ["start", "--no-dashboard", "--no-dashboard-server"])

    assert started.exit_code == 0, started.output
    daemon_mod.subprocess.Popen.assert_called_once()

    # The started daemon is then killed and its PID file removed. The dashboard
    # server remains the only process matching the old broad pgrep pattern.
    (tmp_path / "daemon.pid").unlink()
    kill.reset_mock()
    stopped = runner.invoke(cli, ["stop", "--keep-sessions", "--no-dashboard-server"])

    assert stopped.exit_code == 0, stopped.output
    assert "not running" in stopped.output
    kill.assert_not_called()


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


def test_daemon_environment_removes_claude_and_codex_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "session-token")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setenv("CODEX_API_KEY", "codex-key")

    env = daemon_mod._daemon_environment()

    for key in (
        "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "ANTHROPIC_AUTH_TOKEN",
        "CODEX_SANDBOX", "CODEX_CI",
    ):
        assert key not in env
    assert env["ANTHROPIC_API_KEY"] == "api-key"
    assert env["CODEX_API_KEY"] == "codex-key"


def test_start_warns_when_called_from_an_enclosing_harness(runner, monkeypatch):
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda: True)
    monkeypatch.setattr(daemon_mod, "_maybe_prompt_dashboard", lambda _: None)

    result = runner.invoke(cli, ["start", "--no-dashboard"])

    assert result.exit_code == 0
    assert "detected enclosing harness marker" in result.output
    assert "CODEX_CI" in result.output


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


# -- the database `aq start` needs -------------------------------------------


def _config(tmp_path, monkeypatch, body: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))


def test_the_configured_database_endpoint_is_read_without_its_password(tmp_path, monkeypatch):
    _config(
        tmp_path,
        monkeypatch,
        "database:\n  url: postgresql+asyncpg://agent_queue:secret@db.internal:6543/agent_queue\n",
    )

    assert daemon_mod._configured_database_endpoint() == ("db.internal", 6543)


def test_an_unreadable_configuration_yields_no_endpoint(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "database: [not, a, mapping\n")

    assert daemon_mod._configured_database_endpoint() is None


def test_a_reachable_database_needs_no_docker_at_all(tmp_path, monkeypatch):
    """An installed or operator-run PostgreSQL is the normal case.

    `aq install --with postgres-managed` provisions a *native* server, and a
    release install has no compose file at all; demanding Docker there made
    `aq start` impossible on the very machine the installer had just prepared.
    """
    _config(tmp_path, monkeypatch, "database:\n  url: postgresql://localhost:5432/aq\n")
    monkeypatch.setattr(daemon_mod, "_database_reachable", lambda: True)

    def refuse():  # pragma: no cover - the assertion is that it is never called
        raise AssertionError("Docker must not be consulted for a reachable database")

    monkeypatch.setattr(daemon_mod, "_ensure_docker_postgres", refuse)
    monkeypatch.setattr(daemon_mod, "_find_compose_file", refuse)

    assert daemon_mod._ensure_database() is True


def test_an_unreachable_database_without_a_compose_file_names_the_installer(
    tmp_path, monkeypatch, capsys,
):
    _config(tmp_path, monkeypatch, "database:\n  url: postgresql://db.internal:6543/aq\n")
    monkeypatch.setattr(daemon_mod, "_database_reachable", lambda: False)
    monkeypatch.setattr(daemon_mod, "_find_compose_file", lambda: None)

    assert daemon_mod._ensure_database() is False
    output = capsys.readouterr().out
    assert "db.internal:6543" in output
    assert "aq install" in output


def test_an_unreachable_database_in_a_checkout_still_starts_the_dev_container(
    tmp_path, monkeypatch,
):
    _config(tmp_path, monkeypatch, "database:\n  url: postgresql://localhost:5432/aq\n")
    monkeypatch.setattr(daemon_mod, "_database_reachable", lambda: False)
    monkeypatch.setattr(daemon_mod, "_find_compose_file", lambda: "/x/docker-compose.yml")
    called: list[bool] = []
    monkeypatch.setattr(daemon_mod, "_ensure_docker_postgres", lambda: called.append(True) or True)

    assert daemon_mod._ensure_database() is True
    assert called == [True]


# ---------------------------------------------------------------------------
# Environment scrub: daemon children must never inherit any harness or worker
# marker, or a per-session DB sentinel leaked in.  Regression test for the
# 2026-09-21 incident.
# ---------------------------------------------------------------------------

def test_daemon_environment_strips_all_harness_and_db_markers(monkeypatch):
    """`_daemon_environment` returns an env free of every known leak vector.

    The 2026-09-21 incident: AQ_DB_SCOPE / AQ_DATABASE_URL / AGENT_QUEUE_DB
    entered the daemon env, making it refuse to migrate and report a
    different DB than the operator owns.
    """
    incident_keys = {
        "AQ_DB_SCOPE": "worker",
        "AQ_DATABASE_URL": "aq-worker-no-direct-db://",
        "AGENT_QUEUE_DB": "aq-worker-no-direct-db://",
    }
    # Every key the codebase knows about that a worker or harness session
    # could have set on the parent process.
    all_markers = set(AQ_MARKER_KEYS) | set(DB_ISOLATION_KEYS) | set(DAEMON_ENV_STRIP_KEYS)
    for key in all_markers:
        monkeypatch.setenv(key, "worker" if key in incident_keys else "polluted")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")

    env = daemon_mod._daemon_environment()

    # Every leak vector must be absent.
    for key in all_markers:
        assert key not in env, f"{key} leaked into daemon env"
        assert "CLAUDE_CODE_SESSION_ID" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env

    # Sanity: keys the daemon *needs* must survive.
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert "PATH" in env


# ---------------------------------------------------------------------------
# Backup path: `aq start` (and `aq restart`) backs up the database when the
# schema is behind the checkout's Alembic head, before launching the daemon.
# The dump must reach disk and pass a tail-integrity check before start
# proceeds.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "at_head", [True, False], ids=["at-head-no-backup", "not-at-head-backup"],
)
def test_start_backs_up_only_when_db_is_behind_code(
    runner, tmp_path, monkeypatch, at_head,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    for name, path in {
        "CONFIG_PATH": config_path,
        "CONFIG_DIR": tmp_path,
        "LOCK_DIR": tmp_path / "lock",
        "PID_FILE": tmp_path / "pid",
        "LOG_PATH": tmp_path / "log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(path))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_config_uses_postgres", lambda: True)
    monkeypatch.setattr(daemon_mod, "_ensure_database", lambda: True)
    monkeypatch.setattr(daemon_mod, "_database_is_at_head", lambda: at_head)
    monkeypatch.setattr(daemon_mod, "_resolve_agent_queue_bin", lambda: "agent-queue")
    monkeypatch.setattr("src.cli.client._resolve_api_url", lambda: "http://daemon.test")
    monkeypatch.setattr(daemon_mod.os, "kill", lambda *args: None)

    backup_calls: list[bool] = []
    monkeypatch.setattr(
        daemon_mod, "_backup_database", lambda: backup_calls.append(True),
    )
    # Hermetic: skip the /ready poll + doctor fix entirely.
    monkeypatch.setattr(daemon_mod, "_post_daemon_checks", lambda: None)

    class HealthResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: HealthResp())

    def popen(*a, **k):
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", popen)

    result = runner.invoke(cli, ["start", "--no-dashboard", "--no-dashboard-server"])
    assert result.exit_code == 0, result.output
    expected: list[bool] = [] if at_head else [True]
    assert backup_calls == expected, f"expected {expected!r}, got {backup_calls!r}"


def test_backup_database_dumps_and_passes_integrity(tmp_path, monkeypatch):
    """A pg_dump that lands on disk with the marker survives the integrity check.

    The docker steps are stubbed by writing the dump file ourselves where
    ``docker cp`` would have placed it.
    """
    monkeypatch.setattr(daemon_mod, "BACKUPS_DIR", str(tmp_path))

    docker_calls: list[list[str]] = []

    # Simulate `docker cp` by writing the fake dump to the destination path.
    def stub_run(cmd, **kw):
        docker_calls.append(list(cmd))
        args = list(cmd)
        # "docker cp aq-postgres:/tmp/pre-deploy-<ts>.sql <out>" — copy step
        if "cp" in args:
            with open(args[-1], "w") as f:
                f.write("-- pg_dump tail\nunrestrict;\n")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(daemon_mod.subprocess, "run", stub_run)

    daemon_mod._backup_database()

    # The docker steps ran in order: pg_dump, cp, rm.
    kinds = [c[1] for c in docker_calls]
    assert kinds == ["exec", "cp", "exec"], f"wrong docker step order: {kinds}"

    # The dump file landed on disk and carries the marker.
    dumps = list(tmp_path.glob("pre-deploy-*.sql"))
    assert dumps, "no dump file was written"
    assert "unrestrict" in dumps[0].read_text().lower()


def test_backup_database_raises_on_missing_integrity_marker(tmp_path, monkeypatch):
    """A dump without the marker comment must abort with SystemExit(15)."""
    monkeypatch.setattr(daemon_mod, "BACKUPS_DIR", str(tmp_path))

    def stub_run(cmd, **kw):
        args = list(cmd)
        if "cp" in args:
            with open(args[-1], "w") as f:
                f.write("-- incomplete pg_dump, no marker\n")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(daemon_mod.subprocess, "run", stub_run)

    with pytest.raises(SystemExit, match="15"):
        daemon_mod._backup_database()


def test_backup_database_raises_on_docker_failure(tmp_path, monkeypatch):
    """A failed docker step must abort with SystemExit(13)."""
    monkeypatch.setattr(daemon_mod, "BACKUPS_DIR", str(tmp_path))

    import subprocess as _sp
    def failing_run(cmd, **kw):
        raise _sp.CalledProcessError(1, list(cmd))

    monkeypatch.setattr(daemon_mod.subprocess, "run", failing_run)

    with pytest.raises(SystemExit, match="13"):
        daemon_mod._backup_database()


def test_start_holds_its_lock_while_it_waits_for_the_database(tmp_path, monkeypatch, no_popen):
    """The auto-restart watchdog reads daemon.lock as "a start is in progress";
    the database wait and the pre-migration backup are part of that start."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("database:\n  url: postgresql://localhost/aq\n")
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(tmp_path / "daemon.lock"))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(tmp_path / "daemon.pid"))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    held = []

    def ensure_database():
        held.append((tmp_path / "daemon.lock").is_dir())
        return False

    monkeypatch.setattr(daemon_mod, "_ensure_database", ensure_database)

    assert daemon_mod.start_daemon() is False
    assert held == [True]
    assert not (tmp_path / "daemon.lock").exists()


def test_start_rechecks_for_a_daemon_once_it_holds_the_lock(tmp_path, monkeypatch, no_popen):
    """A start that waited behind another one must not launch a second daemon."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(tmp_path / "daemon.lock"))
    answers = iter([None, 4242])
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: next(answers))

    assert daemon_mod.start_daemon() is True
    no_popen.assert_not_called()


def test_start_withdraws_a_deliberate_stop_and_stop_records_one(tmp_path, monkeypatch):
    from src.daemon_state import read_stop_intent

    marker = tmp_path / "daemon.stopped"
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    daemon_mod.stop_daemon(quiet=True)
    assert read_stop_intent(path=marker).by == "aq stop"

    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(tmp_path / "missing.yaml"))
    assert daemon_mod.start_daemon() is False  # no config -- but the stop is withdrawn
    assert read_stop_intent(path=marker) is None


# ---------------------------------------------------------------------------
# A deliberate stop wins over a start that is already running
# ---------------------------------------------------------------------------


@pytest.fixture
def startable(tmp_path, monkeypatch, no_popen):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("database:\n  url: postgresql://localhost/aq\n")
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(tmp_path / "daemon.lock"))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(tmp_path / "daemon.pid"))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_database_is_at_head", lambda: True)
    return tmp_path


def test_the_watchdogs_start_respects_a_recorded_stop(startable, runner, no_popen):
    from src.daemon_state import STOPPED_EXIT_CODE, record_stop_intent

    record_stop_intent("aq stop", path=startable / "daemon.stopped")
    result = runner.invoke(cli, ["start", "--unless-stopped", "--no-dashboard-server"])

    assert result.exit_code == STOPPED_EXIT_CODE, result.output
    assert "stopped on purpose by aq stop" in result.output
    assert (startable / "daemon.stopped").exists()  # respected, not withdrawn
    no_popen.assert_not_called()


def test_a_stop_during_the_database_wait_stops_the_start_before_it_spawns(
    startable, monkeypatch, no_popen
):
    from src.daemon_state import record_stop_intent

    def database_comes_up_as_the_operator_stops():
        record_stop_intent("aq stop", path=startable / "daemon.stopped")
        return True

    monkeypatch.setattr(daemon_mod, "_ensure_database", database_comes_up_as_the_operator_stops)

    with pytest.raises(daemon_mod.StartHeld) as held:
        daemon_mod.start_daemon()
    assert held.value.during
    no_popen.assert_not_called()
    assert not (startable / "daemon.lock").exists()


def test_stop_waits_for_a_start_in_progress_and_stops_what_it_spawned(tmp_path, monkeypatch):
    from src.daemon_state import acquire_start_lock

    lock = tmp_path / "daemon.lock"
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(lock))
    monkeypatch.setattr(daemon_mod, "PID_FILE", str(tmp_path / "daemon.pid"))
    assert acquire_start_lock(str(lock))
    answers = iter([None, None, 4242])  # the start spawns while the stop waits
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: next(answers, 4242))
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda _: None)
    signals = []

    def kill(pid, sig):
        if pid != 4242:
            return  # the lock owner (this test) is alive
        signals.append(sig)
        if sig == 0:
            raise ProcessLookupError  # gone after the SIGTERM

    monkeypatch.setattr(daemon_mod.os, "kill", kill)

    assert daemon_mod.stop_daemon(quiet=True) is True
    assert signals[0] == daemon_mod.signal.SIGTERM


def _dead_pid() -> int:
    import os

    for pid in range(4_194_000, 4_000_000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            continue
    raise AssertionError("no free pid")


def test_an_abandoned_start_lock_does_not_block_the_next_start(startable, monkeypatch):
    lock = startable / "daemon.lock"
    lock.mkdir()
    (lock / "owner").write_text(str(_dead_pid()))
    reached = []
    monkeypatch.setattr(daemon_mod, "_ensure_database", lambda: reached.append(1) or False)

    assert daemon_mod.start_daemon() is False  # stops at the (patched) database
    assert reached == [1]
    assert not lock.exists()
