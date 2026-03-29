"""Database adapter implementations.

Each adapter satisfies the :class:`~src.database.base.DatabaseBackend`
protocol for a specific database engine.
"""

from src.database.adapters.sqlite import SQLiteDatabaseAdapter

__all__ = ["SQLiteDatabaseAdapter"]
