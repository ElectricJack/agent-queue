"""tiles latency on PostgreSQL (spec §9)."""

from __future__ import annotations

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
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set")

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
    """
    r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
    assert r.status_code == 200, r.text
    times = []
    for _ in range(SAMPLES):
        t0 = time.perf_counter()
        r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
        times.append(time.perf_counter() - t0)
        assert r.status_code == 200
    # p95 estimator: statistics.quantiles(times, n=20)[18] over 50 samples.
    p95 = statistics.quantiles(times, n=20)[18]
    print(
        f"\n[perf] {label}: p95 {p95 * 1000:.1f}ms "
        f"median {statistics.median(times) * 1000:.1f}ms "
        f"max {max(times) * 1000:.1f}ms over {SAMPLES} samples"
    )
    return p95


async def test_tiles_p95_under_100ms_with_big_collapsed_epic_visible(pg):
    app = FastAPI()
    app.include_router(build_graph_layout_router(db=pg))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        big = (await ac.get("/api/projects/perf/graph/node/epic0?variant=all")).json()["node"]
        rect = {"x0": big["x"] - 1, "y0": big["y"] - 1, "x1": big["x"] + 15, "y1": big["y"] + 15}
        payload = {"variant": "all", "rect": rect, "expanded": []}
        p95 = await _p95(ac, payload, "rect/collapsed-big-epic")
    assert p95 < BUDGET, f"p95 {p95:.3f}s"


async def test_tiles_focus_root_p95_under_100ms(pg):
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
