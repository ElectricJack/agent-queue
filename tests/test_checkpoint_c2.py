"""Checkpoint C2: a real task in a real tmux session in a real worktree slot.

C1 (tests/test_session_commands.py::TestEndToEndOnFakeProvider) proved the
protocol — launch fork, ``aq task close``, drain-ack, reap — on a
FakeProvider.  C2 swaps in the real substrate everywhere the protocol is
provider- or workspace-agnostic and proves nothing breaks when it is not
fake anymore:

* the real ``Orchestrator`` (not a stub) drives ``_execute_task``;
* ``_prepare_workspace`` provisions a **real git worktree slot** from a
  real bare origin (no monkeypatched workspace prep);
* the session is a **real tmux session** on an isolated per-test socket,
  running a raw-mode stub agent that paints Claude's ``❯``+NBSP prompt;
* the drain-ack rides the **tmux session environment** (``set-environment``
  → ``show-environment``), which is what survives a daemon restart.

The only stub on the close path is ``_run_completion_pipeline`` (git
commit/PR machinery — its own lane, same stub C1 used).

Spec: docs/analysis/execution-plan.md §4 (checkpoint), session-runtime.md
§3.8/§8, worktree-execution §6.1.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.name != "posix":  # pragma: no cover
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.models import (
    KIND_MODE_WORKTREE,
    SYSTEM_KIND_SCOPE,
    Agent,
    AgentProfile,
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

NBSP = " "

#: Raw-mode REPL stub standing in for a harness TUI.  Ignores whatever
#: argv the spec builder appends (the bootstrap prompt rides argv in
#: ``prompt_mode: arg``) and paints Claude's prompt.
STUB = r"""
import os, sys, termios

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ICANON | termios.ECHO)
termios.tcsetattr(fd, termios.TCSANOW, attrs)

print("❯ ", flush=True)
while True:
    if not os.read(fd, 65536):
        break
    print("❯ ", flush=True)
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


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(["git", "clone", str(origin), str(base)], check=True, capture_output=True)
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


@pytest.fixture
async def orch(tmp_path: Path):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = True
    config.sessions.enabled = True
    config.sessions.provider = "tmux"
    config.sessions.tmux_socket = f"aq-c2-{tmp_path.name}"

    stub = tmp_path / "stub_agent.py"
    stub.write_text(STUB, encoding="utf-8")

    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()

    harness_dir = Path(o.config.vault_root) / "harnesses"
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / "stub.md").write_text(_stub_harness_md(stub), encoding="utf-8")
    load_from_vault(o.harness_registry, o.config.vault_root)
    assert o.harness_registry.get("stub", "p1") is not None, "stub harness failed to load"

    try:
        yield o
    finally:
        observer = TmuxProvider(config=config)
        import contextlib

        with contextlib.suppress(Exception):
            await observer._tmux("kill-server")
        await o.shutdown()


async def _seed(o: Orchestrator, base_repo: Path) -> None:
    await o.db.create_project(
        Project(
            id="p1",
            name="alpha",
            repo_url="",
            repo_default_branch="main",
            max_concurrent_agents=2,
        )
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
        AgentProfile(id="stub-profile", name="Stub", harness="stub", lifecycle="task")
    )
    await o.db.create_agent(Agent(id="a1", name="agent-1", profile_id="stub-profile"))
    await o.db.create_task(
        Task(id="t1", project_id="p1", title="C2", description="d", profile_id="stub-profile")
    )
    await o.db.transition_task("t1", TaskStatus.READY)


class TestCheckpointC2:
    async def test_real_task_real_tmux_real_slot_full_lifecycle(
        self, orch, base_repo, tmp_path, monkeypatch
    ):
        o = orch
        await _seed(o, base_repo)

        async def _pipeline(ctx):
            return (None, True)

        monkeypatch.setattr(o, "_run_completion_pipeline", _pipeline)
        handler = CommandHandler(o, o.config)
        o._command_handler = handler
        observer = TmuxProvider(config=o.config)

        # 1. Launch through the real fork: routing decision, real worktree
        #    slot provisioning, real tmux new-session.
        await o._execute_task(AssignAction(task_id="t1", agent_id="a1", project_id="p1"))

        session = await o.db.get_session_for_task("t1")
        assert session is not None, "no session row — launch failed"
        assert session.provider == "tmux"
        assert session.state == "running"
        assert session.name == "s-t1"

        # The work_dir is slot 0 of the base repo — a real git worktree on
        # the task's own branch, locked by this task in the database.
        slot = slot_path(base_repo, 0)
        assert Path(session.work_dir) == slot
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot) == "aq/t1"
        assert (slot / "README.md").exists()
        slots = await o.db.list_slots_for_base("ws-base")
        assert [s.locked_by_task_id for s in slots] == ["t1"]

        # The tmux session is really there: a fresh provider instance on
        # the same socket sees it, with the row's instance token riding the
        # session environment, and a live python underneath.
        found = await observer.list_running("s-")
        assert [h.name for h in found] == ["s-t1"]
        handle = found[0]
        assert handle.instance_token == session.instance_token
        assert await observer.process_alive(handle, ("python",)) is True
        assert "❯" in await observer.peek(handle)

        # A session-routed task never constructs a runtime adapter.
        assert o._adapters == {}
        assert (await o.db.get_task("t1")).status is TaskStatus.IN_PROGRESS

        # 2. A reconciler tick with a live session changes nothing.
        await o.session_reconciler.tick()
        assert (await o.db.get_session(session.id)).state == "running"
        assert (await o.db.get_task("t1")).status is TaskStatus.IN_PROGRESS

        # 3. The agent closes the task explicitly.
        close = await handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "notes": "Done: C2 wired end to end",
                "summary": "C2 checkpoint complete.",
            },
        )
        assert close["success"] is True, close
        assert (await o.db.get_task("t1")).status is TaskStatus.COMPLETED
        # Agent freed; the slot lock released in the database.
        from src.models import AgentState

        assert (await o.db.get_agent("a1")).state is AgentState.IDLE
        slots = await o.db.list_slots_for_base("ws-base")
        assert [s.locked_by_task_id for s in slots] == [None]

        # 4. The agent acks the drain — the ack lands on the *tmux session
        #    environment*, which is the restart-safe home for it.
        ack = await handler.execute("session_drain_ack", {"session_id": session.id})
        assert ack["success"] is True, ack
        assert (await o.db.get_session(session.id)).state == "draining"
        assert await observer.get_meta(handle, "AQ_DRAIN_ACK") == "1"

        # 5. The next tick reaps it: row stopped, tmux session gone.  The
        #    final listing goes through the *daemon's* cached provider
        #    instance — the one that performed the kill.  The independent
        #    observer would (correctly) raise PartialListError here: the
        #    exit-empty server death is ambiguous to an instance that never
        #    saw the stop, and "unknown is not dead" makes it defer.
        await o.session_reconciler.tick()
        assert (await o.db.get_session(session.id)).state == "stopped"
        daemon_provider = o.session_providers.create("tmux", o.config)
        assert await daemon_provider.list_running("s-") == []
        # And the agent process is really gone from the process table.
        from src.sessions import proctable

        markers = {e.marker for e in await proctable.scan_by_env_marker("AQ_SESSION_ID")}
        assert session.id not in markers
