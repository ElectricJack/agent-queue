"""Persistence layer for the agent queue system.

This package provides a modular, backend-agnostic database access layer
organized around domain-specific query modules and adapter classes.

Architecture
------------
- **base.py** — ``DatabaseBackend`` protocol (trait) defining the full API
- **schema.py** — DDL, migrations, and index definitions
- **connection.py** — Connection lifecycle and startup migration logic
- **queries/** — Domain-specific query mixins (projects, tasks, agents, …)
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

from src.database.adapters.sqlite import SQLiteDatabaseAdapter
from src.database.base import DatabaseBackend

# Backward-compatible alias: existing code does `from src.database import Database`
Database = SQLiteDatabaseAdapter

__all__ = [
    "Database",
    "DatabaseBackend",
    "SQLiteDatabaseAdapter",
]
