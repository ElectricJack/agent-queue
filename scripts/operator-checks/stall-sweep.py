#!/usr/bin/env python3
"""Report stalled work across every ACTIVE AQ project.

Read-only. Prints one line per finding, or "OK: nothing stalled". Written
2026-09-21 after the development-publisher stall hid behind tasks closing
"pass" for three days (vault projects/agent-queue/notes/
integration-pipeline-incident-2026-09-20.md). The supervisor session runs it on
a timer and acts on what it prints.
"""
import datetime
import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

NOW = time.time()
READY_STALE_S = 20 * 60
DEFINED_STALE_S = 30 * 60
SESSION_IDLE_S = 15 * 60
DELIVERY_STALE_S = 6 * 3600
STALE_OPEN_S = 2 * 3600  # BLOCKED/PAUSED/FAILED this long needs a premise check, not a skip
DAEMON_CHECKOUT = "/home/jkern/dev/agent-queue2"
LOG_PATH = "/home/jkern/.agent-queue/logs/agent-queue.log"
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


def aq(*args, timeout=60, missing_ok=False):
    out = subprocess.run(
        ["aq", "--json", *args], capture_output=True, text=True, timeout=timeout
    ).stdout
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        # A failed read hides stalls; say so instead of treating it as healthy.
        print(f"SWEEP-ERROR aq {' '.join(args)}: no JSON response -> this check is incomplete")
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if err and err.get("code") == "out_of_scope":
        # An expired session token makes every call fail; never report "OK" then.
        hint = (
            "project-scoped supervisor token: re-run with `--project <pid>`"
            if args[:2] == ("project", "list") and not ONLY
            else "re-run with a fresh token"
        )
        print(f"SWEEP-AUTH aq {' '.join(args)}: {err.get('message')} -> {hint}")
        sys.exit(2)
    if err and missing_ok and "not found" in str(err.get("message", "")).lower():
        # A referenced task that was deleted is a finding, not an incomplete sweep.
        return {"_missing": True}
    if err:
        print(f"SWEEP-ERROR aq {' '.join(args)}: {err.get('code')}: {str(err.get('message'))[:120]} -> this check is incomplete")
    return body.get("data") if isinstance(body, dict) else None


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
empty_projects: list[str] = []
tasks_by_project: dict[str, list[dict]] = {}

for pid in active:
    project_tasks = tasks_by_project[pid] = aq("task", "list", "-p", pid) or []
    # 2026-10-04: a sweep that ran while the daemon restarted got an empty
    # (successful) task list and printed "OK: nothing stalled". Projects like
    # rom-downloader legitimately have no tasks, though, so an empty list is only
    # a failed read when every active project comes back empty (checked below).
    if not project_tasks:
        empty_projects.append(pid)
        continue
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

if active and len(empty_projects) == len(active):
    # 2026-10-05: a `--project` sweep of one idle project (every task COMPLETED,
    # which the default list hides) tripped this. With one project, ask the
    # daemon directly instead of inferring an outage from an empty list.
    daemon_ok = False
    if ONLY:
        try:
            import os
            import urllib.request
            api = os.environ.get("AQ_API_URL", "http://127.0.0.1:8081").rstrip("/")
            with urllib.request.urlopen(f"{api}/health", timeout=10) as resp:
                daemon_ok = resp.status == 200
        except Exception:
            daemon_ok = False
    if daemon_ok:
        print(f"note: {ONLY} has no open tasks (daemon healthy)")
    else:
        findings.append("SWEEP-ERROR every active project returned an empty task list (daemon restarting or unreachable?) -> this sweep is incomplete; re-run it")

# `aq task explain` is one HTTP round trip each against a daemon that is often
# busy; doing them serially took the sweep past 5 minutes on 2026-09-22 (it had
# been ~45s). Fan them out and cache, so repeat ids cost nothing.
_explain_cache: dict[str, dict] = {}


def explain(tid):
    if tid not in _explain_cache:
        _explain_cache[tid] = aq("task", "explain", "--task-id", tid) or {}
    return _explain_cache[tid]


with ThreadPoolExecutor(max_workers=8) as pool:
    for tid, res in zip(
        [c[1]["id"] for c in candidates],
        pool.map(lambda c: aq("task", "explain", "--task-id", c[1]["id"]) or {}, candidates),
    ):
        _explain_cache[tid] = res

for pid, t in candidates:
        tid, status, prof = t["id"], t["status"], t.get("profile_id") or "-"
        if status in ("READY", "DEFINED"):
            why = explain(tid)
            rows = (why.get("reasons") or []) if isinstance(why, dict) else []
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
stale_ready_ids: dict[str, list[str]] = {}
for pid, t in candidates:
    if t["status"] == "READY" and not t.get("assigned_agent_id"):
        prof = t.get("profile_id") or "-"
        stale_ready[prof] += 1
        stale_ready_ids.setdefault(prof, []).append(t["id"])
# `--project-id` filters only the per-project rows; a pool's own counters stay
# fleet-wide, so a scoped sweep compares against its project's frontier.
pools = aq("pool", "status", *(("--project-id", ONLY) if ONLY else ())) or []
for p in pools if isinstance(pools, list) else pools.get("pools", []):
    prof = p.get("profile_id")
    if not p.get("enabled"):
        continue
    own = [proj for proj in p.get("projects") or [] if ONLY in (None, proj.get("project_id"))]
    seen = sum(proj.get("ready") or 0 for proj in own) if ONLY else (p.get("ready") or 0)
    gap = stale_ready.get(prof, 0) - seen
    if gap > 0:
        ids = ", ".join(stale_ready_ids[prof][:8])
        findings.append(
            f"POOL-STARVED {prof}: {stale_ready[prof]} READY >{READY_STALE_S // 60}m but pool sees "
            f"ready={seen} (desired {p.get('desired')}, busy {p.get('running_busy')}) "
            f"-> off the claim frontier: {ids}"
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
sessions = [s for s in aq("session", "list") or [] if ONLY in (None, s.get("project_id"))]
for s in sessions:
    if s.get("state") not in ("running", "starting") or not s.get("stalled"):
        continue
    if (s.get("idle_seconds") or 0) < SESSION_IDLE_S:
        continue
    if s.get("task_id") and s.get("lifecycle") != "named":
        # A worker holding a task but silent this long has usually died on an
        # error screen (e.g. OpenCode context overflow) the lease has not caught.
        pane = sh(["tmux", "capture-pane", "-p", "-t", s["name"]])
        tail = [line.strip() for line in pane.splitlines() if line.strip()][-12:]
        errors = [line for line in tail if re.search(r"error|failed|not found|limit|no user query|Do you want to proceed|Dangerous", line, re.I)]
        findings.append(
            f"IDLE-WORKER {s['name']} holds {s['task_id']} idle {s['idle_seconds'] / 60:.0f}m"
            + (f" | pane: {errors[-1][:120]}" if errors else "")
        )
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


if DEV_CHECKS:
    development_checks()

print("\n".join(findings) if findings else "OK: nothing stalled")
if superseded_verifiers:
    print("INFO superseded aggregate verifiers (parent COMPLETED; blocked or failed): " + " ".join(superseded_verifiers))
if open_containers:
    print("INFO paused containers with open children (swept per child): " + " ".join(open_containers))
