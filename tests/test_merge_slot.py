"""Merge slot lease — atomic acquire/renew/release/break.

Worktree-execution implementation spec §5 / design §4.1: one integration
lease per project, seeded lazily, atomic conditional UPDATE, TTL-fenced
so a crashed holder cannot starve integration forever.

These tests hit real SQLite (no mocking) — the atomicity guarantees only
hold at the SQL level, and mocking would not observe them.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from src.config import AppConfig
from src.database import Database
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    KIND_MODE_EXCLUSIVE_CLONE,
    KIND_MODE_WORKTREE,
    PhaseResult,
    PipelineContext,
    Project,
    RepoConfig,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.merge_slot import (
    acquire_merge_slot,
    break_expired_merge_slots,
    release_merge_slot,
    renew_merge_slot,
)
from src.runtimes.base import Runtime


class RecordingBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(str(tmp_path / "aq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="alpha"))
    await d.create_project(Project(id="p2", name="beta"))
    try:
        yield d
    finally:
        await d.close()


# ── acquire / release ───────────────────────────────────────────────────


async def test_concurrent_acquire_exactly_one_winner(db):
    """Two concurrent acquires on one project: only one wins."""
    results = await asyncio.gather(
        acquire_merge_slot(db, "p1", "task-A", ttl=60),
        acquire_merge_slot(db, "p1", "task-B", ttl=60),
    )
    assert sorted(results) == [False, True]


async def test_holder_reacquire_is_idempotent_and_renews(db):
    """The current holder can re-acquire — it extends the lease."""
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    # Read the lease
    from src.database.tables import merge_slots
    from sqlalchemy import select

    async with db._engine.begin() as conn:
        row1 = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    exp1 = row1["expires_at"]

    await asyncio.sleep(0.05)
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=120) is True

    async with db._engine.begin() as conn:
        row2 = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    assert row2["expires_at"] > exp1
    assert row2["holder_task_id"] == "task-A"


async def test_non_holder_acquire_against_live_lease_fails(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is False


async def test_expired_lease_is_stealable(db):
    # 0-ttl lease expires immediately.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=0) is True
    await asyncio.sleep(0.01)
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is True

    from src.database.tables import merge_slots
    from sqlalchemy import select

    async with db._engine.begin() as conn:
        row = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    assert row["holder_task_id"] == "task-B"


async def test_release_by_holder_frees_slot(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    await release_merge_slot(db, "p1", "task-A")
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is True


async def test_release_is_idempotent_and_non_holder_release_is_noop(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    # double release
    await release_merge_slot(db, "p1", "task-A")
    await release_merge_slot(db, "p1", "task-A")
    # non-holder release does not free A's original hold — but slot is already free.
    # Re-acquire, then let B try to release.  A stays holder.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    await release_merge_slot(db, "p1", "task-B")
    # A still holds; B cannot acquire.
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is False


async def test_renew_only_by_holder(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await renew_merge_slot(db, "p1", "task-A", ttl=120) is True
    assert await renew_merge_slot(db, "p1", "task-B", ttl=120) is False


# ── break_expired ───────────────────────────────────────────────────────


async def test_break_expired_clears_only_expired(db):
    bus = RecordingBus()
    # p1 has an expired lease, p2 a live one.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=0) is True
    assert await acquire_merge_slot(db, "p2", "task-B", ttl=60) is True
    await asyncio.sleep(0.02)

    count = await break_expired_merge_slots(db, bus)
    assert count == 1

    # p1's slot is now free; p2's still held.
    assert await acquire_merge_slot(db, "p1", "task-C", ttl=60) is True
    assert await acquire_merge_slot(db, "p2", "task-C", ttl=60) is False


async def test_break_expired_returns_zero_when_none_expired(db):
    bus = RecordingBus()
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await break_expired_merge_slots(db, bus) == 0


# ── persistence ──────────────────────────────────────────────────────────


async def test_break_expired_emits_lease_broken_event(db):
    bus = RecordingBus()
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=0) is True
    await asyncio.sleep(0.02)
    assert await break_expired_merge_slots(db, bus) == 1
    types = [t for t, _ in bus.events]
    assert "merge.lease_broken" in types


async def test_slot_state_survives_database_reopen(tmp_path):
    d = Database(str(tmp_path / "aq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="alpha"))
    assert await acquire_merge_slot(d, "p1", "task-A", ttl=60) is True
    await d.close()

    d2 = Database(str(tmp_path / "aq.db"))
    await d2.initialize()
    try:
        # A different task cannot acquire — original row persisted.
        assert await acquire_merge_slot(d2, "p1", "task-B", ttl=60) is False
        # Original holder can renew.
        assert await renew_merge_slot(d2, "p1", "task-A", ttl=120) is True
    finally:
        await d2.close()


# ═══════════════════════════════════════════════════════════════════════════
# _phase_integrate integration tests (Task A2)
#
# These use a real bare-origin git repo + real orchestrator + real slot
# manager.  The point is to exercise the integrate phase end-to-end against
# git — mocking git would defeat the purpose (rebase behavior, conflict
# aborts, force-push absence are all things only real git checks).
# ═══════════════════════════════════════════════════════════════════════════


def _git(args: list[str], cwd) -> str:
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
        raise AssertionError("no runtime should be created in these tests")


@pytest.fixture
def base_repo_for_integrate(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)], check=True, capture_output=True
    )
    (base / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


async def _make_worktree_orch(tmp_path, *, mode=KIND_MODE_WORKTREE, cap=2):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = True
    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()
    return o


async def _seed_wt_project(o, base_repo, *, mode=KIND_MODE_WORKTREE, cap=2):
    from src.models import Project

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
            mode=mode,
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
        AgentProfile(id="test-profile", name="test",
                     model="claude-haiku-4-5-20251001",
                     permission_mode="bypassPermissions")
    )


async def _prep_task_in_slot(o, base_repo, task_id="tsk-1", agent_id="a-1"):
    task = Task(id=task_id, project_id="p1", title=task_id, description="")
    await o.db.create_task(task)
    await o.db.create_agent(Agent(id=agent_id, name=agent_id,
                                  profile_id="test-profile"))
    task_obj = await o.db.get_task(task_id)
    agent = await o.db.get_agent(agent_id)
    slot_path = await o._prepare_workspace(task_obj, agent)
    return await o.db.get_task(task_id), agent, slot_path


def _make_pipeline_ctx(task, agent, slot_path, workspace_id):
    return PipelineContext(
        task=task,
        agent=agent,
        output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=100),
        workspace_path=str(slot_path),
        workspace_id=workspace_id,
        repo=RepoConfig(
            id="r-1", project_id="p1", source_type=RepoSourceType.CLONE,
            default_branch="main",
        ),
        default_branch="main",
    )


class TestPhaseIntegrateHappyPath:
    async def test_integrate_acquires_slot_pushes_merges_emits_events(
        self, tmp_path, base_repo_for_integrate,
    ):
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)
            task, agent, slot = await _prep_task_in_slot(o, base_repo)

            # Agent left a real commit on aq/tsk-1 in the slot
            (Path(slot) / "work.txt").write_text("hello\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "work"], cwd=slot)

            # Record emitted merge.* events
            captured: list[tuple[str, dict]] = []
            orig_emit = o.bus.emit

            async def recording_emit(event_type, payload):
                if event_type.startswith("merge."):
                    captured.append((event_type, payload))
                return await orig_emit(event_type, payload)

            o.bus.emit = recording_emit

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            result = await o._phase_integrate(ctx)
            assert result == PhaseResult.CONTINUE

            types = [t for t, _ in captured]
            assert "merge.started" in types
            assert "merge.succeeded" in types
            assert "merge.conflict" not in types

            # Origin's main advanced to include the commit
            log = _git(["log", "--oneline", "origin/main"], cwd=slot)
            assert "work" in log

            # Merge slot released
            slot_row = await o.db.get_merge_slot("p1")
            assert slot_row is not None
            assert slot_row.holder_task_id is None
        finally:
            await o.shutdown()

    async def test_integrate_contention_second_task_backs_off(
        self, tmp_path, base_repo_for_integrate,
    ):
        """Second task cannot run integrate while first holds the merge slot."""
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo, cap=2)
            t1, a1, s1 = await _prep_task_in_slot(o, base_repo, "tsk-1", "a-1")
            t2, a2, s2 = await _prep_task_in_slot(o, base_repo, "tsk-2", "a-2")
            (Path(s2) / "b.txt").write_text("b\n")
            _git(["add", "-A"], cwd=s2)
            _git(["commit", "-m", "b"], cwd=s2)

            # Manually hold the merge slot for tsk-1 with a long TTL so
            # nothing preempts it under xdist load.
            assert await acquire_merge_slot(o.db, "p1", "tsk-1", ttl=3600) is True

            ws2 = await o.db.get_workspace_for_task(t2.id)
            ctx2 = _make_pipeline_ctx(t2, a2, s2, ws2.id)

            result = await o._phase_integrate(ctx2)
            # Contention → the phase stops without integrating.
            assert result == PhaseResult.STOP
            # Merge slot is still held by tsk-1 (integrate never stole it).
            row = await o.db.get_merge_slot("p1")
            assert row is not None and row.holder_task_id == "tsk-1"
            # Origin main should be untouched (tsk-2 never got pushed).
            log = _git(["log", "--oneline", "origin/main"], cwd=s2)
            assert "b" not in log
        finally:
            await o.shutdown()


class TestPhaseIntegrateConflict:
    async def test_rebase_conflict_blocks_task_and_never_force_pushes(
        self, tmp_path, base_repo_for_integrate,
    ):
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)

            # Set up a conflict: task branch touches README, then origin/main
            # advances with an incompatible README change.
            task, agent, slot = await _prep_task_in_slot(o, base_repo)
            (Path(slot) / "README.md").write_text("task change\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "task edit"], cwd=slot)

            # Now advance origin/main from the base
            (base_repo / "README.md").write_text("main change\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "main edit"], cwd=base_repo)
            _git(["push", "origin", "main"], cwd=base_repo)

            # Capture every git command that ran so we can assert no force push
            arun_calls: list[list[str]] = []
            orig_arun = o.git._arun

            async def spy_arun(args, cwd=None, **kw):
                arun_calls.append(list(args))
                return await orig_arun(args, cwd=cwd, **kw)

            o.git._arun = spy_arun

            captured: list[tuple[str, dict]] = []
            orig_emit = o.bus.emit

            async def recording_emit(event_type, payload):
                if event_type.startswith("merge."):
                    captured.append((event_type, payload))
                return await orig_emit(event_type, payload)

            o.bus.emit = recording_emit

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            result = await o._phase_integrate(ctx)
            assert result == PhaseResult.STOP

            # Task BLOCKED with rejection_reason recorded
            task_after = await o.db.get_task(task.id)
            assert task_after.status == TaskStatus.BLOCKED
            reason = await o.db.get_task_meta(task.id, "rejection_reason")
            assert reason is not None
            assert "conflict" in reason.lower() or "merge" in reason.lower()

            files = await o.db.get_task_meta(task.id, "conflict_files")
            assert isinstance(files, list) and len(files) >= 1
            assert any("README" in f for f in files)

            # merge.conflict emitted
            assert any(t == "merge.conflict" for t, _ in captured)

            # Merge slot released
            slot_row = await o.db.get_merge_slot("p1")
            assert slot_row is not None and slot_row.holder_task_id is None

            # NEVER force-pushed
            for call in arun_calls:
                assert not (
                    "push" in call
                    and any(a.startswith("--force") for a in call if a != "--force-with-lease")
                    and "main" in call
                ), f"Force push to main detected: {call}"
                # Also disallow --force-with-lease against main:
                if "push" in call and "main" in call:
                    assert "--force-with-lease" not in call, (
                        f"Force-with-lease to main detected: {call}"
                    )
        finally:
            await o.shutdown()


class TestPhaseVerifySkipsAutoMergeUnderWorktreeMode:
    async def test_worktree_mode_verify_does_not_auto_merge(
        self, tmp_path, base_repo_for_integrate,
    ):
        """Under worktree mode, _phase_verify's auto-merge-to-default is
        skipped — integration is _phase_integrate's job."""
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)
            task, agent, slot = await _prep_task_in_slot(o, base_repo)
            (Path(slot) / "work.txt").write_text("x\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "x"], cwd=slot)

            # Track checkout calls — the auto-merge path would call
            # `checkout main` inside the slot.
            arun_calls: list[list[str]] = []
            orig_arun = o.git._arun

            async def spy_arun(args, cwd=None, **kw):
                arun_calls.append(list(args))
                return await orig_arun(args, cwd=cwd, **kw)

            o.git._arun = spy_arun

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            await o._phase_verify(ctx)

            # No 'checkout main' inside the slot for auto-merge — the slot
            # stays on aq/tsk-1 for _phase_integrate to handle.
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot) == "aq/tsk-1"
        finally:
            await o.shutdown()

    async def test_exclusive_clone_verify_still_auto_merges(
        self, tmp_path, base_repo_for_integrate,
    ):
        """Regression guard: exclusive-clone kinds keep today's auto-merge."""
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            # Exclusive-clone mode (worktrees.enabled=True is fine — the
            # kind's mode is what controls per-task behavior).
            await _seed_wt_project(o, base_repo, mode=KIND_MODE_EXCLUSIVE_CLONE)

            # Set up a task with a branch that has committed work
            task = Task(id="tsk-ec", project_id="p1", title="ec",
                        description="", branch_name="aq/ec",
                        status=TaskStatus.IN_PROGRESS)
            await o.db.create_task(task)
            await o.db.create_agent(Agent(id="a-ec", name="a-ec",
                                          profile_id="test-profile"))
            task = await o.db.get_task("tsk-ec")
            agent = await o.db.get_agent("a-ec")

            # Simulate agent's work in the clone.
            _git(["switch", "-c", "aq/ec"], cwd=base_repo)
            (base_repo / "work.txt").write_text("x\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "work"], cwd=base_repo)

            ctx = _make_pipeline_ctx(task, agent, str(base_repo), "ws-base")

            result = await o._phase_verify(ctx)
            # Auto-merge should have run — we end on main with the commit.
            assert result == PhaseResult.CONTINUE
            head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo)
            assert head == "main"
        finally:
            await o.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# Adversarial-round fixes.
# ═══════════════════════════════════════════════════════════════════════════


class TestPhaseIntegrateLeaseGuardedPush:
    """Finding #1: if the lease is lost between rebase and push, do NOT push."""

    async def test_lost_lease_before_push_aborts_without_pushing(
        self, tmp_path, base_repo_for_integrate, monkeypatch,
    ):
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)
            task, agent, slot = await _prep_task_in_slot(o, base_repo)

            (Path(slot) / "work.txt").write_text("hello\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "work"], cwd=slot)

            # Intercept renew_merge_slot at the module level used by
            # git_ops (the caller imports the symbol directly).  The
            # first renew (before rebase) succeeds; subsequent renews
            # (guarding the push) all fail — simulating the lease being
            # broken by ``break_expired_merge_slots`` mid-integrate.
            call_count = {"n": 0}
            import src.orchestrator.git_ops as git_ops_mod

            orig_renew = git_ops_mod.renew_merge_slot

            async def flaky_renew(db, project_id, task_id, ttl):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return await orig_renew(db, project_id, task_id, ttl)
                # Simulate: another task now holds the slot.
                await release_merge_slot(db, project_id, task_id)
                await acquire_merge_slot(db, project_id, "OTHER-TASK", ttl=3600)
                return False

            monkeypatch.setattr(git_ops_mod, "renew_merge_slot", flaky_renew)

            # Spy on git push invocations.
            push_calls: list[list[str]] = []
            orig_arun = o.git._arun

            async def spy_arun(args, cwd=None, **kw):
                if len(args) >= 1 and args[0] == "push":
                    push_calls.append(list(args))
                return await orig_arun(args, cwd=cwd, **kw)

            o.git._arun = spy_arun

            # Also spy on apush_branch to catch the branch push.
            push_branch_calls: list[str] = []
            orig_push = o.git.apush_branch

            async def spy_push(*args, **kw):
                push_branch_calls.append(args[1] if len(args) > 1 else kw.get("branch"))
                return await orig_push(*args, **kw)

            o.git.apush_branch = spy_push

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            result = await o._phase_integrate(ctx)
            assert result == PhaseResult.STOP
            # No push happened — neither the branch push nor a default push.
            assert push_branch_calls == [], (
                f"push happened despite lost lease: {push_branch_calls!r}"
            )
            assert not any("main" in c for c in push_calls), (
                f"default-branch push happened despite lost lease: {push_calls!r}"
            )
        finally:
            await o.shutdown()


class TestTaskIsWorktreeModeKindDiscrimination:
    """Finding #2: discriminate on kind.mode, not is_slot alone."""

    async def test_base_workspace_of_worktree_kind_is_worktree_mode(
        self, tmp_path, base_repo_for_integrate,
    ):
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo_for_integrate,
                                   mode=KIND_MODE_WORKTREE)
            # Fabricate a ctx whose workspace_id points at the *base* row
            # (not a slot).  The old is_slot-only detector said False; the
            # kind.mode detector must say True.
            base_ws = await o.db.get_workspace("ws-base")
            assert base_ws is not None and not base_ws.is_slot

            ctx = PipelineContext(
                task=Task(id="tsk-x", project_id="p1", title="x",
                          description=""),
                agent=Agent(id="a-x", name="a-x", profile_id="test-profile"),
                output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=1),
                workspace_path=base_ws.workspace_path,
                workspace_id=base_ws.id,
                repo=RepoConfig(id="r-1", project_id="p1",
                                source_type=RepoSourceType.CLONE,
                                default_branch="main"),
                default_branch="main",
            )
            assert await o._task_is_worktree_mode(ctx) is True
        finally:
            await o.shutdown()

    async def test_base_workspace_of_exclusive_clone_kind_is_not_worktree_mode(
        self, tmp_path, base_repo_for_integrate,
    ):
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo_for_integrate,
                                   mode=KIND_MODE_EXCLUSIVE_CLONE)
            base_ws = await o.db.get_workspace("ws-base")
            ctx = PipelineContext(
                task=Task(id="tsk-x", project_id="p1", title="x",
                          description=""),
                agent=Agent(id="a-x", name="a-x", profile_id="test-profile"),
                output=AgentOutput(result=AgentResult.COMPLETED, tokens_used=1),
                workspace_path=base_ws.workspace_path,
                workspace_id=base_ws.id,
                repo=RepoConfig(id="r-1", project_id="p1",
                                source_type=RepoSourceType.CLONE,
                                default_branch="main"),
                default_branch="main",
            )
            assert await o._task_is_worktree_mode(ctx) is False
        finally:
            await o.shutdown()


class TestAcquireMergeSlotBusyErrorHandling:
    """Finding #3: SQLITE_BUSY / locked surfaces as False, not exception."""

    async def test_operational_error_locked_returns_false(self, tmp_path, monkeypatch):
        d = Database(str(tmp_path / "aq.db"))
        await d.initialize()
        try:
            await d.create_project(Project(id="p1", name="p", repo_url=""))
            from sqlalchemy.exc import OperationalError

            orig_seed = d._seed_merge_slot

            async def busy_seed(conn, project_id, now):
                raise OperationalError(
                    "INSERT", {}, Exception("database is locked")
                )

            monkeypatch.setattr(d, "_seed_merge_slot", busy_seed)

            # Must return False, never raise.
            got = await acquire_merge_slot(d, "p1", "tsk-1", ttl=60)
            assert got is False

            # Restore & confirm normal acquire still works.
            monkeypatch.setattr(d, "_seed_merge_slot", orig_seed)
            assert await acquire_merge_slot(d, "p1", "tsk-1", ttl=60) is True
        finally:
            await d.close()


class TestPhaseIntegratePushFailureRecordsReason:
    """Finding #6: on push failure, record ``rejection_reason`` meta."""

    async def test_push_failure_sets_rejection_reason(
        self, tmp_path, base_repo_for_integrate, monkeypatch,
    ):
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)
            task, agent, slot = await _prep_task_in_slot(o, base_repo)
            (Path(slot) / "work.txt").write_text("hello\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "work"], cwd=slot)

            async def boom(*a, **kw):
                raise RuntimeError("simulated push failure")

            o.git.apush_branch = boom

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            result = await o._phase_integrate(ctx)
            assert result == PhaseResult.STOP

            meta = await o.db.get_task_meta(task.id, "rejection_reason")
            assert meta is not None
            assert "push_failed" in str(meta)
        finally:
            await o.shutdown()

    async def test_default_branch_push_non_git_error_sets_rejection_reason(
        self, tmp_path, base_repo_for_integrate,
    ):
        """Non-GitError from default-branch push is caught symmetrically."""
        base_repo = base_repo_for_integrate
        o = await _make_worktree_orch(tmp_path)
        try:
            await _seed_wt_project(o, base_repo)
            task, agent, slot = await _prep_task_in_slot(o, base_repo)
            (Path(slot) / "work.txt").write_text("hello\n")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "work"], cwd=slot)

            # Let the task-branch push (apush_branch) succeed normally,
            # but make _arun raise a non-GitError when pushing the
            # default branch ("main") so we exercise the broadened
            # except-Exception path in the default-branch push block.
            orig_arun = o.git._arun

            async def arun_boom(args, cwd=None, **kw):
                if args and args[0] == "push" and "main" in args:
                    raise RuntimeError("simulated network failure on default push")
                return await orig_arun(args, cwd=cwd, **kw)

            o.git._arun = arun_boom

            ws = await o.db.get_workspace_for_task(task.id)
            ctx = _make_pipeline_ctx(task, agent, slot, ws.id)

            result = await o._phase_integrate(ctx)
            assert result == PhaseResult.STOP

            meta = await o.db.get_task_meta(task.id, "rejection_reason")
            assert meta is not None
            assert "push_failed" in str(meta)
            assert "main" in str(meta)
        finally:
            await o.shutdown()
