"""Explicit write-transaction helper shared by both adapters.

SQLite's default deferred transaction takes the write lock at the *first*
write statement, so a read-then-write sequence can be interleaved by another
writer and lose the race with ``database is locked``.  Commands that read a
guard condition and then act on it (cascade delete, subtree abandon) need the
write lock from the very first statement — that is ``BEGIN IMMEDIATE``.

PostgreSQL has no such hazard (its default read-committed transaction already
takes row locks as needed), so there ``immediate()`` is exactly
``engine.begin()``.

The SQLite engine uses ``StaticPool`` (one underlying DBAPI connection for
the whole process, see ``create_sqlite_engine``), so two coroutines that
both reach ``BEGIN IMMEDIATE`` before either commits are issuing nested
``BEGIN`` on the *same* raw connection — sqlite3 raises "cannot start a
transaction within a transaction" rather than blocking.  A real SQLite
writer lock across separate connections would simply serialize; a shared
in-process connection needs an explicit ``asyncio.Lock`` to reproduce that
same one-writer-at-a-time behaviour for concurrent claim attempts
(swarm-work-model §10) instead of racing the driver.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransactionQueryMixin:
    """Provides :meth:`immediate` — a write-locked transaction context."""

    def _get_immediate_lock(self) -> asyncio.Lock:
        """Return the mixin's shared ``asyncio.Lock``, creating it lazily.

        Lazy construction avoids binding the lock to a particular event
        loop at ``__init__`` time (adapter instances outlive any given
        loop in tests).
        """
        lock = getattr(self, "_immediate_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._immediate_lock = lock
        return lock

    @asynccontextmanager
    async def immediate(self) -> AsyncIterator[AsyncConnection]:
        """Yield a connection inside a transaction that holds the write lock.

        On SQLite this issues ``BEGIN IMMEDIATE`` on an AUTOCOMMIT connection
        (so the driver does not open its own implicit transaction) and commits
        or rolls back explicitly.  On every other dialect it delegates to
        ``engine.begin()``.
        """
        engine = self._engine
        if engine is None:  # pragma: no cover - defensive
            raise RuntimeError("database is not initialized")

        if engine.dialect.name != "sqlite":
            async with engine.begin() as conn:
                yield conn
            return

        async with self._get_immediate_lock():
            conn = await engine.connect()
            try:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    await conn.exec_driver_sql("ROLLBACK")
                    raise
                else:
                    await conn.exec_driver_sql("COMMIT")
            finally:
                await conn.close()
