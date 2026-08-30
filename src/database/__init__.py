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

import logging
import re
from typing import TYPE_CHECKING

from src.database.adapters.sqlite import SQLiteDatabaseAdapter
from src.database.base import DatabaseBackend

if TYPE_CHECKING:
    from src.config import AppConfig

logger = logging.getLogger(__name__)

# Backward-compatible alias: existing code does `from src.database import Database`
Database = SQLiteDatabaseAdapter

#: ``scheme://user:password@rest`` — the password is group 2.
_DSN_CREDENTIALS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)([^/\s@]*)(@)")


def redact_dsn(url: str) -> str:
    """*url* with any password in its authority replaced by ``***``.

    Log lines outlive the process and get pasted into issues; a DSN is the
    one config value that routinely carries a live credential.
    """
    return _DSN_CREDENTIALS_RE.sub(r"\1***\3", str(url or ""))


def create_database(config: AppConfig) -> DatabaseBackend:
    """Create the appropriate database backend from application config.

    Returns a :class:`SQLiteDatabaseAdapter` (default) or raises for
    unsupported backends.  The returned object is not yet initialized —
    callers must ``await db.initialize()`` before use.

    Logs which backend the URL resolved to.  Backend detection is a
    *prefix match on the DSN scheme* and anything unrecognized is treated
    as a SQLite file path — a correct default (a bare path is legal) that
    fails silently and expensively when the URL was meant to be
    PostgreSQL: the daemon comes up healthy on an empty SQLite file while
    the real database sits untouched.  There is nothing to raise on, so the
    mitigation is that the answer is always in the log, greppable, on every
    start.
    """
    db_url = config.database.url or config.database_path
    logger.info(
        "database backend=%s url=%s",
        config.database.backend,
        redact_dsn(db_url),
    )
    if config.database.backend == "postgresql":
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        return PostgreSQLDatabaseAdapter(
            db_url, config.database.pool_min_size, config.database.pool_max_size
        )
    # Default: SQLite
    return SQLiteDatabaseAdapter(db_url)


def __getattr__(name: str):
    if name == "PostgreSQLDatabaseAdapter":
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        return PostgreSQLDatabaseAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Database",
    "DatabaseBackend",
    "PostgreSQLDatabaseAdapter",
    "SQLiteDatabaseAdapter",
    "create_database",
    "redact_dsn",
]
