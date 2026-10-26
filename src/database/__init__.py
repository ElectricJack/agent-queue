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
- **factory.py** — Configured backend construction

Adapter, protocol and factory exports load on first access. Importing this
package or a schema/DSN helper does not load query mixins or application services.

PostgreSQL is the only supported backend.  ``Database`` is an alias for
:class:`PostgreSQLDatabaseAdapter`, kept so existing imports keep working::

    from src.database import Database
    db = Database("postgresql://user:pass@host/agent_queue")
    await db.initialize()
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.base import DatabaseBackend
    from src.database.factory import create_database

    Database = PostgreSQLDatabaseAdapter


#: ``scheme://user:password@rest`` — the password is group 2.
_DSN_CREDENTIALS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)([^/\s@]*)(@)")


def redact_dsn(url: str) -> str:
    """*url* with any password in its authority replaced by ``***``.

    Log lines outlive the process and get pasted into issues; a DSN is the
    one config value that routinely carries a live credential.
    """
    return _DSN_CREDENTIALS_RE.sub(r"\1***\3", str(url or ""))


def __getattr__(name: str):
    # Keep the public API while letting schema/DSN consumers avoid the adapter.
    if name in {"Database", "PostgreSQLDatabaseAdapter"}:
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        value = PostgreSQLDatabaseAdapter
    elif name == "DatabaseBackend":
        from src.database.base import DatabaseBackend

        value = DatabaseBackend
    elif name == "create_database":
        from src.database.factory import create_database

        value = create_database
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "Database",
    "DatabaseBackend",
    "PostgreSQLDatabaseAdapter",
    "create_database",
    "redact_dsn",
]
