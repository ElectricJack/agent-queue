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
    S15 development delivery   local validation and exact Git publication
    S16 provider failover      exhaust a fake provider: detect, re-route, hold, recover
    S17 phased graph           real CLI graph phases, subtasks, prime, and close semantics
    S18 failure triage         durable task.failed playbook dispatch and supervisor notice
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AQ_LAUNCHER = os.path.join(REPO_ROOT, "scripts", "e2e", "aq.py")
API_URL = os.environ.get("AQ_API_URL", "http://127.0.0.1:8099").rstrip("/")

PROJECT = "e2e"
OTHER_PROJECT = "other"
POOL_PROFILE = "worker"
POOL_CLASS = "fast-high"
PLANNER_PROFILE = "planner"

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
    """``POST /api/execute`` — fixture setup and background state inspection.

    ``gate_list``, ``explain_task`` and friends are categorized but carry
    only a codegen input schema, so the auto-generated Click command takes
    no options.  The REST endpoint does, and it is just as public. Polling
    also uses this path so waiting for the daemon does not repeatedly launch
    a Python CLI. Scenario mutations and explicit CLI assertions still use aq.
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


def api_checked(command: str, args: dict) -> dict | list:
    """Run a fixture command, failing promptly if it was refused."""
    result = api(command, args)
    if isinstance(result, dict):
        check(result.get("success", True) is not False and "error" not in result,
              f"{command} fixture command failed: {result}")
    return result


def wait_for(
    predicate, *, what: str, timeout: float = CONVERGE_TIMEOUT, interval: float = 2.0,
    extend_if=None, diagnostic=None,
):
    """Poll *predicate* until it returns something truthy, or fail loudly.

    Every wait in this file is on the 5s cascade, so the failure message
    matters more than the mechanism: "the pool never reached 2 sessions
    (last saw 1)" is a bug report; "timeout" is not.
    """
    deadline = time.monotonic() + timeout
    last = None
    extended = False
    allowed = timeout
    while True:
        last = predicate()
        if last:
            return last
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
            continue
        if extend_if is not None and not extended:
            extra = extend_if()
            if extra > 0:
                extended = True
                allowed += extra
                deadline = time.monotonic() + extra
                print(f"     pool wait for {what}: allowing {extra:.0f}s more for convergence")
                continue
        detail = f"; {diagnostic()}" if diagnostic is not None else ""
        raise Failure(
            f"timed out after {allowed:.0f}s waiting for {what} "
            f"(last saw {last!r}{detail})"
        )


# ---------------------------------------------------------------------------
# Small domain helpers
# ---------------------------------------------------------------------------


def pool_row(project_id: str = PROJECT, profile_id: str = POOL_PROFILE) -> dict:
    for row in collection_rows(api_checked("pool_status", {"project_id": project_id}), "pools"):
        if row["profile_id"] == profile_id:
            return row
    raise Failure(f"no pool row for {project_id}/{profile_id}")


def pool_sessions(
    project_id: str | None = PROJECT, *, include_draining: bool = False,
) -> list[dict]:
    rows = collection_rows(api_checked("session_list", {"lifecycle": "pool"}), "sessions")
    live = ("starting", "running", "draining") if include_draining else ("starting", "running")
    return [
        s
        for s in rows
        if (project_id is None or s["project_id"] == project_id) and s["state"] in live
    ]


def pool_wait_state(project_id: str, profile_id: str) -> dict:
    """The daemon's own placement inputs, kept compact for a timeout report."""
    try:
        row = pool_row(project_id, profile_id)
    except (CliError, Failure) as exc:
        return {"project": project_id, "profile": profile_id, "status_error": str(exc)}
    project = next(
        (item for item in row.get("projects", []) if item["project_id"] == project_id),
        {},
    )
    return {
        "project": project_id,
        "profile": profile_id,
        "enabled": row.get("enabled"),
        "ready": row.get("ready"),
        "desired": row.get("desired"),
        "running_idle": row.get("running_idle"),
        "running_busy": row.get("running_busy"),
        "starting": row.get("starting"),
        "draining": row.get("draining"),
        "provider_unavailable": row.get("provider_unavailable"),
        "placement": project,
        "instances": [
            {key: instance.get(key) for key in ("session_id", "state", "task_id", "quarantine_reason")}
            for instance in row.get("instances", [])
            if instance.get("project_id") == project_id
        ],
    }


def wait_for_pool_session(
    predicate, *, what: str, project_id: str = PROJECT, profile_id: str = POOL_PROFILE,
    timeout: float = CONVERGE_TIMEOUT,
):
    """Give an observable launch one more window and explain a failed placement."""
    def status() -> dict:
        return pool_wait_state(project_id, profile_id)

    def extend_if() -> float:
        snapshot = status()
        placement = snapshot.get("placement") or {}
        if snapshot.get("provider_unavailable") or snapshot.get("enabled") is False:
            return 0
        quarantine_left = (placement.get("quarantined_until") or 0) - time.time()
        # A launch backoff that will clear within the extra window is
        # convergence.  A long crash quarantine is a real blocker; report it
        # at the original deadline instead of waiting pointlessly.
        if quarantine_left > 0:
            return timeout if quarantine_left < timeout else 0
        if placement.get("starting", 0):
            return timeout
        supply = sum(snapshot.get(key) or 0 for key in ("running_idle", "running_busy", "starting"))
        if (placement.get("ready", 0) and placement.get("workspace_capacity", 0)
                and (snapshot.get("desired") or 0) > supply):
            return timeout
        return 0

    return wait_for(
        predicate, what=what, timeout=timeout, extend_if=extend_if,
        diagnostic=lambda: f"pool_status={json.dumps(status(), sort_keys=True)}",
    )


def session_token(session_id: str) -> str:
    return aq("session", "token", session_id)["token"]


def create_task(
    title: str,
    *,
    project_id: str = PROJECT,
    profile: str | None = None,
    intelligence_class: str | None = None,
) -> str:
    """``create_task`` over REST — ``aq task create`` has no JSON envelope yet."""
    args = {"project_id": project_id, "title": title, "description": f"e2e: {title}"}
    if profile:
        args["profile_id"] = profile
    if intelligence_class is None and profile == POOL_PROFILE:
        # Tier 1 has no assignment-playbook LLM. Explicit classification is
        # therefore the fixture's deterministic assignment decision.
        intelligence_class = POOL_CLASS
    if intelligence_class is not None:
        args["intelligence_class"] = intelligence_class
    result = api("create_task", args)
    task_id = result.get("created") or result.get("task_id")
    check(task_id, f"create_task({title}) returned no id: {result}")
    return task_id


def task_show(task_id: str) -> dict:
    return api_checked("task_show", {"task_id": task_id})


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


def fresh_workers(
    count: int,
    *,
    project_id: str = PROJECT,
    cleanup_projects: tuple[str, ...] = (PROJECT, OTHER_PROJECT),
) -> list[Worker]:
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
        for cleanup_project_id in cleanup_projects:
            _delete_open_pool_tasks(cleanup_project_id)
        # Draining rows still consume supply/capacity until the reconciler
        # marks them stopped.  Leaving them behind can starve the next case.
        live = pool_sessions(None, include_draining=True)
        for s in live:
            api("session_kill", {"session_id": s["id"]})
        return not live and not any(
            _open_pool_tasks(cleanup_project_id) for cleanup_project_id in cleanup_projects
        )

    wait_for(_quiesced, what="the pool to quiesce (no live sessions, empty frontier)")

    fillers = [
        create_task(f"pool primer {n}", project_id=project_id, profile=POOL_PROFILE)
        for n in range(count)
    ]

    def _enough_sessions():
        rows = pool_sessions(project_id)
        return rows if len(rows) >= count else None

    live = wait_for_pool_session(
        _enough_sessions,
        what=f"{count} fresh pool sessions",
        project_id=project_id,
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
    rows = api_checked("list_tasks", {"project_id": project_id})
    return list(rows) if isinstance(rows, list) else rows.get("tasks", [])


def _delete_open_pool_tasks(project_id: str = PROJECT) -> None:
    """Clear the frontier so a scenario starts from zero.

    Cascade because a worker-filed task from S3 may still hang off one of
    these. A refusal is retried by the caller after killing its session.
    This is fixture cleanup; S9 asserts task deletion through the real CLI.
    """
    for task in _open_pool_tasks(project_id):
        api("delete_task", {"task_id": task["id"], "cascade": True})


def idle_worker() -> Worker:
    """Adopt whichever pool session currently holds no task."""

    def _find():
        for s in pool_sessions():
            if not s.get("task_id"):
                return s["id"]
        return None

    sid = wait_for_pool_session(_find, what="an idle pool session")
    return Worker.adopt(sid)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def ensure_project(project_id: str, workspaces: list[str]) -> None:
    """Prepare scaffolding through the same public handlers as the CLI.

    Registration runs both on daemon startup and before scenarios. It is
    fixture setup; S8/S10 independently assert the real project/workspace CLI.
    """
    existing = {p["id"] for p in collection_rows(api_checked("list_projects", {}), "projects")}
    if project_id not in existing:
        api_checked("create_project", {"name": project_id, "default_profile_id": POOL_PROFILE})
    have = {
        w["workspace_path"]
        for w in collection_rows(
            api_checked("list_workspaces", {"project_id": project_id}), "workspaces"
        )
    }
    for path in workspaces:
        if path in have:
            continue
        api_checked("add_workspace", {
            "project_id": project_id, "source": "link", "path": path,
            "name": os.path.basename(path),
        })


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
            for r in collection_rows(api_checked("pool_status", {}), "pools")
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

    row = wait_for_pool_session(_pool_at_max, what="the pool to reach max_active=2 sessions")
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
        shown = api_checked("session_show", {"session_id": worker.session_id})
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

    replacement = wait_for_pool_session(
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

    # A bare worker filing lands as a child of the held task with no gate of
    # its own (7d5af2879; tests/test_worker_filing.py).  The durable routing
    # gate this scenario exercises belongs to a *root* filing -- cross-cutting
    # work that does not ship with the held deliverable -- so ask for one.
    filed = api(
        "create_task",
        {
            "title": "S3 discovered work",
            "description": "filed by a worker mid-task",
            "reason": "follow-up work discovered while executing the held task",
            "root": True,
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
    check(row.get("parent_task_id") is None, f"root filing got a parent: {row.get('parent_task_id')}")
    check(row["project_id"] == PROJECT, "worker-filed work escaped the session's project")
    # The filer's profile bounds an explicit --profile; it is never the route.
    check(
        not row["profile_id"],
        f"worker-filed work inherited the filer's route: {row['profile_id']}",
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

    task_id, session_id = wait_for_pool_session(
        _held, what=f"a session to pick up a child of {container}"
    )
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
        after = aq("task", "show", full_id)
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


DEVELOPMENT_VALIDATION_COMMAND = "pytest test_validation.py -q"


def _seed_development_validation(source: Path) -> None:
    """Seed a finite pytest equivalent of the fixtures' README file check."""
    (source / "test_validation.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_readme_exists():\n"
        "    assert Path('README.md').is_file()\n"
    )


def s15_development_delivery(state: dict) -> str:
    """Operator configure → real published branch → AQ validation/promotion → adoption."""
    from pathlib import Path
    root = Path(os.environ["AQ_E2E_HOME"]) / "onboarding"
    remote, source = root / "development.git", root / "development-source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    _git_text(str(source), "config", "user.name", "AQ E2E")
    _git_text(str(source), "config", "user.email", "e2e@example.test")
    (source / "README.md").write_text("development fixture\n")
    _seed_development_validation(source)
    _git_text(str(source), "add", ".")
    _git_text(str(source), "commit", "-m", "base")
    _git_text(str(source), "push", "origin", "main")
    aq_text("project", "onboard", "--request-id", "e2e-development", "--source-mode", "link",
            "--root-id", "e2e-onboarding", "--relative-path", "development-source",
            "--project-name", "Development", "--project-id", "e2e-development")
    configured = aq("integration", "develop", "e2e-development", "--command", DEVELOPMENT_VALIDATION_COMMAND,
                    "--interval-seconds", "86400", "--reason", "isolated acceptance")
    check(configured.get("outcome") == "configured", str(configured))
    _git_text(str(source), "checkout", "-b", "fixture-feature")
    (source / "feature.txt").write_text("delivered by AQ\n")
    _git_text(str(source), "add", ".")
    _git_text(str(source), "commit", "-m", "feature")
    head = _git_text(str(source), "rev-parse", "HEAD")
    _git_text(str(source), "push", "origin", "fixture-feature")
    task = api("create_task", {"project_id": "e2e-development", "repo_id": configured["repository_id"],
        "title": "development delivery", "description": "real Git fixture"})
    task_id = task.get("task_id") or task.get("created")
    check(bool(task_id), str(task))
    aq("task", "set", task_id, "--branch", "fixture-feature")
    aq("task", "set-status", "--task-id", task_id, "--status", "COMPLETED")
    successor = api("create_task", {
        "project_id": "e2e-development", "title": "wait for delivered code",
        "description": "must not start before the prerequisite reaches main",
        "profile_id": POOL_PROFILE, "intelligence_class": POOL_CLASS,
    })
    successor_id = successor.get("task_id") or successor.get("created")
    check(bool(successor_id), str(successor))
    api("add_dependency", {"task_id": successor_id, "depends_on": task_id})
    check(task_show(successor_id)["is_blocked"], "undelivered code released its successor")
    result = aq("integration", "sweep", "e2e-development")
    check(result.get("outcome") == "delivered", str(result))
    check(_git_text(str(remote), "rev-parse", "main") == head, "AQ did not promote exact checked commit")
    check(not task_show(successor_id)["is_blocked"], "publication did not release the successor")
    status = aq("integration", "status", "e2e-development")
    delivery = next((row for row in status.get("deliveries", []) if row["id"] == result["id"]), None)
    check(delivery is not None, f"missing delivery receipt: {status}")
    evidence = delivery["evidence"]
    checks = evidence.get("checks", [])
    check(evidence.get("conclusion") == "passed" and len(checks) == 1,
          f"validation did not pass: {evidence}")
    validation = checks[0]
    check(validation.get("command") == DEVELOPMENT_VALIDATION_COMMAND
          and bool(validation.get("job_id")) and bool(validation.get("result_hash"))
          and validation.get("exit_code") == 0 and validation.get("outcome") == "passed"
          and validation.get("input_ref") == head and validation.get("input_mode") == "snapshot",
          f"validation receipt does not attest the published snapshot: {validation}")
    adopted = aq("integration", "adopt", "e2e-development", "--task", task_id,
                 "--head-sha", head, "--reason", "prove repeatable operator reconciliation")
    check(adopted.get("outcome") == "adopted", str(adopted))
    status = aq("integration", "status", "e2e-development")
    check(status.get("effective_mode") == "development", str(status))

    # S16 starts by quiescing the e2e project's worker fleet.  Do not leave
    # this S15-only READY successor continuously replacing a worker in its
    # separate development project, or that cross-project session keeps the
    # global pool nonempty forever.  A delete can race an already-started fake
    # session, so stop it and retry the public delete until it lands.
    def _delete_successor() -> bool:
        for session in pool_sessions("e2e-development"):
            aq("session", "kill", session["id"], check_ok=False)
        deleted = aq("task", "delete", "--task-id", successor_id, check_ok=False)
        return deleted.get("deleted") == successor_id

    wait_for(_delete_successor, what=f"S15 successor {successor_id} to be removed")
    return "managed pytest snapshot validation and exact Git publication through real AQ CLI; operator adoption recorded without CI fabrication"


# ---------------------------------------------------------------------------
# S16 — provider failover (docs/specs/provider-failover.md D23, bold-rapids.5)
# ---------------------------------------------------------------------------

#: The fake providers of ``tests/fixtures/provider_failover`` and their rungs.
PROVA, PROVB = "prova", "provb"
STD_A, STD_B, SOLO_A = "std-high-prova", "std-high-provb", "solo-high-prova"
FAILOVER_PROFILES = (STD_A, STD_B, SOLO_A)
FAILOVER_PLAYBOOK = "provider-failover"


def note(message: str) -> None:
    """One line of a long scenario's narrative, printed as it happens.

    S16 is the end-to-end transcript bold-rapids.5 attaches, so it says what
    it is doing at each step rather than only how it finished.
    """
    print(f"     · {message}", flush=True)


def fake_script(**modes: str) -> None:
    """Rewrite the fake session provider's script (``sessions.fake_script_file``).

    Re-read on every fake start and by the fake login probe, so this is how
    the scenario logs a provider out, exhausts it, or restores it.
    """
    path = os.environ.get("E2E_FAKE_SCRIPT") or os.path.join(
        os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e")),
        "fake-provider-script.json",
    )
    script = {PROVA: "ok", PROVB: "ok", **modes}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(script, fh)
    note(f"fake script: {json.dumps(script)}")


def provider(key: str) -> dict:
    """*key*'s availability row, or ``{}`` before the daemon tracks it.

    Tracked providers are re-read from the profiles once a minute, so a
    daemon started moments ago may not list one yet; callers poll.
    """
    rows = collection_rows(api_checked("provider_status", {"provider": key}), "providers")
    return rows[0] if rows else {}


def provider_state(key: str) -> str:
    return str(provider(key).get("state") or "")


def wait_provider(key: str, states: tuple[str, ...], *, what: str) -> dict:
    return wait_for(
        lambda: (row if (row := provider(key)).get("state") in states else None),
        what=f"{key} to become {'/'.join(states)} ({what})",
    )


def set_provider_state(key: str, state: str, reason: str = "e2e S16") -> dict:
    args = ["provider", "set-state", "--provider", key, "--state", state]
    if state != "auto":
        args += ["--reason", reason, "--for", "30m"]
    return aq(*args)


def set_failover_policy_enabled(enabled: bool) -> None:
    """Keep S16's operator-driven sweeps isolated from the automatic policy."""
    result = aq(
        "playbook",
        "set-enabled",
        "--playbook-id",
        FAILOVER_PLAYBOOK,
        "--enabled" if enabled else "--no-enabled",
    )
    check(result.get("enabled") is enabled, f"could not set {FAILOVER_PLAYBOOK}: {result}")


def held_tasks() -> dict[str, dict]:
    rows = collection_rows(aq("provider", "held-tasks", "--project-id", PROJECT), "tasks")
    return {row["task_id"]: row for row in rows}


def reroute(*extra: str) -> dict:
    return aq("provider", "reroute", *extra)


def login_deaths(key: str, *, since: float) -> list[dict]:
    """Launches that died on *key*'s login dialog since *since*, from its evidence ring.

    A startup death leaves no session row ``aq session list`` shows, but
    every one is ``startup_dialog`` evidence on the provider.
    """
    rows = collection_rows(
        aq("provider", "status", "--provider", key, "--verbose"), "providers"
    )
    return [
        e for e in rows[0].get("evidence") or []
        if e.get("kind") == "startup_dialog" and float(e.get("at") or 0) >= since
    ]


def live_sessions_for(profile_id: str) -> list[dict]:
    return [
        s for s in pool_sessions(None)
        if s.get("profile_id") == profile_id
    ]


def failover_task(title: str, profile: str, cls: str, *, priority: int, **extra) -> str:
    args = {
        "project_id": PROJECT,
        "title": title,
        "description": f"e2e S16: {title}",
        "profile_id": profile,
        "intelligence_class": cls,
        "priority": priority,
        **extra,
    }
    result = api("create_task", args)
    task_id = result.get("created") or result.get("task_id")
    check(task_id, f"create_task({title}) returned no id: {result}")
    return task_id


def provider_escalations() -> list[dict]:
    payload = aq("escalation", "list", "--project-id", PROJECT)
    rows = payload.get("escalations", []) if isinstance(payload, dict) else payload
    return [row for row in rows if row.get("source_kind") == "provider_availability"]


def _quiesce_failover() -> None:
    """Start from a clean fleet: no open e2e tasks, no live pool sessions."""

    def _quiet():
        _delete_open_pool_tasks(PROJECT)
        live = pool_sessions(None, include_draining=True)
        for s in live:
            api("session_kill", {"session_id": s["id"]})
        return not live and not _open_pool_tasks(PROJECT)

    wait_for(_quiet, what="the fleet to quiesce before S16")


def _restore_providers() -> None:
    """Leave every provider launchable again, whatever S16 got up to.

    The fleet is quiesced *first*: ``auto`` puts a still-unavailable provider
    on probation, and probation admits one canary launch -- which queued S16
    work would spend on a session nobody ever works, holding the provider's
    launches for ``CANARY_TIMEOUT_SECONDS``.
    """
    _quiesce_failover()
    fake_script()
    for key in ("claude", "codex", PROVB, PROVA):
        api_checked("provider_set_state", {"provider": key, "state": "auto"})


def s16_provider_failover(state: dict) -> str:
    """A provider runs out: detect, suppress, re-route, hold, recover, undo, all-down."""
    return _run_failover_phase(state, _s16)


def _run_failover_phase(state: dict, phase) -> str:
    """Keep each independently prepared CI phase fenced and self-cleaning."""
    state["s16_started"] = True
    # S18 needs command-only playbooks enabled for this same Tier-1 run.  The
    # shipped provider-failover playbook would otherwise consume S16's state
    # change and move the first task before this scenario can inspect the full
    # held set and exercise the operator's dry-run/live commands itself.
    set_failover_policy_enabled(False)
    note("paused the automatic provider-failover playbook for manual sweep coverage")
    try:
        return phase(state)
    finally:
        try:
            _restore_providers()
        except Exception as exc:  # noqa: BLE001 — cleanup failure belongs in the report
            print(f"     ! S16 cleanup could not restore providers: {exc}")
        finally:
            set_failover_policy_enabled(True)


def _s16(state: dict) -> str:
    outage = _s16_outage(state)
    return outage["detail"] + "; " + _s16_recovery(outage)


def _failover_baseline() -> None:
    _quiesce_failover()
    fake_script()  # both fake providers healthy
    for key in ("claude", "codex", PROVB, PROVA):
        api_checked("provider_set_state", {"provider": key, "state": "auto"})
    for key in (PROVA, PROVB):
        row = wait_provider(key, ("available", "degraded"), what="S16 baseline")
        note(f"baseline: {key} {row['state']}")


def _s16_outage(state: dict) -> dict:
    _failover_baseline()
    # -- 1. prova logs out; queue the mix -------------------------------------
    t0 = time.time()
    fake_script(prova="login_required")
    pref = [
        failover_task(f"S16 preferred {n}", STD_A, "std-high", priority=p)
        for n, p in ((1, 10), (2, 20), (3, 30))
    ]
    pinned = failover_task("S16 pinned", STD_A, "std-high", priority=15, pin=True)
    solo = failover_task("S16 solo-high", SOLO_A, "solo-high", priority=40)
    class_only = []
    for n, p in ((1, 25), (2, 35)):
        task_id = failover_task(f"S16 class-only {n}", STD_A, "std-high", priority=p)
        edited = api("edit_task", {"task_id": task_id, "provider_intent": "class_only"})
        check(edited.get("updated") == task_id, f"could not make {task_id} class_only: {edited}")
        class_only.append(task_id)
    intents = {tid: task_show(tid)["provider_intent"] for tid in pref + [pinned, solo] + class_only}
    check(all(intents[t] == "preferred" for t in pref + [solo]), f"explicit profile != preferred: {intents}")
    check(intents[pinned] == "pinned", f"--pin did not pin: {intents[pinned]}")
    check(all(intents[t] == "class_only" for t in class_only), f"class_only edit lost: {intents}")
    note(f"queued 3 preferred {pref}, pinned {pinned}, solo-high {solo}, class_only {class_only}")

    # -- 2. detected within two launches; nothing further launches ------------
    down = wait_provider(PROVA, ("unauthenticated",), what="the login dialog on launch")
    prova_launches = login_deaths(PROVA, since=t0)
    check(
        1 <= len(prova_launches) <= 2,
        f"prova should trip within two launches, saw {len(prova_launches)}",
    )
    note(
        f"prova {down['state']} ({down['reason_code']}): {down['reason']} — after "
        f"{len(prova_launches)} launch(es); remediation: {down['remediation']}"
    )
    time.sleep(12)  # two more cascades: a suppressed provider launches nothing
    after = login_deaths(PROVA, since=t0)
    check(
        len(after) == len(prova_launches),
        f"launches continued against unavailable prova: {len(prova_launches)} -> {len(after)}",
    )
    note(f"no further prova launches in 12s ({len(after)} total)")

    # -- 3. every hold names its reason ----------------------------------------
    held = held_tasks()
    check(set(held) == set(pref + [pinned, solo] + class_only), f"held set: {sorted(held)}")
    check(held[pinned]["kind"] == "provider_pinned", f"pinned hold: {held[pinned]}")
    check(held[solo]["kind"] == "no_equivalent_rung", f"solo-high hold: {held[solo]}")
    note("held-tasks: " + ", ".join(f"{t}={held[t]['kind']}" for t in held))
    explained = api("explain_task", {"task_id": pinned})
    check("provider_hold" in (explained.get("reason_codes") or []), f"explain: {explained}")
    check(
        (explained.get("provider_hold") or {}).get("kind") == "provider_pinned",
        f"explain names no pin: {explained.get('provider_hold')}",
    )
    note(f"explain {pinned}: {explained['provider_hold']['kind']} on {explained['provider_hold']['provider']}")

    # -- 4. the sweep: same class on provb, one pool-width at a time -----------
    plan = reroute("--dry-run")
    check(plan["outcome"] == "rerouted" and plan["applied"] is False, f"dry run: {plan}")
    planned = [d["task_id"] for d in plan["moved"]]
    check(planned == [pref[0]], f"dry run should move only the most urgent task: {planned}")
    check(task_show(pref[0])["profile_id"] == STD_A, "a dry run moved a task")
    note(f"dry run: would move {planned}, hold {plan['held_by_kind']}")

    first = reroute()
    check(first["outcome"] == "rerouted" and first["applied"], f"sweep: {first}")
    check([d["task_id"] for d in first["moved"]] == [pref[0]], f"sweep moved {first['moved']}")
    kinds = {d["task_id"]: d["kind"] for d in first["held"]}
    check(kinds[pinned] == "provider_pinned" and kinds[solo] == "no_equivalent_rung", str(kinds))
    check(
        all(kinds[t] == "awaiting_failover_capacity" for t in pref[1:] + class_only),
        f"the rest should await capacity: {kinds}",
    )
    batch = first["batch_ids"][0]
    moved = task_show(pref[0])
    check(moved["profile_id"] == STD_B, f"moved task is on {moved['profile_id']}")
    check(moved["rerouted_from"] == STD_A, f"rerouted_from: {moved['rerouted_from']}")
    check(moved["provider_intent"] == "preferred", "a re-route changed the intent")
    check(moved["intelligence_class"] == "std-high", "a re-route changed the class")
    comments = aq("task", "comments", pref[0])
    bodies = [c.get("body", "") for c in collection_rows(comments, "comments")]
    check(any("Re-routed from" in b for b in bodies), f"no re-route comment: {bodies}")
    note(f"sweep: moved {pref[0]} {STD_A} -> {STD_B} (batch {batch}); held {first['held_by_kind']}")

    # provb's pool starts at most max_active=1 session; this runner works it.
    peak = 0

    def _provb_session():
        nonlocal peak
        live = live_sessions_for(STD_B)
        peak = max(peak, len(live))
        return live[0] if live else None

    sess = wait_for_pool_session(
        _provb_session, what="a provb pool session for the moved task", profile_id=STD_B,
    )
    worker = Worker.adopt(sess["id"])
    claimed = worker.claim_next()
    check(claimed.get("result") == "claimed", f"provb worker claim: {claimed}")
    check(worker.task_id == pref[0], f"provb worker claimed {worker.task_id}, not {pref[0]}")
    worker.close(summary="S16 moved task done on provb")
    worker.drain_ack()
    note(f"provb session {sess['id']} claimed and closed {pref[0]}")

    second = reroute()
    check([d["task_id"] for d in second["moved"]] == [pref[1]], f"top-up moved {second['moved']}")
    check(second["batch_ids"] == [batch], f"a top-up opened a new batch: {second['batch_ids']}")
    note(f"top-up sweep: moved {pref[1]} into the same batch {batch}")
    for _ in range(3):
        _provb_session()
        time.sleep(2)
    check(peak <= 1, f"provb ran {peak} sessions at once; max_active is 1")
    note(f"provb never exceeded max_active=1 (peak {peak})")

    # -- 5. notifications: one per half change, one per batch per project ------
    global_notes = collection_rows(
        aq("message", "list", "--to-kind", "user", "--to-id", "dashboard", "--since", str(t0)),
        "messages",
    )
    prova_notes = [
        m for m in global_notes if m.get("subject") == f"Provider {PROVA}: unauthenticated"
    ]
    check(len(prova_notes) == 1, f"expected one prova outage notice to the human: {prova_notes}")
    project_notes = [
        m for m in collection_rows(
            aq(
                "message", "list", "--to-kind", "session", "--to-id", f"supervisor-{PROJECT}",
                "--since", str(t0),
            ),
            "messages",
        )
        if batch in (m.get("body") or "")
    ]
    check(len(project_notes) == 1, f"expected one batch notice for {PROJECT}: {project_notes}")
    note(f"notices: 1 to user:dashboard ({prova_notes[0]['subject']!r}), 1 to supervisor-{PROJECT} for {batch}")
    incidents = [e for e in provider_escalations() if e.get("source_identity", "").startswith(f"{PROVA}:")]
    open_incidents = [e for e in incidents if e.get("terminal_at") is None]
    check(len(open_incidents) == 1, f"expected one open prova escalation: {incidents}")
    incident = open_incidents[0]
    check(incident["severity"] == "high", f"escalation severity {incident['severity']}")
    note(f"escalation {incident['id']} ({incident['severity']}, {incident['state']}): {incident['summary']}")
    status = provider(PROVA)
    check(status["rerouted"] == 2 and status["batch_id"] == batch, f"status counts: {status}")
    note(
        f"aq provider status prova: {status['state']} since {status['since']:.0f}, "
        f"held {status['held']}, rerouted {status['rerouted']} (batch {status['batch_id']})"
    )
    return {
        "pref": pref, "pinned": pinned, "solo": solo, "incident": incident,
        "detail": (
            f"prova tripped in {len(prova_launches)} launch(es); moved {pref[0]},{pref[1]} "
            f"to provb within max_active=1 (batch {batch}); pin/solo held; notices deduplicated"
        ),
    }


def _s16_recovery(outage: dict) -> str:
    pref, pinned, solo, incident = (
        outage[key] for key in ("pref", "pinned", "solo", "incident")
    )
    # -- 6. recovery: log in, recheck, probation, one launch -------------------
    fake_script()  # prova's login works again
    recheck = aq("provider", "recheck", "--provider", PROVA)
    check(recheck.get("probe") == "authenticated", f"recheck probe: {recheck}")
    probation = provider(PROVA)
    check(
        probation["state"] == "degraded" and probation.get("probation"),
        f"recheck should put prova on probation: {probation}",
    )
    note(f"recheck: probe authenticated -> {probation['state']} ({probation['reason_code']}), probation")
    # Probation admits one launch -- whichever prova pool asks first.  Its
    # first authenticated call is the success that completes recovery, and
    # the task it claims is a held one, running on prova.
    canary = wait_for(
        lambda: next(iter(live_sessions_for(STD_A) + live_sessions_for(SOLO_A)), None),
        what="prova's canary launch",
    )
    expected = pinned if canary["profile_id"] == STD_A else solo
    worker_a = Worker.adopt(canary["id"])
    claimed = worker_a.claim_next()
    check(claimed.get("result") == "claimed", f"prova canary claim: {claimed}")
    check(worker_a.task_id == expected, f"canary claimed {worker_a.task_id}, not {expected}")
    recovered = wait_provider(PROVA, ("available",), what="the canary's first authenticated call")
    note(
        f"canary {canary['id']} ({canary['profile_id']}) claimed held {expected}; "
        f"prova {recovered['state']}"
    )
    worker_a.close(summary="S16 held task done on prova")
    worker_a.drain_ack()
    if expected != pinned:
        sess_a = wait_for_pool_session(
            lambda: next(iter(live_sessions_for(STD_A)), None),
            what="a std-high-prova session for the pinned task",
            profile_id=STD_A,
        )
        worker_p = Worker.adopt(sess_a["id"])
        claimed = worker_p.claim_next()
        check(worker_p.task_id == pinned, f"prova claimed {worker_p.task_id}, not the pin")
        worker_p.close(summary="S16 pinned task done on prova")
        worker_p.drain_ack()
        note(f"{sess_a['id']} claimed and closed the pinned task {pinned} on prova")
    still = task_show(pref[1])
    check(still["profile_id"] == STD_B, f"moved-and-queued work went home: {still['profile_id']}")
    check(not held_tasks(), f"holds outlived the outage: {sorted(held_tasks())}")
    resolved = wait_for(
        lambda: next(
            (e for e in provider_escalations()
             if e["id"] == incident["id"] and e.get("terminal_at") is not None),
            None,
        ),
        what=f"escalation {incident['id']} to resolve on recovery",
    )
    note(
        f"moved {pref[1]} stayed on {STD_B}; holds cleared; escalation {resolved['state']} "
        f"({resolved.get('terminal_outcome')})"
    )

    undone = aq("provider", "reroute-undo", "--task-id", pref[1])
    check(undone.get("outcome") == "undone", f"undo: {undone}")
    back = task_show(pref[1])
    check(back["profile_id"] == STD_A and back["rerouted_from"] is None, f"undo: {back}")
    note(f"reroute-undo returned {pref[1]} to {STD_A}")

    # -- 7. every provider down: holds, no moves, drain, critical --------------
    # Both fakes go dark first.  Idle sessions the recovery started hold the
    # prova/provb pools at their bounds (and the project at its session
    # cap); stopping them makes prova relaunch into the login dialog and
    # frees a slot for the claude session this phase keeps across the outage.
    fake_script(prova="login_required", provb="usage_limit")
    for sess in live_sessions_for(STD_A) + live_sessions_for(SOLO_A) + live_sessions_for(STD_B):
        aq("session", "kill", sess["id"], check_ok=False)
    wait_provider(PROVA, ("unauthenticated",), what="the second logout")
    filler = create_task("S16 claude worker", profile=POOL_PROFILE)
    claude_sess = wait_for_pool_session(
        lambda: next(iter(live_sessions_for(POOL_PROFILE)), None),
        what="a claude pool session to hold across the outage",
    )
    claude_worker = Worker.adopt(claude_sess["id"])
    for key in ("claude", "codex", PROVB):
        set_provider_state(key, "disabled", "e2e S16: every provider down")
    note("prova logged out again; claude, codex and provb disabled by override")
    sweep = reroute()
    check(not sweep["moved"], f"moved work with every provider down: {sweep['moved']}")
    check(sweep["outcome"] == "held", f"all-down sweep outcome {sweep['outcome']}")
    check(
        "all_providers_unavailable" in sweep["held_by_kind"],
        f"no all_providers_unavailable hold: {sweep['held_by_kind']}",
    )
    note(f"all-down sweep: moved none, held {sweep['held_by_kind']}")
    refused = claude_worker.claim_next(check_ok=False)
    result = refused.get("result") if isinstance(refused, dict) else None
    if result is None and isinstance(refused, dict) and refused.get("_error"):
        result = refused["_error"].result
    check(result in ("drain_requested", "not_admissible"), f"claim while all down: {refused}")
    note(f"claude session claim while every provider is down: {result}")
    critical = wait_for(
        lambda: next(
            (
                e for e in provider_escalations()
                if e.get("terminal_at") is None and e.get("severity") == "critical"
            ),
            None,
        ),
        what="a critical provider escalation",
    )
    note(f"escalation {critical['id']} is {critical['severity']}: {critical['summary']}")
    doctor = aq("doctor", "--check", "providers.availability", check_ok=False)
    [check_row] = [c for c in doctor.get("checks", []) if c["id"] == "providers.availability"]
    check(check_row["severity"] == "error", f"doctor with every provider down: {check_row}")
    note(f"doctor providers.availability: {check_row['severity']} — {check_row['detail'][:120]}")
    aq("task", "delete", "--task-id", filler, check_ok=False)
    return (
        "recheck→probation→available; "
        f"undo returned {pref[1]}; all-down held everything, claim={result}, critical escalation"
    )


def s16a_provider_outage(state: dict) -> str:
    """Detect the outage, inspect holds, reroute with capacity and deduplicate notices."""
    return _run_failover_phase(state, lambda current: _s16_outage(current)["detail"])


def _prepare_failover_recovery(state: dict) -> str:
    """Prepare recovery through public commands, without replaying outage assertions.

    A real login failure creates the provider incident. One preferred task is
    rerouted and stays queued on provb; the remaining preferred, class-only,
    pinned and solo tasks stay on prova, matching S16a's unfinished task mix.
    This is the boundary recovery needs, independently of S16a's fixture.
    """
    _failover_baseline()
    fake_script(prova="login_required")
    moved = failover_task("S16 recovery moved", STD_A, "std-high", priority=20)
    pinned = failover_task("S16 recovery pinned", STD_A, "std-high", priority=15, pin=True)
    solo = failover_task("S16 recovery solo", SOLO_A, "solo-high", priority=40)
    failover_task("S16 recovery preferred", STD_A, "std-high", priority=30)
    for priority in (25, 35):
        task_id = failover_task("S16 recovery class-only", STD_A, "std-high", priority=priority)
        edited = api("edit_task", {"task_id": task_id, "provider_intent": "class_only"})
        check(edited.get("updated") == task_id, f"recovery fixture class-only edit: {edited}")
    wait_provider(PROVA, ("unauthenticated",), what="the recovery fixture login failure")
    sweep = reroute()
    check([row["task_id"] for row in sweep["moved"]] == [moved], f"recovery fixture sweep: {sweep}")
    check(task_show(moved)["profile_id"] == STD_B, "recovery fixture did not reroute to provb")
    incident = wait_for(
        lambda: next(
            (row for row in provider_escalations()
             if row.get("source_identity", "").startswith(f"{PROVA}:")
             and row.get("terminal_at") is None),
            None,
        ),
        what="the recovery fixture provider incident",
    )
    return _s16_recovery({"pref": [None, moved], "pinned": pinned, "solo": solo, "incident": incident})


def s16b_provider_recovery(state: dict) -> str:
    """Recover a provider, preserve queued reroutes, undo, then exercise all-down."""
    return _run_failover_phase(state, _prepare_failover_recovery)


def _ensure_phased_development_project() -> tuple[str, Path, Path]:
    """Create the disposable development project S17 needs, once per e2e home."""
    project_id = "e2e-phased"
    home = Path(os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e")))
    root = home / "onboarding"
    remote = root / "phased-graph.git"
    source = root / "phased-graph-source"
    root.mkdir(parents=True, exist_ok=True)

    if not remote.exists():
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
    if not source.exists():
        subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    if not (source / "README.md").exists():
        _git_text(str(source), "config", "user.name", "AQ E2E")
        _git_text(str(source), "config", "user.email", "e2e@example.test")
        (source / "README.md").write_text("phased graph fixture\n")
        _seed_development_validation(source)
        _git_text(str(source), "add", ".")
        _git_text(str(source), "commit", "-m", "base")
        _git_text(str(source), "push", "origin", "main")

    project_ids = {p["id"] for p in collection_rows(aq("project", "list"), "projects")}
    if project_id not in project_ids:
        aq_text(
            "project",
            "onboard",
            "--request-id",
            "e2e-phased-graph",
            "--source-mode",
            "link",
            "--root-id",
            "e2e-onboarding",
            "--relative-path",
            source.name,
            "--project-name",
            "Phased graph",
            "--project-id",
            project_id,
        )

    configured = aq(
        "integration",
        "develop",
        project_id,
        "--command",
        DEVELOPMENT_VALIDATION_COMMAND,
        "--interval-seconds",
        "86400",
        "--reason",
        "isolated phased-graph acceptance",
    )
    check(configured.get("outcome") == "configured", f"development configuration: {configured}")
    return project_id, home, source


def s17_phased_graph(state: dict) -> str:
    """Cook and work a phased spec through the real CLI in development mode."""
    project_id, home, source = _ensure_phased_development_project()
    first_worker = fresh_workers(
        1,
        project_id=project_id,
        cleanup_projects=(PROJECT, OTHER_PROJECT, project_id),
    )[0]
    retired_session_ids: set[str] = set()

    graph = {
        "version": 1,
        "defaults": {"profile": POOL_PROFILE, "intelligence_class": POOL_CLASS},
        "parent": {"title": "S17 phased graph"},
        "phases": [
            {"key": "prepare", "title": "Prepare fixture"},
            {"key": "exercise", "title": "Exercise runner"},
        ],
        "nodes": [
            {
                "key": "fixture",
                "title": "Create deterministic fixture",
                "phase": "prepare",
                "acceptance": ["fixture contract is explicit"],
                "subtasks": [
                    "Write the fixture",
                    "Record the contract",
                    "Check the expected result",
                ],
            },
            {
                "key": "verification",
                "title": "Verify fixture",
                "phase": "prepare",
                "acceptance": ["fixture verification is complete"],
            },
            {
                "key": "runner",
                "title": "Run the fixture",
                "phase": "exercise",
                "acceptance": ["runner starts after preparation"],
            },
        ],
    }
    spec_relative = f"projects/{project_id}/specs/s17-phased-graph.md"
    spec_path = home / "vault" / spec_relative
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        "---\n"
        "title: S17 phased graph\n"
        f"project: {project_id}\n"
        "status: approved\n"
        "---\n\n"
        "# S17 phased graph\n\n"
        "This disposable fixture proves graph phase and checklist behavior through the CLI.\n\n"
        "```aq-graph\n"
        f"{json.dumps(graph, indent=2)}\n"
        "```\n",
        encoding="utf-8",
    )

    create_args = (
        "task",
        "create",
        "--project",
        project_id,
        "--from-spec",
        spec_relative,
        "--profile",
        POOL_PROFILE,
        "--intelligence-class",
        POOL_CLASS,
    )
    dry_run = aq(*create_args, "--dry-run")
    check(len(dry_run.get("phases", [])) == 2, f"dry-run phases: {dry_run}")
    check(len(dry_run.get("nodes", [])) == 3, f"dry-run nodes: {dry_run}")
    check(
        sum(node.get("subtasks", 0) for node in dry_run.get("nodes", [])) == 3,
        f"dry-run subtask counts: {dry_run}",
    )
    absent = run_aq("task", "show", dry_run["parent_id"])
    check(absent.returncode != 0, f"dry-run persisted {dry_run['parent_id']}: {absent}")

    cooked = aq(*create_args)
    epic = cooked["parent_id"]
    phases = {row["key"]: row["task_id"] for row in cooked["phases"]}
    nodes = {row["key"]: row["task_id"] for row in cooked["nodes"]}
    check(set(phases) == {"prepare", "exercise"}, f"cooked phases: {cooked}")
    check(set(nodes) == {"fixture", "verification", "runner"}, f"cooked nodes: {cooked}")

    def phase_rows() -> list[dict]:
        return aq("task", "phase-list", "--project-id", project_id, "--parent-id", epic)["phases"]

    before = {row["order"]: row for row in phase_rows()}
    check(set(before) == {1, 2}, f"phase orders: {before}")
    check(before[2]["is_blocked"], f"phase 2 was not withheld: {before[2]}")
    check(task_show(nodes["runner"])["is_blocked"], "phase-2 task escaped its withheld parent")

    phase_one_nodes = {nodes["fixture"], nodes["verification"]}

    def fixture_ready() -> dict | None:
        row = task_show(nodes["fixture"])
        return row if row["status"] == "READY" else None

    wait_for(
        fixture_ready,
        what="the first phase-1 task to reach the claim frontier",
    )
    first_claim = first_worker.claim_next()
    check(first_claim.get("result") == "claimed", f"first phase-1 claim: {first_claim}")
    check(first_worker.task_id in phase_one_nodes, f"withheld runner was claimed: {first_worker.task_id}")

    def retire(worker: Worker) -> None:
        worker.drain_ack()

        def stopped() -> dict | None:
            shown = aq("session", "show", worker.session_id)
            row = shown.get("session") or shown
            return row if row.get("state") == "stopped" else None

        wait_for(stopped, what=f"session {worker.session_id} to drain")
        retired_session_ids.add(worker.session_id)

    def replacement_worker(expected_task_id: str, what: str) -> Worker:
        session = wait_for_pool_session(
            lambda: next(
                (
                    row
                    for row in pool_sessions(project_id)
                    if row["id"] not in retired_session_ids and not row.get("task_id")
                ),
                None,
            ),
            what=what,
            project_id=project_id,
        )
        worker = Worker.adopt(session["id"])
        claim = worker.claim_next()
        check(claim.get("result") == "claimed", f"{what} claim: {claim}")
        check(worker.task_id == expected_task_id, f"{what} claimed {worker.task_id}")
        return worker

    def push_current_branch() -> None:
        """Meet development mode's exact-pushed-workspace close requirement."""
        _git_text(str(source), "push", "-u", "origin", "HEAD")

    first_task_id = first_worker.task_id
    if first_task_id == nodes["verification"]:
        push_current_branch()
        first_worker.close(summary="S17 phase-1 verification")
        retire(first_worker)
        fixture_worker = replacement_worker(nodes["fixture"], "a worker for the checklist task")
    else:
        fixture_worker = first_worker
    prime = run_aq(
        "prime",
        json_mode=False,
        token=fixture_worker.token,
        session_id=fixture_worker.session_id,
    )
    check(prime.returncode == 0, f"prime failed: {prime}")
    check("## Subtasks" in prime.stdout, f"prime omitted checklist: {prime.stdout}")
    check("- [ ] 1. Write the fixture" in prime.stdout, f"prime checklist: {prime.stdout}")
    check("- [ ] 3. Check the expected result" in prime.stdout, f"prime checklist: {prime.stdout}")

    refused = fixture_worker.aq(
        "task",
        "close",
        "--outcome",
        "pass",
        "--summary",
        "S17 open-checklist refusal",
        "--work-outcome",
        "no-op",
        "--claim-epoch",
        str(fixture_worker.claim_epoch),
        check_ok=False,
    )
    refusal = refused.get("_error")
    check(refusal is not None, "open checklist close unexpectedly succeeded")
    check(
        "subtasks.open" in f"{refusal.error} {refusal.details}",
        f"open checklist refused for the wrong reason: {refusal}",
    )

    push_current_branch()
    closed = fixture_worker.aq(
        "task",
        "close",
        "--outcome",
        "pass",
        "--summary",
        "S17 skip the graph-seeded checklist",
        "--work-outcome",
        "no-op",
        "--claim-epoch",
        str(fixture_worker.claim_epoch),
        "--skip-open-subtasks",
    )
    check(closed.get("success") is not False, f"skip-open-subtasks close: {closed}")
    retire(fixture_worker)

    if first_task_id == nodes["fixture"]:
        verification_worker = replacement_worker(
            nodes["verification"], "a worker for the second phase-1 task"
        )
        push_current_branch()
        verification_worker.close(summary="S17 phase-1 verification")
        retire(verification_worker)

    subtasks = aq("task", "subtasks", nodes["fixture"])
    rows = subtasks["subtasks"]
    check(subtasks["total"] == 3 and subtasks["settled"] == 3, f"subtask counts: {subtasks}")
    check(
        [row["status"] for row in rows] == ["skipped", "skipped", "skipped"],
        f"skipped rows: {rows}",
    )
    check(
        all(row["note"] == "skipped at close" for row in rows),
        f"skipped-row notes: {rows}",
    )

    def released_phase() -> list[dict] | None:
        rows = api_checked("phase_list", {"project_id": project_id, "parent_id": epic})["phases"]
        by_order = {row["order"]: row for row in rows}
        if by_order[1]["status"] in ("COMPLETED", "DONE") and not by_order[2]["is_blocked"]:
            return rows
        return None

    released = wait_for(
        released_phase,
        what="phase 1 to settle COMPLETED and release phase 2",
    )
    check(task_show(phases["prepare"]).get("branch_name") is None, "phase container has a branch")
    check(not {row["order"]: row for row in released}[2]["is_blocked"], f"phase 2 still held: {released}")

    runner_worker = replacement_worker(
        nodes["runner"], "a replacement pool session for the released phase-2 task"
    )
    push_current_branch()
    runner_worker.close(summary="S17 phase-2 runner")
    retire(runner_worker)

    def epic_settled() -> dict | None:
        row = task_show(epic)
        return row if row["status"] in ("COMPLETED", "DONE") else None

    wait_for(epic_settled, what=f"phased epic {epic} to settle")
    aq("task", "delete", "--task-id", epic, "--cascade")
    gone = run_aq("task", "show", epic)
    check(gone.returncode != 0, f"S17 cleanup left epic {epic}: {gone}")
    return (
        f"dry-run/cook {epic}; phase 2 withheld then released after branchless phase 1 COMPLETED; "
        "prime rendered 3 checklist rows and close skipped all 3"
    )


def s18_supervisor_failure_triage(state: dict) -> str:
    """Drive one real terminal failure through the reviewed playbook path."""
    del state
    worker = fresh_workers(1)[0]
    started_at = time.time()
    task_id = create_task("S18 supervisor failure triage", profile=POOL_PROFILE)

    def ready_task() -> dict | None:
        task = task_show(task_id)
        return task if task["status"] == "READY" else None

    wait_for(ready_task, what="the S18 task to reach the claim frontier")
    claimed = worker.claim_next()
    check(claimed.get("result") == "claimed", f"S18 claim: {claimed}")
    check(worker.task_id == task_id, f"S18 claimed {worker.task_id}, expected {task_id}")

    failed = worker.aq(
        "task",
        "close",
        "--outcome",
        "fail",
        "--failure-class",
        "hard",
        "--work-outcome",
        "abandoned",
        "--summary",
        "S18 deliberate terminal failure",
        "--claim-epoch",
        str(worker.claim_epoch),
    )
    check(failed.get("success") is not False, f"S18 fail close: {failed}")
    check(task_show(task_id)["status"] == "BLOCKED", "hard failure did not reach terminal BLOCKED")
    worker.drain_ack()

    def triage_run() -> dict | None:
        rows = collection_rows(
            api_checked("list_playbook_runs", {
                "playbook_id": "supervisor-failure-triage", "limit": 10,
            }),
            "runs",
        )
        for row in rows:
            detail = api_checked("inspect_playbook_run", {"run_id": row["run_id"]})["run"]
            if detail.get("event", {}).get("task_id") == task_id:
                return detail
        return None

    run = wait_for(triage_run, what="the supervisor-failure-triage playbook run")
    check(run["lifecycle"] == "completed", f"S18 triage run: {run}")

    def supervisor_notice() -> dict | None:
        rows = collection_rows(
            api_checked("message_list", {
                "to_kind": "session", "to_id": f"supervisor-{PROJECT}", "since": started_at,
            }),
            "messages",
        )
        return next((row for row in rows if task_id in (row.get("body") or "")), None)

    notice = wait_for(supervisor_notice, what="the S18 durable supervisor notice")
    aq("task", "delete", "--task-id", task_id)
    return (
        f"task.failed completed triage run {run['run_id']} and queued one durable supervisor "
        f"notice {notice['id']}"
    )


def s19_scoped_planner_graph(state: dict) -> str:
    """File a planner graph as an authenticated scoped session, not a handler call."""
    del state
    worker = fresh_workers(1)[0]
    held_task = create_task(
        "S19 planner graph parent", profile=PLANNER_PROFILE, intelligence_class=POOL_CLASS,
    )
    planner_session = wait_for_pool_session(
        lambda: next(
            (row for row in live_sessions_for(PLANNER_PROFILE) if not row.get("task_id")),
            None,
        ),
        what="an idle planner pool session",
        profile_id=PLANNER_PROFILE,
    )
    planner = Worker.adopt(planner_session["id"])
    claimed = planner.claim_next()
    check(claimed.get("result") == "claimed", f"S19 planner claim: {claimed}")
    check(planner.task_id == held_task, f"planner claimed {planner.task_id}, not {held_task}")

    home = Path(os.environ.get("AQ_E2E_HOME", os.path.expanduser("~/.agent-queue-e2e")))
    graph_path = home / "s19-scoped-planner-graph.json"
    graph = {
        "version": 1,
        "nodes": [{
            "key": "checklist",
            "title": "S19 planner checklist child",
            "acceptance": ["the planner-owned child closes only after its checklist settles"],
            "subtasks": ["Read the scoped graph", "Record the filing result"],
        }],
    }
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    graph_args = (
        "task", "create", "--project", PROJECT, "--graph", str(graph_path),
        "--profile", POOL_PROFILE, "--intelligence-class", POOL_CLASS,
        "--reason", "S19 verifies planner-scoped graph filing",
    )

    before = api("task_children", {"task_id": held_task}, token=planner.token)
    check(before.get("count") == 0, f"planner parent started with children: {before}")
    dry_run = planner.aq(*graph_args, "--dry-run")
    check(dry_run.get("parent_id") == held_task, f"scoped dry-run parent: {dry_run}")
    after_dry_run = api("task_children", {"task_id": held_task}, token=planner.token)
    check(after_dry_run.get("count") == 0, f"scoped dry-run wrote children: {after_dry_run}")

    created = planner.aq(*graph_args)
    check(created.get("created") is True, f"scoped graph creation: {created}")
    check(created.get("request_id"), f"scoped graph omitted request id: {created}")
    child_id = created["nodes"][0]["task_id"]
    child = task_show(child_id)
    check(child.get("parent_task_id") == held_task, f"scoped child parent: {child}")
    provenance = [
        edge for edge in aq("task", "deps", "--task-id", child_id).get("provenance", [])
        if edge.get("dep_type") == "discovered-from"
    ]
    check(
        any(edge.get("id") == held_task for edge in provenance),
        f"scoped child lost discovered-from provenance: {provenance}",
    )

    foreign_parent = create_task("S19 foreign parent", project_id=OTHER_PROJECT)
    foreign = planner.aq(*graph_args, "--parent", foreign_parent, check_ok=False).get("_error")
    check(
        foreign is not None and "hierarchy.parent_out_of_scope" in f"{foreign.error} {foreign.details}",
        f"foreign parent was not refused by the scoped graph route: {foreign}",
    )
    cross_project = planner.aq(
        "task", "create", "--project", OTHER_PROJECT, "--graph", str(graph_path),
        "--reason", "S19 must not cross projects",
        check_ok=False,
    ).get("_error")
    check(
        cross_project is not None and "project_id mismatch" in f"{cross_project.error} {cross_project.details}",
        f"cross-project graph was not refused by token scope: {cross_project}",
    )
    cli_root = run_aq(*graph_args, "--root", token=planner.token, session_id=planner.session_id)
    check(
        cli_root.returncode == 2
        and "--root only applies to single-task creation" in f"{cli_root.stdout} {cli_root.stderr}",
        f"CLI root graph refusal: {cli_root}",
    )
    server_root = api(
        "create_task_graph",
        {
            "project_id": PROJECT,
            "graph": graph,
            "root": True,
            "reason": "S19 must not request root filing",
        },
        token=planner.token,
    )
    check(
        server_root.get("code") == "hierarchy.parent_out_of_scope",
        f"server root graph refusal: {server_root}",
    )
    after_denials = api("task_children", {"task_id": held_task}, token=planner.token)
    check(after_denials.get("count") == 1, f"denied graphs wrote children: {after_denials}")

    def child_ready() -> dict | None:
        row = task_show(child_id)
        return row if row.get("status") == "READY" else None

    wait_for(child_ready, what="the scoped graph child to reach the claim frontier")
    child_claim = worker.claim_next()
    check(child_claim.get("result") == "claimed" and worker.task_id == child_id, f"S19 child claim: {child_claim}")
    prime = run_aq("prime", json_mode=False, token=worker.token, session_id=worker.session_id)
    check(prime.returncode == 0 and "## Subtasks" in prime.stdout, f"S19 checklist prime: {prime}")
    check("- [ ] 1. Read the scoped graph" in prime.stdout, f"S19 checklist contents: {prime.stdout}")
    open_close = worker.aq(
        "task", "close", "--outcome", "pass", "--summary", "S19 open checklist refusal",
        "--work-outcome", "no-op", "--claim-epoch", str(worker.claim_epoch), check_ok=False,
    ).get("_error")
    check(
        open_close is not None and "subtasks.open" in f"{open_close.error} {open_close.details}",
        f"open graph checklist close refusal: {open_close}",
    )
    closed_child = worker.aq(
        "task", "close", "--outcome", "pass", "--summary", "S19 settle graph checklist",
        "--work-outcome", "no-op", "--claim-epoch", str(worker.claim_epoch), "--skip-open-subtasks",
    )
    check(closed_child.get("success") is not False, f"S19 checklist close: {closed_child}")
    worker.drain_ack()

    batch_path = home / "s19-quota-batch.json"
    batch = {
        "version": 1,
        "nodes": [
            {
                "key": f"batch-{index}",
                "title": f"S19 quota child {index}",
                "acceptance": ["the transaction either creates this complete batch or none"],
            }
            for index in range(10)
        ],
    }
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    batch_args = (
        "task", "create", "--project", PROJECT, "--graph", str(batch_path),
        "--profile", POOL_PROFILE, "--intelligence-class", POOL_CLASS,
    )

    def file_batch(reason: str) -> dict:
        return planner.aq(*batch_args, "--reason", reason, check_ok=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(file_batch, ("S19 quota batch one", "S19 quota batch two")))
    outcomes = (first, second)
    successful = [result for result in outcomes if "_error" not in result]
    refused = [result.get("_error") for result in outcomes if "_error" in result]
    check(len(successful) == 1, f"quota boundary did not admit exactly one graph: {outcomes}")
    check(
        len(refused) == 1 and "filing_quota_exceeded" in f"{refused[0].error} {refused[0].details}",
        f"quota boundary refusal: {refused}",
    )
    children_after_quota = api("task_children", {"task_id": held_task}, token=planner.token)
    check(
        children_after_quota.get("count") == 11,
        f"quota refusal left a partial graph: {children_after_quota}",
    )

    for node in successful[0]["nodes"]:
        api_checked("delete_task", {"task_id": node["task_id"]})
    api_checked("delete_task", {"task_id": foreign_parent})
    closed_planner = planner.close(summary="S19 scoped planner graph acceptance")
    check(closed_planner.get("success") is not False, f"S19 planner close: {closed_planner}")
    planner.drain_ack()
    return (
        f"planner {planner.session_id} filed {child_id} under {held_task}; root, cross-project, and "
        f"foreign-parent requests were refused; one 10-node quota batch committed and one rolled back"
    )


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
    Scenario("S15", "development integration", s15_development_delivery, ("integration",)),
    Scenario(
        "S16", "provider failover", s16_provider_failover, ("provider availability/failover",)
    ),
    Scenario("S17", "phased graph", s17_phased_graph, ("task graph/phases/subtasks",)),
    Scenario("S18", "supervisor failure triage", s18_supervisor_failure_triage, ("playbooks/failure triage",)),
    Scenario("S19", "scoped planner graph", s19_scoped_planner_graph, ("authentication/scoped graph/quota",)),
]

# Selecting these IDs replaces S16's serial transcript with independent worlds.
# The default developer run still executes the original nineteen scenarios.
FAILOVER_PHASES = [
    Scenario("S16a", "provider outage and rerouting", s16a_provider_outage, ("provider availability/failover",)),
    Scenario("S16b", "provider recovery and all-down", s16b_provider_recovery, ("provider availability/failover",)),
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
    available = SCENARIOS + FAILOVER_PHASES
    unknown = only - {scenario.key for scenario in available}
    if unknown:
        print("FAIL selection — unknown scenarios: " + ", ".join(sorted(unknown)))
        return 1

    try:
        setup()
    except (Failure, CliError) as exc:
        print(f"FAIL setup — {exc}")
        return 1

    state: dict = {}
    report = Report()
    for scenario in available if only else SCENARIOS:
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
