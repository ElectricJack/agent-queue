"""Idle/busy dispatch against real PostgreSQL and a real Git remote."""

from pathlib import Path

from sqlalchemy import func, select

from src.database.tables import integration_batches
from src.integration.batches import BatchStore
from tests.test_integration_gitops import LocalGit, git
from tests.test_integration_train_sources import MAIN, completed, lane, world as world


async def test_idle_departure_queues_during_ci_and_departs_on_settle(world):
    now = [1000.0]
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0])
    await completed(world, "first")
    first = await train.visit(MAIN)
    assert first.state == "testing"
    store = BatchStore(world.db)
    frozen = await store.members(first.batch_id)
    assert [m.task_id for m in frozen] == ["first"]

    for tid in ("second", "third"):
        await completed(world, tid)
        held = await train.visit(MAIN)
        assert held.batch_id == first.batch_id
    assert {m["task_id"] for m in held.detail["dispatch"]["queued_members"]} == {"second", "third"}
    assert await store.members(first.batch_id) == frozen
    # Advancing time cannot settle CI or alter frozen membership.
    now[0] += 100000
    assert (await train.visit(MAIN)).batch_id == first.batch_id
    checks.green.add(first.candidate_sha)
    successor = await train.visit(MAIN)
    assert successor.state == "testing" and successor.batch_id != first.batch_id
    assert successor.detail["settled_batch_id"] == first.batch_id
    assert {m.task_id for m in await store.members(successor.batch_id)} == {"second", "third"}
    assert successor.target_sha == first.candidate_sha
    assert git(world.origin.url, "rev-parse", "main") == first.candidate_sha
    checks.green.add(successor.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_batches)) == 2


async def test_root_frontier_has_no_size_cap_and_keeps_prerequisite_closure(world):
    await completed(world, "z-prerequisite")
    await completed(world, "a-dependent", needs=("z-prerequisite",))
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    train.batches.limit = 1
    selected = await train.visit(MAIN)
    assert selected.state == "testing"
    members = await BatchStore(world.db).members(selected.batch_id)
    assert [m.task_id for m in members] == ["z-prerequisite", "a-dependent"]


async def test_empty_lane_clock_cannot_create_a_batch(world):
    now = [1000.0]
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0])
    for moment in (1000, 1300, 2800, 100000):
        now[0] = moment
        assert (await train.visit(MAIN)).state == "idle"
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_batches)) == 0


async def test_departure_revalidates_queued_prerequisite_closure(world):
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)))
    await completed(world, "first")
    first = await train.visit(MAIN)
    await completed(world, "prerequisite")
    await completed(world, "dependent", needs=("prerequisite",))
    queued = await train.visit(MAIN)
    assert {m["task_id"] for m in queued.detail["dispatch"]["queued_members"]} == {
        "prerequisite", "dependent",
    }

    async def refused(target, member):
        if member.task_id == "prerequisite":
            return {"code": "awaiting_pr_checks", "task_id": member.task_id}

    green_gate = train.batches.pr_gate
    train.batches.pr_gate = refused
    checks.green.add(first.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    assert await train.batches.current(MAIN) is None
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_batches)) == 1
    # The dependent must remain out of a batch until its prerequisite is ready.
    train.batches.pr_gate = green_gate
    successor = await train.visit(MAIN)
    assert successor.state == "testing"
    assert [m.task_id for m in await BatchStore(world.db).members(successor.batch_id)] == [
        "prerequisite", "dependent",
    ]
