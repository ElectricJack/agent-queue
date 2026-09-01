"""``worktrees.orphans`` — slot worktrees pinned to a task that is gone.

A released slot deliberately stays on its last task's branch (the branch is
the durable artifact, worktree-execution §3.4).  When that task is deleted
the slot keeps ``aq/<task_id>`` checked out with nothing left to finish it,
and git then refuses the branch to every other worktree — which is how a
live task ends up looping on the no-workspace backoff with no way out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.doctor import worktree_checks
from src.doctor.models import RESERVED_CHECK_IDS, Severity
from src.models import (
    WORKTREE_SENTINEL_NAME,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorktreeSentinel,
)

pytestmark = pytest.mark.asyncio

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@pytest.fixture
def slots_root(tmp_path):
    root = tmp_path / "repo" / ".aq" / "worktrees"
    root.mkdir(parents=True)
    return root


async def _slot(db, slots_root, index: int, *, locked: str | None = None) -> Workspace:
    path = slots_root / f"slot-{index}"
    path.mkdir()
    ws = Workspace(
        id=f"slot-{index}",
        project_id=PROJECT_ID,
        workspace_path=str(path),
        source_type=RepoSourceType.CLONE,
        kind_id="project-repo",
        slot_index=index,
        base_workspace_id="base",
        locked_by_task_id=locked,
    )
    await db.create_workspace(ws)
    return ws


def _sentinel(ws: Workspace, task_id: str | None, branch: str | None) -> None:
    payload = WorktreeSentinel(
        slot=f"slot-{ws.slot_index}",
        slot_index=ws.slot_index or 0,
        base_workspace_id="base",
        project_id=ws.project_id,
        workspace_id=ws.id,
        task_id=task_id,
        branch=branch,
    ).to_dict()
    Path(ws.workspace_path, WORKTREE_SENTINEL_NAME).write_text(json.dumps(payload))


async def _task(db, task_id: str) -> None:
    await db.create_task(
        Task(
            id=task_id,
            project_id=PROJECT_ID,
            title=task_id,
            description="d",
            status=TaskStatus.READY,
        )
    )


async def run(db):
    return await worktree_checks.run_check(db, "worktrees.orphans")


async def test_claims_the_reserved_id():
    ids = {c.id for c in worktree_checks.CHECKS}
    assert ids == {"worktrees.orphans"}
    assert all(c.owner == "worktree-execution" for c in worktree_checks.CHECKS)
    assert RESERVED_CHECK_IDS["worktrees.orphans"] == "worktree-execution"


async def test_declares_no_fix():
    """The reserved-id contract allows only ``git worktree prune``, which
    clears neither pin — releasing a lock or moving a slot off a dead task's
    branch is an operator call."""
    assert all(c.fix is None for c in worktree_checks.CHECKS)


async def test_no_slots_is_ok(db):
    result = await run(db)
    assert result.severity is Severity.OK


async def test_live_task_is_not_an_orphan(db, slots_root):
    await _task(db, "live")
    ws = await _slot(db, slots_root, 0, locked="live")
    _sentinel(ws, "live", "aq/live")
    result = await run(db)
    assert result.severity is Severity.OK
    assert result.data.get("count", 0) == 0


async def test_sentinel_pinned_to_a_deleted_task_warns(db, slots_root):
    """An unlocked slot still holding ``aq/crisp-delta`` after crisp-delta
    was deleted — free, but the branch is not."""
    ws = await _slot(db, slots_root, 0)
    _sentinel(ws, "crisp-delta", "aq/crisp-delta")
    result = await run(db)
    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    found = result.data["slots"][0]
    assert found["task_id"] == "crisp-delta"
    assert found["branch"] == "aq/crisp-delta"
    assert found["locked_by_task_id"] is None
    assert "aq/crisp-delta" in result.detail


async def test_a_slot_running_a_live_task_can_still_pin_a_dead_branch(db, slots_root):
    """The pin outlives the assignment: reporting only idle slots would miss
    the case where the next task has already moved in."""
    await _task(db, "live")
    ws = await _slot(db, slots_root, 0, locked="live")
    _sentinel(ws, "crisp-delta", "aq/crisp-delta")
    result = await run(db)
    assert result.severity is Severity.WARN
    assert result.data["slots"][0]["locked_by_task_id"] == "live"


async def test_every_orphan_slot_is_reported(db, slots_root):
    for index, dead in enumerate(("crisp-delta", "old-one")):
        ws = await _slot(db, slots_root, index)
        _sentinel(ws, dead, f"aq/{dead}")
    result = await run(db)
    assert result.data["count"] == 2
    assert {o["task_id"] for o in result.data["slots"]} == {"crisp-delta", "old-one"}


async def test_missing_sentinel_is_not_a_finding(db, slots_root):
    """A slot that was never assigned pins nothing."""
    await _slot(db, slots_root, 0)
    result = await run(db)
    assert result.severity is Severity.OK


async def test_non_slot_workspaces_are_ignored(db, tmp_path):
    """A plain clone has no slot semantics — a stray sentinel there is not
    this check's business."""
    path = tmp_path / "clone"
    path.mkdir()
    ws = Workspace(
        id="clone",
        project_id=PROJECT_ID,
        workspace_path=str(path),
        source_type=RepoSourceType.CLONE,
        kind_id="project-repo",
    )
    await db.create_workspace(ws)
    Path(path, WORKTREE_SENTINEL_NAME).write_text(
        json.dumps({"slot": "slot-0", "slot_index": 0, "base_workspace_id": "base",
                    "project_id": PROJECT_ID, "task_id": "ghost"})
    )
    result = await run(db)
    assert result.severity is Severity.OK


async def test_no_database_is_info_not_ok(db):
    """"nothing was looked at" must not read like "nothing is wrong"."""
    result = await worktree_checks.run_check(None, "worktrees.orphans")
    assert result.severity is Severity.INFO


async def test_registered_at_startup_when_worktrees_are_enabled(tmp_path):
    """Doctor keeps the id reserved; worktree-execution claims it (main.py)."""
    from src.doctor import default_registry

    reg = default_registry()
    assert reg.get("worktrees.orphans") is None
    for check in worktree_checks.worktree_checks():
        reg.register(check)
    assert reg.get("worktrees.orphans") is not None
