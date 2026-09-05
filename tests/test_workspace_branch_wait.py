"""A branch-busy slot reset must not be mistaken for a plan-sibling wait.

Two plan subtasks of the same parent share one branch and serialize onto it
by design (worktree-execution §4.4), so ``git switch`` refusing the branch is
a scheduling wait that clears itself.  A task with no parent branch has no
sibling to wait for: the same refusal means its *own* ``aq/<task_id>`` is
still checked out in a slot nobody is going to move.

Observed live (calm-flare, 2026-09-01): a plain project task — no parent, no
branch — looped on ``waits for branch None — a sibling holds it in another
slot``, which suppressed the operator notice, so the wait never surfaced.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from src.config import AppConfig
from src.database import Database
from src.database.tables import task_branch_origins
from src.git.manager import GitError
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.models import (
    KIND_MODE_WORKTREE,
    Agent,
    AgentProfile,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator

pytestmark = pytest.mark.asyncio

BRANCH_BUSY = GitError(
    "git switch aq/calm-flare failed: fatal: 'aq/calm-flare' is already "
    "checked out at '/repo/.aq/worktrees/slot-3'"
)


class _StubSlots:
    """Stands in for ``WorktreeSlotManager``; every reset raises *exc*."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls: list[tuple[str, str | None]] = []

    async def reset_slot_for_task(
        self, ws, task, *, base_branch=None, resume_branch=None, kind=None
    ):
        self.calls.append((task.id, resume_branch))
        raise self.exc


class _SuccessfulSlots:
    def __init__(self):
        self.calls = []

    async def reset_slot_for_task(self, ws, task, **kwargs):
        self.calls.append((ws.id, task.id, kwargs))
        return f"aq/{task.id}"


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "wait.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p"))
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    base = Workspace(
        id="base",
        project_id="p",
        workspace_path=str(tmp_path / "repo"),
        source_type=RepoSourceType.CLONE,
        kind_id="project-repo",
    )
    slot = Workspace(
        id="slot-0",
        project_id="p",
        workspace_path=str(tmp_path / "repo" / ".aq" / "worktrees" / "slot-0"),
        source_type=RepoSourceType.CLONE,
        kind_id="project-repo",
        slot_index=0,
        base_workspace_id="base",
    )
    await db.create_workspace(base)
    await db.create_workspace(slot)

    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    notices: list[str] = []

    async def _notify(message, project_id=None):
        notices.append(message)

    orch._emit_text_notify = _notify
    kind = WorkspaceKind(project_id="p", id="project-repo", mode=KIND_MODE_WORKTREE)
    attachment = SimpleNamespace(workspace=slot, kind=kind)
    yield SimpleNamespace(
        db=db, orch=orch, slot=slot, attachment=attachment, notices=notices
    )
    await db.close()


async def _run(env, task, exc=BRANCH_BUSY):
    env.orch._worktree_slot_manager = _StubSlots(exc)
    project = await env.db.get_project("p")
    result = await env.orch._prepare_slot_workspace(task, project, env.attachment)
    return result


async def _task(env, task_id, **kwargs):
    task = Task(
        id=task_id,
        project_id="p",
        title=task_id,
        description="d",
        status=TaskStatus.ASSIGNED,
        **kwargs,
    )
    await env.db.create_task(task)
    return task


async def test_plain_task_branch_busy_is_not_a_sibling_wait(env):
    """No parent branch means no sibling: the wait needs its own reason."""
    task = await _task(env, "calm-flare")
    assert await _run(env, task) is None
    assert env.orch._workspace_wait_reasons["calm-flare"] == "branch_held"


async def test_plan_subtask_still_waits_for_its_sibling(env):
    """The sibling wait survives for the case it was written for."""
    parent = await _task(env, "plan", branch_name="aq/plan")
    child = await _task(env, "step-1", parent_task_id=parent.id, is_plan_subtask=True)
    assert await _run(env, child) is None
    assert env.orch._workspace_wait_reasons["step-1"] == "branch_busy"
    assert env.orch._worktree_slot_manager.calls == [("step-1", "aq/plan")]


async def test_plan_subtask_without_a_parent_branch_is_not_a_sibling_wait(env):
    """``is_plan_subtask`` alone is not enough — the parent branch decides.

    A subtask promoted before its parent ever got a branch resumes nothing,
    so it owns ``aq/<task_id>`` exactly like a plain task does.
    """
    parent = await _task(env, "plan2")
    child = await _task(env, "step-2", parent_task_id=parent.id, is_plan_subtask=True)
    assert await _run(env, child) is None
    assert env.orch._workspace_wait_reasons["step-2"] == "branch_held"


async def test_hierarchy_transfer_winning_before_prep_prevents_branch_reset(env):
    await env.db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
    )
    await env.db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    task = await _task(env, "child", repo_id="repo", branch_name="aq/child")
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id=task.id,
                repository_id="repo",
                parent_ref="aq/parent",
                base_sha="a" * 40,
                creation_generation=1,
                reserved=True,
                materialized=True,
                created_at=time.time(),
                materialized_at=time.time(),
            )
        )
    ownership = BranchOwnership(env.db)
    fence = await ownership.acquire(
        BranchKey(repository_id="repo", branch="aq/child"), task.id, "worker"
    )
    origin = await env.db.get_task_branch_origin_for_promotion(task.id, "repo")
    await ownership.transfer(fence, "collector", "collector")
    slots = _StubSlots(BRANCH_BUSY)
    env.orch._worktree_slot_manager = slots

    assert await env.orch._prepare_slot_workspace(
        task,
        await env.db.get_project("p"),
        env.attachment,
        integration_origin=origin,
        integration_fence=fence,
    ) is None
    assert slots.calls == []


async def test_hierarchy_slot_prep_uses_pinned_origin_and_never_parent_resume(env):
    await env.db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
    )
    await env.db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    parent = await _task(env, "parent", repo_id="repo", branch_name="aq/parent")
    child = await _task(
        env,
        "child",
        repo_id="repo",
        branch_name="aq/child",
        parent_task_id=parent.id,
        is_plan_subtask=True,
    )
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id=child.id,
                repository_id="repo",
                parent_task_id=parent.id,
                parent_repository_id="repo",
                parent_ref="aq/parent",
                base_sha="b" * 40,
                creation_generation=1,
                reserved=True,
                materialized=True,
                created_at=time.time(),
                materialized_at=time.time(),
            )
        )
    ownership = BranchOwnership(env.db)
    fence = await ownership.acquire(
        BranchKey(repository_id="repo", branch="aq/child"), child.id, "worker"
    )
    slots = _SuccessfulSlots()
    env.orch._worktree_slot_manager = slots
    origin = await env.db.get_task_branch_origin_for_promotion(child.id, "repo")

    result = await env.orch._prepare_slot_workspace(
        child,
        await env.db.get_project("p"),
        env.attachment,
        integration_origin=origin,
        integration_fence=fence,
    )

    assert result == env.slot.workspace_path
    assert slots.calls[0][2]["base_branch"] == "b" * 40
    assert slots.calls[0][2]["resume_branch"] is None


async def test_non_branch_git_failure_still_reports_a_git_error(env):
    """Unrelated reset failures keep the loud path."""
    task = await _task(env, "boom")
    assert await _run(env, task, GitError("fatal: disk full")) is None
    assert "boom" not in env.orch._workspace_wait_reasons
    assert any("Git Error" in n for n in env.notices)


async def test_branch_held_releases_the_slot_for_other_tasks(env):
    """The slot is never kept hostage by a branch it could not check out."""
    task = await _task(env, "calm-flare")
    assert await env.db.acquire_workspace(
        "p", "agent", task.id, preferred_workspace_id="slot-0"
    ) is not None
    await _run(env, task)
    assert (await env.db.get_workspace("slot-0")).locked_by_task_id is None
