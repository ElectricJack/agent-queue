"""Reusable initialized adapters, separate from lease bookkeeping and cloning."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.database import Database


class InitializedDatabases:
    """Initialize each session-owned lease database once, with fresh adapters.

    The lease pool resets rows and restores migration seeds between tests.
    Each test gets its own adapter and engine so callbacks, asyncio locks, and
    pool configuration (including ``unpooled_postgres``) cannot leak across
    tests. Only schema and startup data initialization are amortized.

    Only use this cache for data tests. Schema, startup migration, and restart
    tests must continue to initialize their own adapters.
    """

    def __init__(self):
        self._initialized: set[str] = set()

    async def get(self, dsn: str) -> Database:
        from src.database import Database
        from src.database.engine import create_postgres_engine

        database = Database(dsn)
        if dsn in self._initialized:
            database._engine = create_postgres_engine(dsn)
        else:
            try:
                await database.initialize()
            except BaseException:
                await database.close()
                raise
            self._initialized.add(dsn)
        return database
