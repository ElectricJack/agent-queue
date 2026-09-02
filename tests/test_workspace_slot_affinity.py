"""A retry must land back on the slot that still holds its branch.

Design §3.4 leaves a released slot **on its last task's branch** — the branch
is the durable artifact, not the worktree.  The cost of that choice is a
collision the scheduler used to walk straight into: a task that retries and
is handed a *different* slot cannot ``git switch aq/<task_id>``, because its
own predecessor slot still has it.  Nothing moves the old slot off the branch
on its own, so the task paused for the no-workspace backoff, retried, and
collided again, forever (sound-grove made that visible; it did not remove it).

The fix is affinity at acquisition: prefer the slot that already holds the
branch.  These tests drive real git worktrees through a real ``Database`` and
a real ``Orchestrator``, so the hint is checked against the same
``git worktree list`` state git itself consults when it refuses a checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.config import AppConfig
from src.database import Database
from src.models import (
    KIND_MODE_WORKTREE,
    Agent,
    AgentProfile,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import task_branch_name

pytestmark = pytest.mark.asyncio


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
async def env(tmp_path):
    """One base clone with two real slots, wired to a real orchestrator."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base_path = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base_path)], check=True, capture_output=True
    )
    (base_path / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=base_path)
    _git(["commit", "-m", "init"], cwd=base_path)
    _git(["push", "origin", "main"], cwd=base_path)

    db = Database(str(tmp_path / "affinity.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p", max_concurrent_agents=2))
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    kind = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE,
        id="project-repo",
        is_git_repo=True,
        lockable=True,
        mode=KIND_MODE_WORKTREE,
        default_lock_mode="exclusive",
    )
    await db.upsert_workspace_kind(kind)
    await db.create_workspace(
        Workspace(
            id="ws-base",
            project_id="p",
            workspace_path=str(base_path),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
    )

    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    config.worktrees.enabled = True
    orch = Orchestrator(config)
    orch.db = db

    base_ws = await db.get_workspace("ws-base")
    project = await db.get_project("p")
    slots = await orch._worktree_slots().ensure_slots(project, base_ws, kind, 2)
    assert len(slots) == 2

    class _Env:
        pass

    e = _Env()
    e.db, e.orch, e.project, e.base_ws, e.kind = db, orch, project, base_ws, kind
    e.slots = {s.slot_index: s for s in slots}
    yield e
    await db.close()


async def _task(env, task_id, **kwargs) -> Task:
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


async def test_no_affinity_before_a_task_has_ever_run(env):
    """First run: every slot is detached, so there is nothing to prefer."""
    task = await _task(env, "eager-impact")
    assert await env.orch._slot_branch_affinity(task, env.project) == {}


async def test_the_predecessor_slot_is_preferred_on_retry(env):
    """The whole point: slot-1 ran the task and kept its branch, so the
    retry is steered back to slot-1 instead of colliding from slot-0."""
    task = await _task(env, "eager-impact")
    slot1 = env.slots[1]
    await env.orch._worktree_slots().reset_slot_for_task(slot1, task, kind=env.kind)
    await env.orch._worktree_slots().restore_slot_after_task(slot1, task_id=task.id)
    assert (
        _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot1.workspace_path)
        == task_branch_name(task.id)
    )

    assert await env.orch._slot_branch_affinity(task, env.project) == {
        "project-repo": slot1.id
    }


async def test_acquisition_hands_the_retry_its_own_slot_back(env):
    """End to end: without the hint acquisition takes slot-0 (first free) and
    ``git switch`` is refused; with it the task gets slot-1 and resets clean."""
    task = await _task(env, "eager-impact")
    slots = env.orch._worktree_slots()
    await slots.reset_slot_for_task(env.slots[1], task, kind=env.kind)
    await slots.restore_slot_after_task(env.slots[1], task_id=task.id)
    # A live holder is never detached by preparation; the acquisition hint
    # is still needed for this (normal) retry path.
    from src.claim_file import write_claim_file

    write_claim_file(env.slots[1].workspace_path, {"task_id": task.id})

    # The collision the hint exists to prevent is real: slot-0 cannot take it.
    with pytest.raises(Exception, match="already (checked out|used by worktree)"):
        await slots.reset_slot_for_task(env.slots[0], task, kind=env.kind)

    agent = await env.db.get_agent("agent")
    path = await env.orch._prepare_workspace_locked(task, agent)
    assert path == env.slots[1].workspace_path
    assert (
        _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        == task_branch_name(task.id)
    )
    assert (await env.db.get_workspace(env.slots[1].id)).locked_by_task_id == task.id


async def test_preparation_detaches_an_unclaimed_stale_branch_holder(env):
    """A dead pool slot cannot pin a task branch forever."""
    task = await _task(env, "stale-holder")
    slots = env.orch._worktree_slots()
    await slots.reset_slot_for_task(env.slots[1], task, kind=env.kind)

    branch = await slots.reset_slot_for_task(env.slots[0], task, kind=env.kind)

    assert branch == task_branch_name(task.id)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=env.slots[1].workspace_path) == "HEAD"


async def test_pool_retry_can_use_another_slot_after_clean_pushed_release(env):
    """Pool slots are acquired before task selection, so release unpins it.

    This is the pool-specific retry sequence: task ran in slot A, closed a
    transient attempt after pushing its branch, then a pool worker already
    bound to slot B claims the retry. Without the clean-release detach, the
    second reset is rejected because slot A still owns ``aq/<task>``.
    """
    task = await _task(env, "pool-cross-slot")
    slots = env.orch._worktree_slots()
    slot_a, slot_b = env.slots[0], env.slots[1]
    await slots.reset_slot_for_task(slot_a, task, kind=env.kind)
    _git(["push", "origin", task_branch_name(task.id)], cwd=slot_a.workspace_path)

    await slots.restore_slot_after_task(slot_a, task_id=task.id)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot_a.workspace_path) == "HEAD"

    branch = await slots.reset_slot_for_task(slot_b, task, kind=env.kind)
    assert branch == task_branch_name(task.id)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot_b.workspace_path) == branch


async def test_pool_doctor_reports_and_detaches_stale_slot_checkout(env):
    task = await _task(env, "doctor-stale")
    slot = env.slots[1]
    await env.orch._worktree_slots().reset_slot_for_task(slot, task, kind=env.kind)

    from src.doctor.models import Severity
    from src.doctor.pool_checks import run_check

    finding = await run_check(env.db, "pools.stale_worktree_checkouts", config=None)
    assert finding.severity is Severity.WARN
    assert finding.data["slots"] == [
        {
            "workspace_id": slot.id,
            "path": slot.workspace_path,
            "branch": task_branch_name(task.id),
            "task_id": task.id,
            "task_status": TaskStatus.ASSIGNED.value,
        }
    ]

    repaired = await run_check(
        env.db, "pools.stale_worktree_checkouts", config=None, repair=True
    )
    assert repaired.severity is Severity.OK
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot.workspace_path) == "HEAD"


async def test_a_plan_subtask_follows_its_parents_branch(env):
    """Subtasks share the parent's branch (§4.4), so affinity is computed on
    the branch the subtask will *resume*, not on ``aq/<subtask_id>``."""
    parent = await _task(env, "plan")
    slot0 = env.slots[0]
    await env.orch._worktree_slots().reset_slot_for_task(slot0, parent, kind=env.kind)
    await env.orch._worktree_slots().restore_slot_after_task(slot0, task_id=parent.id)
    await env.db.update_task(parent.id, branch_name=task_branch_name(parent.id))

    child = await _task(env, "step-1", parent_task_id=parent.id, is_plan_subtask=True)
    assert await env.orch._slot_branch_affinity(child, env.project) == {
        "project-repo": slot0.id
    }


async def test_a_running_sibling_keeps_its_slot_and_the_subtask_falls_back(env):
    """A locked holder is never waited on: the subtask takes a free slot and
    hits the pre-existing (self-clearing) sibling wait instead of parking
    behind whatever the holder is doing."""
    parent = await _task(env, "plan")
    slot0 = env.slots[0]
    await env.orch._worktree_slots().reset_slot_for_task(slot0, parent, kind=env.kind)
    await env.db.update_task(parent.id, branch_name=task_branch_name(parent.id))
    sibling = await _task(env, "step-1", parent_task_id=parent.id, is_plan_subtask=True)
    assert (
        await env.db.acquire_workspace(
            "p", "agent", sibling.id, preferred_workspace_id=slot0.id
        )
        is not None
    )

    child = await _task(env, "step-2", parent_task_id=parent.id, is_plan_subtask=True)
    # The hint still names the holder — acquisition is where "busy" is decided.
    assert await env.orch._slot_branch_affinity(child, env.project) == {
        "project-repo": slot0.id
    }

    from src.orchestrator.workspace_attachments import acquire_for_task

    att = await acquire_for_task(
        env.db,
        child,
        "agent",
        worktrees_enabled=True,
        worktree_slot_cap=2,
        preferred_workspaces={"project-repo": slot0.id},
    )
    assert att.first_of_kind("project-repo").workspace.id == env.slots[1].id
