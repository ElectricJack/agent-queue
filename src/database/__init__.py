"""Persistence layer for the agent queue system.

This package provides a modular, backend-agnostic database access layer
organized around domain-specific query modules and adapter classes.

Architecture
------------
- **base.py** — ``DatabaseBackend`` protocol (trait) defining the full API
- **tables.py** — SQLAlchemy Core table definitions (MetaData + Table objects)
- **engine.py** — Async engine factory, PRAGMA setup, schema lifecycle
- **schema.py** — Legacy DDL constants and ALTER TABLE migrations
- **queries/** — Domain-specific query mixins (projects, tasks, agents, ...)
- **adapters/** — Backend implementations (SQLite, PostgreSQL placeholder)

Backward Compatibility
----------------------
The ``Database`` name is aliased to ``SQLiteDatabaseAdapter`` so that
existing imports (``from src.database import Database``) continue to work
unchanged::

    from src.database import Database
    db = Database("data/queue.db")
    await db.initialize()

Adding a New Backend
--------------------
1. Create a new adapter in ``adapters/`` (e.g. ``postgresql.py``)
2. Implement all methods from :class:`DatabaseBackend`
3. Register it here if you want a factory function

See ``adapters/postgresql.py`` for a skeleton example.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.database.adapters.sqlite import SQLiteDatabaseAdapter
from src.database.base import DatabaseBackend

if TYPE_CHECKING:
    from src.config import AppConfig

# Backward-compatible alias: existing code does `from src.database import Database`
Database = SQLiteDatabaseAdapter


def create_database(config: AppConfig) -> DatabaseBackend:
    """Create the appropriate database backend from application config.

    Returns a :class:`SQLiteDatabaseAdapter` (default) or raises for
    unsupported backends.  The returned object is not yet initialized —
    callers must ``await db.initialize()`` before use.
    """
    db_url = config.database.url or config.database_path
    if config.database.backend == "postgresql":
        raise NotImplementedError(
            "PostgreSQL backend is not yet implemented. Use a SQLite database path for now."
        )
    # Default: SQLite
    return SQLiteDatabaseAdapter(db_url)


__all__ = [
    "Database",
    "DatabaseBackend",
    "SQLiteDatabaseAdapter",
    "create_database",
]
