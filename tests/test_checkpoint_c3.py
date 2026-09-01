"""Checkpoint C3: end-to-end on real tmux, two slots, real integrate.

Extends the C2 pattern (real ``Orchestrator``, bare-origin git fixture,
stub-harness vault markdown, ``_NullRuntimeFactory``, raw-mode Python
stub agent painting Claude's ``❯``+NBSP prompt, per-test tmux socket)
to prove the whole Wave-3 surface at once:

* two READY tasks on one project (cap=2) launch into slots 0 and 1
  via the real ``_execute_task`` fork;
* on ``task_close`` the **real** ``_run_completion_pipeline`` runs —
  no ``_phase_integrate`` stub — and each task rebases + pushes onto
  the bare origin's ``main``.  The merge slot serializes the two
  integrations;
* a ``task``-type gate on task A (created once B is already running —
  assignment now enforces gates) keeps task B ``is_blocked`` until
  ``_sweep_gates`` resolves it after A completes;
* ``explain_task`` returns a ``blocked_gate`` reason while B is
  gate-blocked;
* the Claude transcript path is exercised end-to-end via
  ``orchestrator.transcript_base_dir`` — a hand-shaped JSONL under
  ``tmp/.claude/projects/<slug>/`` is what ``session_logs`` and the
  ``/api/sessions/{id}/stream`` SSE endpoint replay;
* on simulated restart a second ``Orchestrator`` on the same DB/config
  adopts the surviving live slot, and reap disposes of the retired one
  without touching the live one.

Spec: docs/analysis/execution-plan.md §4 (checkpoint), worktree-execution
§§3–§6, session-runtime §3.8, work-graph §5–§6.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

if os.name != "posix":  # pragma: no cover
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.models import (
    KIND_MODE_WORKTREE,
    SYSTEM_KIND_SCOPE,
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import slot_path
from src.runtimes.base import Runtime
from src.scheduler import AssignAction
from src.sessions.harness_registry import load_from_vault
from src.sessions.tmux import TmuxProvider

pytestmark = pytest.mark.tmux

NBSP = " "


#: Raw-mode REPL stub identical to C2 — a session-realism placeholder
#: for a real Claude CLI.  The test drives commits directly via ``_git``
#: in each slot; the stub only has to keep tmux and process_alive happy.
STUB = r"""
import os, sys, termios

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ICANON | termios.ECHO)
termios.tcsetattr(fd, termios.TCSANOW, attrs)

print("❯ ", flush=True)
while True:
    if not os.read(fd, 65536):
        break
    print("❯ ", flush=True)
"""


def _git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


class _NullRuntimeFactory:
    def create(self, agent_type, profile=None, llm_logger=None) -> Runtime:
        raise AssertionError("a session-routed task must never construct a runtime")


def _slug(work_dir: str) -> str:
    """Same slug rule as ``ClaudeTranscriptReader``: '/' and '.' → '-'."""
    return str(work_dir).replace("/", "-").replace(".", "-")


def _now_iso(offset: float = 0.0) -> str:
    t = time.time() + offset
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def _write_transcript(base_dir: Path, work_dir: str, session_key: str,
                       entries: list[dict]) -> Path:
    slug = _slug(work_dir)
    proj = base_dir / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_key}.jsonl"
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(["git", "clone", str(origin), str(base)], check=True,
                    capture_output=True)
    (base / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


def _stub_harness_md(stub_path: Path) -> str:
    command = sys.executable.replace("\\", "\\\\")
    return f"""---
id: stub
name: Stub Harness
tags: [harness, session-runtime]
---

# Stub

Test-only harness: a raw-mode Python REPL that paints Claude's prompt.

## Config

```json
{{
  "command": "{command}",
  "args": ["{stub_path}"],
  "prompt_mode": "arg",
  "ready_delay_ms": 100,
  "ready_prompt_prefix": "❯ ",
  "process_names": ["python"],
  "skip_escape_before_enter": true,
  "max_argv_prompt_bytes": 4096
}}
```
"""


async def _make_orch(tmp_path: Path, config: AppConfig,
                       transcript_base_dir: Path) -> Orchestrator:
    """Build an Orchestrator with the stub harness and the transcript base."""
    stub = tmp_path / "stub_agent.py"
    if not stub.exists():
        stub.write_text(STUB, encoding="utf-8")

    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()

    # Feed the reader (transcript-backed session_logs + SSE) our tmp
    # base so the hand-shaped JSONL lives under a test-owned dir rather
    # than under ~/.claude.
    o.transcript_base_dir = transcript_base_dir

    harness_dir = Path(o.config.vault_root) / "harnesses"
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / "stub.md").write_text(_stub_harness_md(stub), encoding="utf-8")
    load_from_vault(o.harness_registry, o.config.vault_root)
    assert o.harness_registry.get("stub", "p1") is not None
    return o


@pytest.fixture
async def orch(tmp_path: Path):
    """Primary orchestrator + observer teardown identical to C2."""
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = True
    config.sessions.enabled = True
    config.sessions.provider = "tmux"
    config.sessions.tmux_socket = f"aq-c3-{tmp_path.name}"

    o = await _make_orch(tmp_path, config, tmp_path)

    try:
        yield o
    finally:
        observer = TmuxProvider(config=config)
        import contextlib
        with contextlib.suppress(Exception):
            await observer._tmux("kill-server")
        await o.shutdown()


async def _seed_project_and_tasks(o: Orchestrator, base_repo: Path,
                                     *, cap: int = 2) -> None:
    """One project (cap agents), one worktree kind, one base ws, two agents,
    two READY tasks.  Task B has a task-type gate on A."""
    await o.db.create_project(
        Project(id="p1", name="alpha", repo_url="",
                repo_default_branch="main", max_concurrent_agents=cap)
    )
    await o.db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE,
            id="project-repo",
            is_git_repo=True,
            lockable=True,
            writable=True,
            mode=KIND_MODE_WORKTREE,
            default_lock_mode="exclusive",
        )
    )
    await o.db.create_workspace(
        Workspace(
            id="ws-base",
            project_id="p1",
            workspace_path=str(base_repo),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
    )
    await o.db.upsert_profile(
        AgentProfile(id="stub-profile", name="Stub",
                     harness="stub", lifecycle="task")
    )
    await o.db.create_agent(Agent(id="a1", name="agent-1",
                                    profile_id="stub-profile"))
    await o.db.create_agent(Agent(id="a2", name="agent-2",
                                    profile_id="stub-profile"))
    await o.db.create_task(
        Task(id="tA", project_id="p1", title="task A",
             description="d", profile_id="stub-profile",
             integration_mode="direct")
    )
    await o.db.create_task(
        Task(id="tB", project_id="p1", title="task B",
             description="d", profile_id="stub-profile",
             integration_mode="direct")
    )
    await o.db.transition_task("tA", TaskStatus.READY)
    await o.db.transition_task("tB", TaskStatus.READY)


class TestCheckpointC3:
    """Two tmux agents, two slots, serialized merge, gate, transcript, restart."""

    async def test_full_wave3_surface(
        self, orch: Orchestrator, base_repo: Path, tmp_path: Path,
    ):
        o = orch
        await _seed_project_and_tasks(o, base_repo, cap=2)

        handler = CommandHandler(o, o.config)
        o._command_handler = handler

        # ── Spy on acquire_merge_slot to prove serialization ──────────
        # Wrap the module-level symbol imported by git_ops (call site
        # is ``acquire_merge_slot(self.db, ...)``).
        #
        # Install + restore are wrapped in try/finally around the whole
        # spied region below so an assertion failure can't leak the
        # monkeypatch to sibling tests under ``pytest -n auto``.
        import src.orchestrator.git_ops as git_ops_mod

        orig_acquire = git_ops_mod.acquire_merge_slot
        orig_release = git_ops_mod.release_merge_slot
        orig_emit = o.bus.emit

        acquire_log: list[tuple[str, bool, float]] = []
        held_by: dict[str, str | None] = {"p1": None}
        overlap_seen: list[tuple[str, str]] = []
        acquire_lock = asyncio.Lock()

        async def spy_acquire(db, project_id, task_id, ttl):
            got = await orig_acquire(db, project_id, task_id, ttl)
            async with acquire_lock:
                acquire_log.append((task_id, got, time.time()))
                if got:
                    # Serialization guard: no overlap.  Record any observed
                    # overlap for a post-run assertion (raising here would
                    # leave the spy monkeypatch installed on failure paths
                    # even under try/finally, because the exception unwinds
                    # inside orchestrator internals rather than the test).
                    prev_holder = held_by.get(project_id)
                    if prev_holder is not None and prev_holder != task_id:
                        overlap_seen.append((prev_holder, task_id))
                    held_by[project_id] = task_id
            return got

        async def spy_release(db, project_id, task_id):
            async with acquire_lock:
                if held_by.get(project_id) == task_id:
                    held_by[project_id] = None
            return await orig_release(db, project_id, task_id)

        # ── Also capture merge.* bus events ───────────────────────────
        merge_events: list[tuple[str, dict]] = []

        async def recording_emit(event_type, payload):
            if event_type.startswith("merge."):
                merge_events.append((event_type, payload))
            return await orig_emit(event_type, payload)

        git_ops_mod.acquire_merge_slot = spy_acquire
        git_ops_mod.release_merge_slot = spy_release
        o.bus.emit = recording_emit

        try:
            # ── Launch A into slot 0 via the real _execute_task fork ──
            await o._execute_task(AssignAction(
                task_id="tA", agent_id="a1", project_id="p1"
            ))
            session_a = await o.db.get_session_for_task("tA")
            assert session_a is not None and session_a.state == "running"
            slot_a = slot_path(base_repo, 0)
            assert Path(session_a.work_dir) == slot_a
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot_a) == "aq/tA"

            # ── Launch B into slot 1 ──────────────────────────────────
            # B's gate on A is created after launch: since the routing-
            # enforcement change, _execute_task refuses to assign a
            # blocked task, and this checkpoint needs both sessions live
            # to drive real merge-slot contention.  Gates never stop an
            # already-running session, so the shadow projection and
            # explain_task are asserted below with B running.
            await o._execute_task(AssignAction(
                task_id="tB", agent_id="a2", project_id="p1"
            ))
            session_b = await o.db.get_session_for_task("tB")
            assert session_b is not None and session_b.state == "running"
            slot_b = slot_path(base_repo, 1)
            assert Path(session_b.work_dir) == slot_b
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot_b) == "aq/tB"

            # Independent observer sees both tmux sessions on the per-test socket.
            observer = TmuxProvider(config=o.config)
            found = sorted(h.name for h in await observer.list_running("s-"))
            assert found == ["s-tA", "s-tB"]

            # ── task-type gate: B waits for A ─────────────────────────
            gate_res = await handler.execute(
                "gate_create",
                {
                    "project_id": "p1",
                    "gate_type": "task",
                    "title": "await task A",
                    "await_id": "tA",
                    "waiter_task_ids": ["tB"],
                },
            )
            assert gate_res["success"] is True, gate_res
            gate_id = gate_res["gate_id"]

            # Shadow projection: B is is_blocked=1 while the gate is open.
            tB_row = await o.db.get_task("tB")
            assert tB_row.is_blocked is True or tB_row.is_blocked == 1

            # ── Explain B: 'blocked_gate' reason must name the gate ───
            explain = await handler.execute("explain_task", {"task_id": "tB"})
            assert explain["success"] is True
            codes = [r["code"] for r in explain["reasons"]]
            assert "blocked_gate" in codes
            blocked_reason = next(
                r for r in explain["reasons"] if r["code"] == "blocked_gate"
            )
            assert blocked_reason["ref"] == gate_id

            # ── Drive real commits in each slot (the stub is decor) ───
            (slot_a / "a.txt").write_text("A wrote this\n")
            _git(["add", "-A"], cwd=slot_a)
            _git(["commit", "-m", "A work"], cwd=slot_a)
            (slot_b / "b.txt").write_text("B wrote this\n")
            _git(["add", "-A"], cwd=slot_b)
            _git(["commit", "-m", "B work"], cwd=slot_b)

            # ── Hand-craft the Claude transcript for A's session ──────
            # ClaudeTranscriptReader resolves under
            # ``<base_dir>/.claude/projects/<slug(work_dir)>/<session_key>.jsonl``.
            session_key_a = "sk-tA"
            # Bind the session_key on the session row so both session_logs
            # and the SSE endpoint resolve the file we wrote.  Also promote
            # the harness to ``"claude"``: ``resolve_reader`` only knows
            # Claude today (§transcripts/__init__.py), and the stub
            # harness is otherwise identical for reader purposes — it
            # writes no transcript of its own, and the JSONL we author is
            # Claude-shaped.
            await o.db.update_session(
                session_a.id, session_key=session_key_a, harness="claude"
            )
            # Refresh our in-memory copy so downstream reads see the new harness.
            session_a = await o.db.get_session(session_a.id)
            _write_transcript(tmp_path, session_a.work_dir, session_key_a, [
                {"type": "assistant", "uuid": "u-A-1", "parentUuid": None,
                 "timestamp": _now_iso(-10),
                 "message": {"role": "assistant", "model": "claude-test",
                              "content": [{"type": "text",
                                            "text": "starting task A"}]}},
                {"type": "user", "uuid": "u-A-2", "parentUuid": "u-A-1",
                 "timestamp": _now_iso(-9),
                 "message": {"role": "user", "content": "ack"}},
            ])

            # ── session_logs returns source: 'transcript' ─────────────
            logs = await handler.execute(
                "session_logs", {"session_id": session_a.id}
            )
            assert logs["success"] is True, logs
            assert logs["source"] == "transcript", logs
            assert len(logs["entries"]) == 2
            uuids = {e["uuid"] for e in logs["entries"]}
            assert uuids == {"u-A-1", "u-A-2"}

            # ── SSE endpoint replays the same entries via an ASGI client
            from src.api.sessions import build_sessions_router

            app = FastAPI()
            app.include_router(build_sessions_router(
                db=o.db, base_dir=tmp_path,
                session_providers=o.session_providers, config=o.config,
            ))
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test",
                                     timeout=5.0) as client:
                async with client.stream(
                    "GET", f"/api/sessions/{session_a.id}/stream",
                    params={"replay_only": "1"},
                ) as resp:
                    assert resp.status_code == 200
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
            text = body.decode()
            assert "u-A-1" in text
            assert "u-A-2" in text
            assert '"source": "transcript"' in text

            # Pre-capture workspace metadata for both tasks before the
            # first close — task_close releases the workspace on the
            # winning path, but we need the loser's workspace_id to
            # rebuild a PipelineContext for the sequential retry below.
            ws_pre_a = await o.db.get_workspace_for_task("tA")
            ws_pre_b = await o.db.get_workspace_for_task("tB")
            assert ws_pre_a is not None and ws_pre_b is not None
            pre_workspaces = {"tA": ws_pre_a, "tB": ws_pre_b}
            pre_agents = {"tA": "a1", "tB": "a2"}

            # ── Prove serialization by driving REAL contention ────────
            # Sequential closes cannot falsify the "never concurrently"
            # invariant on the merge slot — the spy would never see a
            # second acquire while a first still held.  Two `task_close`
            # calls under ``asyncio.gather`` would race but can trip on
            # unrelated SQLite/StaticPool concurrency artifacts in
            # ``acquire_merge_slot_row`` that are outside this test's
            # scope.
            #
            # Instead, hold the merge slot from a phony external caller
            # with a short TTL, then fire A's close.  Its
            # ``_phase_integrate`` will call ``acquire_merge_slot`` and
            # observe ``got=False`` — proving the acquire IS gated on
            # the slot's holder.  If serialization were ever removed
            # (e.g. someone deletes the acquire call or the WHERE clause
            # allows non-holders through), the spy would record a
            # successful acquire while ``held_by`` still names the phony
            # holder — the ``overlap_seen`` list would populate and the
            # post-hoc assertion below would fail.  The phony holder is
            # released deterministically after A's close returns, so the
            # sequential retry below succeeds cleanly.
            phony_hold_ttl = 30.0
            phony_holder = "external-hold-for-c3"
            assert await orig_acquire(
                o.db, "p1", phony_holder, phony_hold_ttl
            ) is True, "phony external hold must succeed on an idle slot"
            # Record the phony hold in ``held_by`` too, so the spy's
            # overlap detector treats it as a real holder.
            async with acquire_lock:
                held_by["p1"] = phony_holder

            close_a = await handler.execute(
                "task_close",
                {
                    "task_id": "tA",
                    "session_id": session_a.id,
                    "outcome": "pass",
                    "work_outcome": "shipped",
                    "notes": "A done",
                    "summary": "Task A completed.",
                },
            )
            assert close_a["success"] is True, close_a
            # A hit contention — pipeline_ok=False, task went to
            # BLOCKED via session_close_pipeline_stop.
            assert close_a.get("pipeline_ok") is False, close_a
            assert (await o.db.get_task("tA")).status is TaskStatus.BLOCKED
            # The spy recorded exactly one acquire attempt for tA that
            # returned False — proof that the acquire IS the gate.
            assert any(
                tid == "tA" and not got for tid, got, _ in acquire_log
            ), acquire_log
            # Load-bearing invariant, now falsifiable: no acquire ever
            # observed itself succeeding while another holder was live.
            assert overlap_seen == [], (
                f"merge slot serialization broken: overlap={overlap_seen} "
                f"acquire_log={acquire_log}"
            )

            # Release the phony hold.  Same order as spy_release: update
            # ``held_by`` first, then the DB, so the next acquire cannot
            # see the DB free while ``held_by`` still shows the phony.
            async with acquire_lock:
                if held_by.get("p1") == phony_holder:
                    held_by["p1"] = None
            await orig_release(o.db, "p1", phony_holder)
            assert held_by["p1"] is None

            # ── Now close B sequentially: with the slot free, the real
            # ``_phase_integrate`` acquires, integrates, and releases —
            # origin main gets B's file.
            close_b = await handler.execute(
                "task_close",
                {
                    "task_id": "tB",
                    "session_id": session_b.id,
                    "outcome": "pass",
                    "work_outcome": "shipped",
                    "notes": "B done",
                    "summary": "Task B completed.",
                },
            )
            assert close_b["success"] is True, close_b
            assert close_b.get("pipeline_ok") is True, close_b
            assert (await o.db.get_task("tB")).status is TaskStatus.COMPLETED
            assert any(
                tid == "tB" and got for tid, got, _ in acquire_log
            ), acquire_log
            # Still no overlap after B's real integrate.
            assert overlap_seen == [], (
                f"merge slot serialization broken: overlap={overlap_seen} "
                f"acquire_log={acquire_log}"
            )
            assert held_by["p1"] is None

            # A got a False acquire on the first attempt (winner=B by
            # sequencing; A is the loser needing retry).
            losers = ["tA"]

            # ── Retry each loser sequentially: with the slot now free
            # its integrate runs to completion, so origin/main ends up
            # with BOTH files (the end-state we care about).
            #
            # ``task_close`` is not re-entrant (the loser's workspace was
            # released by ``release_session_task_resources`` on the first
            # call, and BLOCKED is not a closeable status).  Rebuild a
            # PipelineContext from the pre-captured workspace and drive
            # ``_run_completion_pipeline`` directly — this is exactly
            # what ``complete_session_task`` does internally, minus the
            # already-executed resource-release tail.
            from src.models import (
                AgentOutput,
                AgentResult,
                PipelineContext,
                RepoConfig,
            )

            for lid in losers:
                loser_ws = pre_workspaces[lid]
                loser_task = await o.db.get_task(lid)
                loser_agent_obj = await o.db.get_agent(pre_agents[lid])
                loser_project = await o.db.get_project(loser_task.project_id)
                loser_default_branch = await o._get_default_branch(
                    loser_project, loser_ws.workspace_path
                )
                loser_ctx = PipelineContext(
                    task=loser_task,
                    agent=loser_agent_obj,
                    output=AgentOutput(
                        result=AgentResult.COMPLETED,
                        summary=f"{lid} retry after contention",
                        error_message=None,
                    ),
                    workspace_path=loser_ws.workspace_path,
                    workspace_id=loser_ws.id,
                    repo=RepoConfig(
                        id=f"project-{loser_task.project_id}",
                        project_id=loser_task.project_id,
                        source_type=loser_ws.source_type,
                        url=loser_project.repo_url if loser_project else "",
                        default_branch=loser_default_branch,
                    ),
                    default_branch=loser_default_branch,
                    project=loser_project,
                )
                _, retry_ok = await o._run_completion_pipeline(loser_ctx)
                assert retry_ok is True, (
                    f"{lid}'s retry pipeline must succeed on solo run"
                )
                await o.db.transition_task(
                    lid, TaskStatus.COMPLETED,
                    context="c3_retry_after_contention",
                )
                assert (await o.db.get_task(lid)).status is TaskStatus.COMPLETED
            # Loser's agent was already idled by the first task_close's
            # release_session_task_resources — verify both agents are IDLE.
            assert (await o.db.get_agent("a1")).state is AgentState.IDLE
            assert (await o.db.get_agent("a2")).state is AgentState.IDLE
            assert held_by["p1"] is None
            # Both tasks now show a successful acquire in the log.
            assert any(tid == "tA" and got for tid, got, _ in acquire_log)
            assert any(tid == "tB" and got for tid, got, _ in acquire_log)
            # Serialization invariant still clean after the retry.
            assert overlap_seen == [], (
                f"merge slot serialization broken during retry: {overlap_seen}"
            )

            # Drain-ack + reconciler tick for A specifically: releases
            # the tmux session so its python stub exits, freeing slot A
            # for reap.  B is deliberately left with its live proc under
            # tmux so the reap-vs-adopt distinction below is meaningful.
            ack = await handler.execute("session_drain_ack",
                                           {"session_id": session_a.id})
            assert ack["success"] is True, ack
            await o.session_reconciler.tick()
            # Poll briefly for the process to exit — kill-server is
            # asynchronous relative to the /proc scan.
            from src.sessions import proctable
            for _ in range(50):
                markers = {e.marker for e in
                            await proctable.scan_by_env_marker("AQ_TASK_ID")}
                if "tA" not in markers:
                    break
                await asyncio.sleep(0.1)

            # Origin main now contains BOTH files (winner's landed in the
            # first integrate; loser's landed on the sequential retry).
            verify_clone = tmp_path / "verify-both"
            subprocess.run(["git", "clone", str(base_repo.parent / "origin.git"),
                              str(verify_clone)], check=True, capture_output=True)
            assert (verify_clone / "a.txt").exists()
            assert (verify_clone / "b.txt").exists()

            # ── Sweep gates: A is COMPLETED → task-type gate resolves ─
            # Force the sweep past its interval throttle so the sweep
            # runs now in-test rather than on the daemon's 5-second
            # cadence.
            o._last_gate_sweep = 0.0
            await o._sweep_gates()
            gate_after = await o.db.get_gate(gate_id)
            assert gate_after["status"] == "resolved", gate_after
            # B's is_blocked projection flipped back off.
            tB_row2 = await o.db.get_task("tB")
            assert not (tB_row2.is_blocked is True or tB_row2.is_blocked == 1)
            explain_after = await handler.execute("explain_task",
                                                     {"task_id": "tB"})
            codes_after = [r["code"] for r in explain_after["reasons"]]
            assert "blocked_gate" not in codes_after

            # ── Simulated restart: adopt lives, reap the retired ──────
            # Both tasks completed via task_close's release path, which
            # also caused the session reconciler to reap both tmux
            # stub procs.  For the reap-vs-adopt distinction we need
            # slot B to look "live" while slot A looks "retired" — so
            # spawn a fresh AQ_TASK_ID-marked process rooted in slot B
            # to represent a still-live session under that slot.  This
            # is exactly what ``_slot_is_live`` scans for (env marker or
            # AQ-marked cwd inside the slot); the original single-close
            # flow relied on B never having been closed yet, but with
            # concurrent close we recreate the "one slot live, one
            # retired" precondition explicitly.
            live_env = {**os.environ, "AQ_TASK_ID": "tB", "AQ_SESSION_ID": "sk-tB-live"}
            live_proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=str(slot_b),
                env=live_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Give /proc a beat to catch up.
            await asyncio.sleep(0.1)

            all_ws = await o.db.list_workspaces(project_id="p1")
            slot_a_ws = next(
                (w for w in all_ws if w.workspace_path == str(slot_a)), None
            )
            slot_b_ws = next(
                (w for w in all_ws if w.workspace_path == str(slot_b)), None
            )
            assert slot_a_ws is not None and slot_b_ws is not None

            # Build a second Orchestrator on the same DB and config.  The
            # first one is still up — that's fine for the adoption call,
            # which only reads the DB / filesystem.  A real restart would
            # drop the first; the assertion is that the second one *can*
            # rebuild its worldview from durable state.
            config2 = AppConfig(
                data_dir=str(tmp_path / "data"),
                database_path=str(tmp_path / "aq.db"),
                workspace_dir=str(tmp_path / "workspaces"),
            )
            config2.worktrees.enabled = True
            config2.sessions.enabled = True
            config2.sessions.provider = "tmux"
            config2.sessions.tmux_socket = o.config.sessions.tmux_socket

            o2 = Orchestrator(config2, runtimes=_NullRuntimeFactory())
            await o2.initialize()
            try:
                mgr = o2._worktree_slots()
                project = await o2.db.get_project("p1")
                report = await mgr.adopt_existing(project)
                adopted_ids = set(report.adopted)
                # Both slot dirs + sentinels still exist post-A-close, so
                # both get adopted by the fresh manager.
                assert slot_b_ws.id in adopted_ids, (
                    f"B's live slot must be adopted; adopted={report.adopted}"
                )
                assert slot_a_ws.id in adopted_ids

                # ── Reap: the retired one (A, not-live) is removed; the
                # live one (B, has an AQ_SESSION_ID / AQ_TASK_ID proc
                # under tmux) is skipped by the liveness guard.
                # A's DB row still points at aq/tA branch but
                # locked_by_task_id is None (released on close).  Slot A
                # is no longer holding a live process — reap should
                # succeed.
                slot_a_after = await o2.db.get_workspace(slot_a_ws.id)
                assert slot_a_after is not None
                reaped_a = await mgr.reap_slot(slot_a_after, reason="retired")
                assert reaped_a is True, (
                    "A's slot has no live process; reap must succeed"
                )
                # A's slot directory removed, row deleted.
                assert not Path(slot_a).exists()
                assert await o2.db.get_workspace(slot_a_ws.id) is None

                # B's slot has a live AQ-marked process (the
                # test-injected sentinel above); reap refuses via
                # ``_slot_is_live``.
                slot_b_after = await o2.db.get_workspace(slot_b_ws.id)
                assert slot_b_after is not None
                reaped_b = await mgr.reap_slot(slot_b_after, reason="retired")
                assert reaped_b is False, (
                    "B's slot has a live process; reap must skip"
                )
                # Row still present, dir still present.
                assert Path(slot_b).exists()
                assert await o2.db.get_workspace(slot_b_ws.id) is not None
            finally:
                await o2.shutdown()
                # Terminate the injected live-slot sentinel process.
                try:
                    live_proc.terminate()
                    live_proc.wait(timeout=5)
                except Exception:
                    try:
                        live_proc.kill()
                    except Exception:
                        pass

            # ── Merge-slot serialization audit ────────────────────────
            # Both tasks eventually acquired the slot and merge.succeeded
            # fired for each.  ``overlap_seen`` stayed empty across the
            # phony-hold contention, the sequential B close, and the A
            # retry — asserted above at each step.
            succeeded = [p["task_id"] for t, p in merge_events
                           if t == "merge.succeeded"]
            assert "tA" in succeeded and "tB" in succeeded, merge_events
            started = [p["task_id"] for t, p in merge_events
                         if t == "merge.started"]
            assert started.count("tA") >= 1 and started.count("tB") >= 1
        finally:
            # Restore the module-level acquire/release/emit so a
            # mid-test failure cannot leak the monkeypatch into sibling
            # tests under ``pytest -n auto``.
            git_ops_mod.acquire_merge_slot = orig_acquire
            git_ops_mod.release_merge_slot = orig_release
            o.bus.emit = orig_emit
