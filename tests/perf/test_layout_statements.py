"""Layout perf on PostgreSQL (spec §9). Skipped without POSTGRES_TEST_DSN.

Wall-clock budgets, so the module is marked ``perf`` and each test takes
``perf_strict`` (``tests/perf/conftest.py``) — see that fixture for why and
for the command that runs them.
"""

from __future__ import annotations

import time

import pytest

from scripts.seed_layout_perf import seed_project
from src.models import Task
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set"),
]


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=20, per_epic=40, big_epic=1000)
    yield any_db


async def _drain(db, drv) -> None:
    """Fold every outstanding dirty mark, however many batches that takes."""
    for _ in range(200):
        if not await db.dirty_layout_projects():
            return
        await drv.process_dirty("perf", min_age_seconds=0)
    raise AssertionError("dirty marks did not drain")


async def test_full_layout_under_budget(perf_strict, pg):
    drv = LayoutDriver(pg)
    t0 = time.perf_counter()
    await drv.full_layout("perf", "all")
    elapsed = time.perf_counter() - t0
    print(f"\nFULL_LAYOUT_SECONDS={elapsed:.4f}")
    assert elapsed < 60.0


async def test_incremental_batch_of_ten_under_550ms(perf_strict, pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    # seed_project's create_task/set_parent/add_dependency calls each write a
    # dirty mark (Task 12), but full_layout above doesn't consume marks — drain
    # those pre-existing marks, untimed, so the timed batch below only measures
    # the 10 new tasks we're about to add. ``pop_layout_dirty`` is capped at
    # 1,000 marks per batch (final review I4), so this loops rather than
    # assuming one call clears the whole seeding backlog.
    await _drain(pg, drv)
    for i in range(10):
        tid = f"epic5-pkg0-new{i}"
        await pg.create_task(Task(id=tid, project_id="perf", title=tid, description=""))
        async with pg._engine.begin() as conn:
            await pg.set_parent(tid, "epic5-pkg0", conn=conn)
    t0 = time.perf_counter()
    await drv.process_dirty("perf", min_age_seconds=0)
    elapsed = time.perf_counter() - t0
    print(f"\nINCREMENTAL_BATCH_SECONDS={elapsed:.4f}")
    # Why this batch is expensive: epic5-pkg0 goes from 10 to 20 cards, which
    # crosses a container growth band and legitimately forces epic5 to resize
    # and, in turn, a full re-lay of every root-level sibling (71 at the
    # committed epics=20 fixture: 20 epics + hub + 50 hub dependents; 151 at
    # the spec's 100-epic scale). That cascade is real work, not an artifact of
    # stale dirty-marking — see
    # ``tests/task_graph/test_layout_driver.py
    # ::test_reparenting_freshly_created_task_does_not_relay_root``.
    #
    # Two round-trip-amplification bugs were fixed for this batch earlier:
    # ``publish_layout`` now issues one INSERT ... ON CONFLICT executemany
    # instead of one round trip per upserted row, and ``_preload_db_rows`` +
    # ``_lay``'s cache seeding cut ``_db_row``'s per-id fetches to 18
    # statements/variant.
    #
    # Budget (final review I5): measured on PostgreSQL (local docker) at the
    # spec's §9 scale — 100 epics / ~5,000 tasks — this root-reflow batch took
    # 0.343 s / 0.428 s / 0.509 s over three runs, so the budget is 550 ms (the
    # slowest run rounded up to the next 50 ms), not the earlier 300 ms.
    # The committed fixture stays at epics=20 (0.16-0.21 s at that scale)
    # because a 100-epic seed makes this single test take ~6 minutes.
    assert elapsed < 0.55


async def test_root_band_crossing_publish_under_1s(perf_strict, pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    # Drain pre-existing dirty marks from seeding (see note above) so only
    # the forced root reflow below is timed.
    await _drain(pg, drv)
    # Grow epic0 (already the biggest) by a package of 60 tasks to force a root reflow.
    await pg.create_task(Task(id="epic0-pkgX", project_id="perf", title="x", description=""))
    async with pg._engine.begin() as conn:
        await pg.set_parent("epic0-pkgX", "epic0", conn=conn)
    for t in range(60):
        tid = f"epic0-pkgX-t{t}"
        await pg.create_task(Task(id=tid, project_id="perf", title=tid, description=""))
        async with pg._engine.begin() as conn:
            await pg.set_parent(tid, "epic0-pkgX", conn=conn)
    t0 = time.perf_counter()
    await drv.process_dirty("perf", min_age_seconds=0)
    elapsed = time.perf_counter() - t0
    print(f"\nROOT_BAND_CROSSING_SECONDS={elapsed:.4f}")
    assert elapsed < 1.0
