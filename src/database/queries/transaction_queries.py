"""Explicit write-transaction helper shared by both adapters.

SQLite's default deferred transaction takes the write lock at the *first*
write statement, so a read-then-write sequence can be interleaved by another
writer and lose the race with ``database is locked``.  Commands that read a
guard condition and then act on it (cascade delete, subtree abandon) need the
write lock from the very first statement — that is ``BEGIN IMMEDIATE``.

PostgreSQL has no such hazard (its default read-committed transaction already
takes row locks as needed), so there ``immediate()`` is exactly
``engine.begin()``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransactionQueryMixin:
    """Provides :meth:`immediate` — a write-locked transaction context."""

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
