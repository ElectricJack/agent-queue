"""Pool wait and query duration reach the perf registry; statement text never does."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import exc, text
from sqlalchemy.pool import AsyncAdaptedQueuePool

from src.database import Database
from src.database.engine import ObservedQueuePool, create_postgres_engine
from src.metrics.perf import PerfRegistry, install_registry
from tests.db_fixtures import lease_dsn


@pytest.fixture
def registry():
    reg = PerfRegistry()
    install_registry(reg)
    yield reg
    install_registry(None)


def test_the_private_pool_hook_still_exists():
    assert callable(getattr(AsyncAdaptedQueuePool, "_do_get", None))
    assert ObservedQueuePool._do_get is not AsyncAdaptedQueuePool._do_get


async def test_query_duration_is_recorded_without_statement_text(registry):
    engine = create_postgres_engine(lease_dsn("perf-observer.db"), pool_min=1, pool_max=2)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(0.05), 'needle-in-sql'"))
    finally:
        await engine.dispose()
    snap = registry.snapshot()
    assert snap["db"]["query"]["count"] >= 1
    assert snap["db"]["query"]["max"] >= 40
    assert "needle" not in repr(snap)


async def test_pool_wait_is_recorded_when_every_connection_is_out(registry):
    engine = create_postgres_engine(lease_dsn("perf-wait.db"), pool_min=1, pool_max=1)
    try:
        holders = [await engine.connect() for _ in range(2)]  # pool_size 1 + overflow 1

        async def release_later():
            await asyncio.sleep(0.1)
            await holders[0].close()

        releaser = asyncio.create_task(release_later())
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await releaser
        await holders[1].close()
    finally:
        await engine.dispose()
    assert registry.snapshot()["db"]["pool_wait"]["max"] >= 80


async def test_a_pool_timeout_is_counted(registry):
    engine = create_postgres_engine(
        lease_dsn("perf-timeout.db"), pool_min=1, pool_max=1, pool_timeout=0.2
    )
    try:
        holders = [await engine.connect() for _ in range(2)]
        with pytest.raises(exc.TimeoutError):
            async with engine.connect():
                pass
        for holder in holders:
            await holder.close()
    finally:
        await engine.dispose()
    assert registry.snapshot()["db"]["counters"]["pool_timeouts"] == 1


async def test_observe_false_builds_the_plain_pool_and_records_nothing(registry):
    engine = create_postgres_engine(lease_dsn("perf-plain.db"), observe=False)
    try:
        assert not isinstance(engine.pool, ObservedQueuePool)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    snap = registry.snapshot()
    assert snap["db"]["query"]["count"] == 0
    assert snap["db"]["pool_wait"]["count"] == 0


async def test_a_disabled_registry_records_nothing(registry):
    registry.enabled = False
    engine = create_postgres_engine(lease_dsn("perf-disabled.db"), pool_min=1, pool_max=1)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    snap = registry.snapshot()
    assert snap["db"]["query"]["count"] == 0
    assert snap["db"]["pool_wait"]["count"] == 0


async def test_pool_gauges_are_read_without_a_round_trip():
    db = Database(lease_dsn("perf-gauges.db"))
    await db.initialize()
    try:
        gauges = db.metrics_pool_gauges()
        assert set(gauges) == {"checked_out", "overflow", "size"}
        assert gauges["checked_out"] == 0
        # SQLAlchemy's raw overflow counter starts at -pool_size; the gauge
        # reports connections beyond the pool, which is none at idle.
        assert gauges["overflow"] == 0
        assert gauges["size"] == 10
    finally:
        await db.close()


def test_pool_gauges_are_null_without_an_engine():
    db = Database("postgresql://unused@127.0.0.1:1/unused")
    assert db.metrics_pool_gauges() == {"checked_out": None, "overflow": None, "size": None}
