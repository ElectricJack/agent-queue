"""Contract tests for daemon start failure reporting (api-cli plan 20).

``start_daemon`` launches external state (a subprocess, Docker, Postgres);
these tests prove the unsafe preconditions each refuse distinctly and that
no daemon subprocess is ever spawned when a precondition fails.  All probes
are patched — nothing here touches Docker or a real process.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import src.cli.daemon as daemon_mod
from src.cli.app import cli
from src.sessions.env import AQ_MARKER_KEYS, DAEMON_ENV_STRIP_KEYS, DB_ISOLATION_KEYS


def test_post_start_doctor_uses_cli_instead_of_daemon_entrypoint(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value.status = 200
    run = MagicMock(return_value=SimpleNamespace(stdout="", stderr="", returncode=0))
    monkeypatch.setattr(daemon_mod.subprocess, "run", run)
    monkeypatch.setattr(
        daemon_mod, "_resolve_agent_queue_bin",
        lambda: pytest.fail("daemon entrypoint treats doctor as a config path"),
    )
    with patch("urllib.request.urlopen", return_value=response):
        daemon_mod._post_daemon_checks()
    command = run.call_args.args[0]
    assert command[:4] == [daemon_mod.sys.executable, "-m", "src.cli.app", "doctor"]
    assert command[4:] == ["--check", "pools.stale_worktree_checkouts", "--fix"]


def test_log_rotation_preserves_bytes_and_retains_only_its_newest_archives(tmp_path, monkeypatch):
    log = tmp_path / "custom.log"
    content = b"subprocess output\xff\n" * 10
    log.write_bytes(content)
    monkeypatch.setattr(daemon_mod, "MAX_LOG_SIZE_BYTES", 1)
    monkeypatch.setattr(daemon_mod.time, "strftime", lambda _: "20261004120000")
    for timestamp in ("20261001120000", "20261002120000", "20261003120000"):
        (tmp_path / f"custom.log.{timestamp}.gz").write_bytes(b"old archive")
    unrelated = tmp_path / "other.log.20261001120000.gz"
    unrelated.write_bytes(b"other log")
    malformed = tmp_path / "custom.log.backup.gz"
    malformed.write_bytes(b"manual backup")

    daemon_mod._rotate_if_needed(str(log))

    assert log.read_bytes() == b""
    with gzip.open(tmp_path / "custom.log.20261004120000.gz", "rb") as archive:
        assert archive.read() == content
    assert not (tmp_path / "custom.log.20261001120000.gz").exists()
    assert (tmp_path / "custom.log.20261002120000.gz").exists()
    assert (tmp_path / "custom.log.20261003120000.gz").exists()
    assert unrelated.read_bytes() == b"other log"
    assert malformed.read_bytes() == b"manual backup"


@pytest.mark.parametrize("exists", [False, True])
def test_small_or_missing_log_is_not_rotated(tmp_path, monkeypatch, exists):
    log = tmp_path / "daemon.log"
    monkeypatch.setattr(daemon_mod, "MAX_LOG_SIZE_BYTES", 4)
    if exists:
        log.write_bytes(b"four")
    daemon_mod._rotate_if_needed(str(log))
    assert list(tmp_path.glob("*.gz")) == []
    assert log.exists() is exists
    if exists:
        assert log.read_bytes() == b"four"


def test_rotation_failure_preserves_active_log_and_existing_archive(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_bytes(b"active output")
    backup = tmp_path / "daemon.log.20261004120000.gz"
    backup.write_bytes(b"existing archive")
    monkeypatch.setattr(daemon_mod, "MAX_LOG_SIZE_BYTES", 1)
    monkeypatch.setattr(daemon_mod.time, "strftime", lambda _: "20261004120000")
    daemon_mod._rotate_if_needed(str(log))
    assert log.read_bytes() == b"active output"
    assert backup.read_bytes() == b"existing archive"


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
    never let a test touch the operator's (their auto-restart service reads it),
    nor wait on the operator's start lock."""
    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "LOCK_DIR", str(tmp_path / "daemon.lock"))


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
        return SimpleNamespace(pid=123, poll=lambda: None)

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
    proc = MagicMock(pid=123)
    proc.poll.return_value = None
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *a, **kw: proc)
    signals = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda pid, sig: signals.append(sig))

    def terminate():
        signals.append(daemon_mod.signal.SIGTERM)
        proc.poll.return_value = -15

    proc.terminate.side_effect = terminate
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
def startup_case(tmp_path, monkeypatch):
    """A launched child and monotonic clock, with every external probe stubbed."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    for name, path in {
        "CONFIG_PATH": config_path,
        "PID_FILE": tmp_path / "daemon.pid",
        "LOG_PATH": tmp_path / "daemon.log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(path))
    monkeypatch.setattr(daemon_mod, "_find_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "_config_uses_postgres", lambda: False)
    monkeypatch.setattr(daemon_mod, "_resolve_agent_queue_bin", lambda: "agent-queue")
    monkeypatch.setattr("src.cli.client._resolve_api_url", lambda: "http://daemon.test")
    proc = MagicMock(pid=123)
    proc.poll.return_value = None
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *a, **kw: proc)
    raw_kill = MagicMock(side_effect=AssertionError("startup must use its child handle"))
    monkeypatch.setattr(daemon_mod.os, "kill", raw_kill)
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(
        daemon_mod, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    post_start = MagicMock()
    monkeypatch.setattr(daemon_mod, "_post_daemon_checks", post_start)
    return SimpleNamespace(
        proc=proc, clock=clock, post_start=post_start, raw_kill=raw_kill,
        pid_file=tmp_path / "daemon.pid",
    )


def _health_response(status=200):
    response = MagicMock()
    response.__enter__.return_value.status = status
    return response


@pytest.mark.parametrize("status", [200, 503])
def test_start_keeps_child_that_becomes_healthy_during_timeout_diagnostics(
    startup_case, monkeypatch, status,
):
    import urllib.error

    diagnostics_done = False

    def tail(lines):
        nonlocal diagnostics_done
        assert lines == 30
        assert startup_case.clock.now == daemon_mod.DAEMON_START_TIMEOUT_SECONDS
        startup_case.clock.now += 10  # initialization finishes during formatting
        diagnostics_done = True

    def health(url, *, timeout):
        assert url == "http://daemon.test/health"
        assert 0 < timeout <= 1
        if not diagnostics_done:
            raise urllib.error.URLError("not listening yet")
        if status == 503:
            raise urllib.error.HTTPError(url, status, "degraded", {}, None)
        return _health_response(status)

    monkeypatch.setattr(daemon_mod, "_tail_log", tail)
    monkeypatch.setattr("urllib.request.urlopen", health)

    assert daemon_mod.start_daemon() is True
    assert diagnostics_done
    assert startup_case.pid_file.read_text() == "123"
    startup_case.proc.terminate.assert_not_called()
    startup_case.proc.kill.assert_not_called()
    startup_case.raw_kill.assert_not_called()
    startup_case.post_start.assert_called_once_with()
    assert not (startup_case.pid_file.parent / "daemon.lock").exists()


def test_start_allows_slow_initialization_within_bounded_budget(startup_case, monkeypatch):
    import urllib.error

    def health(url, *, timeout):
        if startup_case.clock.now < 40:
            raise urllib.error.URLError("still initializing")
        return _health_response()

    monkeypatch.setattr("urllib.request.urlopen", health)
    tail = MagicMock()
    monkeypatch.setattr(daemon_mod, "_tail_log", tail)

    assert daemon_mod.start_daemon() is True
    assert 30 < startup_case.clock.now < daemon_mod.DAEMON_START_TIMEOUT_SECONDS
    tail.assert_not_called()
    startup_case.proc.terminate.assert_not_called()


@pytest.mark.parametrize("force_kill", [False, True])
def test_start_stops_truly_unhealthy_child_after_final_probe(
    startup_case, monkeypatch, force_kill,
):
    import urllib.error

    monkeypatch.setattr(daemon_mod, "DAEMON_START_TIMEOUT_SECONDS", 1.25)
    events = []
    probe_timeouts = []

    def health(url, *, timeout):
        events.append("health")
        probe_timeouts.append(timeout)
        raise urllib.error.URLError("not listening")

    monkeypatch.setattr("urllib.request.urlopen", health)
    monkeypatch.setattr(daemon_mod, "_tail_log", lambda _: events.append("diagnostics"))
    startup_case.proc.terminate.side_effect = lambda: events.append("terminate")
    startup_case.proc.kill.side_effect = lambda: events.append("kill")
    waits = []

    def wait(*, timeout):
        waits.append(timeout)
        if force_kill and len(waits) == 1:
            raise daemon_mod.subprocess.TimeoutExpired("agent-queue", timeout)
        startup_case.proc.poll.return_value = -9 if force_kill else -15
        return startup_case.proc.poll.return_value

    startup_case.proc.wait.side_effect = wait

    assert daemon_mod.start_daemon() is False
    assert events[events.index("diagnostics"):][:3] == ["diagnostics", "health", "terminate"]
    assert startup_case.clock.now == 1.25
    assert startup_case.clock.sleeps[-1] == 0.25
    assert probe_timeouts == [1, 0.75, 0.25, 1]
    assert waits == ([5, 5] if force_kill else [5])
    assert startup_case.proc.kill.call_count == int(force_kill)
    assert not startup_case.pid_file.exists()
    startup_case.raw_kill.assert_not_called()
    startup_case.post_start.assert_not_called()


@pytest.mark.parametrize("replacement_pid", [None, "456"])
def test_start_detects_exited_child_without_signaling_or_removing_successor(
    startup_case, monkeypatch, replacement_pid,
):
    startup_case.proc.poll.return_value = 42

    def tail(_):
        if replacement_pid:
            startup_case.pid_file.write_text(replacement_pid)

    monkeypatch.setattr(daemon_mod, "_tail_log", tail)
    probe = MagicMock(side_effect=AssertionError("exited child cannot become healthy"))
    monkeypatch.setattr("urllib.request.urlopen", probe)

    assert daemon_mod.start_daemon() is False
    assert startup_case.clock.now == 0
    if replacement_pid:
        assert startup_case.pid_file.read_text() == replacement_pid
    else:
        assert not startup_case.pid_file.exists()
    probe.assert_not_called()
    startup_case.raw_kill.assert_not_called()
    startup_case.proc.terminate.assert_not_called()
    startup_case.proc.kill.assert_not_called()


def test_start_does_not_accept_health_from_another_listener_after_child_exits(
    startup_case, monkeypatch,
):
    def health(url, *, timeout):
        startup_case.proc.poll.return_value = 42
        startup_case.pid_file.write_text("456")
        return _health_response()

    monkeypatch.setattr("urllib.request.urlopen", health)
    monkeypatch.setattr(daemon_mod, "_tail_log", lambda _: None)

    assert daemon_mod.start_daemon() is False
    assert startup_case.pid_file.read_text() == "456"
    startup_case.proc.terminate.assert_not_called()
    startup_case.proc.kill.assert_not_called()
    startup_case.post_start.assert_not_called()


def test_start_timeout_targets_launched_child_and_preserves_replacement_pid_file(
    startup_case, monkeypatch,
):
    import urllib.error

    monkeypatch.setattr(daemon_mod, "DAEMON_START_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        "urllib.request.urlopen", MagicMock(side_effect=urllib.error.URLError("not listening")),
    )
    monkeypatch.setattr(daemon_mod, "_tail_log", lambda _: startup_case.pid_file.write_text("456"))

    def wait(*, timeout):
        startup_case.proc.poll.return_value = -15
        return -15

    startup_case.proc.wait.side_effect = wait

    assert daemon_mod.start_daemon() is False
    startup_case.proc.terminate.assert_called_once_with()
    startup_case.proc.kill.assert_not_called()
    startup_case.raw_kill.assert_not_called()
    assert startup_case.pid_file.read_text() == "456"


def test_start_does_not_force_kill_a_child_that_exited_at_grace_deadline(
    startup_case, monkeypatch,
):
    import urllib.error

    monkeypatch.setattr(daemon_mod, "DAEMON_START_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        "urllib.request.urlopen", MagicMock(side_effect=urllib.error.URLError("not listening")),
    )
    monkeypatch.setattr(daemon_mod, "_tail_log", lambda _: None)

    def wait(*, timeout):
        startup_case.proc.poll.return_value = 0
        if startup_case.proc.wait.call_count == 1:
            raise daemon_mod.subprocess.TimeoutExpired("agent-queue", timeout)
        return 0

    startup_case.proc.wait.side_effect = wait

    assert daemon_mod.start_daemon() is False
    startup_case.proc.terminate.assert_called_once_with()
    startup_case.proc.kill.assert_not_called()
    assert not startup_case.pid_file.exists()


def test_tail_log_reads_bounded_suffix_of_large_log(tmp_path, monkeypatch):
    import builtins

    log = tmp_path / "large.log"
    with log.open("wb") as handle:
        handle.seek(16 * 1024 * 1024)  # sparse file; do not allocate a giant test buffer
        handle.write(b"\n")
        for index in range(100):
            handle.write(f"[red]literal log {index}[/]\n".encode())
    monkeypatch.setattr(daemon_mod, "LOG_PATH", str(log))
    reads = []

    class BoundedReader:
        def __enter__(self):
            self.handle = builtins.open(log, "rb")
            return self

        def __exit__(self, *args):
            self.handle.close()

        def seek(self, offset, whence=0):
            return self.handle.seek(offset, whence)

        def read(self, size=-1):
            assert 0 < size <= daemon_mod.LOG_TAIL_MAX_BYTES
            data = self.handle.read(size)
            reads.append(len(data))
            return data

        def readlines(self):
            raise AssertionError("must never scan the whole log")

    monkeypatch.setattr(daemon_mod, "open", lambda *a, **kw: BoundedReader(), raising=False)
    output = MagicMock()
    monkeypatch.setattr(daemon_mod.console, "print", output)

    daemon_mod._tail_log(30)

    assert sum(reads) == daemon_mod.LOG_TAIL_MAX_BYTES
    assert [call.args[0] for call in output.call_args_list] == [
        f"[red]literal log {index}[/]" for index in range(70, 100)
    ]
    assert all(call.kwargs == {"markup": False, "highlight": False} for call in output.call_args_list)


def test_tail_log_bounds_huge_line_and_replaces_split_utf8(tmp_path, monkeypatch):
    log = tmp_path / "huge-line.log"
    log.write_bytes(b"\xff" + "\u20ac".encode() * daemon_mod.LOG_TAIL_MAX_BYTES + b" end")
    monkeypatch.setattr(daemon_mod, "LOG_PATH", str(log))
    output = MagicMock()
    monkeypatch.setattr(daemon_mod.console, "print", output)

    daemon_mod._tail_log(30)

    output.assert_called_once()
    line = output.call_args.args[0]
    assert len(line) <= daemon_mod.LOG_TAIL_MAX_BYTES
    assert line.startswith("\ufffd")
    assert line.endswith(" end")


@pytest.mark.parametrize(
    "contents, budget, expected",
    [
        (b"first\nlast\n", 64 * 1024, ["first", "last"]),
        (b"older line\npartial line\ntwo\nthree\n", 16, ["two", "three"]),
        (b"old\nhello\nlast\n", 12, ["hello", "last"]),
    ],
    ids=["short-log", "clipped-first-line", "complete-line-boundary"],
)
def test_tail_log_preserves_complete_recent_lines(tmp_path, monkeypatch, contents, budget, expected):
    log = tmp_path / "daemon.log"
    log.write_bytes(contents)
    monkeypatch.setattr(daemon_mod, "LOG_PATH", str(log))
    monkeypatch.setattr(daemon_mod, "LOG_TAIL_MAX_BYTES", budget)
    output = MagicMock()
    monkeypatch.setattr(daemon_mod.console, "print", output)

    daemon_mod._tail_log(30)

    assert [call.args[0] for call in output.call_args_list] == expected


@pytest.mark.parametrize("lines", [0, -1])
def test_tail_log_nonpositive_line_count_does_no_io(monkeypatch, lines):
    read = MagicMock(side_effect=AssertionError("no log read requested"))
    monkeypatch.setattr(daemon_mod, "open", read, raising=False)
    daemon_mod._tail_log(lines)
    read.assert_not_called()


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
    proc.poll.return_value = None
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
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", popen)

    result = runner.invoke(cli, ["start", "--no-dashboard", "--no-dashboard-server"])
    assert result.exit_code == 0, result.output
    expected: list[bool] = [] if at_head else [True]
    assert backup_calls == expected, f"expected {expected!r}, got {backup_calls!r}"


@pytest.fixture
def predeploy_backup(tmp_path, monkeypatch):
    """Real archive orchestration, with only the client process boundary stubbed."""
    import src.database.backup as backups

    monkeypatch.setattr(daemon_mod, "BACKUPS_DIR", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(
        "src.config.load_config",
        lambda _: SimpleNamespace(
            database=SimpleNamespace(url="postgresql://custom:secret@localhost:6543/custom_db")
        ),
    )
    restore = tmp_path / "pg_restore"
    restore.touch()
    (tmp_path / "psql").touch()
    state = SimpleNamespace(archive=b"PGDMPmock archive", error=None, calls=[], docker=False)
    monkeypatch.setattr(
        "src.install.update.find_pg_dump",
        lambda: None if state.docker else str(tmp_path / "pg_dump"),
    )
    monkeypatch.setattr(backups.shutil, "which", lambda _: "/usr/bin/docker")

    async def execute(argv, **kwargs):
        state.calls.append((argv, kwargs))
        if argv[:2] == ["docker", "ps"]:
            return b"serving-container\n"
        if argv[:2] == ["docker", "inspect"]:
            return b'{"5432/tcp":[{"HostPort":"6543"}]}'
        if "--version" in argv:
            return b"pg_dump (PostgreSQL) 18.0\n"
        if any(Path(arg).name == "pg_dump" for arg in argv):
            kwargs["destination"].write_bytes(state.archive)
            if state.error:
                raise backups.BackupError(state.error)
        if "--list" in argv:
            return b"; Archive created at 2026-10-06 00:00:00 UTC\n"
        return b""

    monkeypatch.setattr(backups, "_run", execute)
    return state


@pytest.mark.parametrize("docker", [False, True])
def test_backup_database_dumps_and_passes_integrity(tmp_path, predeploy_backup, docker):
    """Local and Docker clients both produce private, inspected custom archives."""
    predeploy_backup.docker = docker
    daemon_mod._backup_database()
    dump, = tmp_path.glob("pre-deploy-*.dump")
    assert dump.read_bytes() == b"PGDMPmock archive"
    assert dump.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.sql"))
    argv, kwargs = next(
        (argv, kwargs) for argv, kwargs in predeploy_backup.calls
        if any(Path(arg).name == "pg_dump" for arg in argv) and "--version" not in argv
    )
    assert "--format=custom" in argv
    assert "secret" not in " ".join(argv)
    assert kwargs["env"]["PGPASSWORD"] == "secret"
    if docker:
        assert "serving-container" in argv and "custom_db" in argv
    else:
        assert "postgresql://custom@localhost:6543/custom_db" in argv
    assert any("--list" in argv for argv, _ in predeploy_backup.calls)


@pytest.mark.parametrize("docker", [False, True])
@pytest.mark.parametrize("archive", [b"", b"incomplete dump"])
def test_backup_database_raises_on_missing_integrity_marker(tmp_path, predeploy_backup, docker, archive):
    predeploy_backup.docker = docker
    predeploy_backup.archive = archive
    with pytest.raises(SystemExit, match="15"):
        daemon_mod._backup_database()
    assert not list(tmp_path.glob("pre-deploy-*.dump"))


@pytest.mark.parametrize("docker", [False, True])
def test_backup_database_raises_on_client_failure(tmp_path, predeploy_backup, docker, capsys):
    predeploy_backup.docker = docker
    predeploy_backup.error = "connection refused"
    with pytest.raises(SystemExit, match="13"):
        daemon_mod._backup_database()
    assert "connection refused" in capsys.readouterr().out
    assert not list(tmp_path.glob("pre-deploy-*.dump"))


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

    # The operator's own start reports it like any start that did not happen...
    assert daemon_mod.start_daemon() is False
    assert not (startable / "daemon.lock").exists()
    # ...and the watchdog's is told it was held, so it is not counted a failure.
    (startable / "daemon.stopped").unlink()
    with pytest.raises(daemon_mod.StartHeld) as held:
        daemon_mod.start_daemon(unless_stopped=True)
    assert held.value.during
    no_popen.assert_not_called()
    assert not (startable / "daemon.lock").exists()


def test_the_watchdogs_start_never_spawns_during_an_update(startable, monkeypatch, no_popen):
    """`aq update` saw the daemon down, so it would neither stop nor restart it."""
    (startable / "update.lock").write_text("")
    with pytest.raises(daemon_mod.StartHeld) as held:
        daemon_mod.start_daemon(unless_stopped=True)
    assert "aq update" in str(held.value)

    def update_begins_during_the_database_wait():
        (startable / "update.lock").write_text("")
        return True

    (startable / "update.lock").unlink()
    monkeypatch.setattr(daemon_mod, "_ensure_database", update_begins_during_the_database_wait)
    with pytest.raises(daemon_mod.StartHeld):
        daemon_mod.start_daemon(unless_stopped=True)
    no_popen.assert_not_called()


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


def test_a_lock_taken_before_a_reboot_or_by_another_user_is_abandoned(tmp_path, monkeypatch):
    import json

    import src.daemon_state as state_mod
    from src.daemon_state import LOCK_ABANDONED, LOCK_HELD, start_lock_state

    lock = tmp_path / "daemon.lock"
    lock.mkdir()
    monkeypatch.setattr(state_mod, "boot_identity", lambda: "boot-now")
    (lock / "owner").write_text(json.dumps({"pid": __import__("os").getpid(), "boot": "boot-now"}))
    assert start_lock_state(str(lock)) == LOCK_HELD
    (lock / "owner").write_text(json.dumps({"pid": __import__("os").getpid(), "boot": "boot-before"}))
    assert start_lock_state(str(lock)) == LOCK_ABANDONED  # PID reused after a reboot
    if __import__("os").getuid() != 0:
        (lock / "owner").write_text(json.dumps({"pid": 1, "boot": "boot-now"}))
        assert start_lock_state(str(lock)) == LOCK_ABANDONED  # not a process of ours


def test_a_lock_is_only_released_by_the_owner_that_was_seen(tmp_path):
    from src.daemon_state import acquire_start_lock, release_start_lock

    lock = str(tmp_path / "daemon.lock")
    assert acquire_start_lock(lock)
    assert release_start_lock(lock, owner=_dead_pid()) is False  # taken by someone else since
    assert (tmp_path / "daemon.lock").is_dir()
    assert release_start_lock(lock, owner=__import__("os").getpid()) is True
    assert not (tmp_path / "daemon.lock").exists()


def test_stop_agent_sessions_kills_pool_task_and_named_sessions_only(monkeypatch):
    from types import SimpleNamespace

    commands = []
    monkeypatch.setattr(daemon_mod, "_tmux_socket", lambda: "fixture-socket")

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="p-worker--fixture\ns-task\nn-supervisor\npersonal\n")

    monkeypatch.setattr(daemon_mod.subprocess, "run", run)
    assert daemon_mod.stop_agent_sessions(quiet=True) == 3
    assert [argv[-1] for argv in commands[1:]] == ["p-worker--fixture", "s-task", "n-supervisor"]
    assert all(argv[2] == "fixture-socket" for argv in commands)
