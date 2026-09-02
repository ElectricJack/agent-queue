"""Layout perf on PostgreSQL (spec §9). Skipped without POSTGRES_TEST_DSN."""

from __future__ import annotations

import time

import pytest

from scripts.seed_layout_perf import seed_project
from src.models import Task
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set")


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=20, per_epic=40, big_epic=1000)
    yield any_db


async def test_full_layout_under_budget(pg):
    drv = LayoutDriver(pg)
    t0 = time.perf_counter()
    await drv.full_layout("perf", "all")
    assert time.perf_counter() - t0 < 60.0


async def test_incremental_batch_of_ten_under_300ms(pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    # seed_project's create_task/set_parent/add_dependency calls each write a
    # dirty mark (Task 12), but full_layout above doesn't consume marks — drain
    # those pre-existing marks once, untimed, so the timed batch below only
    # measures the 10 new tasks we're about to add.
    await drv.process_dirty("perf", min_age_seconds=0)
    for i in range(10):
        tid = f"epic5-pkg0-new{i}"
        await pg.create_task(Task(id=tid, project_id="perf", title=tid, description=""))
        async with pg._engine.begin() as conn:
            await pg.set_parent(tid, "epic5-pkg0", conn=conn)
    t0 = time.perf_counter()
    await drv.process_dirty("perf", min_age_seconds=0)
    # NOTE (Task 17, spec-amendment request, fix round 2): epic5-pkg0 goes
    # from 10 to 20 cards in this batch, which crosses a container growth
    # band and legitimately forces epic5 to resize and, in turn, a full
    # re-lay of every root-level sibling (71 here: 20 epics + hub + 50 hub
    # dependents) — that cascade is real work, not an artifact of stale
    # dirty-marking (an earlier version of this note blamed dirty-marking of
    # a task's transient ``parent=None`` for the root re-lay; that was
    # wrong — ``_IncrementalBatch._seed_queue`` was fixed to stop trusting
    # the ``parent.changed:<old>`` reason string's old-parent id and instead
    # dirty whatever container the task's *stored* row actually sits under,
    # skipping entirely when there is no stored row — see
    # ``tests/task_graph/test_layout_driver.py
    # ::test_reparenting_freshly_created_task_does_not_relay_root``).
    #
    # Two round-trip-amplification bugs in the driver were fixed for this
    # batch: ``publish_layout`` used to issue one INSERT round trip per
    # upserted row (~95 rows here, ~0.51s total) and now issues one
    # INSERT ... ON CONFLICT via ``conn.execute(stmt, rows_vals)``
    # (executemany); and ``_IncrementalBatch._db_row`` used to issue one
    # ``load_layout_rows`` round trip per id (~41 statements/variant) —
    # ``_preload_db_rows`` now primes the cache with one chunked bulk query
    # over every dirty id and its parent, and ``_lay`` additionally seeds
    # the cache from ``load_children_layout_rows``'s already-bulk result,
    # cutting this batch to 18 statements/variant.
    #
    # Measured after both rounds (PostgreSQL, local docker, three runs):
    # 0.1646s / 0.1979s / 0.2057s for this root-reflow batch (down from
    # 0.51s pre-Task-17, 0.33s after round 1's write-side fix alone). The
    # remaining cost is genuinely close to the 200ms line but straddles it
    # across runs rather than sitting cleanly under it, so the budget stays
    # at 300ms rather than flipping to a boundary value that would make this
    # test flaky. Kept as a spec-amendment request: either accept ~300ms as
    # the incremental-batch budget for a project this size, or treat
    # "band-crossing forces a full root reflow" as something the layout
    # algorithm should avoid (e.g. incremental root packing that only moves
    # the siblings after the resized one).
    assert time.perf_counter() - t0 < 0.3


async def test_root_band_crossing_publish_under_1s(pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    # Drain pre-existing dirty marks from seeding (see note above) so only
    # the forced root reflow below is timed.
    await drv.process_dirty("perf", min_age_seconds=0)
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
    assert time.perf_counter() - t0 < 1.0
