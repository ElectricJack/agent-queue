"""Dedicated LISTEN connection arguments and service-loop cleanup safety."""

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.engine import make_url

import src.git.manager as git_manager
from src.waits.listener import WaitSourceListener


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql+asyncpg://user:pw@/db?host=/tmp/sock",
            {"user": "user", "password": "pw", "database": "db", "host": "/tmp/sock"},
        ),
        (
            "postgresql+asyncpg://user:pw@/db?host=/tmp/sock&port=5433",
            {
                "user": "user", "password": "pw", "database": "db",
                "host": "/tmp/sock", "port": "5433",
            },
        ),
        (
            "postgresql+asyncpg://user:pw@localhost:5432/db",
            {
                "user": "user", "password": "pw", "database": "db",
                "host": "localhost", "port": 5432,
            },
        ),
        (
            "postgresql+asyncpg://user:pw@localhost:5432/db?sslmode=require",
            {
                "user": "user", "password": "pw", "database": "db",
                "host": "localhost", "port": 5432, "sslmode": "require",
            },
        ),
        (
            "postgresql+asyncpg://user:pw@localhost:5432/db?host=/tmp/sock&port=5433",
            {
                "user": "user", "password": "pw", "database": "db",
                "host": "/tmp/sock", "port": "5433",
            },
        ),
    ],
    ids=["unix-socket", "unix-socket-port", "tcp", "query-passthrough", "query-overrides"],
)
async def test_listener_connect_arguments(monkeypatch, dsn, expected):
    connection = object()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("asyncpg.connect", connect)
    db = SimpleNamespace(_engine=SimpleNamespace(url=make_url(dsn)))
    listener = WaitSourceListener(SimpleNamespace(orch=SimpleNamespace(db=db)))

    assert await listener._connect() is connection
    connect.assert_awaited_once_with(**expected)


async def test_cancelled_isolated_fetch_cleanup_does_not_restart_listener(
    tmp_path, monkeypatch, caplog,
):
    entered = asyncio.Event()
    manager = git_manager.GitManager()
    leaked = set()
    original_rmtree = git_manager.tempfile.TemporaryDirectory._rmtree

    def failed_rmtree(cls, name, **kwargs):
        if Path(name).name.startswith("aq-app-fetch-"):
            leaked.add(name)
            raise OSError(errno.ENOTEMPTY, "injected refs removal race")
        original_rmtree(name, **kwargs)

    async def blocked_import(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def drain(_watermark):
        await manager._afetch_exact_oid_with_app_auth_to_url(
            str(tmp_path), destination_url=tmp_path.as_uri(), token=None,
            oid="a" * 40, destination_ref="refs/aq/test/listener-cleanup",
        )

    async def timers():
        await asyncio.Event().wait()

    connection = SimpleNamespace(
        add_listener=AsyncMock(), add_termination_listener=lambda callback: None,
        is_closed=lambda: False, close=AsyncMock(),
    )
    connect = AsyncMock(return_value=connection)
    service = SimpleNamespace(
        orch=SimpleNamespace(db=SimpleNamespace(wait_source_watermark=AsyncMock(return_value=0))),
        processing_enabled=AsyncMock(return_value=True), lease=AsyncMock(return_value=True),
        timers=SimpleNamespace(run=timers, rearm=AsyncMock()), drain=drain,
    )
    listener = WaitSourceListener(service, connect=connect)
    running = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(manager, "_run_isolated_import_git", blocked_import)
            patch.setattr(
                git_manager.tempfile.TemporaryDirectory, "_rmtree", classmethod(failed_rmtree),
            )
            running = asyncio.create_task(listener.run())
            await asyncio.wait_for(entered.wait(), 10)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 10)
            assert leaked
            assert "temporary directory leaked" in caplog.text
            assert "wait.listener_restarted" not in caplog.text
            connect.assert_awaited_once()
            connection.close.assert_awaited_once()
    finally:
        if running is not None:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        for path in leaked:
            original_rmtree(path)


async def test_source_budget_adapts_to_timeouts_without_exceeding_lease(monkeypatch):
    from unittest.mock import Mock

    service = SimpleNamespace(orch=SimpleNamespace(db=None), lease=AsyncMock(return_value=True))
    listener = WaitSourceListener(service)
    monkeypatch.setattr(listener, "_retire_unused_retries", AsyncMock(return_value=0))
    monkeypatch.setattr(listener, "_defer", AsyncMock(return_value=True))
    original_timeout = asyncio.timeout
    active = []

    def timeout(seconds):
        budget = original_timeout(None)
        active.append((seconds, budget))
        return budget

    controlled = Mock(wraps=asyncio)
    controlled.timeout.side_effect = timeout
    monkeypatch.setattr("src.waits.listener.asyncio", controlled)
    observed = []

    async def drain_one(watermark, retry_pending, attempt, recovery, **kwargs):
        observed.append(active[-1][0])
        attempt.update(retry=False)
        if len(observed) <= 3:
            active[-1][1].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        return None

    monkeypatch.setattr(listener, "_drain_one", drain_one)
    assert await listener.drain(1) == 0
    assert observed[0] == listener.event_budget_seconds
    assert observed[1] > observed[0]
    assert max(observed) <= listener.lease_seconds / 2
    assert listener._defer.await_count == 3


async def test_adaptive_budget_keeps_graph_work_short(monkeypatch):
    from unittest.mock import Mock

    listener = WaitSourceListener(SimpleNamespace(orch=SimpleNamespace(db=None)))
    listener._budget_multiplier = 3
    original_timeout = asyncio.timeout
    budget = original_timeout(None)
    controlled = Mock(wraps=asyncio)
    controlled.timeout.return_value = budget
    monkeypatch.setattr("src.waits.listener.asyncio", controlled)
    with pytest.raises(TimeoutError):
        async with listener._graph_work():
            budget.reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
    controlled.timeout.assert_called_once_with(listener.event_budget_seconds)
    assert listener._work_budget() > listener.event_budget_seconds


async def test_manual_recovery_wait_does_not_consume_dispatch_budget(monkeypatch):
    from unittest.mock import Mock

    listener = WaitSourceListener(SimpleNamespace(orch=SimpleNamespace(db=None)))
    listener.db = SimpleNamespace(_engine=SimpleNamespace())
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connection():
        yield None

    listener.db._engine.connect = connection
    monkeypatch.setattr(listener, "_redispatch_event", AsyncMock(return_value={"dispatched_at": None}))
    monkeypatch.setattr(listener, "_redispatch_locked", AsyncMock(return_value={"success": True}))
    original_timeout = asyncio.timeout
    budgets = []
    queued = asyncio.Event()

    def timeout(seconds):
        budgets.append(seconds)
        queued.set()
        return original_timeout(None)

    controlled = Mock(wraps=asyncio)
    controlled.timeout.side_effect = timeout
    monkeypatch.setattr("src.waits.listener.asyncio", controlled)
    await listener._tick_lock.acquire()
    request = asyncio.create_task(listener.redispatch({"project_id": "p", "source_seq": 1},
                                                      principal=None, scope=None, handler=None))
    # Let the request reach the queued lock without spending a dispatch budget.
    await queued.wait()
    listener._redispatch_locked.assert_not_awaited()
    assert budgets == [listener.lease_seconds]
    listener._tick_lock.release()
    assert await request == {"success": True}
    assert not listener._tick_lock.locked()
