"""Migrate data from a SQLite database to PostgreSQL.

Used by the setup wizard when a user switches from SQLite to PostgreSQL
and wants to carry their existing data over.

Usage::

    await migrate_sqlite_to_postgres(
        "/home/user/.agent-queue/agent-queue.db",
        "postgresql://user:pass@localhost:5432/agent_queue",
    )
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select, text, insert, update, Integer
from sqlalchemy.ext.asyncio import AsyncEngine

from src.database.engine import create_postgres_engine, create_sqlite_engine
from src.database.tables import (
    agent_profiles,
    agents,
    archived_tasks,
    chat_analyzer_suggestions,
    events,
    hook_runs,
    hooks,
    plugin_data,
    plugins,
    projects,
    rate_limits,
    repos,
    system_config,
    task_context,
    task_criteria,
    task_dependencies,
    task_results,
    task_tools,
    tasks,
    token_ledger,
    workspaces,
)

logger = logging.getLogger(__name__)

# Tables in FK-safe insertion order.
# agents is inserted with current_task_id=NULL first, then updated after tasks.
_ORDERED_TABLES = [
    # No FK dependencies
    system_config,
    agent_profiles,
    plugins,
    rate_limits,
    # FK → agent_profiles
    projects,
    # FK → projects
    repos,
    # FK → repos (current_task_id deferred)
    agents,
    # FK → projects, repos, agents, agent_profiles, workspaces (but workspaces FK is nullable)
    tasks,
    # FK → projects, agents, tasks
    workspaces,
    # FK → tasks
    task_criteria,
    task_dependencies,
    task_context,
    task_tools,
    # FK → projects, agents, tasks
    token_ledger,
    task_results,
    events,
    # FK → projects
    hooks,
    # FK → hooks
    hook_runs,
    # No enforced FKs
    chat_analyzer_suggestions,
    archived_tasks,
    # FK → plugins
    plugin_data,
]

# Columns to NULL out on first insert for agents (circular FK with tasks)
_AGENT_DEFERRED_COLS = {"current_task_id"}


async def migrate_sqlite_to_postgres(
    sqlite_path: str,
    pg_dsn: str,
    *,
    progress_cb: Callable[[str, int], None] | None = None,
) -> dict[str, int]:
    """Copy all data from a SQLite database into PostgreSQL.

    Args:
        sqlite_path: Path to the SQLite database file.
        pg_dsn: PostgreSQL connection DSN.
        progress_cb: Optional callback ``(table_name, row_count)`` called
            after each table is migrated.

    Returns:
        Dict mapping table name to number of rows migrated.

    Raises:
        RuntimeError: If the PostgreSQL database already contains data.
    """
    sqlite_engine = create_sqlite_engine(sqlite_path)
    pg_engine = create_postgres_engine(pg_dsn)

    try:
        await _check_pg_empty(pg_engine)
        counts = await _copy_tables(sqlite_engine, pg_engine, progress_cb)
        await _fixup_agent_task_ids(sqlite_engine, pg_engine)
        await _reset_sequences(pg_engine)
        return counts
    finally:
        await sqlite_engine.dispose()
        await pg_engine.dispose()


async def _check_pg_empty(engine: AsyncEngine) -> None:
    """Raise if any user tables in PostgreSQL already contain data."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
            )
        )
        for row in result:
            count_result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {row[0]}")  # noqa: S608
            )
            if count_result.scalar() > 0:
                raise RuntimeError(
                    f"PostgreSQL table '{row[0]}' already contains data. "
                    "Aborting migration to avoid duplicates. "
                    "Drop the tables or use a fresh database."
                )


async def _copy_tables(
    src: AsyncEngine,
    dst: AsyncEngine,
    progress_cb: Callable[[str, int], None] | None,
) -> dict[str, int]:
    """Copy rows from each table in FK-safe order."""
    counts: dict[str, int] = {}

    for table in _ORDERED_TABLES:
        async with src.connect() as src_conn:
            result = await src_conn.execute(select(table))
            rows = result.mappings().fetchall()

        if not rows:
            counts[table.name] = 0
            if progress_cb:
                progress_cb(table.name, 0)
            continue

        # For agents, NULL out current_task_id on first pass
        if table is agents:
            rows = [
                {k: (None if k in _AGENT_DEFERRED_COLS else v) for k, v in row.items()}
                for row in rows
            ]

        async with dst.begin() as dst_conn:
            await dst_conn.execute(insert(table), [dict(r) for r in rows])

        counts[table.name] = len(rows)
        if progress_cb:
            progress_cb(table.name, len(rows))
        logger.info("Migrated %d rows from %s", len(rows), table.name)

    return counts


async def _fixup_agent_task_ids(src: AsyncEngine, dst: AsyncEngine) -> None:
    """Restore agents.current_task_id values that were NULLed during insert."""
    async with src.connect() as src_conn:
        result = await src_conn.execute(
            select(agents.c.id, agents.c.current_task_id).where(
                agents.c.current_task_id.is_not(None)
            )
        )
        rows = result.fetchall()

    if not rows:
        return

    async with dst.begin() as dst_conn:
        for agent_id, task_id in rows:
            await dst_conn.execute(
                update(agents).where(agents.c.id == agent_id).values(current_task_id=task_id)
            )
    logger.info("Restored current_task_id for %d agents", len(rows))


async def _reset_sequences(engine: AsyncEngine) -> None:
    """Reset PostgreSQL sequences for tables with auto-increment integer PKs."""
    async with engine.begin() as conn:
        for table in _ORDERED_TABLES:
            # Find columns that are autoincrement Integer PKs
            for col in table.columns:
                if col.primary_key and isinstance(col.type, Integer) and col.autoincrement:
                    seq_name = f"{table.name}_{col.name}_seq"
                    max_val = await conn.execute(
                        text(f"SELECT COALESCE(MAX({col.name}), 0) FROM {table.name}")
                    )
                    max_id = max_val.scalar()
                    if max_id and max_id > 0:
                        await conn.execute(text(f"SELECT setval('{seq_name}', {max_id})"))
                        logger.info("Reset sequence %s to %d", seq_name, max_id)
