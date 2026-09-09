"""The caller-owned write transaction shared by every query mixin.

Most query methods open their own short transaction.  A method that reads a
guard condition and then acts on it must not: the read and the write have to
be one unit.  Those methods take a ``conn`` argument and let the caller own
the boundary with ``db.immediate()``::

    async with db.immediate() as conn:
        if await db.open_children(task_id, conn=conn):
            raise Refused("hierarchy.open_children")
        await db.delete_task(task_id, cascade=True, conn=conn)

PostgreSQL is the only backend, so ``immediate()`` is exactly
``engine.begin()``: a read-committed transaction that takes row locks as
needed, which is all a read-then-write sequence requires.  The name is a
leftover from the SQLite era, where the same guarantee needed an explicit
``BEGIN IMMEDIATE`` to grab the write lock before the first read.

A method that takes ``conn`` never commits — the block does.  Post-commit
work (settled-container notifications, ready-frontier entries) is fired by
the caller *after* the block exits, never inside it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


class TransactionQueryMixin:
    """Provides :meth:`immediate` — the caller-owned write transaction."""

    @asynccontextmanager
    async def immediate(self) -> AsyncIterator[AsyncConnection]:
        """Yield a connection inside one committed-on-exit transaction.

        Delegates to ``engine.begin()``: the block commits on a clean exit
        and rolls back on any exception.
        """
        engine = self._engine
        if engine is None:  # pragma: no cover - defensive
            raise RuntimeError("database is not initialized")

        async with engine.begin() as conn:
            yield conn
