#!/usr/bin/env python3
"""Report stalled work and paused projects holding finished AQ work.

Read-only. Prints one line per finding, or "OK: nothing stalled". Written
2026-09-21 after the development-publisher stall hid behind tasks closing
"pass" for three days (vault projects/agent-queue/notes/
integration-pipeline-incident-2026-09-20.md). The supervisor session runs it on
a timer and acts on what it prints.
"""
import datetime
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

NOW = time.time()
READY_STALE_S = 20 * 60
DEFINED_STALE_S = 30 * 60
SESSION_IDLE_S = 15 * 60
DELIVERY_STALE_S = 6 * 3600
STALE_OPEN_S = 2 * 3600  # BLOCKED/PAUSED/FAILED this long needs a premise check, not a skip
DAEMON_CHECKOUT = "/home/jkern/dev/agent-queue2"
LOG_PATH = "/home/jkern/.agent-queue/logs/agent-queue.log"
AUTO_DEPLOY_LOG = os.environ.get(
    "AQ_AUTO_DEPLOY_LOG", "/home/jkern/.agent-queue/logs/aq-auto-deploy.log")
AUTO_DEPLOY_STALL_S = 30 * 60  # six 5-minute auto-deploy runs
AUTO_DEPLOY_GAP_S = 15 * 60  # skips further apart than this are separate stalls
VALIDATION_CONTAINER = "aq-triage-test-20260831"
DESIGN_HINT = re.compile(r"\b(designs?|specs?|architecture|plans?|roadmaps?)\b", re.I)
PANE_TROUBLE = re.compile(
    r"usage limit|Not logged in|login-required|log in|rate limit|hit your", re.I
)
# Terminal rows that are settled but cannot be archived until the
# archive-with-integration-history spec (swift-orbit.1, awaiting Jack's
# approval) is implemented. Skipped only while they stay FAILED.
SETTLED_FAILED = {
    # Retired delegate of cancelled batch 2c484c08; owner released 2026-09-22.
    "repair-repair-batch-integration-batch-2c484c0890ebecbbf98b0b12fd5469ff-1",
    # keen-harbor verifier, retired as cancelled; keen-harbor delivered 09-09.
    "verify-81d0aaee-0c3a-482c-b04c-d3afe6631cbe",
}

# Intentional holds: BLOCKED on purpose, waiting on a human decision that no
# amount of supervisor action can supply. Reported once per sweep as HOLD so
# they stay visible without masquerading as stalls.
HOLDS = {
    # 2026-09-24. Drops push:main from CI. Safe only after the publisher's
    # validation runs the FULL suite; supervisor releases it then.
    "crisp-forge": "awaiting train cutover (smart-stone): main went green 04:3xZ 09-24, but dropping push-to-main CI before PR/train gating would leave main untested",
    # 2026-10-04. Closed blocked correctly: 0/16 old engine modules deletable
    # until the operator cutover (Jack). Recovery incident recovery-56961fc6 held.
    "quick-glacier-88.5": "awaiting Jack's P4 operator cutover (engine-transfer + subjects_overdue zero); recovery held",
}

findings = []
_auth_lock = Lock()
_auth_failed = False


def aq(*args, timeout=60, missing_ok=False):
    global _auth_failed
    with _auth_lock:
        if _auth_failed:
            raise SystemExit(2)

    def failed(reason):
        findings.append(
            f"SWEEP-ERROR aq {' '.join(args)}: {reason} -> this check is incomplete"
        )
        return None

    try:
        proc = subprocess.run(
            ["aq", "--json", *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return failed(f"read timed out after {timeout}s")
    except (subprocess.SubprocessError, OSError) as exc:
        return failed(f"read unavailable ({type(exc).__name__})")
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return failed(f"no JSON response (exit {proc.returncode})")
    if not isinstance(body, dict):
        return failed("malformed response envelope")
    err = body.get("error")
    if err is not None and not isinstance(err, dict):
        return failed("malformed error envelope")
    if err is not None and err.get("code") in {"out_of_scope", "forbidden", "unauthorized"}:
        # An expired session token makes every call fail; never report "OK" then.
        message = str(err.get("message", ""))
        if "invalid or expired token" in message.lower() or "session token required" in message.lower():
            session = shlex.quote(os.environ.get("AQ_SESSION_ID") or "<session-id>")
            hint = (
                f"local operator: run `aq session token {session} --json`, then replace "
                "this session's AQ_API_TOKEN with .data.token; stop this patrol until restored"
            )
        elif args[:2] == ("project", "list") and not ONLY:
            hint = "project-scoped supervisor token: re-run with `--project <pid>`"
        else:
            hint = "check this supervisor's project scope and capability grants"
        # Explain reads run concurrently: only the first auth failure is useful.
        with _auth_lock:
            if not _auth_failed:
                _auth_failed = True
                print(f"SWEEP-AUTH aq {' '.join(args)}: {message} -> {hint}")
        sys.exit(2)
    if err is not None and missing_ok and "not found" in str(err.get("message", "")).lower():
        # A referenced task that was deleted is a finding, not an incomplete sweep.
        return {"_missing": True}
    if err is not None:
        return failed(f"{err.get('code')}: {str(err.get('message'))[:120]}")
    if proc.returncode:
        return failed(f"command failed (exit {proc.returncode})")
    if (
        type(body.get("schema_version")) is not int
        or body["schema_version"] != 1 or "data" not in body
    ):
        return failed("malformed response envelope")
    data = body["data"]
    list_read = args[:2] in {("project", "list"), ("task", "list"), ("session", "list")}
    if list_read and not isinstance(data, list):
        return failed("malformed list data")
    if args[:2] in {
        ("task", "get"), ("task", "show"), ("task", "explain"),
        ("wait", "list"), ("wait", "show"),
    } and not isinstance(data, dict):
        return failed("malformed object data")
    if not isinstance(data, (list, dict)):
        return failed("malformed data")
    if isinstance(data, dict) and data.get("success") is False:
        return failed("unsuccessful observation")
    if isinstance(data, list):
        if any(not isinstance(row, dict) for row in data):
            return failed("malformed list row")
        page = body.get("pagination")
        if not isinstance(page, dict):
            return failed("missing or malformed pagination")
        returned, total, truncated = (page.get(key) for key in ("returned", "total", "truncated"))
        if (
            type(returned) is not int or type(total) is not int
            or returned != len(data) or total < returned or type(truncated) is not bool
        ):
            return failed("malformed pagination")
        if truncated or total > returned:
            return failed(f"incomplete list: returned {returned} of {total} rows")
        if args[:2] == ("project", "list") and any(
            not isinstance(row.get(key), str) for row in data for key in ("id", "status")
        ):
            return failed("malformed project row")
        if args[:2] == ("task", "list"):
            for row in data:
                if any(not isinstance(row.get(key), str) for key in ("id", "status", "title")):
                    return failed("malformed task row")
                try:
                    timestamp = float(row["updated_at"])
                except (KeyError, TypeError, ValueError):
                    return failed("malformed task timestamp")
                if not math.isfinite(timestamp):
                    return failed("malformed task timestamp")
    if args[:2] == ("pool", "status") and isinstance(data, dict):
        rows = data.get("pools")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return failed("malformed pool data")
    if args[:2] == ("task", "explain"):
        reasons = data.get("reasons")
        if not isinstance(reasons, list) or any(
            not isinstance(row, dict)
            or any(not isinstance(row.get(key), str) for key in ("code", "detail"))
            for row in reasons
        ):
            return failed("malformed explain reasons")
    if args[:2] == ("integration", "status"):
        # The legacy subject control projection has no readiness blockers.
        if isinstance(data, dict) and data.get("projection_kind") == "subjects":
            data.setdefault("blockers", [])
        if (not isinstance(data, dict) or data.get("outcome") != "status"
                or data.get("project_id") != args[2]
                or not isinstance(data.get("blockers"), list)
                or any(not isinstance(row, dict)
                       or any(not isinstance(row.get(key), str) for key in ("code", "detail"))
                       for row in data["blockers"])):
            return failed("malformed integration status")
    if args[:2] == ("session", "list") and any(
        any(not isinstance(row.get(key), str) for key in ("id", "name", "state"))
        or not {"project_id", "task_id", "agent_id", "desired_state"}.issubset(row)
        or row.get("project_id") is not None and not isinstance(row["project_id"], str)
        for row in data
    ):
        return failed("malformed session row")
    if args[:2] == ("wait", "list"):
        rows = data.get("waits")
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or type(data.get("count")) is not int or data["count"] != len(rows)):
            return failed("malformed wait inventory")
        # This command has no total/has_more. At its requested cap, absence
        # cannot be proved. Do not page indefinitely or infer an empty history.
        if (len(rows) >= 100 or data.get("has_more") or data.get("truncated")
                or "pagination" in body):
            return failed("possibly incomplete wait inventory (100-row limit)")
    if args[:2] == ("wait", "show") and (
        not isinstance(data.get("wait"), dict) or not isinstance(data.get("next_step"), str)
    ):
        return failed("malformed wait validity response")
    if args[:2] in {("wait", "list"), ("wait", "show")}:
        rows = data["waits"] if args[1] == "list" else [data["wait"]]
        for row in rows:
            if (any(not isinstance(row.get(key), str) or not row[key] for key in (
                "id", "project_id", "owner_kind", "owner_id", "session_id",
                "session_instance_token", "state", "kind",
            )) or type(row.get("claim_epoch")) is not int
                    or row["claim_epoch"] < 0
                    or row["state"] not in {"active", "satisfied", "expired", "cancelled"}
                    or row["kind"] not in {"job", "task", "message", "timer"}):
                return failed("malformed wait identity")
    return data


def sh(cmd, timeout=30):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def age(ts):
    minutes = (NOW - float(ts)) / 60
    return f"{minutes / 60:.1f}h" if minutes >= 90 else f"{minutes:.0f}m"


DELIVERY_LAG_S = 45 * 60
# Reasons that just mean "queued behind its blocker"; anything else still reports.
WAITING_CODES = {"blocked_dependency", "route_waiting_for_compatible_agent", "awaiting_pool_session"}


def foreign_blocker(detail):
    """True when a scoped sweep must not read this blocker: it lives in another project.

    ``aq task explain`` ends a cross-project edge's detail with ``in project '<id>'``.
    """
    m = re.search(r" in project '([^']+)'$", detail)
    return bool(ONLY and m and m.group(1) != ONLY)


def completed_recently(detail):
    """True when the blocker named in a blocked_dependency detail finished < DELIVERY_LAG_S ago."""
    m = re.search(r"dep '([^']+)'", detail)
    # Another project's completion time is out of a scoped sweep's reach, so
    # its undelivered blocker reports rather than hides.
    if not m or foreign_blocker(detail):
        return False
    blocker = aq("task", "get", "--task-id", m.group(1)) or {}
    done = (blocker.get("completion") or {}).get("completed_at")
    return bool(done) and NOW - float(done) < DELIVERY_LAG_S


BLOCKER_STALE_S = 30 * 60
_blocker_cache: dict[str, dict] = {}
_blockers_reported: set[str] = set()


def stalled_blockers(dep_rows):
    """One note per unfinished blocker that has not moved for BLOCKER_STALE_S."""
    notes = []
    for r in dep_rows:
        m = re.search(r"dep '([^']+)'", r["detail"])
        # The owning project's sweep chases its own blocker.
        if not m or foreign_blocker(r["detail"]):
            continue
        bid = m.group(1)
        if bid not in _blocker_cache:
            _blocker_cache[bid] = aq("task", "get", "--task-id", bid) or {}
        b = _blocker_cache[bid]
        bstatus = b.get("status", "?")
        if bstatus in ("COMPLETED", "IN_PROGRESS", "ASSIGNED") or not b.get("updated_at"):
            continue
        if NOW - float(b["updated_at"]) < BLOCKER_STALE_S:
            continue
        why = explain(bid)
        brows = (why.get("reasons") or []) if isinstance(why, dict) else []
        # A blocker that is itself only queued behind another task is a link in
        # a chain; the chain's root reports on its own line. READY blockers are
        # covered by POOL-STARVED. Report each root blocker once per sweep.
        if bstatus in ("DEFINED", "READY") or bid in _blockers_reported:
            continue
        _blockers_reported.add(bid)
        cause = "; ".join(f"{x['code']}: {x['detail'][:70]}" for x in brows[:2]) or "no reason reported"
        notes.append(f"waits on {bid} [{bstatus} {age(b['updated_at'])}] -> {cause}")
    return notes


# A per-project supervisor token reads only its own project; `--project PID`
# sweeps just that one instead of tripping SWEEP-AUTH on the first other one.
ONLY = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else None
# 2026-10-09: the development publisher, its validation database and
# DAEMON_CHECKOUT belong to agent-queue. matter-engine-cpp's `--project` patrol
# was reading agent-queue's tasks and database through those checks, so they
# run only when agent-queue itself is in the sweep and ACTIVE.
DEV_PROJECT = "agent-queue"
# 2026-10-04: after the scope fix deployed, `aq project list` names no
# project-owned target and is refused for a project-scoped token. With
# --project there is nothing to enumerate, so sweep just that project.
projects = [{"id": ONLY, "status": "ACTIVE"}] if ONLY else (aq("project", "list") or [])
# Scoped patrols cannot enumerate projects. Integration status resolves the real
# pause state, and keeps the same authorization/completeness checks as other reads.
for project in projects:
    if ONLY or project["status"] == "PAUSED":
        status = aq("integration", "status", project["id"], "--control-only")
        if status is not None:
            for blocker in status["blockers"]:
                if blocker["code"] == "project_paused":
                    findings.append(f"PAUSED-PROJECT {project['id']}: {blocker['detail']}")
VERIFIER_TITLE = re.compile(r"Verify aggregate for (\S+)$")
open_containers: list[str] = []
superseded_verifiers: list[str] = []
active = [
    p["id"] for p in projects
    if str(p.get("status")).upper() == "ACTIVE" and ONLY in (None, p["id"])
]
DEV_CHECKS = DEV_PROJECT in active
ready_by_profile = Counter()
candidates: list[tuple[str, dict]] = []
tasks_by_project: dict[str, list[dict]] = {}

for pid in active:
    project_tasks = aq("task", "list", "-p", pid)
    if project_tasks is None:
        continue
    # The CLI's successful, complete empty list means there is no open work.
    # Transport, envelope and pagination failures are recorded by aq(), never
    # inferred from queue size or a separate health endpoint.
    tasks_by_project[pid] = project_tasks
    for t in project_tasks:
        tid, status, prof = t["id"], t["status"], t.get("profile_id") or "-"
        stale = NOW - float(t["updated_at"])
        if status == "READY":
            ready_by_profile[prof] += 1
        if status in ("READY", "DEFINED") and prof == "deep-high-claude" and not (
            t.get("task_type") in ("plan", "research") or DESIGN_HINT.search(t["title"])
        ):
            findings.append(f"FABLE-NONDESIGN {pid} {tid} [{status}] {t['title'][:70]}")
        if (
            (status == "READY" and stale > READY_STALE_S)
            or (status == "DEFINED" and stale > DEFINED_STALE_S)
            # A terminal row never becomes unstuck on its own: surface it on
            # the first sweep, not after some staleness window.
            or status in ("BLOCKED", "FAILED", "PAUSED")
        ):
            candidates.append((pid, t))

# `aq task explain` is one HTTP round trip each against a daemon that is often
# busy; doing them serially took the sweep past 5 minutes on 2026-09-22 (it had
# been ~45s). Fan them out and cache, so repeat ids cost nothing.
_explain_cache: dict[str, dict] = {}


def explain(tid):
    if tid not in _explain_cache:
        _explain_cache[tid] = aq("task", "explain", "--task-id", tid)
    return _explain_cache[tid]


with ThreadPoolExecutor(max_workers=8) as pool:
    for tid, res in zip(
        [c[1]["id"] for c in candidates],
        pool.map(lambda c: aq("task", "explain", "--task-id", c[1]["id"]), candidates),
    ):
        _explain_cache[tid] = res

for pid, t in candidates:
        tid, status, prof = t["id"], t["status"], t.get("profile_id") or "-"
        if status in ("READY", "DEFINED"):
            why = explain(tid)
            if why is None:
                continue
            rows = (why.get("reasons") or []) if isinstance(why, dict) else []
            repair_alarm = next(
                (r for r in rows if r["code"] == "train_repair_capacity_wait"), None
            )
            if repair_alarm is not None:
                findings.append(
                    f"TRAIN-REPAIR-CAPACITY {pid} {tid} "
                    f"[{status} {age(t['updated_at'])} {prof}] {repair_alarm['detail']}"
                )
                continue
            deps = [r for r in rows if r["code"] == "blocked_dependency"]
            # Waiting on an unfinished blocker is normal. A COMPLETED blocker
            # means its branch has not been delivered to main yet; that is
            # only a stall once the blocker has sat undelivered longer than a
            # normal publisher cycle plus validation.
            if deps and not any("status=COMPLETED" in r["detail"] for r in deps):
                # Waiting on an unfinished blocker is normal only while that
                # blocker is moving. 2026-09-26: a supervisor-added chain sat
                # behind a blocker dead for over an hour and every sweep
                # skipped it here. Chase the blocker and say why it is stuck.
                for note in stalled_blockers(deps):
                    findings.append(f"BLOCKER-STALLED {pid} {tid} [{status} {age(t['updated_at'])}] {note}")
                continue
            if deps and all(
                completed_recently(r["detail"]) for r in deps if "status=COMPLETED" in r["detail"]
            ) and all(r["code"] in WAITING_CODES for r in rows):
                continue
            # 2026-10-03: a task held by an open *human* gate is waiting on a
            # person, not stalled; its frontier/route reasons are side effects
            # of the gate. Report it as a hold naming the gate. Any other gate
            # type (timer, ci-run, routing...) still reports STUCK.
            human_gates = sorted({
                r.get("ref") or r["detail"].split("'")[1]
                for r in rows
                if r["code"] == "blocked_gate" and "(human:" in r["detail"]
                and "status=open" in r["detail"]
            })
            if human_gates:
                findings.append(
                    f"HOLD-GATE {pid} {tid} [{status} {age(t['updated_at'])}] waits on human "
                    f"gate {', '.join(human_gates)}"
                )
                continue
            reasons = "; ".join(f"{r['code']}: {r['detail'][:90]}" for r in rows[:2])
            findings.append(
                f"STUCK {pid} {tid} [{status} {age(t['updated_at'])} {prof}] {reasons}"
            )
        elif status in ("BLOCKED", "FAILED", "PAUSED"):
            if status == "FAILED" and tid in SETTLED_FAILED:
                continue
            if tid in HOLDS:
                findings.append(
                    f"HOLD {pid} {tid} [{age(t['updated_at'])}] {HOLDS[tid]}"
                )
                continue
            # 2026-09-26: two epics sat BLOCKED/PAUSED for 10-13h with every
            # child done, and two obsolete BLOCKED tasks lingered all day,
            # because patrols read these rows as "known noise". Diagnose each
            # one instead of listing it bare.
            kids = ((aq("task", "get", "--task-id", tid) or {}).get("children") or {})
            if kids.get("total") and kids.get("done") == kids.get("total"):
                findings.append(
                    f"EPIC-DONE {pid} {tid} [{status} {age(t['updated_at'])}] all {kids['total']} "
                    f"children done -> verify their branches are on main, then complete the epic"
                )
                continue
            # 2026-10-03: phase epics stay PAUSED as organizational containers
            # while their children run, and each child is swept on its own
            # line (STUCK / BLOCKER-STALLED). Five such epics took five
            # STALE-OPEN lines on every patrol. List them compactly instead.
            if status == "PAUSED" and kids.get("total"):
                open_containers.append(f"{tid}({kids['done']}/{kids['total']})")
                continue
            # 2026-10-03: a failed aggregate verifier stays BLOCKED after a later
            # generation verifies and completes its parent. Train mode refuses
            # to retire it, so it is history, not work: list it compactly once
            # the parent it verified is COMPLETED.
            m = VERIFIER_TITLE.match(t["title"]) if tid.startswith("verify-") else None
            if status in ("BLOCKED", "FAILED") and m:
                parent = (aq("task", "get", "--task-id", m.group(1), missing_ok=True) or {})
                parent = parent.get("task", parent)
                if parent.get("_missing"):
                    findings.append(
                        f"ORPHAN-VERIFIER {pid} {tid} [{status} {age(t['updated_at'])}] parent {m.group(1)} no longer exists"
                        " -> close or archive this verifier"
                    )
                    continue
                if parent.get("status") == "COMPLETED":
                    superseded_verifiers.append(tid[:15] + ("…" + tid[-3:] if "-g" in tid[-4:] else ""))
                    continue
            tag = "STALE-OPEN" if NOW - float(t["updated_at"]) > STALE_OPEN_S else status
            findings.append(
                f"{tag} {pid} {tid} [{status} {age(t['updated_at'])} {prof}] {t['title'][:60]}"
                + (" -> does its premise still hold? fix it, or close it as obsolete"
                   if tag == "STALE-OPEN" else "")
            )

# The pool only counts READY tasks on its claim frontier. A task can show READY
# yet be excluded (missing or stale delivery receipt, is_blocked, a hold
# label), and then no worker ever takes it while the pool reports ready=0.
stale_ready = Counter()
for pid, t in candidates:
    if t["status"] == "READY" and not t.get("assigned_agent_id"):
        prof = t.get("profile_id") or "-"
        stale_ready[prof] += 1
# `--project-id` filters only the per-project rows; a pool's own counters stay
# fleet-wide, so a scoped sweep compares against its project's frontier.
pools = aq("pool", "status", *(("--project-id", ONLY) if ONLY else ())) or []
for p in pools if isinstance(pools, list) else pools.get("pools", []):
    prof = p.get("profile_id")
    if not p.get("enabled"):
        continue
    projects = p.get("projects")
    if not isinstance(projects, list) or any(not isinstance(proj, dict) for proj in projects):
        findings.append(f"SWEEP-ERROR pool {prof}: malformed project counters -> this check is incomplete")
        continue
    own = [proj for proj in projects if ONLY in (None, proj.get("project_id"))]
    counts = [proj.get("ready") for proj in own] if ONLY else [p.get("ready")]
    if (ONLY and len(own) != 1) or any(type(n) is not int or n < 0 for n in counts):
        findings.append(f"SWEEP-ERROR pool {prof}: missing or malformed ready counters -> this check is incomplete")
        continue
    seen = sum(counts)
    gap = stale_ready.get(prof, 0) - seen
    if gap > 0:
        findings.append(
            f"POOL-STARVED {prof}: {stale_ready[prof]} READY >{READY_STALE_S // 60}m but pool sees "
            f"ready={seen} (desired {p.get('desired')}, busy {p.get('running_busy')}) "
            "-> aggregate discrepancy; observations may have changed; "
            "task exclusions require current per-task frontier evidence"
        )
    for proj in own:
        busy = (proj.get("running_busy") or 0) + (proj.get("running_idle") or 0) + (proj.get("starting") or 0)
        drain = proj.get("draining") or 0
        cap = proj.get("workspace_capacity")
        if drain and cap and busy + drain >= cap:
            findings.append(
                f"DRAIN-HOG {prof} {proj['project_id']}: {drain} draining sessions hold worktrees "
                f"({busy} live + {drain} draining >= capacity {cap})"
            )

# A project-scoped token lists only its own sessions; a global one running
# `--project` lists every project's, so keep the project's rows here.
session_rows = aq("session", "list")
sessions = [s for s in session_rows or [] if ONLY in (None, s.get("project_id"))]
_wait_inventory = {}
checked_holders = set()


def wait_evidence(pid, task, holder, session):
    """Use wait_get's canonical instance/producer check, never a polling policy."""
    if pid not in _wait_inventory:
        _wait_inventory[pid] = aq("wait", "list", "--project-id", pid, "--limit", "100")
    inventory = _wait_inventory[pid]
    if inventory is None:
        return "unknown", None
    for row in inventory["waits"]:
        if row.get("project_id") != pid or row.get("owner_kind") != "task":
            continue
        if row.get("owner_id") != task["id"]:
            continue
        if row.get("state") != "active":
            continue
        if (row.get("session_id") != session["id"]
                or row.get("claim_epoch") != holder["claim_epoch"]):
            continue
        if not isinstance(row.get("id"), str):
            return "unknown", None
        proof = aq("wait", "show", row["id"])
        if proof is None:
            return "unknown", None
        current = proof["wait"]
        if any(current.get(key) != value for key, value in {
            "id": row["id"], "project_id": pid, "owner_kind": "task",
            "owner_id": task["id"], "session_id": session["id"],
            "claim_epoch": holder["claim_epoch"],
            "session_instance_token": row.get("session_instance_token"),
        }.items()):
            return "unknown", None
        if current.get("state") != "active":
            continue
        try:
            checked, deadline = float(current["checked_at"]), float(current["deadline_at"])
        except (KeyError, TypeError, ValueError):
            return "unknown", None
        now = time.time()
        if not math.isfinite(checked) or not math.isfinite(deadline):
            return "unknown", None
        if deadline <= now:
            continue
        if not 0 <= now - checked < SESSION_IDLE_S:
            return "unknown", None
        # wait_get replaces this public guidance when blocking_wait_for rejects
        # the current claim, instance or producer. checked_at alone is not proof.
        if (current.get("session_instance_token")
                and "The claim, workspace and seat remain held." in proof["next_step"]):
            return "valid", current
    return "none", None


# An empty successful list proves absence; a failed/truncated read does not.
# Read current claim identity before classifying an old task-list observation.
if session_rows is not None:
    for pid, project_tasks in tasks_by_project.items():
        for t in project_tasks:
            if (t["status"] != "IN_PROGRESS" or t.get("is_container")
                    or t.get("task_type") in ("epic", "container")
                    or NOW - float(t["updated_at"]) <= SESSION_IDLE_S):
                continue
            task = aq("task", "show", t["id"])
            if task is None:
                continue
            prefix = f"{pid} {t['id']} [IN_PROGRESS {age(t['updated_at'])}]"
            if (task.get("id") != t["id"] or task.get("project_id") != pid
                    or task.get("status") != "IN_PROGRESS" or "claimed_by" not in task
                    or not {"assigned_agent", "assigned_agent_id"}.intersection(task)
                    or not isinstance(task.get("children"), dict)
                    or type(task["children"].get("total")) is not int
                    or task["children"]["total"] < 0):
                findings.append(f"HOLDER-UNKNOWN {prefix}: task observation changed or is unreadable")
                continue
            holder = task["claimed_by"]
            assigned = task.get("assigned_agent") or task.get("assigned_agent_id")
            if assigned != (t.get("assigned_agent") or t.get("assigned_agent_id")):
                findings.append(f"HOLDER-UNKNOWN {prefix}: assignment changed during the sweep")
                continue
            if not holder and not assigned and (
                task.get("is_container") or task.get("task_type") in ("epic", "container")
                or (task.get("children") or {}).get("total")
            ):
                continue
            if holder is None and not assigned:
                findings.append(f"ORPHANED {prefix}: no current holder -> inspect recovery; retain branch and checkout")
                continue
            if (not isinstance(holder, dict)
                    or not {"session_id", "agent_id", "claim_epoch"}.issubset(holder)
                    or not holder.get("agent_id")
                    or holder["agent_id"] != assigned
                    or type(holder.get("claim_epoch")) is not int
                    or holder["claim_epoch"] < 0
                    or holder.get("session_id") is not None
                    and (not isinstance(holder["session_id"], str) or not holder["session_id"])):
                findings.append(f"HOLDER-UNKNOWN {prefix}: inconsistent claim identity")
                continue
            matches = [s for s in sessions if s.get("project_id") == pid and (
                s["id"] == holder.get("session_id")
                if holder.get("session_id") else s.get("agent_id") == assigned
            )]
            related = [s for s in sessions if s.get("project_id") == pid
                       and (s.get("task_id") == t["id"] or s.get("agent_id") == assigned)
                       and s["state"] not in ("stopped", "failed")]
            if not matches and related:
                checked_holders.update(s["id"] for s in related)
                findings.append(f"HOLDER-MISMATCH {prefix}: live session differs from the current claim")
                continue
            if not matches or all(s["state"] in ("stopped", "failed") for s in matches):
                findings.append(f"ORPHANED {prefix}: assigned agent {assigned} has no live session -> inspect recovery; retain branch and checkout")
                continue
            if len(matches) != 1:
                findings.append(f"HOLDER-UNKNOWN {prefix}: ambiguous session identity")
                continue
            s = matches[0]
            checked_holders.add(s["id"])
            if (s.get("task_id") != t["id"] or s.get("agent_id") != assigned
                    or s.get("lifecycle") == "pool" and s.get("claim_phase") != "active"):
                findings.append(f"HOLDER-MISMATCH {prefix}: session {s['id']} is bound to another task or claim")
                continue
            if s.get("desired_state") == "sleeping" or s["state"] == "sleeping":
                findings.append(f"SLEEPING-WORKER {prefix}: session {s['id']} retains its holder")
                continue
            if s["state"] not in ("starting", "running", "draining") or s.get("desired_state") != "running":
                findings.append(f"HOLDER-UNKNOWN {prefix}: session {s['id']} state/intent needs inspection")
                continue
            try:
                last = float(s.get("last_activity") or s["started_at"])
            except (KeyError, TypeError, ValueError):
                last = math.nan
            if not math.isfinite(last) or last > time.time():
                findings.append(f"HOLDER-UNKNOWN {prefix}: unreadable session activity")
                continue
            if time.time() - last < SESSION_IDLE_S:
                continue
            evidence, wait = wait_evidence(pid, task, holder, s)
            if evidence == "valid":
                findings.append(f"WAITING-WORKER {prefix}: session {s['id']} holds verified {wait['kind']} wait {wait['id']}")
            elif evidence == "unknown":
                findings.append(f"HOLDER-UNKNOWN {prefix}: wait evidence is incomplete or stale")
            else:
                findings.append(f"IDLE-WORKER {s['name']} holds {t['id']}: live holder has no verified wait; inspect activity and recovery")
for s in sessions:
    failures = s.get("claim_failure_count") or 0
    phase = s.get("claim_phase")
    matching_ready = sum(
        t["status"] == "READY" and t.get("profile_id") == s.get("profile_id")
        for t in tasks_by_project.get(s.get("project_id"), [])
    )
    if (s.get("state") in ("running", "starting") and s.get("lifecycle") == "pool"
            and s.get("claims") == 0 and phase not in ("active", "claiming")
            and (phase != "preparing" or failures) and s.get("started_at")
            and NOW - float(s["started_at"]) >= SESSION_IDLE_S
            and (matching_ready or failures)):
        findings.append(
            f"POOL-NO-CLAIM {s['name']} activated no claim in {age(s['started_at'])}; "
            f"{matching_ready} READY on {s.get('profile_id')}; "
            f"{failures} identical preparation refusals"
            + (f": {str(s.get('claim_failure_reason') or '')[:200]}" if failures else "")
        )
    if s.get("state") not in ("running", "starting") or not s.get("stalled"):
        continue
    if s["id"] in checked_holders:
        continue
    if (s.get("idle_seconds") or 0) < SESSION_IDLE_S:
        continue
    if s.get("task_id") and s.get("lifecycle") != "named":
        # A session omitted from current task evidence cannot prove a dead or
        # idle worker. Fresh task rows need no stall diagnosis this sweep.
        task = next((t for t in tasks_by_project.get(s.get("project_id"), [])
                     if t["id"] == s["task_id"]), None)
        if task is None:
            findings.append(f"HOLDER-UNKNOWN {s['name']} holds {s['task_id']}: current task evidence unavailable")
        continue
    if s.get("lifecycle") != "pool":
        continue
    if not ready_by_profile.get(s.get("profile_id")):
        continue
    pane = sh(["tmux", "capture-pane", "-p", "-t", s["name"]])
    trouble = [line.strip() for line in pane.splitlines() if PANE_TROUBLE.search(line)]
    findings.append(
        f"IDLE-POOL {s['name']} idle {s['idle_seconds'] / 60:.0f}m with "
        f"{ready_by_profile[s['profile_id']]} READY on {s['profile_id']}"
        + (f" | pane: {trouble[-1][:120]}" if trouble else "")
    )

recent = Counter(
    s["name"] for s in sessions
    if s.get("name", "").startswith("s-") and NOW - float(s.get("started_at") or 0) < 3600
)
for name, n in recent.items():
    if n >= 5:
        findings.append(f"CRASH-LOOP {name} started {n}x in the last hour")

def development_checks():
    """agent-queue's development pipeline: delivery lag, stranded branches,
    publisher skips, the validation database and the publisher log."""
    last = sh(["git", "-C", DAEMON_CHECKOUT, "log", "origin/main", "-1", "--format=%ct %h",
               "--author=Agent Queue", "--author=^aq "]).split()
    if last and NOW - int(last[0]) > DELIVERY_STALE_S:
        _open = [t for t in tasks_by_project.get(DEV_PROJECT, [])
                 if t["status"] not in ("COMPLETED", "CANCELLED", "ARCHIVED")
                 and t.get("id") not in HOLDS]
        pending = sum(1 for t in _open if t["status"] == "DEFINED")
        if _open:
          findings.append(
            f"DELIVERY last worker commit on origin/main {age(last[0])} ago ({last[1]}); "
            f"{pending} DEFINED tasks in {DEV_PROJECT}. Run `git fetch` first if stale; "
            "check `aq doctor --check integration.development_publisher_stalled`"
        )

    # Until agile-stone is deployed: the development publisher skips every
    # dependent of a delivered blocker whose branch was cleaned up and whose
    # completion lists no commits. Restoring the blocker's branch at its delivered
    # SHA unsticks the chain (incident note, 2026-09-22 ~07:30Z).
    STRAND_SQL = f"""
    select distinct b.id, t.id, (
      select m->>'source_sha' from development_deliveries d, jsonb_array_elements(d.manifest::jsonb) m
       where m->>'task_id' = b.id and (d.state = 'adopted' or (d.state = 'delivered' and d.reason = 'development batch'))
       order by d.created_at desc limit 1)
    from task_dependencies dep
    join tasks t on t.id = dep.task_id
    join tasks b on b.id = dep.depends_on_task_id
    where dep.dep_type = 'blocks' and b.status = 'COMPLETED' and t.project_id = '{DEV_PROJECT}'
      and t.status in ('DEFINED', 'READY', 'COMPLETED')  -- a running dependent is not stranded yet
      and not exists (
        select 1 from development_deliveries d2, jsonb_array_elements(d2.manifest::jsonb) m2
         where m2->>'task_id' = t.id and (d2.state = 'adopted' or (d2.state = 'delivered' and d2.reason = 'development batch')))
    """
    sh(["docker", "exec", "aq-postgres", "psql", "-U", "agent_queue", "-d", "agent_queue", "-At",
        "-F", " ", "-o", "/tmp/strand.txt", "-c", STRAND_SQL])
    def explain_cached(tid):
        if tid not in _explain_cache:
            _explain_cache[tid] = aq("task", "explain", "--task-id", tid) or {}
        return _explain_cache[tid]

    for line in sh(["docker", "exec", "aq-postgres", "cat", "/tmp/strand.txt"]).splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        blocker, dependent, sha = parts
        if sh(["git", "-C", DAEMON_CHECKOUT, "rev-parse", "--verify", "-q", f"origin/aq/{blocker}"]).strip():
            continue
        # A dependent that has simply not been claimed yet is NOT stranded: the SQL
        # only proves it has no delivery of its own, which is true of all unstarted
        # work. Ask the daemon whether it is actually held by THIS blocker
        # (2026-09-22: smart-beacon.2/.3 and grand-falcon.2 were false positives).
        why = explain_cached(dependent)
        reasons = why.get("reasons", []) if isinstance(why, dict) else []
        if not any(r.get("code") == "blocked_dependency" and blocker in (r.get("detail", "") + r.get("ref", ""))
                   for r in reasons):
            continue
        findings.append(
            f"RESTORE-BRANCH {blocker} delivered as {sha[:9]} but its branch is gone and a dependent is undelivered: "
            f"git -C {DAEMON_CHECKOUT} push origin {sha}:refs/heads/aq/{blocker}"
        )

    # The publisher logs "skipping <child> because dependency <blocker> is
    # unavailable" when a delivered blocker's branch was deleted. It then never
    # publishes that child, silently, and the graph check above cannot see it
    # because the child IS delivered-eligible from the task side (2026-09-22,
    # clear-meadow.2 sat 40+ min while the publisher looped on the message).
    # Only skips from the last 15 minutes: reading the tail regardless of age kept
    # reporting repairs for hours after they were adopted (2026-09-26).
    _cutoff = datetime.datetime.fromtimestamp(NOW - 900, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    _skips = "\n".join(
        line for line in sh(["bash", "-lc",
            "grep -a 'development publisher: skipping' " + LOG_PATH + " | tail -400"]).splitlines()
        if (m := re.search(r'"timestamp": "([^"]{19})', line)) and m.group(1) >= _cutoff
    )
    _seen = {}
    _dep_re = re.compile(r"skipping (\S+) because dependency (\S+) is unavailable")
    _cyc_re = re.compile(r"skipping (\S+?):? .*?\bcycle\b.*?\b(development-repair-[0-9a-f]+|[a-z]+-[a-z]+(?:\.\d+)?)")
    for line in _skips.splitlines():
        m = _dep_re.search(line)
        if m:
            _seen[m.group(1)] = ("dependency", m.group(2))
            continue
        m = _cyc_re.search(line)
        if m:
            _seen[m.group(1)] = ("cycle", m.group(2))
    _main_tree = sh(["git", "-C", DAEMON_CHECKOUT, "rev-parse", "origin/main^{tree}"]).strip()
    for child, (kind, other) in _seen.items():
        child_tip = sh(["git", "-C", DAEMON_CHECKOUT, "rev-parse", "--verify", "-q",
                        f"origin/aq/{child}"]).strip()
        if not child_tip:
            continue
        # The log keeps every skip message forever. The repository decides whether
        # a skip still matters: resolved when the child's commit is on main, OR when
        # merging it into main would change nothing (its content already landed via
        # another branch, e.g. a superseded repair; 2026-09-24).
        delivered = subprocess.run(
            ["git", "-C", DAEMON_CHECKOUT, "merge-base", "--is-ancestor",
             child_tip, "origin/main"], capture_output=True).returncode == 0
        if not delivered and _main_tree:
            merged = sh(["git", "-C", DAEMON_CHECKOUT, "merge-tree", "--write-tree",
                         "origin/main", child_tip]).split("\n")[0].strip()
            delivered = merged == _main_tree
        if delivered:
            continue
        if kind == "cycle":
            findings.append(
                f"PUBLISHER-SKIP {child} is in a repair dependency cycle with {other}; "
                f"its content is not on main. Re-deliver the underlying source task "
                f"onto current main rather than restoring refs.")
        else:
            findings.append(
                f"PUBLISHER-SKIP {child} is not being published: the publisher says "
                f"dependency {other} is unavailable. If {other}'s branch exists on origin, "
                f"this is not a deleted ref; check for a cycle or re-deliver the source task.")

    state = sh(["docker", "inspect", "-f", "{{.State.Status}}", VALIDATION_CONTAINER]).strip()
    if state != "running":
        findings.append(f"VALIDATION-DB {VALIDATION_CONTAINER} is {state or 'missing'}")

    log_tail = sh(["tail", "-c", "400000", "/home/jkern/.agent-queue/daemon.log"])
    log_tail = re.sub(r"\x1b\[[0-9;]*m", "", log_tail)
    for pattern in ("development publisher tick failed", "Development batch remains recoverable"):
        hits = [line for line in log_tail.splitlines() if pattern in line]
        if hits:
            findings.append(f"PUBLISHER {hits[-1][:200]}")

    auto_deploy_check()


def _auto_deploy_skips():
    """The trailing run of ``skip:`` lines in the auto-deploy log, oldest first,
    as (epoch, reason). A deploy, a rollback or a gap longer than one missed run
    ends the run; nothing-to-deploy runs log nothing."""
    try:
        with open(AUTO_DEPLOY_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    run = []
    for line in reversed(lines):
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)", line)
        if not m:
            continue  # output of a command the script ran
        if not m.group(2).startswith("skip: "):
            if m.group(2).startswith(("deployed ", "BLOCKED:")):
                break
            continue
        ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.UTC).timestamp()
        if (run[-1][0] if run else NOW) - ts > AUTO_DEPLOY_GAP_S:
            break
        run.append((ts, m.group(2)[len("skip: "):]))
    return run[::-1]


def auto_deploy_check():
    """The 5-minute auto-deploy (scripts/operator-checks/aq-auto-deploy.sh)
    skips while the live checkout is dirty, off dev or diverged. Uncommitted
    edits there stalled every deploy for ~17 hours with nothing but a log line
    (2026-10-14), so a skip run past 30 minutes is a finding, and so is a dirty
    checkout behind origin/dev the log does not explain."""
    behind = sh(["git", "-C", DAEMON_CHECKOUT, "rev-list", "--count", "HEAD..origin/dev"]).strip()
    if not behind.isdigit() or int(behind) == 0:
        return
    dirty = [line[3:] for line in sh(["git", "-C", DAEMON_CHECKOUT, "status", "--porcelain",
                                      "--untracked-files=no"]).splitlines() if line.strip()]
    run = _auto_deploy_skips()
    if run and NOW - run[0][0] >= AUTO_DEPLOY_STALL_S:
        why = f"auto-deploy has skipped every run for {age(run[0][0])} (last: {run[-1][1]})"
    elif dirty and not run:
        why = f"the live checkout is dirty and {AUTO_DEPLOY_LOG} records no recent skip"
    else:
        return
    edits = f" Uncommitted edits: {', '.join(dirty[:5])}." if dirty else ""
    findings.append(
        f"AUTO-DEPLOY-STALLED {DAEMON_CHECKOUT} is {behind} commits behind origin/dev: {why}.{edits} "
        "Agents must never edit the live checkout -> save the edits (rescue branch + "
        "~/.agent-queue/backups/), never discard them, then restore a clean tree on dev; "
        "the next run deploys")


if DEV_CHECKS:
    development_checks()

print("\n".join(findings) if findings else "OK: nothing stalled")
if superseded_verifiers:
    print("INFO superseded aggregate verifiers (parent COMPLETED; blocked or failed): " + " ".join(superseded_verifiers))
if open_containers:
    print("INFO paused containers with open children (swept per child): " + " ".join(open_containers))
