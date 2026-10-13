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
        "id": f"session-{task_id or name}", "agent_id": f"{task_id}-agent",
        "name": name, "project_id": project_id, "task_id": task_id, "state": "running",
        "desired_state": "running", "last_activity": time.time() - 3600,
        "stalled": True, "idle_seconds": 3600, "lifecycle": "task", "profile_id": "standard-claude",
        "started_at": time.time() - 7200,
    }


def _run(tmp_path, responses, *args, missing_commands=(), session_id=None):
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
    if session_id:
        env["AQ_SESSION_ID"] = session_id
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
    responses = {"aq": {
        "task list -p rom": task_response,
        "pool status --project-id rom": [],
        "session list": [],
        "wait list --project-id rom --limit 100": {"waits": [], "count": 0},
    }}
    if isinstance(task_response, list):
        for task in task_response:
            agent = task.get("assigned_agent")
            responses["aq"][f"task show {task['id']}"] = task | {
                "project_id": "rom",
                "assigned_agent": agent,
                "children": {"total": 0, "done": 0},
                "claimed_by": {"session_id": f"session-{task['id']}", "agent_id": agent,
                               "claim_epoch": 1} if agent else None,
            }
    return responses


def test_train_repair_capacity_alarm_reaches_timed_patrol(tmp_path):
    responses = _scoped_responses([_task("delegate", "READY", age_s=1900)])
    responses["aq"]["task explain --task-id delegate"] = {"reasons": [
        {"code": "awaiting_pool_session", "detail": "pool at capacity"},
        {"code": "train_repair_capacity_wait", "ref": "operation-1",
         "detail": "active train repair for batch train-1 (operation operation-1) waits"},
    ]}
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert proc.returncode == 0, proc.stderr
    assert "TRAIN-REPAIR-CAPACITY rom delegate" in proc.stdout
    assert "train-1" in proc.stdout and "operation-1" in proc.stdout
    assert "STUCK rom delegate" not in proc.stdout


def test_orphaned_assignment_without_session_is_reported(tmp_path):
    responses = _scoped_responses([
        _task("lost", "IN_PROGRESS", assigned_agent="idle-agent"),
        _task("fresh", "IN_PROGRESS", assigned_agent="new-agent", age_s=30),
        _task("container", "IN_PROGRESS", is_container=True),
        _task("busy", "IN_PROGRESS", assigned_agent="busy-agent"),
        _task("starting", "IN_PROGRESS", assigned_agent="starting-agent"),
    ])
    responses["aq"]["session list"] = [
        _session("busy-worker", "rom", "busy") | {"stalled": False},
        _session("launching", "rom", None) | {
            "state": "starting", "agent_id": "starting-agent", "stalled": False,
        },
    ]
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert proc.returncode == 0, proc.stderr
    orphaned = [line for line in proc.stdout.splitlines() if "ORPHANED" in line]
    assert len(orphaned) == 1
    assert "lost" in orphaned[0] and "idle-agent" in orphaned[0]


@pytest.mark.parametrize("failure", [
    _reply({"error": "unavailable"}, exit_code=3),
    _reply(_page([], total=1, truncated=True)),
])
def test_orphaned_requires_complete_successful_session_read(tmp_path, failure):
    responses = _scoped_responses([_task("lost", "IN_PROGRESS", assigned_agent="idle")])
    responses["aq"]["session list"] = failure
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "SWEEP-ERROR" in proc.stdout
    assert "ORPHANED" not in proc.stdout


def _waiting_responses(kind="job"):
    responses = _scoped_responses([_task("held", "IN_PROGRESS", assigned_agent="held-agent")])
    responses["aq"]["session list"] = [_session("worker", "rom", "held") | {
        "lifecycle": "pool", "claim_phase": "active", "harness": "codex",
    }]
    row = {
        "id": "wait-1", "project_id": "rom", "owner_kind": "task", "owner_id": "held",
        "session_id": "session-held", "session_instance_token": "instance-1", "claim_epoch": 1,
        "state": "active", "kind": kind, "checked_at": time.time(),
        "deadline_at": time.time() + 3600,
    }
    responses["aq"]["wait list --project-id rom --limit 100"] = {"waits": [row], "count": 1}
    responses["aq"]["wait show wait-1"] = {
        "wait": row.copy(),
        "next_step": "End this turn. The claim, workspace and seat remain held. Do not poll or sleep.",
    }
    return responses


@pytest.mark.parametrize("kind", ["job", "message"])
def test_verified_current_wait_is_live_without_heartbeat(tmp_path, kind):
    responses = _waiting_responses(kind)
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"holds verified {kind} wait wait-1" in proc.stdout
    assert "ORPHANED" not in proc.stdout and "IDLE-WORKER" not in proc.stdout
    assert ["aq", "wait", "show", "wait-1"] in calls
    assert not any(call[:3] == ["aq", "agent", "list"] for call in calls)
    assert not any(call[0] == "tmux" for call in calls)


def test_active_opencode_worker_does_not_need_heartbeat_or_wait(tmp_path):
    responses = _waiting_responses()
    responses["aq"]["session list"][0].update(
        harness="opencode", last_activity=time.time(), stalled=False,
    )
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert proc.stdout.strip() == "OK: nothing stalled"
    assert not any(call[1] in ("wait", "agent") for call in calls)


@pytest.mark.parametrize("state,assigned", [("stopped", True), ("absent", True), ("absent", False)])
def test_stopped_or_unbound_holder_is_an_orphan(tmp_path, state, assigned):
    task = _task("held", "IN_PROGRESS", **({"assigned_agent": "held-agent"} if assigned else {}))
    responses = _scoped_responses([task])
    if state == "stopped":
        responses["aq"]["session list"] = [_session("worker", "rom", "held") | {
            "state": "stopped", "desired_state": "stopped",
        }]
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert "ORPHANED rom held" in proc.stdout
    assert "retain branch and checkout" in proc.stdout
    assert "restart" not in proc.stdout and "--yes" not in proc.stdout
    assert not any(call[1] == "wait" for call in calls)


@pytest.mark.parametrize("change", [
    {"task_id": "another"}, {"agent_id": "another-agent"}, {"claim_phase": "preparing"},
    {"id": "another-session"},
])
def test_unrelated_session_cannot_clear_a_holder(tmp_path, change):
    responses = _waiting_responses()
    responses["aq"]["session list"][0].update(change)
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "HOLDER-MISMATCH rom held" in proc.stdout
    assert "ORPHANED" not in proc.stdout and "WAITING-WORKER" not in proc.stdout
    assert "OK:" not in proc.stdout


def test_sleeping_holder_is_reported_as_a_hold(tmp_path):
    responses = _waiting_responses()
    responses["aq"]["session list"][0]["desired_state"] = "sleeping"
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert "SLEEPING-WORKER rom held" in proc.stdout
    assert "ORPHANED" not in proc.stdout
    assert not any(call[1] == "wait" for call in calls)


@pytest.mark.parametrize("change", [
    {"state": "cancelled"}, {"state": "expired"}, {"deadline_at": time.time() - 60},
    {"claim_epoch": 0}, {"session_id": "old-session"},
])
def test_historical_or_expired_wait_never_clears_idle_holder(tmp_path, change):
    responses = _waiting_responses()
    responses["aq"]["wait list --project-id rom --limit 100"]["waits"][0].update(change)
    responses["aq"]["wait show wait-1"]["wait"].update(change)
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "IDLE-WORKER worker holds held" in proc.stdout
    assert "WAITING-WORKER" not in proc.stdout and "ORPHANED" not in proc.stdout


def test_server_rejected_instance_or_producer_never_clears_holder(tmp_path):
    responses = _waiting_responses()
    responses["aq"]["wait show wait-1"]["next_step"] = (
        "This active record has no current inactivity exemption. Check your claim and producer."
    )
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "IDLE-WORKER" in proc.stdout and "WAITING-WORKER" not in proc.stdout


@pytest.mark.parametrize("change", [
    {"session_instance_token": "replaced-instance"}, {"claim_epoch": 2},
    {"checked_at": time.time() - 3600}, {"checked_at": "nan"},
    {"deadline_at": "broken"}, {"owner_id": "another"},
])
def test_changed_or_stale_wait_proof_reports_unknown(tmp_path, change):
    responses = _waiting_responses()
    responses["aq"]["wait show wait-1"]["wait"].update(change)
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "HOLDER-UNKNOWN rom held" in proc.stdout
    assert all(tag not in proc.stdout for tag in ("ORPHANED", "WAITING-WORKER", "IDLE-WORKER", "OK:"))


@pytest.mark.parametrize("command,response", [
    ("session list", _reply(_page([{}]))),
    ("task show held", {"_stdout": "unreadable"}),
    ("task show held", {"id": "another"}),
    ("wait list --project-id rom --limit 100", {"waits": [{}], "count": 1}),
    ("wait list --project-id rom --limit 100", {"waits": [], "count": 0, "has_more": True}),
    ("wait list --project-id rom --limit 100", {"waits": [{}] * 100, "count": 100}),
    ("wait show wait-1", {"_stdout": "", "_exit": 3}),
    ("wait show wait-1", {"wait": {}}),
])
def test_failed_holder_evidence_neither_orphans_nor_clears(tmp_path, command, response):
    responses = _waiting_responses()
    responses["aq"][command] = response
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "SWEEP-ERROR" in proc.stdout or "HOLDER-UNKNOWN" in proc.stdout
    assert all(tag not in proc.stdout for tag in ("ORPHANED", "WAITING-WORKER", "IDLE-WORKER", "OK:"))


@pytest.mark.parametrize("command", ["session list", "task show held", "wait list --project-id rom --limit 100", "wait show wait-1"])
@pytest.mark.parametrize("code", ["out_of_scope", "forbidden", "unauthorized"])
def test_holder_authorization_denial_stops_all_later_reads(tmp_path, command, code):
    responses = _waiting_responses()
    responses["aq"][command] = _reply({
        "schema_version": 1, "data": None, "error": {"code": code, "message": "read denied"},
    }, exit_code=2)
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert proc.returncode == 2
    assert len(proc.stdout.splitlines()) == 1 and "SWEEP-AUTH" in proc.stdout
    assert calls[-1] == ["aq", *command.split()]


def test_pool_gap_does_not_name_capacity_queued_tasks_as_excluded(tmp_path):
    tasks = [_task(tid, "READY") for tid in ("eligible-a", "eligible-b", "held")]
    responses = _scoped_responses(tasks)
    for task in tasks[:2]:
        responses["aq"][f"task explain --task-id {task['id']}"] = {"reasons": [
            {"code": "route_waiting_for_compatible_agent", "detail": "all compatible agents busy"},
            {"code": "shared_capacity", "detail": "fleet at capacity"},
        ]}
    responses["aq"]["task explain --task-id held"] = {"reasons": [
        {"code": "frontier_recovery_hold", "detail": "operator recovery hold"},
    ]}
    responses["aq"]["pool status --project-id rom"] = [{
        "profile_id": "standard-claude", "enabled": True, "ready": 21, "desired": 6,
        "running_busy": 7, "projects": [{"project_id": "rom", "ready": 2}],
    }]
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    pool_line = next(line for line in proc.stdout.splitlines() if "POOL-STARVED" in line)
    assert "ready=2" in pool_line and "aggregate discrepancy" in pool_line
    assert all(tid not in pool_line for tid in ("eligible-a", "eligible-b", "held"))
    assert "off the claim frontier" not in proc.stdout
    assert "STUCK rom held" in proc.stdout and "frontier_recovery_hold" in proc.stdout


@pytest.mark.parametrize("counter", [None, -1, "2", True])
def test_invalid_scoped_ready_count_does_not_prove_a_gap(tmp_path, counter):
    responses = _scoped_responses([_task("ready", "READY")])
    responses["aq"]["task explain --task-id ready"] = {"reasons": []}
    responses["aq"]["pool status --project-id rom"] = [{
        "profile_id": "standard-claude", "enabled": True, "ready": 21,
        "projects": [{"project_id": "rom", "ready": counter}],
    }]
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "SWEEP-ERROR pool" in proc.stdout
    assert "POOL-STARVED" not in proc.stdout and "OK:" not in proc.stdout


def test_other_project_session_cannot_clear_own_holder(tmp_path):
    responses = _waiting_responses()
    responses["aq"]["session list"][0]["project_id"] = "other"
    proc, calls = _run(tmp_path, responses, "--project", "rom")
    assert "ORPHANED rom held" in proc.stdout
    assert not any("other" in call for call in calls)
    assert not any(call[1] == "wait" for call in calls)


@pytest.mark.parametrize("change", [
    {"children": None}, {"children": {}}, {"claimed_by": {}},
    {"assigned_agent": "new-holder"},
])
def test_partial_or_changed_task_identity_cannot_prove_an_orphan(tmp_path, change):
    responses = _waiting_responses()
    responses["aq"]["task show held"].update(change)
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert "HOLDER-UNKNOWN rom held" in proc.stdout
    assert "ORPHANED" not in proc.stdout and "WAITING-WORKER" not in proc.stdout


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
    assert "HOLDER-UNKNOWN s-matter-1 holds m-busy" in proc.stdout
    assert "STALE-OPEN matter m-blocked" in proc.stdout
    # Another project's blocker is never read; an undelivered one still reports
    # from the explain row alone instead of hiding.
    assert not any("o-blocker" in call or "o-done" in call for call in calls), calls
    assert "m-waits" not in proc.stdout
    assert "STUCK matter m-undelivered" in proc.stdout
    # The project's own frontier excludes m-ready even though other projects
    # keep the fleet-wide ``ready`` above zero.
    assert "POOL-STARVED standard-claude: 1 READY" in proc.stdout
    assert "aggregate discrepancy" in proc.stdout.split("POOL-STARVED", 1)[1]
    assert "m-ready" not in proc.stdout.split("POOL-STARVED", 1)[1]


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


@pytest.mark.parametrize("scoped", [False, True])
def test_expired_token_reports_one_recovery_command(tmp_path, scoped):
    response = _reply({
        "schema_version": 1, "data": None,
        "error": {"code": "out_of_scope", "message": "invalid or expired token"},
    }, exit_code=4)
    command = "task list -p rom" if scoped else "project list"
    proc, calls = _run(
        tmp_path, {"aq": {command: response}},
        *(("--project", "rom") if scoped else ()), session_id="supervisor-id",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert len(proc.stdout.splitlines()) == 1
    assert proc.stdout.count("SWEEP-AUTH") == 1
    assert "local operator" in proc.stdout
    assert "aq session token supervisor-id --json" in proc.stdout
    assert "AQ_API_TOKEN with .data.token" in proc.stdout
    assert "project-scoped supervisor token" not in proc.stdout
    assert calls == [["aq", *command.split()]]


def test_project_scope_error_keeps_project_fix(tmp_path):
    response = _reply({
        "schema_version": 1, "data": None,
        "error": {"code": "out_of_scope", "message": "project_list denied"},
    }, exit_code=4)
    proc, calls = _run(tmp_path, {"aq": {"project list": response}})
    assert proc.returncode == 2
    assert "re-run with `--project <pid>`" in proc.stdout
    assert "session token" not in proc.stdout
    assert calls == [["aq", "project", "list"]]


def test_parallel_auth_failures_report_once_and_stop_patrol(tmp_path):
    tasks = [_task(f"ready-{i}", "READY") for i in range(8)]
    responses = _scoped_responses(tasks)
    for task in tasks:
        responses["aq"][f"task explain --task-id {task['id']}"] = _reply({
            "schema_version": 1, "data": None,
            "error": {"code": "out_of_scope", "message": "invalid or expired token"},
        }, exit_code=4)
    proc, calls = _run(tmp_path, responses, "--project", "rom", session_id="supervisor-id")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert len(proc.stdout.splitlines()) == 1
    assert proc.stdout.count("SWEEP-AUTH") == 1
    assert "aq session token supervisor-id --json" in proc.stdout
    assert not any(call[1:3] in (["pool", "status"], ["session", "list"]) for call in calls)


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


@pytest.mark.parametrize("claims,phase,failures,ready,expected", [
    (0, None, 2, False, True), (0, None, 0, True, True),
    (0, None, 0, False, False), (1, None, 2, True, False),
    (0, "preparing", 0, True, False), (0, "preparing", 2, False, True),
    (0, "active", 2, True, False), (0, "claiming", 2, True, False),
])
def test_never_claimed_pool_is_reported_despite_fresh_activity(
    tmp_path, claims, phase, failures, ready, expected
):
    responses = _scoped_responses([_task("ready", "READY", age_s=30)] if ready else [])
    responses["aq"]["session list"] = [
        _session("retrying", "rom", None) | {
            "lifecycle": "pool", "stalled": False, "idle_seconds": 0,
            "claims": claims, "claim_phase": phase, "claim_failure_count": failures,
            "claim_failure_reason": "own branch handoff is unproven",
        },
    ]
    proc, _ = _run(tmp_path, responses, "--project", "rom")
    assert proc.returncode == 0, proc.stderr
    assert ("POOL-NO-CLAIM retrying" in proc.stdout) is expected
