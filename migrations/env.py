"""Alembic environment configuration for async SQLAlchemy migrations.

PostgreSQL is the only supported backend (see
``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md``).  The
database URL is resolved from:

1. A pre-existing connection passed via ``config.attributes["connection"]``
   (used when called programmatically from engine.py at startup)
2. ``AGENT_QUEUE_DB_URL`` env var  (full SQLAlchemy DSN)
3. ``sqlalchemy.url`` in alembic.ini

There is deliberately no default.  This module used to fall back to a local
SQLite file under ``~/.agent-queue/``, so a bare ``alembic upgrade head`` with
nothing configured quietly created and migrated that file instead of failing —
the same fail-silent shape ``create_database`` was hardened against.  An
unresolved URL is now an error.
"""

from __future__ import annotations

import asyncio
import os
import re

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import is_postgres_url
from src.database.tables import metadata

# Alembic Config object — provides access to alembic.ini values.
config = context.config

target_metadata = metadata


def _get_url() -> str:
    """Resolve the database URL, or raise if none is configured.

    The bare ``postgres://`` / ``postgresql://`` spellings are normalised to
    ``postgresql+asyncpg://`` the same way :func:`create_postgres_engine` does:
    this environment is async, and the bare form otherwise resolves to the sync
    psycopg2 dialect and dies with ``No module named 'psycopg2'`` — which is
    exactly the DSN an operator copies out of ``config.yaml``.

    Raises:
        RuntimeError: when no URL resolves, or when the one that does is not a
            PostgreSQL DSN.
    """
    url = os.environ.get("AGENT_QUEUE_DB_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "no database URL configured for alembic: set AGENT_QUEUE_DB_URL to a "
            "PostgreSQL DSN (postgresql+asyncpg://user:pass@host/db) or fill in "
            "sqlalchemy.url in alembic.ini. There is no default — migrating an "
            "unintended database is worse than not running."
        )
    if not is_postgres_url(url):
        raise RuntimeError(
            f"alembic needs a PostgreSQL DSN, got {url!r}. PostgreSQL is the only "
            "supported backend; run `aq db import-sqlite <path>` to carry an old "
            "SQLite database over."
        )
    return re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout without a DB connection."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # No ``transaction_per_migration`` here: offline mode only emits SQL
        # text and never executes a revision body against a live bind, so
        # revision b2c3d4e5f6a7's preflight (which needs a second, committed
        # connection — see ``_do_run_migrations``) cannot run offline at all.
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    """Configure context and run migrations (called inside sync connection)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # detect column type changes
        # One transaction PER revision, not one around the whole chain.
        # Required by revision b2c3d4e5f6a7 (hierarchy canonicalise): its
        # preflight opens a SECOND connection to inspect and repair the
        # tables created by revision a1b2c3d4e5f6.  Under a single
        # chain-wide transaction that second connection cannot see
        # revision A's uncommitted DDL on a fresh Postgres database, and
        # blocks on A's ACCESS EXCLUSIVE locks on an existing one.
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    connectable = create_async_engine(_get_url())

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to DB and apply."""
    # If called programmatically from engine.py, a connection is pre-supplied
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _do_run_migrations(connectable)
        return

    # Otherwise (CLI usage: `alembic upgrade head`), create our own engine.
    # Print the resolved URL up front so ``Can't locate revision …``
    # failures are obviously attributable to a specific database.
    resolved = _get_url()
    print(f"[alembic] target DB: {resolved}")
    try:
        asyncio.run(run_async_migrations())
    except Exception:
        print(
            f"[alembic] migration FAILED against: {resolved}\n"
            "  If you see 'Can't locate revision identified by X', the\n"
            "  alembic_version row on this DB names a revision this\n"
            "  branch's migrations/versions/ does not contain. Confirm\n"
            "  AGENT_QUEUE_DB_URL points at the DB you meant, and check\n"
            "  the alembic_version row before running any repair."
        )
        raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
