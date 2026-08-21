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
* a ``task``-type gate on task A keeps task B ``is_blocked`` (shadow
  projection) until ``_sweep_gates`` resolves it after A completes;
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
             description="d", profile_id="stub-profile")
    )
    await o.db.create_task(
        Task(id="tB", project_id="p1", title="task B",
             description="d", profile_id="stub-profile")
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

        # ── task-type gate: B waits for A ─────────────────────────────
        handler = CommandHandler(o, o.config)
        o._command_handler = handler
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
        # (blocked_state_authoritative stays False by design — the
        # projection is recorded, not enforced at scheduling.)
        tB_row = await o.db.get_task("tB")
        assert tB_row.is_blocked is True or tB_row.is_blocked == 1

        # ── Explain B: 'blocked_gate' reason must name the gate ───────
        explain = await handler.execute("explain_task", {"task_id": "tB"})
        assert explain["success"] is True
        codes = [r["code"] for r in explain["reasons"]]
        assert "blocked_gate" in codes
        blocked_reason = next(
            r for r in explain["reasons"] if r["code"] == "blocked_gate"
        )
        assert blocked_reason["ref"] == gate_id

        # ── Spy on acquire_merge_slot to prove serialization ──────────
        # Wrap the module-level symbol imported by git_ops (call site
        # is ``acquire_merge_slot(self.db, ...)``).
        import src.orchestrator.git_ops as git_ops_mod

        orig_acquire = git_ops_mod.acquire_merge_slot
        acquire_log: list[tuple[str, bool, float]] = []
        held_by: dict[str, str | None] = {"p1": None}
        acquire_lock = asyncio.Lock()

        async def spy_acquire(db, project_id, task_id, ttl):
            got = await orig_acquire(db, project_id, task_id, ttl)
            async with acquire_lock:
                acquire_log.append((task_id, got, time.time()))
                if got:
                    # Serialization guard: no overlap.  If another task
                    # already 'holds' from our recorded perspective, the
                    # invariant is broken.
                    prev_holder = held_by.get(project_id)
                    assert prev_holder is None, (
                        f"merge slot acquired by {task_id} while {prev_holder} "
                        f"still held it — serialization broken"
                    )
                    held_by[project_id] = task_id
            return got

        orig_release = git_ops_mod.release_merge_slot

        async def spy_release(db, project_id, task_id):
            async with acquire_lock:
                if held_by.get(project_id) == task_id:
                    held_by[project_id] = None
            return await orig_release(db, project_id, task_id)

        git_ops_mod.acquire_merge_slot = spy_acquire
        git_ops_mod.release_merge_slot = spy_release

        # ── Also capture merge.* bus events ───────────────────────────
        merge_events: list[tuple[str, dict]] = []
        orig_emit = o.bus.emit

        async def recording_emit(event_type, payload):
            if event_type.startswith("merge."):
                merge_events.append((event_type, payload))
            return await orig_emit(event_type, payload)

        o.bus.emit = recording_emit

        # ── Launch A into slot 0 via the real _execute_task fork ──────
        await o._execute_task(AssignAction(
            task_id="tA", agent_id="a1", project_id="p1"
        ))
        session_a = await o.db.get_session_for_task("tA")
        assert session_a is not None and session_a.state == "running"
        slot_a = slot_path(base_repo, 0)
        assert Path(session_a.work_dir) == slot_a
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot_a) == "aq/tA"

        # ── Launch B into slot 1 ──────────────────────────────────────
        # Even with the gate open (shadow projection), _execute_task
        # honors the AssignAction — the gate's authoritative enforcement
        # is out of scope for this checkpoint.  We only assert that the
        # projection is recorded and reflected in explain_task.
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

        # ── Drive real commits in each slot (the stub is decor) ───────
        (slot_a / "a.txt").write_text("A wrote this\n")
        _git(["add", "-A"], cwd=slot_a)
        _git(["commit", "-m", "A work"], cwd=slot_a)
        (slot_b / "b.txt").write_text("B wrote this\n")
        _git(["add", "-A"], cwd=slot_b)
        _git(["commit", "-m", "B work"], cwd=slot_b)

        # ── Hand-craft the Claude transcript for A's session ──────────
        # ClaudeTranscriptReader resolves under
        # ``<base_dir>/.claude/projects/<slug(work_dir)>/<session_key>.jsonl``.
        session_key_a = "sk-tA"
        # Bind the session_key on the session row so both session_logs
        # and the SSE endpoint resolve the file we wrote.  Also promote
        # the harness to ``"claude"``: ``resolve_reader`` only knows
        # Claude today (§transcripts/__init__.py), and the stub harness
        # is otherwise identical for reader purposes — it writes no
        # transcript of its own, and the JSONL we author is Claude-shaped.
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

        # ── session_logs returns source: 'transcript' ─────────────────
        logs = await handler.execute(
            "session_logs", {"session_id": session_a.id}
        )
        assert logs["success"] is True, logs
        assert logs["source"] == "transcript", logs
        assert len(logs["entries"]) == 2
        uuids = {e["uuid"] for e in logs["entries"]}
        assert uuids == {"u-A-1", "u-A-2"}

        # ── SSE endpoint replays the same entries via an ASGI client ──
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

        # ── Close task A: real completion pipeline runs ───────────────
        # verify + integrate; integrate acquires the merge slot, fetches,
        # rebases aq/tA onto origin/main, pushes, merges into main in the
        # base, and pushes main to origin.
        close_a = await handler.execute(
            "task_close",
            {
                "task_id": "tA",
                "session_id": session_a.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "notes": "A done",
            },
        )
        assert close_a["success"] is True, close_a
        assert close_a.get("pipeline_ok") is True, close_a
        assert (await o.db.get_task("tA")).status is TaskStatus.COMPLETED
        assert (await o.db.get_agent("a1")).state is AgentState.IDLE
        # tA acquired the slot at least once and released it.
        assert any(tid == "tA" and got for tid, got, _ in acquire_log)
        assert held_by["p1"] is None

        # Drain-ack + reconciler tick: releases the tmux session so its
        # python stub exits, freeing the slot for reap.  Without this,
        # ``_slot_is_live`` still finds the AQ_TASK_ID-marked python
        # process and skips the reap.
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

        # Origin main now contains A's file (via merge from base's local push).
        # Read via a fresh clone so we're not looking at any live worktree.
        verify_clone = tmp_path / "verify-a"
        subprocess.run(["git", "clone", str(base_repo.parent / "origin.git"),
                          str(verify_clone)], check=True, capture_output=True)
        assert (verify_clone / "a.txt").exists()

        # ── Sweep gates: A is COMPLETED → task-type gate resolves ─────
        # Force the sweep past its interval throttle so the sweep runs
        # now in-test rather than on the daemon's 5-second cadence.
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

        # ── Simulated restart: adopt lives, reap the retired ──────────
        # Grab needed identifiers before we tear things down.
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
            # live one (B, has an AQ_SESSION_ID / AQ_TASK_ID proc under
            # tmux) is skipped by the liveness guard.
            # A's DB row still points at aq/tA branch but locked_by_task_id
            # is None (released on close).  Slot A is no longer holding
            # a live process — reap should succeed.
            slot_a_after = await o2.db.get_workspace(slot_a_ws.id)
            assert slot_a_after is not None
            reaped_a = await mgr.reap_slot(slot_a_after, reason="retired")
            assert reaped_a is True, (
                "A's slot has no live process; reap must succeed"
            )
            # A's slot directory removed, row deleted.
            assert not Path(slot_a).exists()
            assert await o2.db.get_workspace(slot_a_ws.id) is None

            # B's slot has the live stub agent under tmux; reap refuses.
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

        # Original observer still sees B's tmux session — adoption did
        # not disturb it.
        found2 = sorted(h.name for h in await observer.list_running("s-"))
        assert "s-tB" in found2, f"B's tmux session must survive: {found2}"

        # ── Close task B: real integrate, merge slot serializes ───────
        close_b = await handler.execute(
            "task_close",
            {
                "task_id": "tB",
                "session_id": session_b.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "notes": "B done",
            },
        )
        assert close_b["success"] is True, close_b
        assert close_b.get("pipeline_ok") is True, close_b
        assert (await o.db.get_task("tB")).status is TaskStatus.COMPLETED
        assert (await o.db.get_agent("a2")).state is AgentState.IDLE
        assert any(tid == "tB" and got for tid, got, _ in acquire_log)
        assert held_by["p1"] is None

        # Origin main now contains BOTH files.
        verify_clone2 = tmp_path / "verify-both"
        subprocess.run(["git", "clone", str(base_repo.parent / "origin.git"),
                          str(verify_clone2)], check=True, capture_output=True)
        assert (verify_clone2 / "a.txt").exists()
        assert (verify_clone2 / "b.txt").exists()

        # ── Merge-slot serialization audit ─────────────────────────────
        # Both tasks acquired the slot, and merge.succeeded fired for
        # each.  ``held_by`` never observed an overlap (asserted inline
        # in spy_acquire).
        succeeded = [p["task_id"] for t, p in merge_events
                       if t == "merge.succeeded"]
        assert "tA" in succeeded and "tB" in succeeded, merge_events
        started = [p["task_id"] for t, p in merge_events
                     if t == "merge.started"]
        assert started.count("tA") >= 1 and started.count("tB") >= 1

        # Restore the module-level acquire/release so this test's teardown
        # (kill-server) is unaffected.  This also releases any hold this
        # test may not have cleaned up; belt-and-braces.
        git_ops_mod.acquire_merge_slot = orig_acquire
        git_ops_mod.release_merge_slot = orig_release
