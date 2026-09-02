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


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=100, per_epic=40, big_epic=1000, hub_dependents=50)
    drv = LayoutDriver(any_db)
    await drv.full_layout("perf", "all")
    await drv.full_layout("perf", "active")
    yield any_db


async def test_tiles_p95_under_100ms_with_big_collapsed_epic_visible(pg):
    app = FastAPI()
    app.include_router(build_graph_layout_router(db=pg))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        big = (await ac.get("/api/projects/perf/graph/node/epic0?variant=all")).json()["node"]
        rect = {"x0": big["x"] - 1, "y0": big["y"] - 1, "x1": big["x"] + 15, "y1": big["y"] + 15}
        payload = {"variant": "all", "rect": rect, "expanded": []}
        # Discarded warm-up: primes connection pool / query plan caches so
        # the timed loop measures steady-state latency, not cold-start.
        r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
        assert r.status_code == 200
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            r = await ac.post("/api/projects/perf/graph/tiles", json=payload)
            times.append(time.perf_counter() - t0)
            assert r.status_code == 200
        # p95 estimator: statistics.quantiles(times, n=20)[18] over 50 samples.
        p95 = statistics.quantiles(times, n=20)[18]
        assert p95 < 0.1, f"p95 {p95:.3f}s"
