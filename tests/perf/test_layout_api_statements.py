"""tiles latency on PostgreSQL (spec §9).

Every budget here is wall-clock, so every test takes ``perf_strict``
(``tests/perf/conftest.py``) and the module is marked ``perf``: without
both, a latency assertion runs in CI's ``Tests (default)`` job under
``-n auto --dist loadfile`` and fails on a saturated box rather than on a
regression.  Run them deliberately, serially, on a quiet machine, with
``POSTGRES_TEST_DSN`` and ``AQ_PERF_STRICT=1`` in the environment::

    aq test -m perf -p no:xdist -s tests/perf/test_layout_api_statements.py
"""

from __future__ import annotations

import gc
import statistics
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scripts.seed_layout_perf import seed_project
from src.api.graph_layout import build_graph_layout_router
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set"),
]

SAMPLES = 50
BUDGET = 0.1


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=100, per_epic=40, big_epic=1000, hub_dependents=50)
    drv = LayoutDriver(any_db)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    yield any_db


async def _p95(ac, payload, label: str) -> float:
    """Time ``SAMPLES`` tiles requests after a discarded warm-up.

    The warm-up primes connection pool and query-plan caches so the timed
    loop measures steady-state latency, not cold start.  The p95 is printed
    as well as asserted, so a passing run still records the margin (``-s``).

    ``gc.freeze()`` around the loop is what makes the number a measurement
    of the endpoint rather than of the fixture.  This process is holding the
    seeded 5,100-task project alive, and a gen-2 collection has to walk all
    of it: measured here, ~2 to 5 of 50 samples caught one and each cost an
    extra 50-70 ms, which is the entire difference between a 43 ms median
    and a 95 ms "p95".  Freezing moves everything allocated up to this point
    into the permanent generation, so gen-2 stops rescanning the fixture.
    GC stays *enabled* — per-request garbage is still collected, so the
    request keeps paying for its own allocations, which is the cost a real
    server would pay.  A server holds a connection pool, not a test's object
    graph.

    Measured by these two tests on PostgreSQL 18, one 24-core box at load
    ~2, run serially -- p95 / max in milliseconds, against a 100 ms budget:

    ================================  =============  ============
    case                              unfrozen       frozen
    ================================  =============  ============
    rect/collapsed-big-epic           123.4 / 130.8  53.1 / 56.3
    focus/root=epic0 expanded=[]       80.5 / 157.7  59.1 / 61.5
    focus/root=epic0 expanded=[pkg0]  105.7 / 150.0  57.9 / 64.4
    ================================  =============  ============

    Frozen, every case keeps ~40% headroom and max lands within 25% of the
    median.  Unfrozen, two of the three miss and the assertion is really
    "did three gen-2 collections happen to land in this loop" -- note that
    the unfrozen medians (49.8 / 73.2 / 72.7) sit as far under the budget
    as the frozen ones do.  The endpoint was never the problem.
    """
    r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
    assert r.status_code == 200, r.text
    times = []
    gc.collect()
    gc.freeze()
    try:
        for _ in range(SAMPLES):
            t0 = time.perf_counter()
            r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
            times.append(time.perf_counter() - t0)
            assert r.status_code == 200
    finally:
        gc.unfreeze()
    # p95 estimator: statistics.quantiles(times, n=20)[18].  At SAMPLES=50
    # the exclusive method interpolates between the 2nd- and 3rd-slowest
    # sample ((50 + 1) x 0.95 = 48.45), so this is only as stable as the
    # tail is -- one extra outlier moves it by tens of milliseconds.  That
    # is affordable because the frozen loop above has no outliers; do not
    # reintroduce one without also raising SAMPLES.
    p95 = statistics.quantiles(times, n=20)[18]
    print(
        f"\n[perf] {label}: p95 {p95 * 1000:.1f}ms "
        f"median {statistics.median(times) * 1000:.1f}ms "
        f"max {max(times) * 1000:.1f}ms over {SAMPLES} samples"
    )
    return p95


async def test_tiles_p95_under_100ms_with_big_collapsed_epic_visible(perf_strict, pg):
    app = FastAPI()
    app.include_router(build_graph_layout_router(db=pg))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        big = (await ac.get("/api/projects/perf/graph/node/epic0?variant=all")).json()["node"]
        rect = {"x0": big["x"] - 1, "y0": big["y"] - 1, "x1": big["x"] + 15, "y1": big["y"] + 15}
        payload = {"variant": "all", "rect": rect, "expanded": []}
        p95 = await _p95(ac, payload, "rect/collapsed-big-epic")
    assert p95 < BUDGET, f"p95 {p95:.3f}s"


async def test_tiles_focus_root_p95_under_100ms(perf_strict, pg):
    """Focus on the 1,000-task epic: cost must track the open containers.

    ``root`` disables the rect cap and ``max_depth``, so before the
    container-scoped candidate load this request pulled epic0's entire
    subtree on every poll.
    """
    app = FastAPI()
    app.include_router(build_graph_layout_router(db=pg))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        big = (await ac.get("/api/projects/perf/graph/node/epic0?variant=all")).json()["node"]
        # Under `root` the rect neither caps nor culls; it is still required
        # by the request model, so send the focused node's own box.
        rect = {"x0": big["x"], "y0": big["y"], "x1": big["x"] + 1, "y1": big["y"] + 1}
        for expanded in ([], ["epic0-pkg0"]):
            payload = {"variant": "all", "rect": rect, "root": "epic0", "expanded": expanded}
            warm = await ac.post("/api/projects/perf/graph/tiles", json=payload)
            assert warm.status_code == 200, warm.text
            ids = {n["id"] for n in warm.json()["nodes"]}
            assert "epic0" in ids
            if expanded:
                # the open package's children are there; its siblings' are not
                assert "epic0-pkg0-t0" in ids
                assert not any(i.startswith("epic0-pkg1-") for i in ids)
            p95 = await _p95(ac, payload, f"focus/root=epic0 expanded={expanded}")
            assert p95 < BUDGET, f"expanded={expanded} p95 {p95:.3f}s"
