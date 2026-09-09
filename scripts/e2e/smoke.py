#!/usr/bin/env python3
"""Tier 1 functional-test kit — stateful CLI scenarios, no LLM.

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
    S8  project onboarding     link + init through the real CLI and daemon
    S9  task lifecycle         partial/full create, update, delete, rollback
    S10 workspace + writes     workspace, file, git and note CRUD
    S11 messages               queue, read, inject, reply; no external delivery
    S12 MCP registry           CRUD plus an unavailable optional endpoint
    S13 plugin extensions      installed entry point present and absent
    S14 graph + vault          layout mutations and isolated vault dry-run
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
POOL_CLASS = "standard-medium"

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


@dataclass(frozen=True)
class CliRun:
    """One CLI process, including the exit contract hidden by convenience helpers."""

    returncode: int
    stdout: str
    stderr: str


def _cli_env(
    *,
    token: str | None = None,
    session_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a subprocess environment fenced to the disposable e2e world.

    Most commands use HTTP, but plugin management and a few filesystem commands
    resolve the data directory/database directly during CLI startup.  A worker
    running this kit normally carries the production-refusal sentinel, so each
    child is pointed at the already-isolated e2e resources instead.  The worker's
    ``AQ_DB_SCOPE`` is deliberately retained; this helper never authorizes an
    upgrade or starts a daemon.
    """
    env = dict(os.environ)
    env["AQ_API_URL"] = API_URL
    env.pop("AQ_API_TOKEN", None)
    env.pop("AQ_SESSION_ID", None)
    if token:
        env["AQ_API_TOKEN"] = token
    if session_id:
        env["AQ_SESSION_ID"] = session_id
    e2e_home = env.get("AQ_E2E_HOME")
    if e2e_home:
        env["AGENT_QUEUE_DATA"] = e2e_home
    e2e_db = env.get("E2E_DB_URL")
    if e2e_db:
        env["AGENT_QUEUE_DB"] = e2e_db
        env["AQ_DATABASE_URL"] = e2e_db
    if extra:
        env.update(extra)
    return env


def run_aq(
    *args: str,
    json_mode: bool = True,
    token: str | None = None,
    session_id: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> CliRun:
    """Run the worktree CLI without interpreting success or failure."""
    argv = [sys.executable, AQ_LAUNCHER]
    if json_mode:
        argv.append("--json")
    argv.extend(args)
    proc = subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        env=_cli_env(token=token, session_id=session_id, extra=extra_env),
        timeout=timeout,
    )
    return CliRun(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def aq(
    *args: str,
    token: str | None = None,
    session_id: str | None = None,
    check_ok: bool = True,
    timeout: float = 120.0,
) -> dict | list:
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
    proc = run_aq(
        *args,
        token=token,
        session_id=session_id,
        timeout=timeout,
    )
    stdout = proc.stdout
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


def collection_rows(payload: dict | list, legacy_key: str) -> list[dict]:
    """Read a collection from the current envelope or its legacy wrapper.

    Collection commands now expose their rows directly as ``data: [...]``.
    Accepting the former keyed payload keeps the standalone fixture helpers
    compatible with raw CommandHandler responses used by focused tests.
    """
    if isinstance(payload, list):
        return payload
    rows = payload.get(legacy_key, [])
    if isinstance(rows, list):
        return rows
    raise Failure(f"{legacy_key} response is not a list: {payload}")


def aq_text(*args: str, timeout: float = 120.0) -> str:
    """Run a human-mode CLI command and return its stdout.

    A few hand-crafted commands intentionally render operator output instead
    of the JSON envelope used by :func:`aq`.  S8 uses this helper so project
    onboarding is exercised through the exact public CLI operators run, not
    by calling the service or ``/api/execute`` directly.
    """
    proc = run_aq(*args, json_mode=False, timeout=timeout)
    if proc.returncode != 0:
        raise Failure(
            f"aq {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stdout[:400]!r} / stderr {proc.stderr[:400]!r}"
        )
    return proc.stdout


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
    for row in collection_rows(aq("pool", "status", "--project-id", project_id), "pools"):
        if row["profile_id"] == profile_id:
            return row
    raise Failure(f"no pool row for {project_id}/{profile_id}")


def pool_sessions(project_id: str | None = PROJECT) -> list[dict]:
    rows = collection_rows(aq("session", "list", "--lifecycle", "pool"), "sessions")
    live = ("starting", "running")
    return [
        s
        for s in rows
        if (project_id is None or s["project_id"] == project_id) and s["state"] in live
    ]


def session_token(session_id: str) -> str:
    return aq("session", "token", session_id)["token"]


def create_task(title: str, *, project_id: str = PROJECT, profile: str | None = None) -> str:
    """``create_task`` over REST — ``aq task create`` has no JSON envelope yet."""
    args = {"project_id": project_id, "title": title, "description": f"e2e: {title}"}
    if profile:
        args["profile_id"] = profile
    if profile == POOL_PROFILE:
        # Tier 1 has no assignment-playbook LLM. Explicit classification is
        # therefore the fixture's deterministic assignment decision.
        args["intelligence_class"] = POOL_CLASS
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
    def adopt(cls, session_id: str) -> Worker:
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
        # ``no-op``, not ``shipped``: this runner is the session and commits
        # nothing, and a ``shipped`` close under the ``pull_request`` default
        # is refused for the PR the bare e2e remote can never carry (see
        # ``_close_next_child``).
        args = [
            "task", "close",
            "--outcome", "pass",
            "--summary", summary,
            "--work-outcome", "no-op",
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
        # A fresh-context close already marks a pool session for teardown.
        # The reconciler may stop it and revoke its token before this separate
        # CLI process receives a response, so the caller verifies durable
        # session state instead of treating an empty/error response as failure.
        return self.aq(
            "session", "drain-ack", "--session-id", self.session_id, check_ok=False
        )


def fresh_workers(count: int) -> list[Worker]:
    """Put the pool in a known state: exactly *count* idle, unspent workers.

    Scenarios that ran earlier leave the pool in whatever shape they
    finished in — sessions part-way through a claim, one retired and
    replaced, tasks half-worked.  S5 and S7 both need workers that can
    still claim, and S7 additionally needs an *empty* frontier so the one
    task it creates is the only thing to race for.  Rebuilding beats
    guessing:

    1. drain the pool profile's frontier in both fixture projects and kill
       every live session, in a loop.  Pool bounds are global per profile: an
       ``other`` task left by S5 can otherwise consume one of S7's two slots
       and make the race time out waiting for two workers in ``e2e``.  Both
       halves are needed and neither is enough on its own: a session holding a
       task blocks the delete, and demand that outlives a kill just makes the
       sizer start a replacement on the next tick.  The loop converges once
       both frontiers are empty and no session is live anywhere.
    2. create *count* filler tasks so the sizer starts *count* workers,
    3. delete the fillers again — nothing claims on its own under the fake
       provider, so they are still untouched and the frontier goes empty.
    """

    def _quiesced():
        for project_id in (PROJECT, OTHER_PROJECT):
            _delete_open_pool_tasks(project_id)
        live = pool_sessions(None)
        for s in live:
            aq("session", "kill", s["id"], check_ok=False)
        return not live and not any(
            _open_pool_tasks(project_id) for project_id in (PROJECT, OTHER_PROJECT)
        )

    wait_for(_quiesced, what="the pool to quiesce (no live sessions, empty frontier)")

    fillers = [create_task(f"pool primer {n}", profile=POOL_PROFILE) for n in range(count)]

    def _enough_sessions():
        rows = pool_sessions()
        return rows if len(rows) >= count else None

    live = wait_for(
        _enough_sessions,
        what=f"{count} fresh pool sessions",
    )
    for task_id in fillers:
        aq("task", "delete", "--task-id", task_id)
    return [Worker.adopt(s["id"]) for s in live[:count]]


def _open_pool_tasks(project_id: str = PROJECT) -> list[dict]:
    """Every unfinished task in one fixture project.

    ``aq task list`` already hides COMPLETED/FAILED/BLOCKED and returns a
    bare list, and its rows carry no ``profile_id`` — so this cannot filter
    to the pool profile.  It does not need to: at the S5/S7 boundary
    everything still open in either isolated fixture project is leftover
    scenario scaffolding, and clearing all of it is exactly the point.
    """
    rows = aq("task", "list", "--project", project_id)
    return list(rows) if isinstance(rows, list) else rows.get("tasks", [])


def _delete_open_pool_tasks(project_id: str = PROJECT) -> None:
    """Clear the frontier so a scenario starts from zero.

    ``--cascade`` because a worker-filed task from S3 may still hang off
    one of these; ``check_ok=False`` because a task a session is still
    holding refuses deletion, and the caller's loop retries after the kill
    has released it.
    """
    for task in _open_pool_tasks(project_id):
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
    existing = {p["id"] for p in collection_rows(aq("project", "list"), "projects")}
    if project_id not in existing:
        aq(
            "project", "create",
            "--name", project_id,
            "--default-profile-id", POOL_PROFILE,
        )
    have = {
        w["workspace_path"]
        for w in collection_rows(
            aq("project", "list-workspaces", "--project-id", project_id), "workspaces"
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
        any(
            r["profile_id"] == POOL_PROFILE
            for r in collection_rows(aq("pool", "status"), "pools")
        ),
        f"profile '{POOL_PROFILE}' is not a pool profile — is the vault fixture in place?",
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def s1_pool_sizing(state: dict) -> str:
    """Demand creates workers, and `max_active` caps them."""
    ids = [create_task(f"S1 worker task {n}", profile=POOL_PROFILE) for n in (1, 2, 3)]
    state["s1_tasks"] = ids

    def _pool_at_max():
        row = pool_row()
        return row if row["running_idle"] + row["running_busy"] + row["starting"] == 2 else None

    row = wait_for(_pool_at_max, what="the pool to reach max_active=2 sessions")
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
    """The full worker loop: claim, fence, request next, drain, replace."""
    initial_session_ids = {session["id"] for session in pool_sessions()}
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

    closed = worker.close(claim_next=True, summary="S2 task")
    nxt = closed.get("next") or {}
    check(
        nxt.get("result") == "drain_requested",
        f"fresh-context close --claim-next should request a drain, got {nxt.get('result')}",
    )
    check(nxt["session"]["claims"] == 1, f"claim counter should be 1: {nxt['session']}")
    check(task_show(first_task)["status"] in ("COMPLETED", "DONE"), "first task did not close")

    worker.drain_ack()

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
        return row if row.get("state") == "stopped" else None

    stopped = wait_for(_retired, what=f"session {worker.session_id} to reach state=stopped")
    check(
        stopped.get("end_reason") == "drained",
        f"fresh-context session stopped for {stopped.get('end_reason')!r}, not 'drained'",
    )

    orphan_check = _swarm_checks().get("pools.orphan_agents", {})
    check(
        orphan_check.get("severity") == "ok",
        f"pools.orphan_agents is {orphan_check.get('severity')}: {orphan_check.get('detail')}",
    )

    replacement = wait_for(
        lambda: next(
            (s for s in pool_sessions() if s["id"] not in initial_session_ids),
            None,
        ),
        what="a fresh-context replacement pool session",
    )
    return (
        f"claimed 1/1 then drain_requested; {worker.session_id} retired, "
        f"replaced by {replacement['id']}"
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
        {
            "title": "S3 discovered work",
            "description": "filed by a worker mid-task",
            "reason": "follow-up work discovered while executing the held task",
        },
        token=worker.token,
    )
    filed_id = filed.get("created") or filed.get("task_id")
    check(filed_id, f"worker-filed create_task returned no id: {filed}")
    state["s3_filed"] = filed_id
    check(
        filed.get("status") == "DEFINED",
        f"worker-filed create response must start DEFINED, got {filed.get('status')}",
    )

    row = task_show(filed_id)
    check(
        row["status"] in ("DEFINED", "READY"),
        f"worker-filed work moved to unexpected status {row['status']}",
    )
    check(row["is_blocked"], "worker-filed work lost its routing blocker")
    check(row["project_id"] == PROJECT, "worker-filed work escaped the session's project")
    check(
        row["profile_id"] == POOL_PROFILE,
        f"worker-filed work did not inherit the caller profile: {row['profile_id']}",
    )
    check(
        row["intelligence_class"] is None,
        f"unrouted work should carry no intelligence class: {row['intelligence_class']}",
    )

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
    routed = aq(
        "task", "route",
        "--task-id", filed_id,
        "--profile-id", POOL_PROFILE,
        "--intelligence-class", POOL_CLASS,
    )
    check(routed.get("success"), f"task route failed: {routed}")
    check(routed["resolved_gate_ids"], "routing did not resolve the gate")

    after = task_show(filed_id)
    check(after["profile_id"] == POOL_PROFILE, f"profile not written: {after['profile_id']}")
    check(
        after["intelligence_class"] == POOL_CLASS,
        f"intelligence class not written: {after['intelligence_class']}",
    )
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

    children = collection_rows(
        aq("task", "children", "--task-id", container), "children"
    )
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

    def _settled():
        task = task_show(container)
        return task if task["status"] in ("COMPLETED", "DONE") else None

    settled = wait_for(
        _settled,
        what=f"container {container} to settle",
    )
    return (
        f"cooked {container} ({', '.join(cooked['task_ids'])}); "
        f"as-cooked matches; settled as {settled['status']}"
    )


def _close_next_child(container: str) -> None:
    """Wait for the next child to be picked up, then close it as its session."""
    def _held():
        for child in collection_rows(
            aq("task", "children", "--task-id", container), "children"
        ):
            if child["status"] in ("COMPLETED", "DONE"):
                continue
            sessions = collection_rows(aq("session", "list"), "sessions")
            for s in sessions:
                if s.get("task_id") == child["id"] and s["state"] in ("starting", "running"):
                    return (child["id"], s["id"])
        return None

    task_id, session_id = wait_for(_held, what=f"a session to pick up a child of {container}")
    token = session_token(session_id)
    # ``no-op`` is the truthful outcome: under Tier 1 this runner *is* the
    # session and it commits nothing.  A ``shipped`` close is held to the
    # project's integration policy, and under the ``pull_request`` default
    # that means an open PR for the task branch — which a bare local
    # remote can never carry, so the close was refused and S4 never
    # settled (task solid-forge-63).
    out = aq(
        "task", "close", task_id,
        "--outcome", "pass",
        "--summary", "S4 child closed by its session",
        "--work-outcome", "no-op",
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

    # Pool bounds are global. Remove this scope-only fixture before closing
    # the holder frees capacity, or OTHER_PROJECT starts a worker that consumes
    # one of S7's two required slots for the entire scale-down grace period.
    aq("task", "delete", "--task-id", foreign)
    state.pop("s5_foreign", None)
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
        "fresh_context_per_task": True,
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

    def _pools_disabled_at(severity: str):
        check_result = _swarm_checks().get("pools.disabled", {})
        return check_result if check_result.get("severity") == severity else None

    try:
        disabled = wait_for(
            lambda: _pools_disabled_at("warn"),
            what="pools.disabled to warn after swarm.enabled=false",
            timeout=30,
        )
    finally:
        _set_swarm_enabled(True)
        state["swarm_disabled"] = False

    restored = wait_for(
        lambda: _pools_disabled_at("ok"),
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
    loser = next(o for o in outcomes if o != "claimed")
    check(
        loser in ("no_ready_work", "claim_conflict", "session_exhausted"),
        f"loser returned an unexpected result: {loser}",
    )
    return f"outcomes {outcomes} — one winner, loser said {loser}"


def _git_text(path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    check(
        proc.returncode == 0,
        f"git -C {path} {' '.join(args)} failed: {proc.stderr.strip()}",
    )
    return proc.stdout.strip()


def _onboarded_workspace(project_id: str, source_type: str, expected_path: str) -> dict:
    rows = collection_rows(
        aq("project", "list-workspaces", "--project-id", project_id), "workspaces"
    )
    check(len(rows) == 1, f"{project_id} has {len(rows)} workspaces, expected exactly one")
    workspace = rows[0]
    check(workspace["kind_id"] == "project-repo", f"wrong kind: {workspace}")
    check(workspace["source_type"] == source_type, f"wrong source type: {workspace}")
    check(workspace["enabled"] is True, f"primary workspace is disabled: {workspace}")
    check(
        os.path.realpath(workspace["workspace_path"]) == os.path.realpath(expected_path),
        f"workspace path differs from {expected_path}: {workspace}",
    )
    return workspace


def s8_project_onboarding(state: dict) -> str:
    """Link and initialize repositories through the real operator CLI."""
    root = os.environ.get(
        "E2E_ONBOARDING_ROOT",
        os.path.join(os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e")),
                     "onboarding"),
    )
    linked = os.path.join(root, "linked-repository")
    initialized = os.path.join(root, "initialized-repository")
    check(os.path.isdir(os.path.join(linked, ".git")), f"missing linked fixture: {linked}")

    linked_head = _git_text(linked, "rev-parse", "HEAD")
    linked_status = _git_text(linked, "status", "--porcelain=v1", "--untracked-files=all")

    link_output = aq_text(
        "project", "onboard",
        "--request-id", "e2e-link-onboarding-v1",
        "--source-mode", "link",
        "--root-id", "e2e-onboarding",
        "--relative-path", "linked-repository",
        "--project-name", "E2E Linked Project",
        "--project-id", "e2e-onboard-link",
    )
    check("e2e-onboard-link" in link_output, f"link CLI output omitted project: {link_output}")

    init_output = aq_text(
        "project", "onboard",
        "--request-id", "e2e-init-onboarding-v1",
        "--source-mode", "init",
        "--root-id", "e2e-onboarding",
        "--relative-path", "initialized-repository",
        "--project-name", "E2E Initialized Project",
        "--project-id", "e2e-onboard-init",
    )
    check("e2e-onboard-init" in init_output, f"init CLI output omitted project: {init_output}")

    projects = [
        row
        for row in collection_rows(aq("project", "list"), "projects")
        if row["id"] in {"e2e-onboard-link", "e2e-onboard-init"}
    ]
    check(len(projects) == 2, f"onboarding projects were not registered exactly once: {projects}")
    _onboarded_workspace("e2e-onboard-link", "link", linked)
    _onboarded_workspace("e2e-onboard-init", "init", initialized)

    check(_git_text(linked, "rev-parse", "HEAD") == linked_head, "link onboarding moved HEAD")
    check(
        _git_text(linked, "status", "--porcelain=v1", "--untracked-files=all")
        == linked_status,
        "link onboarding modified the repository",
    )
    check(
        _git_text(initialized, "symbolic-ref", "HEAD") == "refs/heads/main",
        "init onboarding did not default to main",
    )
    check(
        _git_text(initialized, "log", "-1", "--format=%s") == "Initial commit",
        "init onboarding did not create the default README commit",
    )
    readme = os.path.join(initialized, "README.md")
    check(os.path.isfile(readme), "init onboarding did not create README.md")
    with open(readme, encoding="utf-8") as handle:
        check(handle.read() == "# E2E Initialized Project\n", "README content is not normalized")

    vault = os.path.join(
        os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e")), "vault"
    )
    for project_id in ("e2e-onboard-link", "e2e-onboard-init"):
        project_vault = os.path.join(vault, "projects", project_id)
        for relative in ("memory/knowledge", "playbooks", "notes", "specs", "references"):
            check(
                os.path.isdir(os.path.join(project_vault, relative)),
                f"{project_id} is missing vault/{relative}",
            )

    return (
        "real CLI linked an unchanged repository and initialized main + README; "
        "each project has one enabled project-repo workspace and standard vault storage"
    )


def s9_task_lifecycle(state: dict) -> str:
    """Create through the real CLI, persist edits, delete, and prove rollback."""
    stamp = f"{os.getpid()}-{int(time.time())}"
    partial_title = f"S9 partial flags {stamp}"
    full_title = f"S9 full flags {stamp}"
    created: list[str] = []
    try:
        partial = run_aq("task", "create", "--project", PROJECT, "--title", partial_title)
        check(partial.returncode == 2, f"partial noninteractive create exited {partial.returncode}")
        try:
            partial_payload = json.loads(partial.stdout)
        except json.JSONDecodeError as exc:
            raise Failure(f"partial refusal was not a JSON envelope: {partial}") from exc
        partial_error = partial_payload.get("error") or {}
        check(partial_error.get("code") == "usage_error", f"wrong refusal envelope: {partial}")
        check(
            "--description" in partial_error.get("message", ""),
            f"partial refusal omitted missing flag: {partial}",
        )
        rows = aq("task", "list", "--project", PROJECT)
        rows = list(rows) if isinstance(rows, list) else rows.get("tasks", [])
        check(
            all(row.get("title") != partial_title for row in rows),
            "partial noninteractive create contacted the daemon and persisted a task",
        )

        full = aq(
            "task",
            "create",
            "--project",
            PROJECT,
            "--title",
            full_title,
            "--description",
            "stateful CLI full-flags receipt",
            "--priority",
            "137",
            "--type",
            "test",
            "--profile",
            POOL_PROFILE,
            "--intelligence-class",
            POOL_CLASS,
            "--requires-kind",
            "project-repo",
        )
        full_id = full.get("created") or full.get("task_id")
        check(full_id, f"full task-create JSON omitted its id: {full}")
        created.append(full_id)
        check(
            full.get("requires_kinds") == [{"kind": "project-repo", "alias": None}],
            f"full task-create receipt lost requires_kinds: {full}",
        )

        changed = aq(
            "task",
            "set",
            full_id,
            "--description",
            "stateful CLI updated description",
            "--label",
            "+e2e-stateful",
            "--meta",
            "source=smoke",
        )
        check(changed.get("success", True) is not False, f"task set failed: {changed}")
        after = task_show(full_id)
        check(after["description"] == "stateful CLI updated description", f"edit lost: {after}")
        check("e2e-stateful" in after.get("labels", []), f"label did not persist: {after}")

        invalid_title = f"S9 invalid priority {stamp}"
        refused = run_aq(
            "task",
            "create",
            "--project",
            PROJECT,
            "--title",
            invalid_title,
            "--description",
            "must roll back",
            "--priority",
            "0",
        )
        check(refused.returncode != 0, "priority=0 unexpectedly exited zero")
        rows = aq("task", "list", "--project", PROJECT)
        rows = list(rows) if isinstance(rows, list) else rows.get("tasks", [])
        check(
            all(row.get("title") != invalid_title for row in rows),
            "invalid priority persisted a task instead of rolling back",
        )
    finally:
        for task_id in created:
            aq("task", "delete", "--task-id", task_id, check_ok=False)

    for task_id in created:
        gone = run_aq("task", "show", task_id)
        check(gone.returncode != 0, f"deleted task {task_id} is still readable")
    return (
        f"partial flags exited 2 without persistence; full JSON receipt yielded {created}; "
        "update persisted; priority failure rolled back; task deleted"
    )


def _workspace_by_path(path: str) -> dict | None:
    rows = collection_rows(
        aq("project", "list-workspaces", "--project-id", PROJECT), "workspaces"
    )
    wanted = os.path.realpath(path)
    return next(
        (row for row in rows if os.path.realpath(row["workspace_path"]) == wanted),
        None,
    )


def s10_workspace_file_git_note(state: dict) -> str:
    """Exercise filesystem-backed families only inside a throwaway clone/vault."""
    home = os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e"))
    workspace_path = os.environ.get(
        "E2E_STATEFUL_WORKSPACE", os.path.join(home, "workspaces", "stateful-cli")
    )
    check(os.path.isdir(os.path.join(workspace_path, ".git")), f"missing clone {workspace_path}")
    stamp = f"{os.getpid()}-{int(time.time())}"
    workspace_name = f"stateful-cli-{stamp}"
    note_title = f"stateful-cli-{stamp}"
    target = os.path.join(workspace_path, f"stateful-{stamp}.txt")
    forbidden = os.path.join(home, "not-a-workspace", f"stateful-{stamp}.txt")
    workspace_id: str | None = None
    try:
        existing = _workspace_by_path(workspace_path)
        if existing:
            aq(
                "project",
                "remove-workspace",
                "--workspace-id",
                existing["id"],
                "--project-id",
                PROJECT,
            )
        aq(
            "project",
            "add-workspace",
            "--project-id",
            PROJECT,
            "--source",
            "link",
            "--path",
            workspace_path,
            "--name",
            workspace_name,
        )
        workspace = _workspace_by_path(workspace_path)
        check(workspace is not None, "workspace add was not visible on a separate read")
        workspace_id = workspace["id"]

        duplicate = aq(
            "project",
            "add-workspace",
            "--project-id",
            PROJECT,
            "--source",
            "link",
            "--path",
            workspace_path,
            "--name",
            f"duplicate-{stamp}",
            check_ok=False,
        )
        check(duplicate.get("_error") is not None, "duplicate workspace add was not refused")
        matches = [
            row
            for row in collection_rows(
                aq("project", "list-workspaces", "--project-id", PROJECT), "workspaces"
            )
            if os.path.realpath(row["workspace_path"]) == os.path.realpath(workspace_path)
        ]
        check(len(matches) == 1, f"duplicate workspace add was not atomic: {matches}")

        branch = f"e2e/stateful-{stamp}"
        aq(
            "git",
            "create-branch",
            "--project-id",
            PROJECT,
            "--workspace",
            workspace_id,
            "--branch-name",
            branch,
        )
        aq("file", "write", "--path", target, "--content", "created")
        read = aq("file", "read", "--path", target)
        check(read.get("content") == "created", f"file read differed from write: {read}")
        aq(
            "file",
            "edit",
            "--path",
            target,
            "--old-string",
            "created",
            "--new-string",
            "updated",
        )
        check(aq("file", "read", "--path", target).get("content") == "updated", "edit lost")

        escaped = aq("file", "write", "--path", forbidden, "--content", "escape", check_ok=False)
        check(escaped.get("_error") is not None, "file write outside registered workspaces succeeded")
        check(not os.path.exists(forbidden), "refused file write left data outside the workspace")

        commit = aq(
            "git",
            "commit",
            "--project-id",
            PROJECT,
            "--workspace",
            workspace_id,
            "--message",
            "e2e: stateful CLI write",
        )
        check(commit.get("success", True) is not False, f"git commit failed: {commit}")
        aq(
            "git",
            "push",
            "--project-id",
            PROJECT,
            "--workspace",
            workspace_id,
            "--branch",
            branch,
        )
        log = aq(
            "git", "log", "--project-id", PROJECT, "--workspace", workspace_id, "--count", "1"
        )
        check("stateful CLI write" in json.dumps(log), f"git log omitted committed mutation: {log}")

        aq(
            "note",
            "write",
            "--project-id",
            PROJECT,
            "--title",
            note_title,
            "--content",
            "first",
        )
        aq(
            "note",
            "append",
            "--project-id",
            PROJECT,
            "--title",
            note_title,
            "--content",
            "second",
        )
        note = aq("note", "read", "--project-id", PROJECT, "--title", note_title)
        check("first" in note.get("content", "") and "second" in note.get("content", ""), note)
        aq("note", "delete", "--project-id", PROJECT, "--title", note_title)
        missing_note = aq(
            "note", "read", "--project-id", PROJECT, "--title", note_title, check_ok=False
        )
        check(missing_note.get("_error") is not None, "deleted note remained readable")
    finally:
        aq("note", "delete", "--project-id", PROJECT, "--title", note_title, check_ok=False)
        if workspace_id:
            aq(
                "project",
                "remove-workspace",
                "--workspace-id",
                workspace_id,
                "--project-id",
                PROJECT,
                check_ok=False,
            )
        if os.path.exists(forbidden):
            os.unlink(forbidden)

    check(_workspace_by_path(workspace_path) is None, "workspace record survived removal")
    return "workspace/file/git/note CRUD persisted across processes; refusals rolled back"


def s11_messages(state: dict) -> str:
    """Use a database-only sink recipient: no Discord, webhook, or real user."""
    # Profile mailboxes are intentionally pull-only: the delivery engine does
    # not wake or contact anything for them, so this is a deterministic sink
    # whose queued -> injected transition belongs entirely to these CLI calls.
    recipient = f"profile:e2e-sink-{os.getpid()}"
    sent = aq(
        "message",
        "send",
        "--project",
        PROJECT,
        "--to",
        recipient,
        "--body",
        "stateful message",
        "--subject",
        "e2e only",
        "--thread-id",
        f"e2e-{os.getpid()}",
    )
    message_id = sent.get("message_id")
    check(message_id, f"message send omitted id: {sent}")
    inbox = aq("message", "inbox", "--to", recipient)
    messages = collection_rows(inbox, "messages")
    check(any(row.get("id") == message_id for row in messages), f"message not persisted: {inbox}")
    queued = aq("message", "status", message_id)
    check(queued.get("state") == "queued", f"new sink message is not queued: {queued}")

    injected = aq("message", "inbox", "--to", recipient, "--inject")
    check(
        any(row.get("id") == message_id for row in collection_rows(injected, "messages")),
        injected,
    )
    delivered = aq("message", "status", message_id)
    check(delivered.get("state") in ("delivered", "acknowledged"), delivered)
    reply = aq("message", "reply", message_id, "stateful reply", "--via", "e2e-sink")
    check(reply.get("reply_id"), f"reply omitted id: {reply}")

    malformed = run_aq("message", "send", "--to", "not-a-recipient", "--body", "nope")
    check(malformed.returncode != 0, "malformed recipient unexpectedly exited zero")
    return f"queued/injected/replied to {message_id} through database-only sink; bad target refused"


def s12_mcp_registry(state: dict) -> str:
    """Persist MCP registry changes while treating an absent service as a capability result."""
    name = f"e2e-unavailable-{os.getpid()}"
    aq("mcp", "delete-server", "--name", name, "--project-id", PROJECT, check_ok=False)
    try:
        aq(
            "mcp",
            "create-server",
            "--name",
            name,
            "--transport",
            "http",
            "--project-id",
            PROJECT,
            "--description",
            "deliberately unavailable e2e endpoint",
            "--url",
            "http://127.0.0.1:1/mcp",
        )
        shown = aq("mcp", "get-server", "--name", name, "--project-id", PROJECT)
        check(shown.get("name") == name, f"MCP registry write did not persist: {shown}")
        probe = aq(
            "mcp", "probe-server", "--name", name, "--project-id", PROJECT, check_ok=False
        )
        if probe.get("_error") is None:
            probe_text = json.dumps(probe).lower()
            check(
                any(word in probe_text for word in ("error", "failed", "unavailable", "refused")),
                f"unreachable MCP endpoint was reported healthy: {probe}",
            )
    finally:
        aq("mcp", "delete-server", "--name", name, "--project-id", PROJECT, check_ok=False)
    visible = aq("mcp", "list-servers", "--project-id", PROJECT)
    check(name not in json.dumps(visible), "deleted MCP server remained in the registry")
    return "registry create/read/delete passed; loopback:1 probe = dependency-unavailable"


def s13_plugin_extensions(state: dict) -> str:
    """Prove CLI entry-point discovery both with and without a disposable plugin."""
    fixture = os.environ.get("AQ_E2E_PLUGIN_FIXTURE", "")
    check(os.path.isdir(fixture), f"missing generated plugin fixture directory: {fixture!r}")
    inherited = os.environ.get("PYTHONPATH", "")
    plugin_path = fixture + (os.pathsep + inherited if inherited else "")
    present = run_aq(
        "e2e-fixture", "ping", "--value", "stateful", json_mode=False,
        extra_env={"PYTHONPATH": plugin_path},
    )
    check(present.returncode == 0, f"plugin-present CLI failed: {present}")
    check(present.stdout == "e2e-plugin:stateful", f"unexpected plugin output: {present.stdout!r}")

    absent = run_aq("e2e-fixture", "ping", json_mode=False)
    check(absent.returncode == 2, f"plugin-absent command exited {absent.returncode}, expected 2")
    check("No such command" in absent.stderr, f"plugin absence was not actionable: {absent}")
    return "disposable entry point loaded when present and was an exit-2 unknown command when absent"


def s14_graph_and_vault(state: dict) -> str:
    """Exercise safe graph mutations and a read-only vault migration preview."""
    rebuilt = aq("graph", "layout-rebuild", "--project-id", PROJECT)
    check(set(rebuilt.get("versions", {})) == {"all", "active"}, f"layout rebuild: {rebuilt}")
    tidy = aq("graph", "tidy", "--project-id", PROJECT, "--variant", "active")
    check(len(tidy.get("jobs", [])) == 1, f"graph tidy did not enqueue one job: {tidy}")
    missing = aq("graph", "layout-rebuild", "--project-id", "e2e-does-not-exist", check_ok=False)
    check(missing.get("_error") is not None, "graph rebuild accepted a missing project")

    home = os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e"))
    preview = aq_text("vault", "migrate", "--dry-run", "--data-dir", home, "--project", PROJECT)
    check("dry" in preview.lower() or "would" in preview.lower(), f"vault preview unclear: {preview}")
    return "layout rebuild/tidy persisted through daemon; isolated vault migration preview made no writes"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    key: str
    title: str
    fn: object
    families: tuple[str, ...] = ()
    result: str = ""
    ok: bool | None = None
    seconds: float = 0.0
    detail: str = ""


SCENARIOS: list[Scenario] = [
    Scenario("S1", "pool sizing", s1_pool_sizing, ("agent/session/pool",)),
    Scenario("S2", "claim loop as a worker", s2_claim_loop, ("task/session/workspace",)),
    Scenario("S3", "worker-filed work", s3_worker_filed_work, ("task/dependency/gate",)),
    Scenario("S4", "formulas", s4_formulas, ("formula/task-graph",)),
    Scenario("S5", "fence + scope", s5_fence_and_scope, ("authentication/scope",)),
    Scenario("S6", "doctor", s6_doctor, ("config/doctor",)),
    Scenario("S7", "PostgreSQL claim race", s7_claim_race, ("task/claim/postgresql",)),
    Scenario("S8", "project onboarding", s8_project_onboarding, ("project/git/vault",)),
    Scenario("S9", "task lifecycle", s9_task_lifecycle, ("task CRUD/rollback",)),
    Scenario(
        "S10",
        "workspace + file/git/note writes",
        s10_workspace_file_git_note,
        ("workspace CRUD", "file/git/note CRUD"),
    ),
    Scenario("S11", "messages", s11_messages, ("message CRUD",)),
    Scenario("S12", "MCP registry", s12_mcp_registry, ("MCP registry CRUD",)),
    Scenario("S13", "plugin extensions", s13_plugin_extensions, ("plugin extension startup",)),
    Scenario("S14", "graph + vault", s14_graph_and_vault, ("graph/vault",)),
]

# These exclusions are intentional properties of Tier 1, not silent omissions.
# The final report uses the same vocabulary for every family, including failures.
EXPLICIT_CAPABILITIES: tuple[tuple[str, str, str], ...] = (
    (
        "memory semantic search",
        "dependency-unavailable",
        "memory.enabled=false; no Milvus/Ollama or aq-memory service is contacted",
    ),
    (
        "MCP remote tools",
        "dependency-unavailable",
        "S12 probes only a deliberately closed loopback port",
    ),
    (
        "agent questions",
        "unsupported",
        "questions belong to harness transcripts; no second CLI ask-human state machine",
    ),
    (
        "plugin hook history",
        "unsupported",
        "hooks were replaced by playbooks; the compatibility command is not history",
    ),
    (
        "real providers/tmux/external messaging",
        "explicitly-untested",
        "Tier 2 only; Tier 1 spends no tokens and sends no network messages",
    ),
    (
        "plugin install/update/remove from remote git",
        "explicitly-untested",
        "would mutate the interpreter environment or require an external repository",
    ),
    (
        "playbook activation/run",
        "explicitly-untested",
        "playbooks.enabled=false in deterministic Tier 1; formula mutation is covered by S4",
    ),
    (
        "database upgrade/daemon control",
        "explicitly-untested",
        "workers never migrate databases or start/stop anything except the disposable daemon wrapper",
    ),
)


@dataclass
class Report:
    scenarios: list[Scenario] = field(default_factory=list)

    @property
    def failed(self) -> list[Scenario]:
        return [s for s in self.scenarios if not s.ok]

    def capability_rows(self) -> list[dict[str, str]]:
        rows = [
            {
                "family": family,
                "status": "passed" if scenario.ok else "broken",
                "detail": f"{scenario.key}: {scenario.detail}",
            }
            for scenario in self.scenarios
            for family in scenario.families
        ]
        rows.extend(
            {"family": family, "status": status, "detail": detail}
            for family, status, detail in EXPLICIT_CAPABILITIES
        )
        return rows


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
        except Exception:  # noqa: BLE001 — cleanup failure belongs in the report
            print("\n! could not restore swarm.enabled — run `aq system update-config` by hand")

    passed = len(report.scenarios) - len(report.failed)
    print(f"\n{passed}/{len(report.scenarios)} scenarios passed")
    print("\nCapability report (passed | broken | unsupported | dependency-unavailable | explicitly-untested)")
    for row in report.capability_rows():
        print(f"  {row['status']:<22} {row['family']}: {row['detail']}")
    if report.failed:
        print("failed: " + ", ".join(s.key for s in report.failed))
        print("\nTriage: scripts/e2e-daemon.sh logs 200 | aq doctor | aq system get-recent-events")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
