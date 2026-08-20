"""End-to-end: the orchestrator prepares a real slot worktree.

Worktree-execution implementation spec §6.1 (``_prepare_workspace``'s
worktree branch) and §3.1–3.2 (lazy slot creation, per-assignment reset),
driven through the real ``Orchestrator`` against a real git repo with a
real bare origin.  No git mocking — the point of this file is that the
directories, branches and ``git status`` output are actually right.

What is *not* here: integration/merge (phase 3) and reaping/adoption
(phase 4).  ``_phase_verify``'s auto-merge is untouched by this lane; see
the note in ``test_verify_automerge_is_still_the_clone_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_EXCLUSIVE_CLONE,
    KIND_MODE_WORKTREE,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import WorktreeSlotManager, slot_path
from src.runtimes.base import Runtime


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
        raise AssertionError("no runtime should be created in these tests")


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
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
    (base / ".gitignore").write_text("node_modules/\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


async def _orch(tmp_path: Path, *, worktrees_enabled: bool) -> Orchestrator:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = worktrees_enabled
    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()
    return o


async def _seed(o: Orchestrator, base_repo: Path, *, mode: str, cap: int = 2) -> None:
    db = o.db
    await db.create_project(
        Project(
            id="p1",
            name="alpha",
            repo_url="",
            repo_default_branch="main",
            max_concurrent_agents=cap,
        )
    )
    await db.upsert_workspace_kind(
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
    await db.create_workspace(
        Workspace(
            id="ws-base",
            project_id="p1",
            workspace_path=str(base_repo),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
    )
    await db.upsert_profile(
        AgentProfile(
            id="test-profile",
            name="test",
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
        )
    )


async def _mk(o: Orchestrator, task_id: str, agent_id: str, **task_kw):
    await o.db.create_task(
        Task(id=task_id, project_id="p1", title=task_id, description="", **task_kw)
    )
    await o.db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="test-profile"))
    return await o.db.get_task(task_id), await o.db.get_agent(agent_id)


# ─────────────────────────────── the happy path ──────────────────────────


class TestPrepareCreatesAndResetsASlot:
    async def test_first_dispatch_provisions_slot_zero(self, tmp_path, base_repo):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE)
            task, agent = await _mk(o, "tsk-1", "a-1")

            path = await o._prepare_workspace(task, agent)

            expected = slot_path(base_repo, 0)
            assert path is not None
            assert Path(path) == expected, "the agent works in the slot, not the base"
            assert expected.is_dir()
            assert (expected / "README.md").exists()

            # On its own task branch, tree clean.
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=expected) == "aq/tsk-1"
            assert _git(["status", "--porcelain"], cwd=expected) == ""

            # The base is untouched: still on main, still clean.  This is the
            # .git/info/exclude block doing its job — .aq/ lives *inside* the
            # base's working tree.
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo) == "main"
            assert _git(["status", "--porcelain"], cwd=base_repo) == ""

            # Registered with git and with the DB.
            assert str(expected.resolve()) in {
                str(Path(e["path"]).resolve())
                for e in await o.git.aworktree_list(str(base_repo))
            }
            slots = await o.db.list_slots_for_base("ws-base")
            assert [s.slot_index for s in slots] == [0]
            assert slots[0].locked_by_task_id == "tsk-1"

            # The branch is recorded on the task, and the sentinel agrees.
            assert (await o.db.get_task("tsk-1")).branch_name == "aq/tsk-1"
            sentinel = WorktreeSlotManager.read_sentinel(expected)
            assert sentinel.task_id == "tsk-1"
            assert sentinel.branch == "aq/tsk-1"
            assert sentinel.base_workspace_id == "ws-base"

            # And the sync git-lock resolver can now map slot -> base mutex.
            assert o._resolve_git_lock(str(expected)) is o._git_mutexes[str(base_repo)]
        finally:
            await o.shutdown()

    async def test_growth_is_one_slot_per_dispatch(self, tmp_path, base_repo):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=2)

            t1, a1 = await _mk(o, "tsk-1", "a-1")
            p1 = await o._prepare_workspace(t1, a1)
            assert len(await o.db.list_slots_for_base("ws-base")) == 1

            t2, a2 = await _mk(o, "tsk-2", "a-2")
            p2 = await o._prepare_workspace(t2, a2)
            assert len(await o.db.list_slots_for_base("ws-base")) == 2

            assert {Path(p1), Path(p2)} == {
                slot_path(base_repo, 0),
                slot_path(base_repo, 1),
            }
            # Two tasks, two branches, two working trees.
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p1) == "aq/tsk-1"
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p2) == "aq/tsk-2"
        finally:
            await o.shutdown()

    async def test_cap_bounds_parallelism(self, tmp_path, base_repo):
        """Slots = cap, so the third task gets nothing and takes the backoff."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=2)
            for i in (1, 2):
                t, a = await _mk(o, f"tsk-{i}", f"a-{i}")
                assert await o._prepare_workspace(t, a) is not None

            t3, a3 = await _mk(o, "tsk-3", "a-3")
            assert await o._prepare_workspace(t3, a3) is None
            assert len(await o.db.list_slots_for_base("ws-base")) == 2
        finally:
            await o.shutdown()

    async def test_slot_is_reused_and_reset_after_release(self, tmp_path, base_repo):
        """The slot survives its task; the next one gets it pristine."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            t1, a1 = await _mk(o, "tsk-1", "a-1")
            p1 = Path(await o._prepare_workspace(t1, a1))

            # Agent leaves a commit plus some untracked mess and a warm cache.
            (p1 / "work.txt").write_text("done\n")
            _git(["add", "-A"], cwd=p1)
            _git(["commit", "-m", "work"], cwd=p1)
            (p1 / "scratch.tmp").write_text("junk\n")
            cache = p1 / "node_modules"
            cache.mkdir()
            (cache / "dep.js").write_text("expensive\n")

            await o._release_workspaces_for_task("tsk-1")

            # Row survives — the branch is the artifact, not the directory.
            slots = await o.db.list_slots_for_base("ws-base")
            assert len(slots) == 1 and slots[0].locked_by_task_id is None
            assert p1.is_dir()

            t2, a2 = await _mk(o, "tsk-2", "a-2")
            p2 = Path(await o._prepare_workspace(t2, a2))
            assert p2 == p1, "the same slot is reused"

            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p2) == "aq/tsk-2"
            assert _git(["status", "--porcelain"], cwd=p2) == ""
            assert not (p2 / "work.txt").exists(), "predecessor's commit not inherited"
            assert not (p2 / "scratch.tmp").exists(), "untracked junk cleaned"
            assert (cache / "dep.js").exists(), "gitignored cache survives (no -x)"

            # tsk-1's branch is still there for whoever integrates it.
            assert "aq/tsk-1" in _git(["branch"], cwd=base_repo)
        finally:
            await o.shutdown()

    async def test_dirty_predecessor_is_salvaged_not_lost(self, tmp_path, base_repo):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            t1, a1 = await _mk(o, "tsk-1", "a-1")
            p1 = Path(await o._prepare_workspace(t1, a1))

            # Crash mid-edit: uncommitted changes, lock never released cleanly.
            (p1 / "README.md").write_text("half-finished\n")
            await o.db.release_workspaces_for_task("tsk-1")

            t2, a2 = await _mk(o, "tsk-2", "a-2")
            await o._prepare_workspace(t2, a2)

            ctxs = await o.db.get_task_contexts("tsk-1")
            salvage = [c for c in ctxs if c["type"] == "worktree_salvage"]
            assert len(salvage) == 1, "the crashed task's work must be recoverable"
            assert "half-finished" in salvage[0]["content"]
            assert _git(["status", "--porcelain"], cwd=p1) == ""
        finally:
            await o.shutdown()

    async def test_plan_subtask_resumes_the_parent_branch(self, tmp_path, base_repo):
        """One plan, one branch, one PR (§6.1 resume_branch mapping)."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            parent, a1 = await _mk(o, "tsk-parent", "a-1")
            p = Path(await o._prepare_workspace(parent, a1))
            (p / "step1.txt").write_text("one\n")
            _git(["add", "-A"], cwd=p)
            _git(["commit", "-m", "step 1"], cwd=p)
            await o._release_workspaces_for_task("tsk-parent")

            child, a2 = await _mk(
                o, "tsk-child", "a-2", is_plan_subtask=True, parent_task_id="tsk-parent"
            )
            p2 = Path(await o._prepare_workspace(child, a2))

            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p2) == "aq/tsk-parent"
            assert (p2 / "step1.txt").exists(), "the plan's commits must accumulate"
            assert (await o.db.get_task("tsk-child")).branch_name == "aq/tsk-parent"
        finally:
            await o.shutdown()

    async def test_retry_of_a_task_restarts_its_branch(self, tmp_path, base_repo):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            t, a = await _mk(o, "tsk-1", "a-1")
            p = Path(await o._prepare_workspace(t, a))
            (p / "attempt.txt").write_text("bad\n")
            _git(["add", "-A"], cwd=p)
            _git(["commit", "-m", "bad attempt"], cwd=p)
            await o._release_workspaces_for_task("tsk-1")

            t = await o.db.get_task("tsk-1")
            p2 = Path(await o._prepare_workspace(t, a))
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p2) == "aq/tsk-1"
            assert not (p2 / "attempt.txt").exists(), "a retry starts clean"
        finally:
            await o.shutdown()


# ─────────────────────────── the flag actually gates ─────────────────────


class TestRolloutGate:
    async def test_flag_off_uses_the_base_clone_and_makes_no_slots(
        self, tmp_path, base_repo
    ):
        """Default config: nothing about this lane is reachable."""
        o = await _orch(tmp_path, worktrees_enabled=False)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE)
            task, agent = await _mk(o, "tsk-1", "a-1")

            path = await o._prepare_workspace(task, agent)
            assert Path(path) == base_repo
            assert await o.db.list_slots_for_base("ws-base") == []
            assert not (base_repo / ".aq").exists()
        finally:
            await o.shutdown()

    async def test_exclusive_clone_kind_is_unchanged_even_with_the_flag_on(
        self, tmp_path, base_repo
    ):
        """Upgrading installs are backfilled to exclusive-clone; they must not
        change behavior just because the operator turned the flag on."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_EXCLUSIVE_CLONE)
            task, agent = await _mk(o, "tsk-1", "a-1")

            path = await o._prepare_workspace(task, agent)
            assert Path(path) == base_repo
            assert await o.db.list_slots_for_base("ws-base") == []
        finally:
            await o.shutdown()


# ───────────────────────────── phase boundaries ──────────────────────────


class TestPhaseBoundaries:
    async def test_no_merge_slot_is_touched_by_prepare(self, tmp_path, base_repo):
        """Integration is phase 3.  Preparing a slot must not lease anything."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE)
            task, agent = await _mk(o, "tsk-1", "a-1")
            await o._prepare_workspace(task, agent)

            import sqlalchemy as sa

            from src.database.tables import merge_slots

            async with o.db._engine.begin() as conn:
                rows = (await conn.execute(sa.select(merge_slots))).fetchall()
            assert rows == [], "phase 2 must not write merge_slots"
        finally:
            await o.shutdown()

    async def test_verify_automerge_is_still_the_clone_path(self):
        """Phase 2 does not change merge behavior — deliberately.

        ``_phase_verify`` still auto-merges the task branch into the default
        branch, which contradicts merge-slot serialization.  The spec resolves
        that by skipping the auto-merge under worktree mode, and that
        resolution belongs to phase 3.  This test pins the *current* state so
        the change is visible when phase 3 lands rather than silent.
        """
        import inspect

        from src.orchestrator.git_ops import GitOpsMixin

        src = inspect.getsource(GitOpsMixin._phase_verify)
        assert "auto-merge" in src.lower(), (
            "if the auto-merge left _phase_verify, phase 3 has landed and this "
            "guard should be replaced by its worktree-mode assertions"
        )
        assert "worktree" not in src.lower(), (
            "_phase_verify became worktree-aware — that is phase 3's change, "
            "not phase 2's; update this test alongside it"
        )
