#!/usr/bin/env python3
"""Tier 1 of the swarm functional-test kit — seven scenarios, no LLM.

Driven by ``scripts/e2e-smoke.sh`` against the daemon
``scripts/e2e-daemon.sh start`` put up.  Every assertion goes through a
public surface: the ``aq`` CLI's ``--json`` envelope, or ``POST
/api/execute`` for the handful of commands whose CLI form cannot carry
arguments yet (their tool definitions are codegen-only).

With ``sessions.provider: fake`` nothing is spawned, so this script *is*
the pool worker: it mints each session's bearer token with ``aq session
token`` and then runs the same ``aq`` commands, with the same
``AQ_API_TOKEN`` / ``AQ_SESSION_ID`` environment handshake, that a real
harness would.  Everything the daemon sees is indistinguishable from a
live worker.

Scenario map — see docs/guides/e2e-swarm.md for what each one proves:

    S1  pool sizing            demand → sessions, bounded by max_active
    S2  the claim loop         claim / fence / close --claim-next / retire
    S3  worker-filed work      DEFINED + discovered-from + routing gate
    S4  formulas               list / show / cook / as-cooked / settle
    S5  fence + scope          cross-session and cross-project refusals
    S6  doctor                 the swarm checks, clean and then warning
    S7  Postgres race          two concurrent claims, exactly one winner
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AQ_LAUNCHER = os.path.join(REPO_ROOT, "scripts", "e2e", "aq.py")
API_URL = os.environ.get("AQ_API_URL", "http://127.0.0.1:8099").rstrip("/")

PROJECT = "e2e"
OTHER_PROJECT = "other"
POOL_PROFILE = "worker"

#: How long a scenario waits for the 5s cascade to converge before failing.
CONVERGE_TIMEOUT = float(os.environ.get("AQ_E2E_CONVERGE_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# Failure signalling
# ---------------------------------------------------------------------------


class Failure(Exception):
    """A scenario assertion that did not hold.  Caught by the runner."""


def check(condition, message: str) -> None:
    if not condition:
        raise Failure(message)


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


class CliError(Exception):
    def __init__(self, envelope: dict, stderr: str):
        self.envelope = envelope
        self.error = (envelope or {}).get("error") or {}
        self.details = self.error.get("details") or {}
        super().__init__(self.error.get("message") or stderr or "aq failed")

    @property
    def result(self) -> str | None:
        """The claim-protocol result code, when the failure carries one."""
        return self.details.get("result")


def aq(
    *args: str,
    token: str | None = None,
    session_id: str | None = None,
    check_ok: bool = True,
    timeout: float = 120.0,
) -> dict:
    """Run this worktree's ``aq`` with ``--json`` and return the envelope data.

    *token* / *session_id* populate ``AQ_API_TOKEN`` / ``AQ_SESSION_ID`` —
    the same two variables ``src/sessions/env.py`` sets inside a real
    session, so a command run this way is authenticated exactly as an agent
    would be.

    Raises :class:`CliError` on an error envelope unless *check_ok* is
    false, in which case the error envelope is returned as
    ``{"_error": ...}`` for the caller to inspect (several scenarios assert
    on a *refusal*).
    """
    env = dict(os.environ)
    env["AQ_API_URL"] = API_URL
    env.pop("AQ_API_TOKEN", None)
    env.pop("AQ_SESSION_ID", None)
    if token:
        env["AQ_API_TOKEN"] = token
    if session_id:
        env["AQ_SESSION_ID"] = session_id

    proc = subprocess.run(
        [sys.executable, AQ_LAUNCHER, "--json", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    stdout = proc.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise Failure(
            f"aq {' '.join(args)} printed non-JSON (exit {proc.returncode}): "
            f"{stdout[:400]!r} / stderr {proc.stderr[:200]!r}"
        ) from exc

    if isinstance(payload, dict) and payload.get("error"):
        err = CliError(payload, proc.stderr)
        if check_ok:
            raise err
        return {"_error": err}
    # Commands that print their payload directly (no envelope) are returned
    # as-is; enveloped ones hand back ``data``.
    if isinstance(payload, dict) and "schema_version" in payload:
        return payload.get("data") or {}
    return payload


def api(command: str, args: dict | None = None, *, token: str | None = None) -> dict:
    """``POST /api/execute`` — the surface for commands the CLI cannot pass args to.

    ``gate_list``, ``explain_task`` and friends are categorized but carry
    only a codegen input schema, so the auto-generated Click command takes
    no options.  The REST endpoint does, and it is just as public.
    """
    body = json.dumps({"command": command, "args": args or {}}).encode()
    req = urllib.request.Request(
        f"{API_URL}/api/execute",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b"{}")
    if not payload.get("ok"):
        return {"success": False, "error": payload.get("error"), **(payload.get("details") or {})}
    return payload.get("result") or {}


def wait_for(predicate, *, what: str, timeout: float = CONVERGE_TIMEOUT, interval: float = 2.0):
    """Poll *predicate* until it returns something truthy, or fail loudly.

    Every wait in this file is on the 5s cascade, so the failure message
    matters more than the mechanism: "the pool never reached 2 sessions
    (last saw 1)" is a bug report; "timeout" is not.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise Failure(f"timed out after {timeout:.0f}s waiting for {what} (last saw {last!r})")


# ---------------------------------------------------------------------------
# Small domain helpers
# ---------------------------------------------------------------------------


def pool_row(project_id: str = PROJECT, profile_id: str = POOL_PROFILE) -> dict:
    for row in aq("pool", "status", "--project-id", project_id).get("pools", []):
        if row["profile_id"] == profile_id:
            return row
    raise Failure(f"no pool row for {project_id}/{profile_id}")


def pool_sessions(project_id: str = PROJECT) -> list[dict]:
    rows = aq("session", "list", "--lifecycle", "pool").get("sessions", [])
    live = ("starting", "running")
    return [s for s in rows if s["project_id"] == project_id and s["state"] in live]


def session_token(session_id: str) -> str:
    return aq("session", "token", session_id)["token"]


def create_task(title: str, *, project_id: str = PROJECT, profile: str | None = None) -> str:
    """``create_task`` over REST — ``aq task create`` has no JSON envelope yet."""
    args = {"project_id": project_id, "title": title, "description": f"e2e: {title}"}
    if profile:
        args["profile_id"] = profile
    result = api("create_task", args)
    task_id = result.get("created") or result.get("task_id")
    check(task_id, f"create_task({title}) returned no id: {result}")
    return task_id


def task_show(task_id: str) -> dict:
    return aq("task", "show", task_id)


@dataclass
class Worker:
    """One pool session, addressed the way its own agent would address it."""

    session_id: str
    token: str
    claim_epoch: int | None = None
    task_id: str | None = None

    @classmethod
    def adopt(cls, session_id: str) -> "Worker":
        return cls(session_id=session_id, token=session_token(session_id))

    def aq(self, *args: str, check_ok: bool = True) -> dict:
        return aq(*args, token=self.token, session_id=self.session_id, check_ok=check_ok)

    def claim_next(self, *, check_ok: bool = True) -> dict:
        out = self.aq("task", "claim", "--next", check_ok=check_ok)
        if isinstance(out, dict) and out.get("result") == "claimed":
            self.task_id = out["task"]["id"]
            self.claim_epoch = out["claim_epoch"]
        return out

    def close(self, *, claim_next: bool = False, summary: str = "e2e close") -> dict:
        args = [
            "task", "close",
            "--outcome", "pass",
            "--summary", summary,
            "--work-outcome", "shipped",
            "--claim-epoch", str(self.claim_epoch),
        ]
        if claim_next:
            args.append("--claim-next")
        out = self.aq(*args)
        nxt = out.get("next") or {}
        if nxt.get("result") == "claimed":
            self.task_id = nxt["task"]["id"]
            self.claim_epoch = nxt["claim_epoch"]
        else:
            self.task_id = None
            self.claim_epoch = None
        return out

    def drain_ack(self) -> dict:
        return self.aq("session", "drain-ack", "--session-id", self.session_id)


def fresh_workers(count: int) -> list[Worker]:
    """Put the pool in a known state: exactly *count* idle, unspent workers.

    Scenarios that ran earlier leave the pool in whatever shape they
    finished in — sessions part-way through their two claims, one retired
    and replaced, tasks half-worked.  S5 and S7 both need workers that can
    still claim, and S7 additionally needs an *empty* frontier so the one
    task it creates is the only thing to race for.  Rebuilding beats
    guessing:

    1. drain the pool profile's frontier and kill every live session, in a
       loop.  Both halves are needed and neither is enough on its own: a
       session holding a task blocks the delete, and demand that outlives a
       kill just makes the sizer start a replacement on the next tick.  The
       loop converges once the frontier is empty *and* no session is live.
    2. create *count* filler tasks so the sizer starts *count* workers,
    3. delete the fillers again — nothing claims on its own under the fake
       provider, so they are still untouched and the frontier goes empty.
    """

    def _quiesced():
        _delete_open_pool_tasks()
        live = pool_sessions()
        for s in live:
            aq("session", "kill", s["id"], check_ok=False)
        return not live and not _open_pool_tasks()

    wait_for(_quiesced, what="the pool to quiesce (no live sessions, empty frontier)")

    fillers = [create_task(f"pool primer {n}", profile=POOL_PROFILE) for n in range(count)]
    live = wait_for(
        lambda: (lambda rows: rows if len(rows) >= count else None)(pool_sessions()),
        what=f"{count} fresh pool sessions",
    )
    for task_id in fillers:
        aq("task", "delete", "--task-id", task_id)
    return [Worker.adopt(s["id"]) for s in live[:count]]


def _open_pool_tasks() -> list[dict]:
    """Every unfinished task in the project.

    ``aq task list`` already hides COMPLETED/FAILED/BLOCKED and returns a
    bare list, and its rows carry no ``profile_id`` — so this cannot filter
    to the pool profile.  It does not need to: at the S5/S7 boundary
    everything still open in ``e2e`` is leftover scenario scaffolding, and
    clearing all of it is exactly the point.
    """
    rows = aq("task", "list", "--project", PROJECT)
    return list(rows) if isinstance(rows, list) else rows.get("tasks", [])


def _delete_open_pool_tasks() -> None:
    """Clear the frontier so a scenario starts from zero.

    ``--cascade`` because a worker-filed task from S3 may still hang off
    one of these; ``check_ok=False`` because a task a session is still
    holding refuses deletion, and the caller's loop retries after the kill
    has released it.
    """
    for task in _open_pool_tasks():
        aq("task", "delete", "--task-id", task["id"], "--cascade", check_ok=False)


def idle_worker() -> Worker:
    """Adopt whichever pool session currently holds no task."""

    def _find():
        for s in pool_sessions():
            if not s.get("task_id"):
                return s["id"]
        return None

    sid = wait_for(_find, what="an idle pool session")
    return Worker.adopt(sid)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def ensure_project(project_id: str, workspaces: list[str]) -> None:
    existing = {p["id"] for p in aq("project", "list").get("projects", [])}
    if project_id not in existing:
        aq(
            "project", "create",
            "--name", project_id,
            "--default-profile-id", POOL_PROFILE,
            "--no-auto-create-channels",
        )
    have = {
        w["workspace_path"]
        for w in aq("project", "list-workspaces", "--project-id", project_id).get(
            "workspaces", []
        )
    }
    for path in workspaces:
        if path in have:
            continue
        aq(
            "project", "add-workspace",
            "--project-id", project_id,
            "--source", "link",
            "--path", path,
            "--name", os.path.basename(path),
        )


def workspace_paths(project_id: str) -> list[str]:
    """The workspace clones ``e2e-env.sh`` laid down for *project_id*."""
    home = os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e"))
    ws = os.path.join(home, "workspaces")
    if project_id == PROJECT:
        return [os.path.join(ws, f"e2e-{n}") for n in (1, 2, 3, 4, 5)]
    return [os.path.join(ws, "other-1")]


def setup() -> None:
    # `e2e-daemon.sh start` already ran `e2e-env.sh --register`; this is the
    # same idempotent call, so the runner also works against a daemon
    # somebody started by hand.
    ensure_project(PROJECT, workspace_paths(PROJECT))
    ensure_project(OTHER_PROJECT, workspace_paths(OTHER_PROJECT))
    # The pool profile must have reached the DB from the vault, or every
    # scenario below fails for the same uninteresting reason.
    check(
        any(r["profile_id"] == POOL_PROFILE for r in aq("pool", "status").get("pools", [])),
        f"profile '{POOL_PROFILE}' is not a pool profile — is the vault fixture in place?",
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def s1_pool_sizing(state: dict) -> str:
    """Demand creates workers, and `max_active` caps them."""
    ids = [create_task(f"S1 worker task {n}", profile=POOL_PROFILE) for n in (1, 2, 3)]
    state["s1_tasks"] = ids

    row = wait_for(
        lambda: (lambda r: r if r["running_idle"] + r["running_busy"] + r["starting"] == 2 else None)(
            pool_row()
        ),
        what="the pool to reach max_active=2 sessions",
    )
    check(row["max_active"] == 2, f"max_active should be 2, got {row['max_active']}")
    check(row["ready"] >= 3, f"expected >=3 ready tasks, saw {row['ready']}")
    check(
        len(pool_sessions()) == 2,
        f"expected exactly 2 live pool sessions, saw {len(pool_sessions())}",
    )

    events = api("get_recent_events", {"event_type": "pool.scaled", "limit": 10})["events"]
    scaled = [e for e in events if e["project_id"] == PROJECT]
    check(scaled, "no pool.scaled audit row for project 'e2e'")
    check(
        any(e["payload"].startswith("start") for e in scaled),
        f"pool.scaled rows record no scale-up: {[e['payload'] for e in scaled]}",
    )
    return f"2 sessions for 3 ready tasks; pool.scaled = {scaled[0]['payload']!r}"


def s2_claim_loop(state: dict) -> str:
    """The full worker loop: claim, fence, close --claim-next, exhaust, retire."""
    worker = idle_worker()
    state["s2_session"] = worker.session_id

    first = worker.claim_next()
    check(first["result"] == "claimed", f"first claim: {first}")
    check(isinstance(first["claim_epoch"], int), f"no integer claim_epoch: {first}")
    first_task = worker.task_id

    # A wrong epoch is the fence: the task is no longer (or not yet) yours.
    stale = worker.aq(
        "task", "heartbeat", "--claim-epoch", str(worker.claim_epoch + 41), check_ok=False
    )
    err = stale.get("_error")
    check(err is not None, "heartbeat with a wrong claim_epoch was accepted")
    check(err.result == "stale_claim", f"expected stale_claim, got {err.result}: {err}")

    # …and the right one is not.
    beat = worker.aq("task", "heartbeat", "--claim-epoch", str(worker.claim_epoch))
    check(beat.get("success"), f"heartbeat with the current epoch was refused: {beat}")

    second = worker.close(claim_next=True, summary="S2 first task")
    nxt = second.get("next") or {}
    check(nxt.get("result") == "claimed", f"close --claim-next did not claim again: {nxt}")
    check(nxt["task"]["id"] != first_task, "close --claim-next re-served the same task")
    check(nxt["session"]["claims"] == 2, f"claim counter should be 2: {nxt['session']}")

    third = worker.close(claim_next=True, summary="S2 second task")
    nxt = third.get("next") or {}
    check(
        nxt.get("result") == "session_exhausted",
        f"expected session_exhausted at max_claims_per_session=2, got {nxt.get('result')}",
    )

    ack = worker.drain_ack()
    check(ack.get("success"), f"drain-ack refused: {ack}")

    # The reconciler now retires the agent, frees the workspace, and the
    # sizer starts a replacement for the still-unclaimed third task.
    #
    # ``agents.state == RETIRED`` itself has no public reader — `aq agent
    # list` reports *workspace slots*, not agent rows — so this asserts the
    # three consequences that do: the session row goes terminal, the slot it
    # held comes back, and `pools.orphan_agents` (which *does* read agent
    # rows) stays clean, which it would not if the agent were left behind.
    def _retired():
        shown = aq("session", "show", worker.session_id)
        row = shown.get("session") or shown
        return row.get("state") == "stopped"

    wait_for(_retired, what=f"session {worker.session_id} to reach state=stopped")

    orphan_check = _swarm_checks().get("pools.orphan_agents", {})
    check(
        orphan_check.get("severity") == "ok",
        f"pools.orphan_agents is {orphan_check.get('severity')}: {orphan_check.get('detail')}",
    )

    replacement = wait_for(
        lambda: (lambda live: live if any(s["id"] != worker.session_id for s in live) else None)(
            pool_sessions()
        ),
        what="a replacement pool session",
    )
    new_ids = [s["id"] for s in replacement if s["id"] != worker.session_id]
    return (
        f"claimed 2/2 then session_exhausted; {worker.session_id} retired, "
        f"replaced by {new_ids[0]}"
    )


def s3_worker_filed_work(state: dict) -> str:
    """Work a worker discovers is DEFINED, provenance-linked and gated."""
    worker = idle_worker()
    claimed = worker.claim_next()
    check(claimed["result"] == "claimed", f"S3 needs a held task: {claimed}")
    held = worker.task_id
    state["s3_worker"] = worker

    filed = api(
        "create_task",
        {"title": "S3 discovered work", "description": "filed by a worker mid-task"},
        token=worker.token,
    )
    filed_id = filed.get("created") or filed.get("task_id")
    check(filed_id, f"worker-filed create_task returned no id: {filed}")
    state["s3_filed"] = filed_id

    row = task_show(filed_id)
    check(row["status"] == "DEFINED", f"worker-filed work must start DEFINED, got {row['status']}")
    check(row["project_id"] == PROJECT, "worker-filed work escaped the session's project")
    check(row["profile_id"] is None, f"unrouted work should carry no profile: {row['profile_id']}")

    deps = aq("task", "deps", "--task-id", filed_id)
    origins = [
        p for p in deps.get("provenance", []) if p.get("dep_type") == "discovered-from"
    ]
    check(origins, f"no discovered-from edge on {filed_id}: {deps}")
    check(
        origins[0]["id"] == held,
        f"discovered-from points at {origins[0]['id']}, not the held task {held}",
    )

    reasons = api("explain_task", {"task_id": filed_id}).get("reasons", [])
    gate_reasons = [
        r for r in reasons if r["code"] == "blocked_gate" and "routing" in r["detail"]
    ]
    check(gate_reasons, f"no open routing gate on {filed_id}; explain said {reasons}")
    gate_id = gate_reasons[0]["ref"]

    # `task_route` is the only resolver for a routing gate (dv2 phase 1).
    # A triage *agent* calls it; with no LLM in Tier 1 the operator surface
    # stands in, which exercises the same command the agent would run.
    routed = aq("task", "route", "--task-id", filed_id, "--profile-id", POOL_PROFILE)
    check(routed.get("success"), f"task route failed: {routed}")
    check(routed["resolved_gate_ids"], "routing did not resolve the gate")

    after = task_show(filed_id)
    check(after["profile_id"] == POOL_PROFILE, f"profile not written: {after['profile_id']}")
    gate = api("gate_show", {"gate_id": gate_id})
    status = (gate.get("gate") or gate).get("status")
    check(status == "resolved", f"routing gate {gate_id} is still {status}")
    left = [
        r
        for r in api("explain_task", {"task_id": filed_id}).get("reasons", [])
        if r["code"] == "blocked_gate"
    ]
    check(not left, f"{filed_id} is still gate-blocked after routing: {left}")

    worker.close(summary="S3 held task")
    return f"{filed_id} DEFINED + discovered-from {held} + routing gate {gate_id} resolved"


def s4_formulas(state: dict) -> str:
    """A formula resolves, cooks, renders back as-cooked, and settles."""
    names = {f["name"] for f in aq("formula", "list", "--project-id", PROJECT)["formulas"]}
    check({"base-review", "review-and-fix"} <= names, f"formula fixtures missing: {names}")

    shown = aq(
        "formula", "show", "review-and-fix", "--project-id", PROJECT, "--var", "branch=feat/x"
    )
    check(shown.get("success"), f"formula show failed: {shown.get('errors')}")
    check(shown["chain"] == ["base-review", "review-and-fix"], f"extends chain: {shown['chain']}")
    check(shown["vars"]["effective"]["branch"] == "feat/x", "branch var not substituted")
    titles = [n["title"] for n in shown["graph"]["nodes"]]
    check(
        all("feat/x" in t for t in titles),
        f"node titles kept an unsubstituted var: {titles}",
    )

    cooked = aq(
        "formula", "cook", "review-and-fix",
        "--project-id", PROJECT,
        "--var", "branch=feat/x",
    )
    container = cooked["container_id"]
    state["s4_container"] = container
    check(len(cooked["task_ids"]) == 2, f"expected 2 children, got {cooked['task_ids']}")

    row = task_show(container)
    check(
        "formula:review-and-fix" in (row.get("labels") or []),
        f"container carries no formula label: {row.get('labels')}",
    )
    check(row["children"]["total"] == 2, f"container children: {row['children']}")

    children = aq("task", "children", "--task-id", container)["children"]
    check(len(children) == 2, f"task children returned {len(children)}")

    snapshot = aq("formula", "show", "--as-cooked", container)
    check(snapshot.get("success"), f"--as-cooked failed: {snapshot}")
    snap_titles = [n["title"] for n in snapshot["graph"]["nodes"]]
    check(
        snap_titles == titles,
        f"as-cooked titles differ from the resolved ones: {snap_titles} vs {titles}",
    )

    # Close both children through their own sessions.  Both are routed to
    # task-lifecycle profiles, so the push scheduler launches a session for
    # each as it enters the frontier — `review` first, `fix` once `review`
    # is done.  Minting each session's token and closing through it is the
    # same completion protocol a real harness runs.
    for _ in range(2):
        _close_next_child(container)

    progress = aq("task", "progress", "--task-id", container)
    check(progress["done"] == 2, f"container progress not settled: {progress}")
    settled = wait_for(
        lambda: (lambda t: t if t["status"] in ("COMPLETED", "DONE") else None)(
            task_show(container)
        ),
        what=f"container {container} to settle",
    )
    return (
        f"cooked {container} ({', '.join(cooked['task_ids'])}); "
        f"as-cooked matches; settled as {settled['status']}"
    )


def _close_next_child(container: str) -> None:
    """Wait for the next child to be picked up, then close it as its session."""
    def _held():
        for child in aq("task", "children", "--task-id", container)["children"]:
            if child["status"] in ("COMPLETED", "DONE"):
                continue
            sessions = aq("session", "list").get("sessions", [])
            for s in sessions:
                if s.get("task_id") == child["id"] and s["state"] in ("starting", "running"):
                    return (child["id"], s["id"])
        return None

    task_id, session_id = wait_for(_held, what=f"a session to pick up a child of {container}")
    token = session_token(session_id)
    out = aq(
        "task", "close", task_id,
        "--outcome", "pass",
        "--summary", "S4 child closed by its session",
        "--work-outcome", "shipped",
        token=token,
        session_id=session_id,
    )
    check(out.get("success"), f"closing child {task_id} failed: {out}")


def s5_fence_and_scope(state: dict) -> str:
    """A token is an identity, not a key to the daemon."""
    holder, intruder = fresh_workers(2)
    create_task("S5 task for the holder", profile=POOL_PROFILE)
    claimed = holder.claim_next()
    check(claimed["result"] == "claimed", f"S5 needs a held task: {claimed}")
    held, epoch = holder.task_id, holder.claim_epoch

    # A second, *different* pool session's token must not touch it.
    denied = intruder.aq(
        "task", "heartbeat", held, "--claim-epoch", str(epoch), check_ok=False
    )
    err = denied.get("_error")
    check(err is not None, f"session {intruder.session_id} heartbeat another's task unrefused")
    check(
        err.result == "out_of_scope" or "out of scope" in str(err).lower(),
        f"expected out_of_scope, got {err.result}: {err}",
    )

    # And a token scoped to one project must not prime a task in another.
    foreign = create_task("S5 task in another project", project_id=OTHER_PROJECT)
    state["s5_foreign"] = foreign
    refused = holder.aq("prime", "--task-id", foreign, check_ok=False)
    err = refused.get("_error")
    check(err is not None, f"prime across projects was allowed for {foreign}")
    text = f"{err} {err.result}".lower()
    check(
        "out_of_scope" in text or "out of scope" in text or "scope" in text,
        f"cross-project prime refused for the wrong reason: {err}",
    )

    holder.close(summary="S5 held task")
    return f"cross-session heartbeat and cross-project prime both refused ({foreign})"


_SWARM_CHECK_PREFIXES = ("pools.", "claims.", "hierarchy.")
_SWARM_CHECK_IDS = ("formulas.parse",)


def _swarm_checks() -> dict[str, dict]:
    out = {}
    for c in aq("doctor", "--json")["checks"]:
        if c["id"].startswith(_SWARM_CHECK_PREFIXES) or c["id"] in _SWARM_CHECK_IDS:
            out[c["id"]] = c
    return out


def _set_swarm_enabled(enabled: bool) -> None:
    # `update_config` *replaces* the section, so every key has to be sent —
    # dropping one would silently reset it to the dataclass default and
    # change what the rest of the run is measuring.  These mirror the block
    # scripts/e2e-env.sh writes.
    data = {
        "enabled": enabled,
        "claim_wait_max": 30,
        "max_starts_per_tick": 2,
        "max_drains_per_tick": 5,
        "scale_down_grace": 3600,
        "prepare_timeout": 120,
        "max_filings_per_task": 20,
    }
    result = aq("system", "update-config", "--section", "swarm", "--data", json.dumps(data))
    check(result.get("success", True) is not False, f"update-config failed: {result}")


def s6_doctor(state: dict) -> str:
    """The swarm's own health checks, clean — and honest when switched off."""
    checks = _swarm_checks()
    check(checks, "doctor reported no pools./claims./hierarchy./formulas checks at all")
    bad = {
        cid: c["detail"] for cid, c in checks.items() if c["severity"] in ("warn", "error")
    }
    check(not bad, f"swarm doctor checks are not clean: {bad}")
    check("formulas.parse" in checks, "formulas.parse check missing")

    _set_swarm_enabled(False)
    state["swarm_disabled"] = True
    try:
        disabled = wait_for(
            lambda: (lambda c: c if c.get("severity") == "warn" else None)(
                _swarm_checks().get("pools.disabled", {})
            ),
            what="pools.disabled to warn after swarm.enabled=false",
            timeout=30,
        )
    finally:
        _set_swarm_enabled(True)
        state["swarm_disabled"] = False

    restored = wait_for(
        lambda: (lambda c: c if c.get("severity") == "ok" else None)(
            _swarm_checks().get("pools.disabled", {})
        ),
        what="pools.disabled to return to ok",
        timeout=30,
    )
    return (
        f"{len(checks)} swarm checks clean; hot-reload flip warned "
        f"({disabled['detail']!r}) and restored ({restored['detail']!r})"
    )


def s7_claim_race(state: dict) -> str:
    """One ready task, two workers, one winner — on real PostgreSQL."""
    # Two unspent workers and an empty frontier, so the task created below
    # is the only thing either of them can win.
    workers = fresh_workers(2)
    tokens = [(w.session_id, w.token) for w in workers]

    task_id = create_task("S7 single contested task", profile=POOL_PROFILE)
    state["s7_task"] = task_id
    # Let the task land in the frontier before both callers ask for it.
    wait_for(lambda: task_show(task_id)["status"] == "READY", what=f"{task_id} to be READY")

    procs = []
    for sid, token in tokens:
        env = dict(os.environ)
        env.update({"AQ_API_URL": API_URL, "AQ_API_TOKEN": token, "AQ_SESSION_ID": sid})
        procs.append(
            subprocess.Popen(
                [sys.executable, AQ_LAUNCHER, "--json", "task", "claim", "--next"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    outcomes = []
    for p in procs:
        out, _err = p.communicate(timeout=120)
        payload = json.loads(out.strip() or "{}")
        data = payload.get("data") or {}
        details = (payload.get("error") or {}).get("details") or {}
        outcomes.append(data.get("result") or details.get("result") or "unknown")

    winners = [o for o in outcomes if o == "claimed"]
    check(
        len(winners) == 1,
        f"exactly one claim must win, got {outcomes}",
    )
    loser = [o for o in outcomes if o != "claimed"][0]
    check(
        loser in ("no_ready_work", "claim_conflict", "session_exhausted"),
        f"loser returned an unexpected result: {loser}",
    )
    return f"outcomes {outcomes} — one winner, loser said {loser}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    key: str
    title: str
    fn: object
    result: str = ""
    ok: bool | None = None
    seconds: float = 0.0
    detail: str = ""


SCENARIOS: list[Scenario] = [
    Scenario("S1", "pool sizing", s1_pool_sizing),
    Scenario("S2", "claim loop as a worker", s2_claim_loop),
    Scenario("S3", "worker-filed work", s3_worker_filed_work),
    Scenario("S4", "formulas", s4_formulas),
    Scenario("S5", "fence + scope", s5_fence_and_scope),
    Scenario("S6", "doctor", s6_doctor),
    Scenario("S7", "PostgreSQL claim race", s7_claim_race),
]


@dataclass
class Report:
    scenarios: list[Scenario] = field(default_factory=list)

    @property
    def failed(self) -> list[Scenario]:
        return [s for s in self.scenarios if not s.ok]


def main() -> int:
    only = set(sys.argv[1:])
    print(f"swarm e2e (Tier 1) — daemon at {API_URL}\n")

    try:
        setup()
    except (Failure, CliError) as exc:
        print(f"FAIL setup — {exc}")
        return 1

    state: dict = {}
    report = Report()
    for scenario in SCENARIOS:
        if only and scenario.key not in only:
            continue
        report.scenarios.append(scenario)
        started = time.monotonic()
        try:
            scenario.detail = scenario.fn(state) or ""
            scenario.ok = True
        except (Failure, CliError) as exc:
            scenario.ok = False
            scenario.detail = str(exc)
        except Exception as exc:  # noqa: BLE001 — a crash is a failed scenario
            scenario.ok = False
            scenario.detail = f"{exc.__class__.__name__}: {exc}"
        scenario.seconds = time.monotonic() - started
        status = "PASS" if scenario.ok else "FAIL"
        print(f"{status} {scenario.key} {scenario.title} ({scenario.seconds:.1f}s)")
        print(f"     {scenario.detail}")

    # A scenario that bailed mid-flight may have left swarm disabled.
    if state.get("swarm_disabled"):
        try:
            _set_swarm_enabled(True)
            print("\n(restored swarm.enabled=true after a failed S6)")
        except Exception:
            print("\n! could not restore swarm.enabled — run `aq system update-config` by hand")

    passed = len(report.scenarios) - len(report.failed)
    print(f"\n{passed}/{len(report.scenarios)} scenarios passed")
    if report.failed:
        print("failed: " + ", ".join(s.key for s in report.failed))
        print("\nTriage: scripts/e2e-daemon.sh logs 200 | aq doctor | aq system get-recent-events")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
