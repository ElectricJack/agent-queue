"""Alembic environment configuration for async SQLAlchemy migrations.

Supports both SQLite (with batch mode for ALTER TABLE limitations) and
PostgreSQL.  The database URL is resolved from:

1. A pre-existing connection passed via ``config.attributes["connection"]``
   (used when called programmatically from engine.py at startup)
2. ``AGENT_QUEUE_DB_URL`` env var  (full SQLAlchemy URL)
3. ``sqlalchemy.url`` in alembic.ini
4. Default: ``sqlite+aiosqlite:///~/.agent-queue/agent-queue.db``
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from src.database.tables import metadata

# Alembic Config object — provides access to alembic.ini values.
config = context.config

target_metadata = metadata


def _get_url() -> str:
    """Resolve the database URL."""
    url = os.environ.get("AGENT_QUEUE_DB_URL")
    if url:
        return url
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    default_path = Path.home() / ".agent-queue" / "agent-queue.db"
    return f"sqlite+aiosqlite:///{default_path}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout without a DB connection."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite compatibility
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    """Configure context and run migrations (called inside sync connection)."""
    is_sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=is_sqlite,  # batch mode for SQLite ALTER TABLE
        compare_type=True,  # detect column type changes
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    url = _get_url()

    # Use the same StaticPool for SQLite to match production behavior
    engine_kwargs = {}
    if "sqlite" in url:
        engine_kwargs["poolclass"] = pool.StaticPool
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    connectable = create_async_engine(url, **engine_kwargs)

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

    # Otherwise (CLI usage: `alembic upgrade head`), create our own engine
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
