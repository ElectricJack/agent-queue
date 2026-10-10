"""Project isolation of the supervisor stall sweep (scripts/operator-checks/stall-sweep.py).

The script is driven as a subprocess whose PATH holds only a recording fake for
every external command it runs (``aq``, ``git``, ``docker``, ``tmux``, ``bash``,
``tail``), so a test never reaches the operator's daemon, logs or tmux server,
and every query the sweep makes is visible to the assertions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "operator-checks" / "stall-sweep.py"
COMMANDS = ("aq", "git", "docker", "tmux", "bash", "tail")

FAKE = """\
import json, os, sys
name = os.path.basename(sys.argv[1])  # the shim passes its own path first
args = [a for a in sys.argv[2:] if not (name == "aq" and a == "--json")]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps([name, *args]) + "\\n")
responses = json.load(open(os.environ["FAKE_RESPONSES"])).get(name, {})
if name == "aq":
    key = " ".join(args)
    if key in responses:
        print(json.dumps({"data": responses[key]}))
    else:
        print(json.dumps({"error": {"code": "fake_missing", "message": key}}))
elif name == "git" and len(args) > 2:
    sys.stdout.write(responses.get(args[2], ""))
"""


def _task(tid, status, *, age_s=3600, **extra):
    return {
        "id": tid, "status": status, "title": f"title {tid}", "profile_id": "standard-claude",
        "updated_at": time.time() - age_s, **extra,
    }


def _session(name, project_id, task_id):
    return {
        "name": name, "project_id": project_id, "task_id": task_id, "state": "running",
        "stalled": True, "idle_seconds": 3600, "lifecycle": "task", "profile_id": "standard-claude",
        "started_at": time.time() - 7200,
    }


def _run(tmp_path, responses, *args, api_url="http://127.0.0.1:9"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    for name in COMMANDS:
        shim = bin_dir / name
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} -I {fake} \"$0\" \"$@\"\n")
        shim.chmod(0o755)
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    log = tmp_path / "calls.jsonl"
    log.touch()
    env = {
        "PATH": str(bin_dir), "HOME": str(tmp_path), "AQ_API_URL": api_url,
        "FAKE_LOG": str(log), "FAKE_RESPONSES": str(tmp_path / "responses.json"),
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path, check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return proc, calls


def _project_args(calls):
    """Every project id an ``aq`` call names explicitly."""
    named = []
    for name, *args in calls:
        if name != "aq":
            continue
        for flag in ("-p", "--project", "--project-id"):
            if flag in args:
                named.append(args[args.index(flag) + 1])
    return named


@pytest.fixture
def health_server():
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_scoped_sweep_never_reads_or_reports_another_project(tmp_path):
    responses = {"aq": {
        "task list -p matter": [
            _task("m-blocked", "BLOCKED", age_s=3 * 3600),
            _task("m-ready", "READY"),
            _task("m-waits", "DEFINED"),
            _task("m-undelivered", "DEFINED"),
        ],
        "task explain --task-id m-blocked": {"reasons": []},
        "task explain --task-id m-ready": {"reasons": []},
        # Cross-project edges: the blocker is another project's to chase.
        "task explain --task-id m-waits": {"reasons": [{
            "code": "blocked_dependency", "ref": "o-blocker",
            "detail": "blocked by blocks dep 'o-blocker' (t) status=BLOCKED in project 'other'",
        }]},
        "task explain --task-id m-undelivered": {"reasons": [{
            "code": "blocked_dependency", "ref": "o-done",
            "detail": "blocked by blocks dep 'o-done' (t) status=COMPLETED in project 'other'",
        }]},
        "task get --task-id m-blocked": {"children": {}},
        # The daemon filters the per-project breakdown; pool totals stay
        # fleet-wide, so ``ready`` here counts other projects' work.
        "pool status --project-id matter": [{
            "profile_id": "standard-claude", "enabled": True, "ready": 4, "desired": 4,
            "running_busy": 0,
            "projects": [{
                "project_id": "matter", "ready": 0, "running_busy": 0, "running_idle": 0,
                "starting": 0, "draining": 0, "workspace_capacity": 4,
            }],
        }],
        # A global token running ``--project matter`` still lists every session.
        "session list": [
            _session("s-matter-1", "matter", "m-busy"),
            _session("s-other-1", "other", "o-busy"),
        ],
    }}
    proc, calls = _run(tmp_path, responses, "--project", "matter")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP-ERROR" not in proc.stdout and "SWEEP-AUTH" not in proc.stdout, proc.stdout
    assert set(_project_args(calls)) == {"matter"}
    assert ["aq", "project", "list"] not in calls
    assert not any("agent-queue" in " ".join(call) for call in calls), calls
    # Development-publisher, stranded-branch and validation-DB checks belong
    # to agent-queue: none of their git, docker or log reads run here.
    assert [c for c in calls if c[0] in ("git", "docker", "bash", "tail")] == []
    # Another project's session is neither reported nor captured.
    assert "s-other-1" not in proc.stdout and "o-busy" not in proc.stdout
    assert not any(c[0] == "tmux" and "s-other-1" in c for c in calls)
    assert "IDLE-WORKER s-matter-1 holds m-busy" in proc.stdout
    assert "STALE-OPEN matter m-blocked" in proc.stdout
    # Another project's blocker is never read; an undelivered one still reports
    # from the explain row alone instead of hiding.
    assert not any("o-blocker" in call or "o-done" in call for call in calls), calls
    assert "m-waits" not in proc.stdout
    assert "STUCK matter m-undelivered" in proc.stdout
    # The project's own frontier excludes m-ready even though other projects
    # keep the fleet-wide ``ready`` above zero.
    assert "POOL-STARVED standard-claude: 1 READY" in proc.stdout
    assert "m-ready" in proc.stdout.split("POOL-STARVED", 1)[1]


@pytest.mark.parametrize("healthy", [True, False])
def test_empty_project_sweep_stays_in_scope(tmp_path, health_server, healthy):
    responses = {"aq": {
        "task list -p rom": [],
        "pool status --project-id rom": [],
        "session list": [_session("s-other-1", "other", "o-busy")],
    }}
    proc, calls = _run(
        tmp_path, responses, "--project", "rom",
        api_url=health_server if healthy else "http://127.0.0.1:9",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(_project_args(calls)) == {"rom"}
    assert not any("agent-queue" in " ".join(call) for call in calls), calls
    assert [c for c in calls if c[0] in ("git", "docker", "bash", "tail", "tmux")] == []
    assert "s-other-1" not in proc.stdout
    if healthy:
        assert "note: rom has no open tasks (daemon healthy)" in proc.stdout
        assert "OK: nothing stalled" in proc.stdout
        assert "SWEEP-ERROR" not in proc.stdout
    else:
        assert "SWEEP-ERROR every active project returned an empty task list" in proc.stdout


@pytest.mark.parametrize("agent_queue_status", ["ACTIVE", "PAUSED"])
def test_global_sweep_runs_development_checks_only_for_active_agent_queue(
    tmp_path, agent_queue_status
):
    stale_commit = int(time.time()) - 7 * 3600
    responses = {
        "aq": {
            "project list": [
                {"id": "agent-queue", "status": agent_queue_status},
                {"id": "rom", "status": "ACTIVE"},
            ],
            "task list -p agent-queue": [_task("aq-new", "DEFINED", age_s=60)],
            "task list -p rom": [_task("rom-new", "DEFINED", age_s=60)],
            "pool status": [],
            "session list": [],
        },
        "git": {"log": f"{stale_commit} abc1234\n"},
    }
    proc, calls = _run(tmp_path, responses)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP-ERROR" not in proc.stdout, proc.stdout
    dev_reads = [c for c in calls if c[0] in ("git", "docker", "bash", "tail")]
    if agent_queue_status == "ACTIVE":
        assert "DELIVERY last worker commit on origin/main" in proc.stdout
        assert any(c[0] == "docker" and "psql" in c for c in calls)
        assert dev_reads
    else:
        assert dev_reads == []
        assert "agent-queue" not in _project_args(calls)
        assert "DELIVERY" not in proc.stdout


def test_script_is_executable_and_parses():
    assert os.access(SCRIPT, os.X_OK)
    compile(SCRIPT.read_text(), str(SCRIPT), "exec")
