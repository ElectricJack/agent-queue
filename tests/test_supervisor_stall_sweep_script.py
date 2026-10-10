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
import time
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
        data = responses[key]
        if isinstance(data, dict) and "_stdout" in data:
            sys.stdout.write(data["_stdout"])
            sys.exit(data.get("_exit", 0))
        body = {"schema_version": 1, "data": data}
        if isinstance(data, list):
            body["pagination"] = {"returned": len(data), "total": len(data), "truncated": False}
        print(json.dumps(body))
    else:
        print(json.dumps({"schema_version": 1, "data": None,
                          "error": {"code": "fake_missing", "message": key}}))
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


def _run(tmp_path, responses, *args, missing_commands=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    for name in COMMANDS:
        if name in missing_commands:
            continue
        shim = bin_dir / name
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} -I {fake} \"$0\" \"$@\"\n")
        shim.chmod(0o755)
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    log = tmp_path / "calls.jsonl"
    log.touch()
    env = {
        "PATH": str(bin_dir), "HOME": str(tmp_path), "AQ_API_URL": "http://127.0.0.1:9",
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


def _reply(body, *, exit_code=0):
    return {"_stdout": json.dumps(body), "_exit": exit_code}


def _page(data, **pagination):
    return {
        "schema_version": 1, "data": data,
        "pagination": {"returned": len(data), "total": len(data), "truncated": False,
                       **pagination},
    }


def _scoped_responses(task_response):
    return {"aq": {
        "task list -p rom": task_response,
        "pool status --project-id rom": [],
        "session list": [],
    }}


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


def test_empty_project_sweep_stays_in_scope_without_health_probe(tmp_path):
    responses = {"aq": {
        "task list -p rom": [],
        "pool status --project-id rom": [],
        "session list": [_session("s-other-1", "other", "o-busy")],
    }}
    proc, calls = _run(tmp_path, responses, "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(_project_args(calls)) == {"rom"}
    assert not any("agent-queue" in " ".join(call) for call in calls), calls
    assert [c for c in calls if c[0] in ("git", "docker", "bash", "tail", "tmux")] == []
    assert "s-other-1" not in proc.stdout
    assert proc.stdout.strip() == "OK: nothing stalled"


@pytest.mark.parametrize("tasks", [[], [_task("rom-new", "DEFINED", age_s=60)]])
def test_successful_task_reads_are_healthy(tmp_path, tasks):
    proc, calls = _run(tmp_path, _scoped_responses(tasks), "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "OK: nothing stalled"
    assert calls == [
        ["aq", "task", "list", "-p", "rom"],
        ["aq", "pool", "status", "--project-id", "rom"],
        ["aq", "session", "list"],
    ]


@pytest.mark.parametrize("failed_project", [None, "rom"])
def test_global_empty_queues_distinguish_a_failed_project_read(tmp_path, failed_project):
    responses = {"aq": {
        "project list": [{"id": pid, "status": "ACTIVE"} for pid in ("rom", "matter")],
        "task list -p rom": [],
        "task list -p matter": [],
        "pool status": [],
        "session list": [],
    }}
    if failed_project:
        responses["aq"][f"task list -p {failed_project}"] = _reply({
            "schema_version": 1, "data": None,
            "error": {"code": "daemon_unreachable", "message": "daemon restarting"},
        }, exit_code=3)
    proc, calls = _run(tmp_path, responses)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(_project_args(calls)) == {"rom", "matter"}
    assert all(c[0] == "aq" for c in calls)
    if failed_project:
        assert "SWEEP-ERROR aq task list -p rom: daemon_unreachable" in proc.stdout
        assert "OK: nothing stalled" not in proc.stdout
    else:
        assert proc.stdout.strip() == "OK: nothing stalled"


@pytest.mark.parametrize(("response", "reason"), [
    pytest.param({"_stdout": "", "_exit": 3}, "no JSON response (exit 3)", id="unreachable"),
    pytest.param({"_stdout": "not json"}, "no JSON response", id="non-json"),
    pytest.param(_reply([]), "malformed response envelope", id="bare-list"),
    pytest.param(_reply(None), "malformed response envelope", id="null-envelope"),
    pytest.param(_reply({"schema_version": 1}), "malformed response envelope", id="no-data"),
    pytest.param(_reply({"schema_version": 2, "data": []}),
                 "malformed response envelope", id="unknown-version"),
    pytest.param(_reply({"schema_version": 1, "data": None}),
                 "malformed list data", id="null-data"),
    pytest.param(_reply({"schema_version": 1, "data": {}}),
                 "malformed list data", id="object-data"),
    pytest.param(_reply({"schema_version": 1, "data": [], "error": "denied"}),
                 "malformed error envelope", id="malformed-error"),
    pytest.param(_reply({"schema_version": 1, "data": [],
                         "error": {"code": "forbidden", "message": "read denied"}}),
                 "forbidden: read denied", id="denied-with-empty-data"),
    pytest.param(_reply(_page([]), exit_code=3), "command failed (exit 3)", id="failed-exit"),
    pytest.param(_reply({"schema_version": 1, "data": []}),
                 "missing or malformed pagination", id="missing-pagination"),
    pytest.param(_reply(_page([], total=1, truncated=True)),
                 "incomplete list", id="empty-incomplete"),
    pytest.param(_reply(_page([_task("rom-new", "DEFINED", age_s=60)], total=2)),
                 "incomplete list", id="populated-incomplete"),
    pytest.param(_reply(_page([], truncated=True)), "incomplete list", id="truncated-flag"),
    pytest.param(_reply(_page([], returned=1)), "malformed pagination", id="wrong-returned"),
    pytest.param(_reply(_page([], total=-1)), "malformed pagination", id="negative-total"),
    pytest.param(_reply(_page([], total="0")), "malformed pagination", id="invalid-total"),
    pytest.param(_reply(_page([], truncated="false")),
                 "malformed pagination", id="invalid-truncated"),
    pytest.param(_reply(_page([None])), "malformed list row", id="invalid-row"),
    pytest.param(_reply(_page([{}])), "malformed task row", id="missing-task-fields"),
    pytest.param(_reply(_page([_task("rom-new", "DEFINED", updated_at="broken")])),
                 "malformed task timestamp", id="invalid-timestamp"),
    pytest.param(_reply(_page([_task("rom-new", "DEFINED", updated_at="nan")])),
                 "malformed task timestamp", id="non-finite-timestamp"),
])
def test_failed_task_reads_are_incomplete_and_stay_scoped(tmp_path, response, reason):
    proc, calls = _run(tmp_path, _scoped_responses(response), "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SWEEP-ERROR aq task list -p rom: {reason}" in proc.stdout
    assert "this check is incomplete" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout
    assert set(_project_args(calls)) == {"rom"}
    assert all(c[0] == "aq" for c in calls)
    assert not any("task-id" in arg for call in calls for arg in call)


def test_out_of_scope_read_preserves_auth_exit(tmp_path):
    response = _reply({
        "schema_version": 1, "data": [],
        "error": {"code": "out_of_scope", "message": "task_list denied"},
    }, exit_code=2)
    proc, calls = _run(tmp_path, _scoped_responses(response), "--project", "rom")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "SWEEP-AUTH aq task list -p rom: task_list denied" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout
    assert calls == [["aq", "task", "list", "-p", "rom"]]


def test_unavailable_cli_is_incomplete(tmp_path):
    proc, calls = _run(tmp_path, {}, "--project", "rom", missing_commands=("aq",))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP-ERROR aq task list -p rom: read unavailable (FileNotFoundError)" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout
    assert calls == []


def test_read_timeout_is_incomplete(tmp_path):
    launcher = """\
import runpy, subprocess, sys
from unittest.mock import patch
sys.argv = sys.argv[1:]
with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("aq", 60)):
    runpy.run_path(sys.argv[0], run_name="__main__")
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", launcher, str(SCRIPT), "--project", "rom"],
        capture_output=True, text=True, timeout=10, cwd=tmp_path, check=False,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP-ERROR aq task list -p rom: read timed out after 60s" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout


@pytest.mark.parametrize("command", ["pool status --project-id rom", "session list"])
def test_failed_auxiliary_reads_do_not_print_ok(tmp_path, command):
    responses = _scoped_responses([])
    responses["aq"][command] = {"_stdout": "", "_exit": 3}
    proc, _calls = _run(tmp_path, responses, "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SWEEP-ERROR aq {command}: no JSON response (exit 3)" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout


@pytest.mark.parametrize(("command", "response", "reason"), [
    ("pool status --project-id rom", {}, "malformed pool data"),
    ("session list", _reply({"schema_version": 1, "data": {}}), "malformed list data"),
])
def test_malformed_auxiliary_reads_do_not_print_ok(tmp_path, command, response, reason):
    responses = _scoped_responses([])
    responses["aq"][command] = response
    proc, _calls = _run(tmp_path, responses, "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"SWEEP-ERROR aq {command}: {reason}" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout


def test_malformed_project_read_does_not_hide_active_queues(tmp_path):
    responses = {"aq": {"project list": [{}], "pool status": [], "session list": []}}
    proc, calls = _run(tmp_path, responses)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SWEEP-ERROR aq project list: malformed project row" in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout
    assert not any(c[:3] == ["aq", "task", "list"] for c in calls)


def test_missing_verifier_parent_is_a_finding_not_a_failed_read(tmp_path):
    responses = _scoped_responses([
        _task("verify-g1", "BLOCKED", title="Verify aggregate for gone"),
    ])
    responses["aq"].update({
        "task explain --task-id verify-g1": {"reasons": []},
        "task get --task-id verify-g1": {"children": {}},
        "task get --task-id gone": _reply({
            "schema_version": 1, "data": None,
            "error": {"code": "not_found", "message": "task not found"},
        }, exit_code=1),
    })
    proc, _calls = _run(tmp_path, responses, "--project", "rom")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ORPHAN-VERIFIER rom verify-g1" in proc.stdout
    assert "SWEEP-ERROR" not in proc.stdout
    assert "OK: nothing stalled" not in proc.stdout


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
