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
