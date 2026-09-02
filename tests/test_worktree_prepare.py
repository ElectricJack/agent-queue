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

    async def test_retry_keeps_what_the_previous_attempt_pushed(
        self, tmp_path, base_repo
    ):
        """A pushed attempt is published; the retry must see its own work.

        Re-pointing the branch at the fresh start point is right while the
        work is only local.  Once it is on ``origin/<branch>`` — very often
        with a PR open on it — discarding it locally unpublishes nothing:
        the agent just finds an empty branch, and its next push is a
        non-fast-forward.  So the retry resumes from the published tip.
        """
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            t, a = await _mk(o, "tsk-1", "a-1")
            p = Path(await o._prepare_workspace(t, a))
            (p / "attempt.txt").write_text("real work\n")
            _git(["add", "-A"], cwd=p)
            _git(["commit", "-m", "real work"], cwd=p)
            pushed = _git(["rev-parse", "HEAD"], cwd=p)
            _git(["push", "origin", "aq/tsk-1"], cwd=p)
            await o._release_workspaces_for_task("tsk-1")

            t = await o.db.get_task("tsk-1")
            p2 = Path(await o._prepare_workspace(t, a))
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=p2) == "aq/tsk-1"
            assert _git(["rev-parse", "HEAD"], cwd=p2) == pushed
            assert (p2 / "attempt.txt").exists(), "pushed work must survive the retry"
        finally:
            await o.shutdown()

    async def test_retry_moves_forward_when_the_pushed_tip_is_already_merged(
        self, tmp_path, base_repo
    ):
        """Keeping the published tip must not pin a retry to stale history."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            t, a = await _mk(o, "tsk-1", "a-1")
            p = Path(await o._prepare_workspace(t, a))
            (p / "attempt.txt").write_text("merged work\n")
            _git(["add", "-A"], cwd=p)
            _git(["commit", "-m", "merged work"], cwd=p)
            _git(["push", "origin", "aq/tsk-1"], cwd=p)
            await o._release_workspaces_for_task("tsk-1")

            # Someone merged it and main moved on.
            _git(["checkout", "main"], cwd=base_repo)
            _git(["merge", "--ff-only", "aq/tsk-1"], cwd=base_repo)
            (base_repo / "later.txt").write_text("after\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "later"], cwd=base_repo)
            _git(["push", "origin", "main"], cwd=base_repo)
            head_of_main = _git(["rev-parse", "main"], cwd=base_repo)

            t = await o.db.get_task("tsk-1")
            p2 = Path(await o._prepare_workspace(t, a))
            assert _git(["rev-parse", "HEAD"], cwd=p2) == head_of_main
            assert (p2 / "later.txt").exists(), "the retry starts from current main"
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Phase 3 owns this: _phase_verify must skip its auto-merge under "
            "worktree mode, because integration belongs to the merge slot. "
            "Today it attempts `git checkout <default>` in the slot, which git "
            "refuses while the base holds that branch — so it usually fails "
            "harmlessly with a logged warning rather than merging without the "
            "slot. 'Usually' is the problem: if the base is not on its default "
            "branch the checkout succeeds and the merge really does run "
            "un-serialized. When phase 3 lands, this xfail turns into a strict "
            "failure and the test becomes the assertion."
        ),
    )
    async def test_verify_skips_the_automerge_under_worktree_mode(
        self, tmp_path, base_repo
    ):
        from src.models import AgentOutput, PipelineContext

        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            task, agent = await _mk(o, "tsk-1", "a-1")
            slot = Path(await o._prepare_workspace(task, agent))
            (slot / "work.txt").write_text("done")
            _git(["add", "-A"], cwd=slot)
            _git(["commit", "-m", "work"], cwd=slot)

            issued: list[list[str]] = []
            real_arun = o.git._arun

            async def recording(args, cwd=None, timeout=None):
                issued.append(list(args))
                return await real_arun(args, cwd=cwd, timeout=timeout)

            o.git._arun = recording
            try:
                await o._phase_verify(
                    PipelineContext(
                        task=await o.db.get_task("tsk-1"),
                        agent=agent,
                        output=AgentOutput(success=True, exit_code=0),
                        workspace_path=str(slot),
                        workspace_id="ws-base",
                        repo=None,
                        default_branch="main",
                        project=await o.db.get_project("p1"),
                    )
                )
            finally:
                o.git._arun = real_arun

            assert ["checkout", "main"] not in issued, (
                "the auto-merge must not even be attempted in a slot — "
                "integration is the merge slot's job (design §4.2)"
            )
        finally:
            await o.shutdown()


# ─────────────────── restart: adopt, don't delete (F1 / F8) ──────────────


class TestRecoveryDoesNotDestroySlots:
    """`_recover_stale_state` walks every ``source_type=WORKTREE`` row.

    Slot rows are now in that set, and `aworktree_base_path` resolves them
    (the retired `_get_worktree_base_path` returned None and accidentally
    spared them), so an unguarded call would run `git worktree remove` with
    a `--force` fallback against a live slot on every daemon start.
    """

    async def _slot_with_work(self, o, base_repo):
        await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=2)
        task, agent = await _mk(o, "tsk-1", "a-1")
        slot = Path(await o._prepare_workspace(task, agent))
        (slot / "in-progress.txt").write_text("uncommitted work")
        cache = slot / "node_modules"
        cache.mkdir()
        (cache / "dep.js").write_text("expensive")
        return slot, cache

    async def test_uncommitted_work_and_caches_survive_a_restart(
        self, tmp_path, base_repo
    ):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            slot, cache = await self._slot_with_work(o, base_repo)

            await o._recover_stale_state()

            assert slot.is_dir(), "the slot directory must survive a restart"
            assert (slot / "in-progress.txt").read_text() == "uncommitted work"
            assert cache.joinpath("dep.js").exists(), "warm caches must survive"
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot) == "aq/tsk-1"

            rows = await o.db.list_slots_for_base("ws-base")
            assert len(rows) == 1, "the slot row must survive too"
            assert rows[0].locked_by_agent_id is None, "but its stale lock is released"
            assert rows[0].locked_by_task_id is None

            # Still a registered worktree of the base.
            paths = {
                str(Path(e["path"]).resolve())
                for e in await o.git.aworktree_list(str(base_repo))
            }
            assert str(slot.resolve()) in paths
        finally:
            await o.shutdown()

    async def test_recovery_rebuilds_the_slot_to_base_lock_map(
        self, tmp_path, base_repo
    ):
        """F8: a task IN_PROGRESS across a restart never re-enters
        `_prepare_workspace`, so a map built only there stays empty and the
        slot's `fetch` stops serializing against the base's object store."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            slot, _cache = await self._slot_with_work(o, base_repo)

            # Simulate the fresh process: nothing cached in memory.
            o._worktree_base_paths.clear()
            o._git_mutexes.clear()
            assert o._resolve_git_lock(str(slot)) is None

            await o._recover_stale_state()

            assert o._resolve_git_lock(str(slot)) is o._git_mutexes[str(base_repo)]
        finally:
            await o.shutdown()

    async def test_legacy_branch_isolated_rows_are_still_cleaned(
        self, tmp_path, base_repo
    ):
        """The guard is `is_slot`, not `source_type` — pre-lane rows still go."""
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=2)
            legacy_dir = tmp_path / "legacy-worktree"
            await o.git.aworktree_add(str(base_repo), str(legacy_dir), ref="main")
            await o.db.create_workspace(
                Workspace(
                    id="ws-legacy",
                    project_id="p1",
                    workspace_path=str(legacy_dir),
                    source_type=RepoSourceType.WORKTREE,
                    kind_id="project-repo",
                )
            )

            await o._recover_stale_state()

            assert await o.db.get_workspace("ws-legacy") is None
            assert not legacy_dir.exists()
        finally:
            await o.shutdown()


# ──────────────── terminal failure never stashes in a slot (F4) ──────────


class TestTerminalCleanupInASlot:
    async def test_cleanup_salvages_instead_of_stashing(self, tmp_path, base_repo):
        o = await _orch(tmp_path, worktrees_enabled=True)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_WORKTREE, cap=1)
            task, agent = await _mk(o, "tsk-1", "a-1")
            slot = Path(await o._prepare_workspace(task, agent))
            (slot / "README.md").write_text("half-done")
            (slot / "junk.tmp").write_text("junk")
            cache = slot / "node_modules"
            cache.mkdir()
            (cache / "dep.js").write_text("expensive")

            await o._cleanup_workspace_for_next_task(str(slot), "main", "tsk-1")

            # 1. No stash anywhere — the stack is shared with the base.
            assert _git(["stash", "list"], cwd=base_repo) == ""
            assert _git(["stash", "list"], cwd=slot) == ""
            # 2. Warm caches survive (never `clean -fdx`).
            assert cache.joinpath("dep.js").exists()
            # 3. Still on the task branch — `checkout main` would fail anyway,
            #    the base holds it.
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot) == "aq/tsk-1"
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo) == "main"
            # 4. And the work is recoverable.
            salvage = [
                c
                for c in await o.db.get_task_contexts("tsk-1")
                if c["type"] == "worktree_salvage"
            ]
            assert len(salvage) == 1 and "half-done" in salvage[0]["content"]
            assert _git(["status", "--porcelain"], cwd=slot) == ""
            assert not (slot / "junk.tmp").exists()
        finally:
            await o.shutdown()

    async def test_clone_workspaces_keep_the_legacy_ladder(self, tmp_path, base_repo):
        """The slot branch must not change exclusive-clone behavior."""
        o = await _orch(tmp_path, worktrees_enabled=False)
        try:
            await _seed(o, base_repo, mode=KIND_MODE_EXCLUSIVE_CLONE, cap=1)
            (base_repo / "dirty.txt").write_text("uncommitted")

            await o._cleanup_workspace_for_next_task(str(base_repo), "main", "tsk-x")

            # The clone path commits rather than salvaging, and ends on main.
            assert _git(["status", "--porcelain"], cwd=base_repo) == ""
            assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo) == "main"
        finally:
            await o.shutdown()
