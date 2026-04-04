"""PostgreSQL database adapter (placeholder).

This module provides a skeleton for a future PostgreSQL adapter that
implements the :class:`~src.database.base.DatabaseBackend` protocol
using SQLAlchemy Core with the asyncpg driver.

With the SQLAlchemy Core migration, all query mixins are now
dialect-portable — a PostgreSQL adapter can reuse them directly.
The only differences from the SQLite adapter are:

1. Engine creation: ``create_async_engine("postgresql+asyncpg://...")``
   with connection pooling instead of ``StaticPool``
2. No SQLite PRAGMAs (WAL mode, foreign_keys)
3. PostgreSQL-specific features (LISTEN/NOTIFY, advisory locks, etc.)

Example::

    from sqlalchemy.ext.asyncio import create_async_engine

    class PostgreSQLDatabaseAdapter(
        ProjectQueryMixin,
        TaskQueryMixin,
        # ... all other mixins ...
    ):
        def __init__(self, dsn: str, pool_min: int = 2, pool_max: int = 10):
            self._dsn = dsn
            self._engine = None

        async def initialize(self) -> None:
            self._engine = create_async_engine(
                self._dsn,
                pool_size=pool_max,
                min_size=pool_min,
            )
            await run_schema_setup(self._engine)

        async def close(self) -> None:
            if self._engine:
                await self._engine.dispose()

        # All query methods are inherited from mixins — no overrides needed.
        # Only assign_task_to_agent() needs to be copied from sqlite.py.
"""

# Future implementation will go here.
# See src/database/base.py for the full protocol specification.
# See src/database/adapters/sqlite.py for a working reference implementation.
