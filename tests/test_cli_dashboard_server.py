"""The dashboard server's lifecycle (docs/specs/dashboard-server.md §1, §7 "CLI / process").

``aq dashboard start|stop|restart|status`` and how ``aq start`` / ``aq stop`` /
``aq restart`` / ``aq status`` drive them.  Most cases use fakes for the probe
and the spawn; the cases marked "for real" start the actual
``python -m src.dashboard_server`` on a free loopback port against a staged
bundle, and kill it the way a crash would.

Every path is redirected into ``tmp_path`` -- nothing here reads or writes
``~/.agent-queue``, and the daemon itself is never started.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import src.cli.daemon as daemon_mod
from src.cli.app import cli
from src.dashboard_server import process
from tests.dashboard_server_helpers import unused_port
from tests.test_dashboard_server_app import stage_bundle


@pytest.fixture(autouse=True)
def _pg_backend(monkeypatch):
    """Process lifecycle only; never allocate a real test database.

    These are operator-shell lifecycle tests. A pool worker's inherited
    markers must not replace the behavior under test with the worker safety
    refusal; that refusal has its own coverage in ``test_cli_daemon.py``.
    """
    monkeypatch.delenv("AQ_SESSION_ID", raising=False)
    monkeypatch.delenv("AQ_SESSION_KIND", raising=False)
    monkeypatch.delenv("AQ_DB_SCOPE", raising=False)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A private state dir, config, and staged bundle; returns a namespace of paths."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    port = unused_port()
    api_port = unused_port()
    config = state_dir / "config.yaml"
    config.write_text(
        f"dashboard:\n  server:\n    port: {port}\nmcp_server:\n  port: {api_port}\n",
        encoding="utf-8",
    )
    for name, value in {
        "CONFIG_DIR": state_dir,
        "CONFIG_PATH": config,
        "PID_FILE": state_dir / "daemon.pid",
        "LOG_PATH": state_dir / "daemon.log",
        "LOCK_DIR": state_dir / "daemon.lock",
        "DASHBOARD_PID_FILE": state_dir / "dashboard.pid",
        "DASHBOARD_LOG_PATH": state_dir / "dashboard.log",
        "DASHBOARD_SERVER_PID_FILE": state_dir / "dashboard-server.pid",
        "DASHBOARD_SERVER_LOG_PATH": state_dir / "dashboard-server.log",
    }.items():
        monkeypatch.setattr(daemon_mod, name, str(value))
    bundle = stage_bundle(tmp_path)
    monkeypatch.setattr(process, "bundle_directory", lambda: bundle)
    # The server's proxy resolves the daemon from the environment first.
    monkeypatch.setenv("AQ_API_URL", f"http://127.0.0.1:{api_port}")
    files = process.ServerFiles.in_state_dir(state_dir)
    return SimpleNamespace(
        dir=state_dir, config=config, port=port, url=f"http://127.0.0.1:{port}/",
        bundle=bundle, files=files,
    )


@pytest.fixture
def runner():
    return CliRunner()


def _ours(pid: int, digest: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "service": process.SERVICE_NAME,
        "version": "1.0",
        "pid": pid,
        "bundle": {"version": "9.9.9", "files": 2, "verified": True, "manifest_sha256": digest},
        "api_url": "http://127.0.0.1:8081",
        "upstream_ok": True,
        **extra,
    }


def _sleeper(*, ignore_term: bool = False) -> subprocess.Popen:
    """A live child whose command line names ``src.dashboard_server``, as a real one's does."""
    body = "import signal, time\n"
    if ignore_term:
        body += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    body += "print('ready', flush=True)\ntime.sleep(60)\n"
    child = subprocess.Popen(
        [sys.executable, "-c", body, "src.dashboard_server"], stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None and child.stdout.readline().strip() == "ready"
    return child


def _detached_sleeper(tmp_path: Path, *, ignore_term: bool = False) -> tuple[int, Path]:
    """Like :func:`_sleeper`, but not our child -- as a server an earlier `aq` started is.

    Returns its PID and a file its ``SIGTERM`` handler writes, which tells a
    graceful stop from a ``SIGKILL`` without being able to wait on the process.
    """
    marker = tmp_path / f"term-{time.monotonic_ns()}"
    ready = marker.with_suffix(".ready")
    handler = "signal.SIG_IGN" if ignore_term else (
        f"lambda *a: (open({str(marker)!r}, 'w').close(), sys.exit(0))"
    )
    body = (
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, {handler})\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(60)\n"
    )
    launcher = subprocess.run(
        ["sh", "-c", 'nohup "$0" -c "$1" src.dashboard_server >/dev/null 2>&1 & echo $!',
         sys.executable, body],
        capture_output=True, text=True, check=True,
    )
    pid = int(launcher.stdout.strip())
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline, "the sleeper never started"
        time.sleep(0.02)
    return pid, marker


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while process.process_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The environment the server is started with
# ---------------------------------------------------------------------------


def test_the_server_environment_is_an_allowlist_without_secrets_or_harness_markers():
    env = process.server_environment({
        "PATH": "/venv/bin:/usr/bin",
        "HOME": "/home/op",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AQ_API_URL": "http://127.0.0.1:8081",
        "AQ_DATABASE_URL": "postgresql://aq:secret@db/aq",
        "AGENT_QUEUE_DB_URL": "postgresql://aq:secret@db/aq",
        "DATABASE_URL": "postgresql://aq:secret@db/aq",
        "PGPASSWORD": "secret",
        "ANTHROPIC_API_KEY": "sk-ant",
        "OPENAI_API_KEY": "sk-oai",
        "DISCORD_BOT_TOKEN": "tok",
        "AQ_API_TOKEN": "bearer",
        "AQ_DB_SCOPE": "worker",
        # An enclosing harness's session markers (src.env_scrub)
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "CODEX_SANDBOX": "seatbelt",
    })

    assert env == {
        "PATH": "/venv/bin:/usr/bin",
        "HOME": "/home/op",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AQ_API_URL": "http://127.0.0.1:8081",
        "PYTHONUNBUFFERED": "1",
    }


def test_the_daemon_environment_keeps_its_harness_scrub(monkeypatch):
    """The dashboard server did not change how `aq start` launches the daemon."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")

    env = daemon_mod._daemon_environment()

    assert "CLAUDECODE" not in env
    assert env["ANTHROPIC_API_KEY"] == "api-key"


# ---------------------------------------------------------------------------
# inspect(): the states `aq status` reports
# ---------------------------------------------------------------------------


def test_nothing_running_is_stopped(state):
    status = process.inspect(state.files, probe=lambda url: (process.NOTHING, None))

    assert (status.state, status.url, status.pid) == ("stopped", state.url, None)


def test_a_server_that_answers_as_ours_is_running_and_current(state):
    digest = process.installed_manifest_sha256()
    seen = []

    def probe(url):
        seen.append(url)
        return process.OURS, _ours(4242, digest)

    status = process.inspect(state.files, probe=probe)

    assert seen == [state.url]
    assert (status.state, status.pid, status.bundle_current) == ("running", 4242, True)
    assert status.to_dict()["bundle"]["manifest_sha256"] == digest


def test_a_server_serving_an_older_build_is_running_but_stale(state):
    status = process.inspect(
        state.files, probe=lambda url: (process.OURS, _ours(4242, "0" * 64)),
    )

    assert status.state == "running" and status.bundle_current is False
    assert "aq dashboard restart" in process.describe(status)


def test_a_port_that_answers_as_something_else_is_a_port_conflict(state):
    status = process.inspect(state.files, probe=lambda url: (process.FOREIGN, None))

    assert status.state == "port_conflict"
    assert "dashboard.server.port" in status.detail and str(state.port) in status.detail


def test_a_pid_file_naming_an_exited_process_is_a_stale_pid(state):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    state.files.pid_file.write_text(f"{child.pid}\n")

    status = process.inspect(state.files, probe=lambda url: (process.NOTHING, None))

    assert (status.state, status.stale_pid) == ("stale_pid", child.pid)
    assert state.files.pid_file.exists(), "inspect is read-only"


def test_a_pid_file_naming_someone_elses_live_process_is_not_ours(state):
    """A recycled PID: alive, but not a dashboard server.  Here it is this test's own."""
    state.files.pid_file.write_text(f"{os.getpid()}\n")

    status = process.inspect(state.files, probe=lambda url: (process.NOTHING, None))

    assert status.state == "stale_pid"


def test_our_live_process_that_does_not_answer_is_unresponsive(state):
    child = _sleeper()
    try:
        state.files.pid_file.write_text(f"{child.pid}\n")
        status = process.inspect(state.files, probe=lambda url: (process.NOTHING, None))
    finally:
        child.kill()
        child.wait()

    assert (status.state, status.pid) == ("unresponsive", child.pid)


def test_disabled_and_no_bundle_are_reported_when_nothing_of_ours_answers(state, monkeypatch):
    state.config.write_text("dashboard:\n  server:\n    enabled: false\n")
    assert process.inspect(state.files, probe=lambda url: (process.NOTHING, None)).state == (
        "disabled"
    )

    state.config.write_text("{}\n")
    monkeypatch.setattr(process, "bundle_directory", lambda: state.dir / "no-bundle-here")
    status = process.inspect(state.files, probe=lambda url: (process.NOTHING, None))
    assert status.state == "no_bundle"
    assert "npm -w dashboard run dev" in status.detail


def test_a_running_server_wins_over_disabled(state):
    state.config.write_text("dashboard:\n  server:\n    enabled: false\n")

    status = process.inspect(state.files, probe=lambda url: (process.OURS, _ours(7, None)))

    assert status.state == "running" and status.enabled is False


def test_an_invalid_config_is_misconfigured_and_names_the_key(state):
    state.config.write_text("dashboard:\n  server:\n    port: 8081\nmcp_server:\n  port: 8081\n")

    status = process.inspect(state.files, probe=lambda url: pytest.fail("no probe"))

    assert status.state == "misconfigured"
    assert "dashboard.server.port" in status.detail


def test_the_identity_probe_tells_ours_from_foreign_from_nothing(state):
    """Against real sockets: nothing listening, and a server that is not ours."""
    kind, identity = process.probe_identity(state.url, timeout=1.0)
    assert (kind, identity) == ("none", None)

    import http.server
    import threading

    class NotOurs(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"service": "something-else"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", state.port), NotOurs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert process.probe_identity(state.url, timeout=2.0) == ("foreign", None)
        assert process.inspect(state.files).state == "port_conflict"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# start() and stop() with fakes
# ---------------------------------------------------------------------------


class FakeChild:
    def __init__(self, pid: int, *, exits_with: int | None = None) -> None:
        self.pid = pid
        self._code = exits_with

    def poll(self) -> int | None:
        return self._code


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_start_is_idempotent_for_a_current_server(state):
    digest = process.installed_manifest_sha256()

    outcome = process.start(
        state.files,
        probe=lambda url: (process.OURS, _ours(4242, digest)),
        popen=lambda *a, **kw: pytest.fail("must not spawn"),
    )

    assert (outcome.ok, outcome.action, outcome.pid) == (True, "already_running", 4242)


@pytest.mark.parametrize(
    ("config", "probe_kind", "action", "exit_code"),
    [
        ("dashboard:\n  server:\n    enabled: false\n", process.NOTHING, "disabled", 1),
        (None, process.FOREIGN, "port_conflict", 1),
    ],
)
def test_start_refuses_without_spawning(state, config, probe_kind, action, exit_code):
    if config is not None:
        state.config.write_text(config)

    outcome = process.start(
        state.files,
        probe=lambda url: (probe_kind, None),
        popen=lambda *a, **kw: pytest.fail("must not spawn"),
    )

    assert (outcome.ok, outcome.action, outcome.exit_code) == (False, action, exit_code)


def test_start_without_a_bundle_exits_2_and_names_the_way_forward(state, monkeypatch):
    monkeypatch.setattr(process, "bundle_directory", lambda: state.dir / "none")

    outcome = process.start(
        state.files,
        probe=lambda url: (process.NOTHING, None),
        popen=lambda *a, **kw: pytest.fail("must not spawn"),
    )

    assert (outcome.ok, outcome.action, outcome.exit_code) == (False, "no_bundle", 2)
    assert "aq install --restart-from dashboard.build" in outcome.message


def test_start_spawns_the_module_detached_and_waits_for_its_own_identity(state, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    spawned: list[dict[str, Any]] = []
    answers = iter([(process.NOTHING, None), (process.OURS, _ours(999, None))])

    def popen(argv, **kwargs):
        spawned.append({"argv": argv, **kwargs})
        return FakeChild(4242)

    def probe(url):
        kind, identity = next(answers, (process.OURS, _ours(4242, None)))
        return kind, identity

    clock = Clock()
    outcome = process.start(
        state.files, probe=probe, popen=popen, python="/venv/bin/python",
        clock=clock, sleep=clock.sleep,
    )

    assert (outcome.ok, outcome.action, outcome.pid) == (True, "started", 4242)
    (call,) = spawned
    assert call["argv"] == [
        "/venv/bin/python", "-m", "src.dashboard_server",
        "--config", str(state.config), "--bundle-dir", str(state.bundle),
    ]
    assert call["start_new_session"] is True
    assert call["stdin"] == subprocess.DEVNULL
    assert call["cwd"] == str(process.source_root())
    assert "ANTHROPIC_API_KEY" not in call["env"] and "CLAUDECODE" not in call["env"]
    assert process.read_pid_file(state.files.pid_file) == 4242


def test_a_server_that_exits_during_startup_is_reported_with_its_log(state):
    def popen(argv, *, stdout, **kwargs):
        stdout.write(b"dashboard server: cannot listen on 127.0.0.1:8082 (Address in use)\n")
        return FakeChild(4242, exits_with=1)

    outcome = process.start(state.files, probe=lambda url: (process.NOTHING, None), popen=popen)

    assert (outcome.ok, outcome.action, outcome.exit_code) == (False, "exited", 1)
    assert "Address in use" in outcome.message
    assert outcome.log_excerpt[-1].endswith("(Address in use)")
    assert not state.files.pid_file.exists()


def test_a_server_that_never_answers_is_stopped_after_the_timeout(state):
    child = _sleeper()
    clock = Clock()
    try:
        outcome = process.start(
            state.files, probe=lambda url: (process.NOTHING, None),
            popen=lambda *a, **kw: child, clock=clock, sleep=clock.sleep,
        )
        stopped = _gone(child.pid)
    finally:
        _kill(child.pid)

    assert (outcome.ok, outcome.action) == (False, "timeout")
    assert stopped
    assert not state.files.pid_file.exists()


def test_stop_sends_sigterm_first(state, tmp_path):
    pid, terminated = _detached_sleeper(tmp_path)
    state.files.pid_file.write_text(f"{pid}\n")
    try:
        outcome = process.stop(state.files, probe=lambda url: (process.NOTHING, None))
        assert _gone(pid)
    finally:
        _kill(pid)

    assert (outcome.ok, outcome.action, outcome.pid) == (True, "stopped", pid)
    assert terminated.exists(), "the server was asked to stop, not killed"
    assert not state.files.pid_file.exists()


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(state, tmp_path):
    pid, terminated = _detached_sleeper(tmp_path, ignore_term=True)
    state.files.pid_file.write_text(f"{pid}\n")
    try:
        outcome = process.stop(
            state.files, probe=lambda url: (process.NOTHING, None), timeout=0.3,
        )
        assert _gone(pid)
    finally:
        _kill(pid)

    assert outcome.action == "stopped"
    assert not terminated.exists()


def test_stop_never_signals_a_process_that_is_not_ours(state):
    """The PID file names this very test process -- a recycled PID."""
    state.files.pid_file.write_text(f"{os.getpid()}\n")

    outcome = process.stop(state.files, probe=lambda url: (process.NOTHING, None))

    assert (outcome.ok, outcome.action) == (True, "not_running")
    assert not state.files.pid_file.exists()


def test_stop_finds_a_server_by_its_identity_when_the_pid_file_is_gone(state, tmp_path):
    pid, terminated = _detached_sleeper(tmp_path)
    try:
        outcome = process.stop(state.files, probe=lambda url: (process.OURS, _ours(pid, None)))
        assert _gone(pid)
    finally:
        _kill(pid)

    assert (outcome.action, outcome.pid) == ("stopped", pid)
    assert terminated.exists()


# ---------------------------------------------------------------------------
# For real: `aq dashboard start|status|stop` and `aq status`
# ---------------------------------------------------------------------------


def _json(result) -> dict[str, Any]:
    return json.loads(result.stdout)


def _stop_quietly(files: process.ServerFiles) -> None:
    pid = process.read_pid_file(files.pid_file)
    if pid is not None and process.process_alive(pid):
        os.kill(pid, signal.SIGKILL)


def test_the_managed_server_for_real(state, runner):
    try:
        started = runner.invoke(cli, ["dashboard", "start"])
        assert started.exit_code == 0, started.output
        assert "Dashboard server started" in started.output
        pid = process.read_pid_file(state.files.pid_file)
        assert pid is not None

        status = _json(runner.invoke(cli, ["--json", "dashboard", "status"]))["data"]
        assert (status["state"], status["pid"], status["url"]) == ("running", pid, state.url)
        assert status["bundle"]["verified"] is True and status["bundle_current"] is True

        again = runner.invoke(cli, ["dashboard", "start"])
        assert again.exit_code == 0 and "already running" in again.output
        assert process.read_pid_file(state.files.pid_file) == pid

        # `aq status` answers with the daemon down: the dashboard line, exit 3.
        down = runner.invoke(cli, ["--json", "status"])
        assert down.exit_code == 3
        envelope = _json(down)
        assert envelope["error"]["code"] == "daemon_unreachable"
        assert envelope["error"]["details"]["dashboard_server"]["state"] == "running"

        # A crash: the status says so, and `aq dashboard start` heals it.
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while process.process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        crashed = _json(runner.invoke(cli, ["--json", "dashboard", "status"]))["data"]
        assert (crashed["state"], crashed["stale_pid"]) == ("stale_pid", pid)
        healed = runner.invoke(cli, ["dashboard", "start"])
        assert healed.exit_code == 0, healed.output
        healed_pid = process.read_pid_file(state.files.pid_file)
        assert healed_pid not in (None, pid)

        # `aq dashboard restart` replaces the process, e.g. to serve a rebuilt bundle.
        restarted = runner.invoke(cli, ["--json", "dashboard", "restart"])
        assert restarted.exit_code == 0, restarted.output
        new_pid = _json(restarted)["data"]["pid"]
        assert new_pid not in (None, healed_pid) and not process.process_alive(healed_pid)
        assert process.read_pid_file(state.files.pid_file) == new_pid

        stopped = runner.invoke(cli, ["dashboard", "stop"])
        assert stopped.exit_code == 0 and "stopped" in stopped.output
        assert not state.files.pid_file.exists()
        assert not process.process_alive(new_pid)
        assert _json(runner.invoke(cli, ["--json", "dashboard", "status"]))["data"]["state"] == (
            "stopped"
        )
    finally:
        _stop_quietly(state.files)


def test_aq_dashboard_start_reports_a_busy_port_for_real(state, runner):
    """The port is taken by a socket that is bound but not listening: the probe is
    refused at once, and the server's own bind fails, naming the key."""
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", state.port))
    try:
        result = runner.invoke(cli, ["--json", "dashboard", "start"])
    finally:
        holder.close()
        _stop_quietly(state.files)

    assert result.exit_code == 1, result.output
    error = _json(result)["error"]
    assert error["code"] == "dashboard_server_exited"
    assert "dashboard.server.port" in error["message"]
    assert not state.files.pid_file.exists()


def test_aq_start_status_and_stop_manage_a_real_dashboard_server(state, runner, monkeypatch):
    """The acceptance path: `aq start` brings the server up with the daemon, `aq status`
    shows it, `aq stop` stops it.  Only the daemon half is stubbed."""
    daemon = {"up": False}

    def start_daemon():
        daemon["up"] = True
        return True

    def stop_daemon(quiet=False):
        daemon["up"] = False
        return True

    monkeypatch.setattr(daemon_mod, "start_daemon", start_daemon)
    monkeypatch.setattr(daemon_mod, "stop_daemon", stop_daemon)
    monkeypatch.setattr(daemon_mod, "stop_agent_sessions", lambda quiet=False: 0)
    try:
        started = runner.invoke(cli, ["start", "--no-dashboard"])
        assert started.exit_code == 0, started.output
        assert daemon["up"] and "Dashboard server started" in started.output
        pid = process.read_pid_file(state.files.pid_file)
        assert pid is not None and process.process_alive(pid)

        status = runner.invoke(cli, ["--json", "status"])  # no daemon answers the API here
        details = _json(status)["error"]["details"]
        assert details["dashboard_server"]["state"] == "running"
        assert details["dashboard_server"]["pid"] == pid

        stopped = runner.invoke(cli, ["stop"])
        assert stopped.exit_code == 0, stopped.output
        assert not daemon["up"]
        assert _gone(pid) and not state.files.pid_file.exists()
    finally:
        _stop_quietly(state.files)


# ---------------------------------------------------------------------------
# `aq start` / `aq stop` / `aq restart` ordering and flags
# ---------------------------------------------------------------------------


@pytest.fixture
def lifecycle(state, monkeypatch):
    """Record the order of daemon and dashboard-server operations."""
    events: list[str] = []

    def start_daemon():
        events.append("daemon.start")
        return True

    def stop_daemon(quiet=False):
        events.append("daemon.stop")
        return True

    def start_server(**kwargs):
        events.append("server.start")
        return process.Outcome(True, "started", "Dashboard server started.", pid=1)

    def stop_server(files, **kwargs):
        events.append("server.stop")
        return process.Outcome(True, "stopped", "Dashboard server stopped.", pid=1)

    import src.cli.dashboard as dashboard_cli

    monkeypatch.setattr(daemon_mod, "start_daemon", start_daemon)
    monkeypatch.setattr(daemon_mod, "stop_daemon", stop_daemon)
    monkeypatch.setattr(daemon_mod, "stop_agent_sessions", lambda quiet=False: 0)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dashboard_cli, "start_dashboard_server", start_server)
    monkeypatch.setattr(process, "stop", stop_server)
    monkeypatch.setattr(
        daemon_mod, "_maybe_prompt_dashboard", lambda flag: events.append(f"vite.prompt({flag})")
    )
    return events


def test_aq_start_starts_the_daemon_then_the_dashboard_server(runner, lifecycle):
    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0, result.output
    assert lifecycle == ["daemon.start", "server.start", "vite.prompt(False)"]


def test_no_dashboard_only_skips_the_vite_prompt(runner, lifecycle):
    """Spec §6.2: an updater runs `aq start --no-dashboard` and expects the dashboard served."""
    result = runner.invoke(cli, ["start", "--no-dashboard"])

    assert result.exit_code == 0, result.output
    assert lifecycle == ["daemon.start", "server.start", "vite.prompt(True)"]


def test_no_dashboard_server_starts_the_daemon_alone(runner, lifecycle):
    result = runner.invoke(cli, ["start", "--no-dashboard", "--no-dashboard-server"])

    assert result.exit_code == 0, result.output
    assert lifecycle == ["daemon.start", "vite.prompt(True)"]


def test_the_dashboard_server_is_not_started_when_the_daemon_fails(runner, lifecycle, monkeypatch):
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda: lifecycle.append("daemon.start"))

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 1
    assert lifecycle == ["daemon.start"]


def test_aq_stop_stops_the_dashboard_server_then_the_daemon(runner, lifecycle):
    assert runner.invoke(cli, ["stop"]).exit_code == 0
    assert runner.invoke(cli, ["stop", "--keep-sessions"]).exit_code == 0

    assert lifecycle == ["server.stop", "daemon.stop"] * 2


def test_aq_restart_restarts_both(runner, lifecycle):
    result = runner.invoke(cli, ["restart", "--no-dashboard"])

    assert result.exit_code == 0, result.output
    assert lifecycle == [
        "server.stop", "daemon.stop", "daemon.start", "server.start", "vite.prompt(True)",
    ]


def test_aq_restart_no_dashboard_server_leaves_it_alone(runner, lifecycle):
    result = runner.invoke(cli, ["restart", "--no-dashboard-server"])

    assert result.exit_code == 0, result.output
    assert lifecycle == ["daemon.stop", "daemon.start", "vite.prompt(False)"]


def test_a_dashboard_server_failure_is_a_warning_not_a_failed_start(runner, state, monkeypatch):
    import src.cli.dashboard as dashboard_cli

    monkeypatch.setattr(daemon_mod, "start_daemon", lambda: True)
    monkeypatch.setattr(
        dashboard_cli, "start_dashboard_server",
        lambda **kw: process.Outcome(
            False, "port_conflict", "port 8082 answers, but not as the dashboard server",
            exit_code=1,
        ),
    )

    result = runner.invoke(cli, ["start", "--no-dashboard"])

    assert result.exit_code == 0, result.output
    assert "the daemon is up, but the dashboard server is not" in result.output
    assert "aq dashboard start" in result.output


def test_a_source_checkout_without_a_bundle_keeps_the_vite_offer(runner, state, monkeypatch):
    monkeypatch.setattr(process, "bundle_directory", lambda: state.dir / "none")
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda: True)
    monkeypatch.setattr(daemon_mod, "_dashboard_running", lambda port=5173: False)
    monkeypatch.setattr(daemon_mod, "_repo_root", lambda: state.dir)
    offered: list[str] = []
    monkeypatch.setattr(daemon_mod.click, "confirm", lambda text, **kw: offered.append(text))
    spawned = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **kw: spawned.append(a))
    terminal = SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), executable=sys.executable)
    monkeypatch.setattr(daemon_mod, "sys", terminal)

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0, result.output
    assert spawned == [], "no dashboard server without a bundle"
    assert offered and "5173" in offered[0]


def test_aq_start_never_prompts_without_a_terminal(runner, state, monkeypatch):
    monkeypatch.setattr(process, "bundle_directory", lambda: state.dir / "none")
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda: True)
    monkeypatch.setattr(daemon_mod, "_dashboard_running", lambda port=5173: False)
    monkeypatch.setattr(daemon_mod, "_repo_root", lambda: state.dir)
    monkeypatch.setattr(
        daemon_mod.click, "confirm", lambda *a, **kw: pytest.fail("prompted without a terminal")
    )

    result = runner.invoke(cli, ["start"])  # CliRunner's stdin is not a terminal

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# `aq status`
# ---------------------------------------------------------------------------


def _mock_client(get_status: dict[str, Any]):
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.execute = AsyncMock(return_value=get_status)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def test_aq_status_shows_the_daemon_and_the_dashboard_server(runner, state, monkeypatch):
    from unittest.mock import patch

    digest = process.installed_manifest_sha256()
    monkeypatch.setattr(
        process, "probe_identity", lambda url, **kw: (process.OURS, _ours(4242, digest))
    )
    payload = {"projects": 1, "tasks": {"by_status": {"ready": 2}}}

    with patch("src.cli.app._get_client", return_value=_mock_client(payload)):
        human = runner.invoke(cli, ["status"])
        machine = runner.invoke(cli, ["--json", "status"])

    assert human.exit_code == 0, human.output
    assert "Agent Q Status" in human.output
    assert f"Dashboard: running at {state.url} (PID 4242)" in human.output
    data = _json(machine)["data"]
    assert data["projects"] == 1
    assert data["dashboard_server"]["state"] == "running"
    assert data["dashboard_server"]["url"] == state.url


def test_aq_status_with_the_daemon_down_still_reports_the_dashboard_server(runner, state):
    """Nothing listens on the configured API port, and no dashboard server runs."""
    result = runner.invoke(cli, ["--json", "status"])

    assert result.exit_code == 3
    details = _json(result)["error"]["details"]
    assert details["dashboard_server"]["state"] == "stopped"
    assert details["daemon"]["state"] == "unreachable"
