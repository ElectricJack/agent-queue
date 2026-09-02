"""A task that pays for worktree-slot growth must not starve behind it.

Observed live 2026-09-02 06:10–06:25 (cap 26, 22 live sessions): a
priority-3 merge sweep sat READY/PAUSED for ~40 minutes while priority-30
tasks kept dispatching.  Each round it triggered ``ensure_slots(len+1)``,
lost the new slot to a concurrent dispatch, and was PAUSED for 60 s with
"worktree slot pool is still warming up" — invisible to the scheduler's
priority ordering for the whole window, during which the next lower-priority
READY task took the slot it had just funded.

Three independent defects, one per section below:

* growth reported only a stale pre-growth ``warming`` bool, so the dispatch
  that created a slot could not prefer it (``SlotGrowth`` / the acquisition
  preference);
* every outcome collapsed into the same quiet 60 s "warming up" pause, so a
  lost race, a stalled pool and a genuinely full pool were indistinguishable
  (``_slot_wait_reason``);
* a slot-starved pause outlived the wait, so priority never got to decide
  who took a freed slot (``_resume_slot_starved_tasks``).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.models import (
    KIND_MODE_EXCLUSIVE_CLONE,
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
from src.orchestrator.workspace import SlotGrowth
from src.orchestrator.worktree_manager import slot_path
from src.scheduler import Scheduler, SchedulerState


def _git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


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
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


async def _orch(tmp_path: Path, *, worktrees_enabled: bool = True) -> Orchestrator:
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = worktrees_enabled
    o = Orchestrator(config)
    await o.initialize()
    return o


async def _seed(
    o: Orchestrator,
    base_repo: Path | None,
    *,
    mode: str = KIND_MODE_WORKTREE,
    cap: int = 3,
) -> None:
    await o.db.create_project(
        Project(
            id="p1",
            name="alpha",
            repo_url="",
            repo_default_branch="main",
            max_concurrent_agents=cap,
        )
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
    if base_repo is not None:
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


# ── 1. growth hands the slot to the task that paid for it ────────────────


class TestGrowthHandsOverTheSlot:
    async def test_new_slot_goes_to_the_dispatch_that_created_it(
        self, tmp_path, base_repo
    ):
        """The second dispatch lands in the slot its own growth provisioned."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo, cap=3)
            first, agent_a = await _mk(o, "tsk-a", "a-1")
            assert Path(await o._prepare_workspace(first, agent_a)) == slot_path(
                base_repo, 0
            )

            # slot-0 is now locked, so this dispatch must grow the pool — and
            # must then be the one that gets what it grew.
            second, agent_b = await _mk(o, "tsk-b", "a-2")
            path = await o._prepare_workspace(second, agent_b)

            assert Path(path) == slot_path(base_repo, 1)
            assert "tsk-b" not in o._workspace_wait_reasons
        finally:
            await o.shutdown()

    async def test_growth_reports_the_row_it_created(self, tmp_path, base_repo):
        """``SlotGrowth.created`` names the new slot so it can be preferred."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo, cap=3)
            task, _agent = await _mk(o, "tsk-a", "a-1")
            project = await o.db.get_project("p1")

            first = await o._ensure_worktree_slots_for_task(task, project)
            assert list(first.created) == ["project-repo"]
            slot_0 = await o.db.get_workspace(first.created["project-repo"])
            assert slot_0.slot_index == 0
            assert first.warming is True

            # With slot-0 locked, the next dispatch has to grow again — and
            # must be told which row it just bought.
            assert await o.db.acquire_workspace(
                "p1", "a-1", "tsk-a", preferred_workspace_id=slot_0.id
            ) is not None
            second = await o._ensure_worktree_slots_for_task(task, project)

            assert list(second.created) == ["project-repo"]
            assert second.created["project-repo"] != slot_0.id
            new_ws = await o.db.get_workspace(second.created["project-repo"])
            assert new_ws.slot_index == 1
            assert second.grew is True

            # A pool with something free grows nothing and claims nothing.
            idle = await o._ensure_worktree_slots_for_task(task, project)
            assert idle.created == {}
            assert idle.grew is False
            assert idle.worktree is True
        finally:
            await o.shutdown()

    async def test_acquisition_prefers_the_fresh_slot(self, tmp_path, base_repo, monkeypatch):
        """The created id is handed to ``acquire_for_task`` as the preference."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo, cap=3)
            first, agent_a = await _mk(o, "tsk-a", "a-1")
            await o._prepare_workspace(first, agent_a)

            seen: dict = {}
            import src.orchestrator.workspace_attachments as wa

            real = wa.acquire_for_task

            async def _spy(db, task, agent_id, **kwargs):
                seen.update(kwargs)
                return await real(db, task, agent_id, **kwargs)

            monkeypatch.setattr(wa, "acquire_for_task", _spy)

            second, agent_b = await _mk(o, "tsk-b", "a-2")
            await o._prepare_workspace(second, agent_b)

            preferred = seen["preferred_workspaces"]
            new_ws = await o.db.get_workspace(preferred["project-repo"])
            assert new_ws.slot_index == 1
        finally:
            await o.shutdown()

    async def test_branch_affinity_still_wins_over_the_fresh_slot(self):
        """A slot holding the task's branch is a correctness hint, not fairness.

        Only that slot will accept ``git switch aq/<task_id>``; preferring a
        brand-new one instead would re-create the collision loop §3.4 exists
        to remove.
        """
        growth = SlotGrowth(created={"project-repo": "ws-new"}, worktree=True)
        affinity = {"project-repo": "ws-holding-branch"}
        assert {**growth.created, **affinity} == {"project-repo": "ws-holding-branch"}


# ── 2. the wait reason tells the four outcomes apart ─────────────────────


class TestWaitReason:
    def test_a_created_slot_is_never_reported_as_warming_up(self):
        """The bug: growth succeeded, so this is a lost race, not a ramp."""
        growth = SlotGrowth(
            created={"project-repo": "ws-new"}, warming=True, worktree=True
        )
        assert Orchestrator._slot_wait_reason(growth) == "slot_lost_race"

    def test_stalled_growth_outranks_warming(self):
        growth = SlotGrowth(warming=True, stalled=True, worktree=True)
        assert Orchestrator._slot_wait_reason(growth) == "slot_stalled"

    def test_a_ramping_pool_is_still_warming(self):
        assert (
            Orchestrator._slot_wait_reason(SlotGrowth(warming=True, worktree=True))
            == "slot_warming"
        )

    def test_a_full_pool_is_contention_not_a_ramp(self):
        assert Orchestrator._slot_wait_reason(SlotGrowth(worktree=True)) == "slots_full"

    def test_the_clone_path_has_no_slot_reason(self):
        assert Orchestrator._slot_wait_reason(SlotGrowth()) is None

    async def test_lost_race_reason_is_set_when_acquisition_loses(
        self, tmp_path, base_repo, monkeypatch
    ):
        """End-to-end: grow, have the row stolen, and report the right wait."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo, cap=3)
            first, agent_a = await _mk(o, "tsk-a", "a-1")
            await o._prepare_workspace(first, agent_a)

            import src.orchestrator.workspace_attachments as wa

            async def _stolen(db, task, agent_id, **kwargs):
                raise wa.AcquisitionFailed("project-repo")

            monkeypatch.setattr(wa, "acquire_for_task", _stolen)

            second, agent_b = await _mk(o, "tsk-b", "a-2")
            assert await o._prepare_workspace(second, agent_b) is None
            assert o._workspace_wait_reasons["tsk-b"] == "slot_lost_race"
        finally:
            await o.shutdown()

    async def test_stalled_growth_is_reported_when_no_row_appears(
        self, tmp_path, base_repo
    ):
        """``ensure_slots`` swallows git failures; the caller must not."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo, cap=3)
            task, agent = await _mk(o, "tsk-a", "a-1")
            await o._prepare_workspace(task, agent)
            project = await o.db.get_project("p1")

            existing = await o.db.list_slots_for_base("ws-base")

            async def _no_new_slots(project, base, kind, count):
                return existing

            o._worktree_slot_manager = SimpleNamespace(ensure_slots=_no_new_slots)
            growth = await o._ensure_worktree_slots_for_task(task, project)

            assert growth.stalled is True
            assert growth.grew is False
            assert Orchestrator._slot_wait_reason(growth) == "slot_stalled"
        finally:
            await o.shutdown()


# ── 3. priority, not backoff order, wins a freed slot ────────────────────


async def _slot_env(tmp_path, *, cap=2, free_slot=False):
    """A worktree project with two slots, both locked unless *free_slot*."""
    o = await _orch(tmp_path)
    await _seed(o, None, cap=cap)
    await o.db.create_workspace(
        Workspace(
            id="ws-base",
            project_id="p1",
            workspace_path=str(tmp_path / "repo"),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
    )
    for idx in range(cap):
        await o.db.create_workspace(
            Workspace(
                id=f"slot-{idx}",
                project_id="p1",
                workspace_path=str(tmp_path / "repo" / ".aq" / "worktrees" / f"slot-{idx}"),
                source_type=RepoSourceType.WORKTREE,
                kind_id="project-repo",
                slot_index=idx,
                base_workspace_id="ws-base",
            )
        )
    busy = range(cap - 1) if free_slot else range(cap)
    for idx in busy:
        await o.db.create_agent(
            Agent(id=f"holder-{idx}", name=f"holder-{idx}", profile_id="test-profile")
        )
        await o.db.create_task(
            Task(
                id=f"holder-task-{idx}",
                project_id="p1",
                title=f"holder-task-{idx}",
                description="",
            )
        )
        assert await o.db.acquire_workspace(
            "p1", f"holder-{idx}", f"holder-task-{idx}", preferred_workspace_id=f"slot-{idx}"
        ) is not None
    return o


async def _park(o, task_id, priority, *, reason="slot_lost_race", backoff=60.0):
    await o.db.create_task(
        Task(
            id=task_id,
            project_id="p1",
            title=task_id,
            description="",
            priority=priority,
            status=TaskStatus.READY,
        )
    )
    await o.db.transition_task(
        task_id,
        TaskStatus.PAUSED,
        context="no_workspace_available",
        resume_after=time.time() + backoff,
    )
    from src.orchestrator.execution import _SLOT_WAIT_REASONS

    if reason in _SLOT_WAIT_REASONS:
        o._slot_starved_pauses[task_id] = "p1"


class TestFreedSlotGoesToTheHighestPriorityWaiter:
    async def test_backoff_is_cut_short_when_a_slot_frees(self, tmp_path):
        o = await _slot_env(tmp_path, free_slot=True)
        try:
            await _park(o, "urgent", 3)
            await o._resume_paused_tasks()

            assert (await o.db.get_task("urgent")).status == TaskStatus.READY
            assert "urgent" not in o._slot_starved_pauses
        finally:
            await o.shutdown()

    async def test_a_full_pool_leaves_the_backoff_alone(self, tmp_path):
        """No free slot means nothing to race for — the timer still rules."""
        o = await _slot_env(tmp_path, free_slot=False)
        try:
            await _park(o, "urgent", 3)
            await o._resume_paused_tasks()

            assert (await o.db.get_task("urgent")).status == TaskStatus.PAUSED
            assert "urgent" in o._slot_starved_pauses
        finally:
            await o.shutdown()

    async def test_a_branch_held_wait_is_not_resumed_early(self, tmp_path):
        """A free slot does not resolve "my branch is checked out elsewhere".

        Resuming it every cycle would turn its operator notice into spam.
        """
        o = await _slot_env(tmp_path, free_slot=True)
        try:
            await _park(o, "held", 3, reason="branch_held")
            await o._resume_paused_tasks()

            assert (await o.db.get_task("held")).status == TaskStatus.PAUSED
        finally:
            await o.shutdown()

    async def test_priority_3_beats_priority_30_for_the_freed_slot(self, tmp_path):
        """The whole point: the resumed high-priority task takes the slot.

        Both tasks were parked on the same wait; the freed slot wakes both,
        and the scheduler's ``(priority, id)`` ordering — not whichever
        backoff expired first — decides which one dispatches.
        """
        o = await _slot_env(tmp_path, free_slot=True)
        try:
            await _park(o, "merge-sweep", 3)
            await _park(o, "routine-a", 30)
            await _park(o, "routine-b", 30)

            await o._resume_paused_tasks()

            tasks = [await o.db.get_task(t) for t in ("merge-sweep", "routine-a", "routine-b")]
            assert all(t.status == TaskStatus.READY for t in tasks)

            agent = Agent(
                id="idle-1", name="idle-1", profile_id="test-profile", state=AgentState.IDLE
            )
            actions = Scheduler.schedule(
                SchedulerState(
                    projects=[await o.db.get_project("p1")],
                    tasks=tasks,
                    agents=[agent],
                    project_token_usage={},
                    project_active_agent_counts={"p1": 1},
                    tasks_completed_in_window={},
                    project_available_workspaces={"p1": 1},
                )
            )

            assert [a.task_id for a in actions] == ["merge-sweep"]
        finally:
            await o.shutdown()

    async def test_stale_entries_are_pruned_once_the_task_leaves_paused(self, tmp_path):
        o = await _slot_env(tmp_path, free_slot=False)
        try:
            await _park(o, "urgent", 3)
            await o.db.transition_task("urgent", TaskStatus.READY, resume_after=None)

            await o._resume_paused_tasks()

            assert o._slot_starved_pauses == {}
        finally:
            await o.shutdown()


# ── the inventory question the cascade actually asks ─────────────────────


class TestCountFreeSlots:
    async def test_counts_unlocked_in_cap_slots_only(self, tmp_path):
        o = await _slot_env(tmp_path, cap=2, free_slot=True)
        try:
            assert await o.db.count_free_slots("p1", worktree_slot_cap=2) == 1
            # A shrunk cap makes slot-1 out of cap, so nothing is acquirable.
            assert await o.db.count_free_slots("p1", worktree_slot_cap=1) == 0
        finally:
            await o.shutdown()

    async def test_headroom_is_not_inventory(self, tmp_path):
        """``count_available_workspaces`` says "yes" all through the ramp.

        That is right for the reconciler and wrong for the cascade: a
        mid-ramp project would be told a slot is free on every cycle.
        """
        o = await _slot_env(tmp_path, cap=2, free_slot=False)
        try:
            assert await o.db.count_available_workspaces("p1", worktree_slot_cap=4) > 0
            assert await o.db.count_free_slots("p1", worktree_slot_cap=4) == 0
        finally:
            await o.shutdown()

    async def test_clone_mode_rows_are_not_slots(self, tmp_path):
        o = await _orch(tmp_path)
        try:
            await _seed(o, None, mode=KIND_MODE_EXCLUSIVE_CLONE, cap=2)
            await o.db.create_workspace(
                Workspace(
                    id="clone-0",
                    project_id="p1",
                    workspace_path=str(tmp_path / "clone-0"),
                    source_type=RepoSourceType.CLONE,
                    kind_id="project-repo",
                )
            )
            assert await o.db.count_free_slots("p1", worktree_slot_cap=2) == 0
        finally:
            await o.shutdown()
