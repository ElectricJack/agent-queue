"""Dedicated LISTEN connections preserve SQLAlchemy URL connection arguments."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.engine import make_url

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


async def test_inactive_listener_sleeps_even_with_overdue_retry(monkeypatch):
    import asyncio

    listener = WaitSourceListener(SimpleNamespace(orch=SimpleNamespace(db=None)), clock=lambda: 1)
    listener._schedule_retry(1, 0)
    listener.tick = AsyncMock(return_value=False)
    delays = []

    class StopLive(Exception):
        pass

    async def wait_for(awaitable, timeout):
        awaitable.close()
        delays.append(timeout)
        raise StopLive

    listener_asyncio = Mock(wraps=asyncio)
    listener_asyncio.wait_for = wait_for
    monkeypatch.setattr("src.waits.listener.asyncio", listener_asyncio)
    with pytest.raises(StopLive):
        await listener._live(SimpleNamespace(is_closed=lambda: False))
    assert delays == [listener.renewal_seconds]
