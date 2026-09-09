"""The e2e kit must not call a daemon healthy when it cannot use its schema.

``GET /api/health`` is a static liveness stub — it answers 200 the moment
uvicorn is listening and never touches the database.  The kit used to gate
``e2e-daemon.sh start`` and ``status`` on exactly that, so a daemon whose
database had been dropped out from under it (a second checkout running
``e2e-env.sh --reset`` against the shared ``AQ_E2E_HOME`` / ``E2E_DB_NAME``
does this) was reported healthy, and ``e2e-smoke.sh`` then ran all fifteen
scenarios against an empty schema.  The capability report came out full of
``relation "projects" does not exist`` rows that read like product
regressions, and the only way to find the truth was to start the daemon by
hand (task vivid-rapids).

These pin the three places that now refuse instead:

* ``scripts/e2e/probe.py`` — reads ``/ready``, which runs the daemon's own
  health provider, and reports the database error verbatim;
* ``e2e-daemon.sh start`` / ``status`` — gate on that probe;
* ``e2e-env.sh --reset`` — will not drop a database out from under a live
  daemon it does not own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "scripts" / "e2e" / "probe.py"
E2E_DAEMON = REPO_ROOT / "scripts" / "e2e-daemon.sh"
E2E_ENV = REPO_ROOT / "scripts" / "e2e-env.sh"
E2E_SMOKE = REPO_ROOT / "scripts" / "e2e-smoke.sh"

READY_BODY = {
    "ready": True,
    "checks": {"messaging": {"ok": True}, "database": {"ok": True}},
}
BROKEN_BODY = {
    "ready": False,
    "checks": {
        "messaging": {"ok": True},
        "database": {
            "ok": False,
            "error": (
                '(sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) '
                '<class \'asyncpg.exceptions.UndefinedTableError\'>: '
                'relation "agents" does not exist\n[SQL: SELECT agents.id ...]'
            ),
        },
    },
}


def _load_probe():
    spec = importlib.util.spec_from_file_location("e2e_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


probe = _load_probe()


# ---------------------------------------------------------------------------
# classify() — the pure half
# ---------------------------------------------------------------------------


def test_classify_reports_ready_when_the_database_check_passes():
    assert probe.classify(READY_BODY) == (probe.READY, "database ok")


def test_classify_reports_the_database_error_and_drops_the_sql_wall():
    code, message = probe.classify(BROKEN_BODY)
    assert code == probe.UNUSABLE
    assert 'relation "agents" does not exist' in message
    # The SQLAlchemy error is a dozen lines of SQL; only the first is useful.
    assert "\n" not in message
    assert "SELECT agents.id" not in message


def test_classify_treats_a_missing_database_check_as_unusable():
    code, message = probe.classify({"ready": False, "checks": {"messaging": {"ok": True}}})
    assert code == probe.UNUSABLE
    assert "no database check" in message


@pytest.mark.parametrize("body", [{"status": "ok"}, [], "nope", None])
def test_classify_treats_a_body_that_is_not_the_readiness_envelope_as_unreachable(body):
    # Something answered on the port, but it was not this daemon — telling
    # the operator their schema is broken would be a lie.
    assert probe.classify(body)[0] == probe.UNREACHABLE


# ---------------------------------------------------------------------------
# A stub daemon: /api/health always 200, /ready configurable.
# ---------------------------------------------------------------------------


class _StubDaemon:
    def __init__(self, ready_body: dict, ready_status: int):
        self.ready_body = ready_body
        self.ready_status = ready_status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # BaseHTTPRequestHandler's contract
                if self.path == "/api/health":
                    payload, status = {"status": "ok"}, 200
                elif self.path == "/ready":
                    payload, status = outer.ready_body, outer.ready_status
                else:
                    payload, status = {"error": "not found"}, 404
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep pytest output clean
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_fetch_reads_the_diagnosis_out_of_a_503_ready_body():
    # /ready answers 503 when a check fails, so the probe must not treat a
    # non-2xx as "no answer" — the body is where the error lives.
    with _StubDaemon(BROKEN_BODY, 503) as stub:
        code, message = probe.fetch(stub.url, timeout=10)
    assert code == probe.UNUSABLE
    assert 'relation "agents" does not exist' in message


def test_fetch_reports_ready_against_a_healthy_daemon():
    with _StubDaemon(READY_BODY, 200) as stub:
        assert probe.fetch(stub.url, timeout=10)[0] == probe.READY


def test_fetch_reports_unreachable_when_nothing_is_listening():
    code, message = probe.fetch(f"http://127.0.0.1:{_free_port()}", timeout=2)
    assert code == probe.UNREACHABLE
    assert "not answering" in message


def test_probe_cli_prints_the_hint_only_for_an_unusable_database():
    with _StubDaemon(BROKEN_BODY, 503) as stub:
        broken = subprocess.run(
            [sys.executable, str(PROBE), "--url", stub.url],
            capture_output=True,
            text=True,
            check=False,
        )
        quiet = subprocess.run(
            [sys.executable, str(PROBE), "--url", stub.url, "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
    assert broken.returncode == probe.UNUSABLE
    assert "AQ_E2E_HOME" in broken.stderr and "side by side" in broken.stderr
    # --quiet exists for shell conditionals: exit code only.
    assert quiet.returncode == probe.UNUSABLE
    assert quiet.stderr == ""


# ---------------------------------------------------------------------------
# The shell wrappers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_daemon_process(tmp_path):
    """A live process whose cmdline says ``src.main``.

    ``e2e-daemon.sh``'s ``running_pid`` reads ``/proc/<pid>/cmdline`` and
    refuses a pid whose process is not the daemon, so a plain ``sleep`` will
    not do.
    """
    script = tmp_path / "src.main"
    script.write_text("import time\nwhile True:\n    time.sleep(0.2)\n")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)


def _kit_env(home: Path, url: str, port: int) -> dict:
    env = dict(os.environ)
    env.update(
        AQ_E2E_HOME=str(home),
        AQ_E2E_PORT=str(port),
        AQ_E2E_API_URL=url,
        # The guards under test all fire before any database is touched;
        # naming a database nothing owns keeps it that way if one ever does.
        E2E_DB_NAME="agent_queue_e2e_probe_unittest",
    )
    return env


def _run(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def test_daemon_status_is_zero_only_while_the_schema_is_usable(tmp_path, fake_daemon_process):
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.pid").write_text(str(fake_daemon_process.pid))

    with _StubDaemon(READY_BODY, 200) as stub:
        env = _kit_env(home, stub.url, stub.port)
        ready = _run(E2E_DAEMON, "status", env=env)
        assert ready.returncode == 0, ready.stderr
        assert "ready at" in ready.stdout

        # Same live process, same answering port — only the schema changed.
        stub.ready_body, stub.ready_status = BROKEN_BODY, 503
        broken = _run(E2E_DAEMON, "status", env=env)

    assert broken.returncode != 0
    assert "NOT usable" in broken.stdout
    assert 'relation "agents" does not exist' in broken.stdout


def test_daemon_start_refuses_to_report_success_for_an_unusable_daemon(
    tmp_path, fake_daemon_process
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.pid").write_text(str(fake_daemon_process.pid))
    (home / "config.yaml").write_text("# stub\n")

    with _StubDaemon(BROKEN_BODY, 503) as stub:
        started = _run(E2E_DAEMON, "start", env=_kit_env(home, stub.url, stub.port))

    # The old code printed "already running (pid N)" and returned 0 here,
    # which is what let e2e-smoke.sh run against an empty database.
    assert started.returncode != 0
    assert "not usable" in started.stderr
    assert 'relation "agents" does not exist' in started.stderr


def test_smoke_refuses_to_run_the_scenarios_against_an_unusable_database(
    tmp_path, fake_daemon_process
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.pid").write_text(str(fake_daemon_process.pid))
    (home / "config.yaml").write_text("# stub\n")

    with _StubDaemon(BROKEN_BODY, 503) as stub:
        smoked = _run(E2E_SMOKE, env=_kit_env(home, stub.url, stub.port))

    assert smoked.returncode != 0
    assert 'relation "agents" does not exist' in smoked.stderr
    # It must not have reached scripts/e2e/smoke.py.
    assert "swarm e2e (Tier 1)" not in smoked.stdout


@pytest.mark.skipif(shutil.which("curl") is None, reason="the kit shells out to curl")
def test_env_reset_refuses_to_drop_a_database_under_a_daemon_it_does_not_own(tmp_path):
    # No pid file: from this home's point of view, whoever is answering on
    # the port belongs to somebody else — most likely another checkout on
    # the kit's shared defaults, which is the collision that produced the
    # empty-schema run in the first place.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".aq-e2e").write_text("marker\n")

    with _StubDaemon(READY_BODY, 200) as stub:
        reset = _run(E2E_ENV, "--reset", env=_kit_env(home, stub.url, stub.port))

    assert reset.returncode == 2
    assert "does not own" in reset.stderr
    assert "AQ_E2E_HOME" in reset.stderr
    # Refused before touching anything: the home is still there.
    assert (home / ".aq-e2e").exists()


@pytest.mark.skipif(shutil.which("curl") is None, reason="the kit shells out to curl")
def test_env_reset_stops_the_daemon_this_home_owns_before_rebuilding(
    tmp_path, fake_daemon_process
):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".aq-e2e").write_text("marker\n")
    (home / "daemon.pid").write_text(str(fake_daemon_process.pid))

    # The stub keeps answering after the pid is killed, so the reset stops
    # the process it owns and then refuses on the still-live port rather
    # than deleting the home anyway.  What is pinned here is the stop.
    with _StubDaemon(READY_BODY, 200) as stub:
        env = _kit_env(home, stub.url, stub.port)
        env["AQ_E2E_STOP_GRACE"] = "10"
        reset = _run(E2E_ENV, "--reset", env=env)

    assert "stopping it before the reset" in reset.stdout
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and fake_daemon_process.poll() is None:
        time.sleep(0.1)
    assert fake_daemon_process.poll() is not None, "the reset left the daemon running"
