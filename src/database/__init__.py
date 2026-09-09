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
- **adapters/** — The PostgreSQL backend implementation

PostgreSQL is the only supported backend.  ``Database`` is an alias for
:class:`PostgreSQLDatabaseAdapter`, kept so existing imports keep working::

    from src.database import Database
    db = Database("postgresql://user:pass@host/agent_queue")
    await db.initialize()
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
from src.database.base import DatabaseBackend

from src.config import is_postgres_url

if TYPE_CHECKING:
    from src.config import AppConfig

logger = logging.getLogger(__name__)

# Backward-compatible alias: existing code does `from src.database import Database`
Database = PostgreSQLDatabaseAdapter

#: ``scheme://user:password@rest`` — the password is group 2.
_DSN_CREDENTIALS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)([^/\s@]*)(@)")


def redact_dsn(url: str) -> str:
    """*url* with any password in its authority replaced by ``***``.

    Log lines outlive the process and get pasted into issues; a DSN is the
    one config value that routinely carries a live credential.
    """
    return _DSN_CREDENTIALS_RE.sub(r"\1***\3", str(url or ""))


def create_database(config: AppConfig) -> DatabaseBackend:
    """Create the PostgreSQL backend from application config.

    Raises when ``database.url`` is not a PostgreSQL DSN.  This used to fall
    through to SQLite for anything unrecognised, which meant a typo'd DSN
    brought the daemon up healthy on an empty file while the real database
    sat untouched.  There is one backend now, so the mistake is a hard error.

    The returned object is not yet initialized — callers must
    ``await db.initialize()`` before use.
    """
    db_url = config.database.url
    if not is_postgres_url(db_url):
        raise ValueError(
            "database.url must be a PostgreSQL DSN "
            f"(got {redact_dsn(db_url) or 'empty'}). SQLite is no longer supported; "
            "run `aq db import-sqlite <path>` to carry an old database over."
        )
    logger.info("database url=%s", redact_dsn(db_url))
    return PostgreSQLDatabaseAdapter(
        db_url, config.database.pool_min_size, config.database.pool_max_size
    )


__all__ = [
    "Database",
    "DatabaseBackend",
    "PostgreSQLDatabaseAdapter",
    "create_database",
    "redact_dsn",
]
