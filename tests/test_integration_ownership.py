"""Fenced ownership for repository-qualified integration branches."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import insert, update

from src.database import Database
from src.database.tables import integration_branch_owners
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.models import Project, RepoSourceType, Task, Workspace


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "ownership.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="p"))
    yield database
    await database.close()


async def test_collector_cannot_acquire_a_branch_while_parent_is_active(db):
    """Removing the busy check would let two writers mutate one parent ref."""
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="parent")

    parent = await ownership.acquire(target, "parent-session", "parent")

    with pytest.raises(BranchBusy):
        await ownership.acquire(target, "collector-session", "collector")
    await ownership.assert_current(parent)


async def test_expired_attached_owner_still_blocks_takeover(db):
    """Expiry is not evidence that the attached session stopped writing."""
    target = BranchKey(repository_id="repo", branch="parent")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id=target.repository_id,
                ref=target.branch,
                owner_id="parent-session",
                owner_role="parent",
                fence_token=7,
                handoff_state="attached",
                session_id="parent-session",
                workspace_id="slot-1",
                expires_at=time.time() - 60,
                created_at=1.0,
                updated_at=1.0,
            )
        )

    with pytest.raises(BranchBusy):
        await BranchOwnership(db).acquire(target, "collector-session", "collector")


async def test_stale_owner_cannot_write_after_confirmed_transfer(db):
    """Dropping the token increment would allow the old owner to write again."""
    handoffs: list[dict] = []

    async def confirmed(row: dict) -> bool:
        handoffs.append(row)
        return True

    ownership = BranchOwnership(db, confirm_handoff=confirmed)
    target = BranchKey(repository_id="repo", branch="parent")
    parent = await ownership.acquire(target, "parent-session", "parent")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .values(
                handoff_state="attached",
                session_id="parent-session",
                workspace_id="slot-1",
            )
        )

    collector = await ownership.transfer(parent, "collector-session", "collector")

    assert collector.token == parent.token + 1
    assert collector.owner_id == "collector-session"
    assert handoffs[0]["owner_id"] == "parent-session"
    assert handoffs[0]["session_id"] == "parent-session"
    assert handoffs[0]["workspace_id"] == "slot-1"
    with pytest.raises(StaleFence):
        await ownership.assert_current(parent)
    await ownership.assert_current(collector)


async def test_failed_handoff_keeps_the_old_fence_current(db):
    """Treating a failed stop confirmation as detached would create two writers."""
    async def not_confirmed(_row: dict) -> bool:
        return False

    ownership = BranchOwnership(db, confirm_handoff=not_confirmed)
    target = BranchKey(repository_id="repo", branch="parent")
    parent = await ownership.acquire(target, "parent-session", "parent")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .values(handoff_state="attached", session_id="parent-session", workspace_id="slot-1")
        )

    with pytest.raises(BranchBusy):
        await ownership.transfer(parent, "collector-session", "collector")
    with pytest.raises(BranchBusy):
        await ownership.assert_current(parent)


async def test_failed_handoff_can_be_retried_with_the_same_fence(db):
    """Rejecting ``handoff_pending`` forever would wedge a crashed confirmation."""
    confirmations = iter((False, True))

    async def eventually_confirmed(_row: dict) -> bool:
        return next(confirmations)

    ownership = BranchOwnership(db, confirm_handoff=eventually_confirmed)
    target = BranchKey(repository_id="repo", branch="parent")
    parent = await ownership.acquire(target, "parent-task", "worker")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .values(handoff_state="attached", session_id="session-1", workspace_id="slot-1")
        )

    with pytest.raises(BranchBusy):
        await ownership.transfer(parent, "collector-op", "collector")

    collector = await ownership.transfer(parent, "collector-op", "collector")

    assert collector == Fence(target=target, owner_id="collector-op", token=2)
    with pytest.raises(StaleFence):
        await ownership.assert_current(parent)


async def test_concurrent_handoff_retries_advance_the_fence_once(db):
    """Two successful confirmations must not grant two successor fences."""
    both_confirming = asyncio.Event()
    confirmations = 0

    async def confirmed(_row: dict) -> bool:
        nonlocal confirmations
        confirmations += 1
        if confirmations == 2:
            both_confirming.set()
        await both_confirming.wait()
        return True

    ownership = BranchOwnership(db, confirm_handoff=confirmed)
    target = BranchKey(repository_id="repo", branch="parent")
    parent = await ownership.acquire(target, "parent-task", "worker")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .values(handoff_state="attached", session_id="session-1", workspace_id="slot-1")
        )

    results = await asyncio.wait_for(
        asyncio.gather(
            ownership.transfer(parent, "collector-op", "collector"),
            ownership.transfer(parent, "collector-op", "collector"),
            return_exceptions=True,
        ),
        timeout=1.0,
    )

    assert confirmations == 2
    assert sum(isinstance(result, Fence) for result in results) == 1
    assert sum(isinstance(result, (BranchBusy, StaleFence)) for result in results) == 1
    winner = next(result for result in results if isinstance(result, Fence))
    assert winner == Fence(target=target, owner_id="collector-op", token=2)


async def test_concurrent_initial_acquisition_has_one_winner_and_one_named_busy(db):
    """A first-row race must not leak a repository uniqueness IntegrityError."""
    target = BranchKey(repository_id="repo", branch="parent")
    ownership = BranchOwnership(db)

    results = await asyncio.gather(
        ownership.acquire(target, "worker-a", "worker"),
        ownership.acquire(target, "worker-b", "worker"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, Fence) for result in results) == 1
    assert sum(isinstance(result, BranchBusy) for result in results) == 1


async def test_released_fence_cannot_write_and_same_owner_reacquires_with_new_token(db):
    """Returning token N from a released row would revive a detached writer."""
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="parent")
    old = await ownership.acquire(target, "parent-task", "worker")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.repository_id == target.repository_id)
            .where(integration_branch_owners.c.ref == target.branch)
            .values(handoff_state="released", confirmed_workspace_id="slot-1")
        )

    with pytest.raises(BranchBusy):
        await ownership.assert_current(old)

    reacquired = await ownership.acquire(target, "parent-task", "worker")

    assert reacquired == Fence(target=target, owner_id="parent-task", token=2)
    with pytest.raises(StaleFence):
        await ownership.assert_current(old)
    await ownership.assert_current(reacquired)


async def test_attach_binds_reserved_fence_to_locked_workspace(db, tmp_path):
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="aq/task")
    await db.create_task(Task(id="task", project_id="p", title="task", description=""))
    fence = await ownership.acquire(target, "task", "worker")
    await db.create_workspace(
        Workspace(
            id="slot",
            project_id="p",
            workspace_path=str(tmp_path / "slot"),
            source_type=RepoSourceType.WORKTREE,
            locked_by_task_id="task",
        )
    )

    await ownership.attach(fence, "session", "slot")

    owner = await ownership.get_owner(target)
    assert owner["handoff_state"] == "attached"
    assert owner["session_id"] == "session"
    assert owner["workspace_id"] == "slot"


async def test_transfer_that_wins_before_attach_prevents_writer_binding(db, tmp_path):
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="aq/task")
    await db.create_task(Task(id="task", project_id="p", title="task", description=""))
    fence = await ownership.acquire(target, "task", "worker")
    await db.create_workspace(
        Workspace(
            id="slot",
            project_id="p",
            workspace_path=str(tmp_path / "slot"),
            source_type=RepoSourceType.WORKTREE,
            locked_by_task_id="task",
        )
    )
    await ownership.transfer(fence, "collector", "collector")

    with pytest.raises(StaleFence):
        await ownership.attach(fence, "session", "slot")


async def test_transfer_waits_for_bounded_reserved_mutation_exclusion(db):
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="aq/task")
    fence = await ownership.acquire(target, "task", "worker")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare():
        async with ownership.mutation_exclusion(fence):
            entered.set()
            await release.wait()

    preparation = asyncio.create_task(prepare())
    await entered.wait()
    transfer = asyncio.create_task(ownership.transfer(fence, "collector", "collector"))
    await asyncio.sleep(0.05)
    assert not transfer.done()
    release.set()

    await preparation
    successor = await transfer
    assert successor.owner_id == "collector"
