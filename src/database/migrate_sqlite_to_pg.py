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

from sqlalchemy import Integer, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from src.database.engine import create_postgres_engine, create_sqlite_engine
from src.database.tables import (
    agent_profiles,
    agents,
    api_session_tokens,
    archived_tasks,
    chat_analyzer_suggestions,
    events,
    gates,
    merge_slots,
    messages,
    playbook_runs,
    plugin_data,
    plugins,
    project_constraints,
    projects,
    rate_limits,
    repos,
    sessions,
    system_config,
    task_context,
    task_criteria,
    task_dependencies,
    task_gates,
    task_labels,
    task_metadata,
    task_proposals,
    task_results,
    task_tools,
    task_workspace_requirements,
    tasks,
    token_ledger,
    workflows,
    workspace_kinds,
    workspaces,
)

logger = logging.getLogger(__name__)

# Tables in FK-safe insertion order.
#
# This list must cover every table in ``tables.metadata`` — a missing table is
# silently dropped data for anyone migrating a SQLite install to PostgreSQL.
# ``tests/test_migrate_sqlite_to_pg.py`` asserts the two sets match so the list
# cannot drift when a new table is added to ``tables.py``.
#
# Circular and self-referential FKs are handled by inserting the offending
# columns as NULL (see ``_DEFERRED_COLS``) and restoring them afterwards.
_ORDERED_TABLES = [
    # No FK dependencies
    system_config,
    agent_profiles,
    plugins,
    rate_limits,
    events,
    workspace_kinds,
    playbook_runs,
    api_session_tokens,
    chat_analyzer_suggestions,
    archived_tasks,
    # FK → agent_profiles
    projects,
    # FK → projects
    repos,
    gates,
    merge_slots,
    project_constraints,
    # FK → projects, playbook_runs
    workflows,
    # FK → projects (reply_to_id is a self-FK — deferred)
    messages,
    # FK → repos (current_task_id deferred)
    agents,
    # FK → projects, repos, agents, agent_profiles, workflows
    # (preferred_workspace_id and parent_task_id deferred)
    tasks,
    # FK → projects, agents, tasks
    workspaces,
    # FK → tasks
    task_criteria,
    task_dependencies,
    task_context,
    task_metadata,
    task_tools,
    task_labels,
    task_workspace_requirements,
    # FK → gates, tasks
    task_gates,
    # FK → projects
    task_proposals,
    # FK → projects, tasks
    sessions,
    # FK → projects, agents, tasks
    token_ledger,
    task_results,
    # hooks and hook_runs tables removed (playbooks spec §13 Phase 3)
    # FK → plugins
    plugin_data,
]

# Columns NULLed out on first insert because they point at a table that is
# inserted later (or at the same table), then restored by
# ``_fixup_deferred_columns``.  Keyed by table name.
_DEFERRED_COLS: dict[str, frozenset[str]] = {
    # agents ⇄ tasks circular FK
    "agents": frozenset({"current_task_id"}),
    # tasks → workspaces (inserted later) and tasks → tasks (self-FK)
    "tasks": frozenset({"preferred_workspace_id", "parent_task_id"}),
    # messages → messages (self-FK)
    "messages": frozenset({"reply_to_id"}),
}


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
        await _fixup_deferred_columns(sqlite_engine, pg_engine)
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
    """Copy rows from each table in FK-safe order.

    SQLite databases may contain orphaned FK references (e.g. events pointing
    to archived/deleted tasks) because SQLite does not enforce FKs by default.
    We disable FK trigger checks during the bulk copy via PostgreSQL's
    ``session_replication_role = replica`` to handle this gracefully.
    """
    counts: dict[str, int] = {}

    async with dst.begin() as dst_conn:
        # Disable FK trigger checks for the duration of the bulk copy
        await dst_conn.execute(text("SET session_replication_role = replica"))

        for table in _ORDERED_TABLES:
            async with src.connect() as src_conn:
                result = await src_conn.execute(select(table))
                rows = result.mappings().fetchall()

            if not rows:
                counts[table.name] = 0
                if progress_cb:
                    progress_cb(table.name, 0)
                continue

            # NULL out columns whose FK target is not inserted yet
            deferred = _DEFERRED_COLS.get(table.name)
            if deferred:
                rows = [{k: (None if k in deferred else v) for k, v in row.items()} for row in rows]

            await dst_conn.execute(insert(table), [dict(r) for r in rows])

            counts[table.name] = len(rows)
            if progress_cb:
                progress_cb(table.name, len(rows))
            logger.info("Migrated %d rows from %s", len(rows), table.name)

        # Re-enable FK trigger checks
        await dst_conn.execute(text("SET session_replication_role = DEFAULT"))

    return counts


async def _fixup_deferred_columns(src: AsyncEngine, dst: AsyncEngine) -> None:
    """Restore the ``_DEFERRED_COLS`` values that were NULLed during insert."""
    for table in _ORDERED_TABLES:
        names = _DEFERRED_COLS.get(table.name)
        if not names:
            continue

        pk_cols = list(table.primary_key.columns)
        deferred_cols = [table.c[name] for name in sorted(names)]

        async with src.connect() as src_conn:
            result = await src_conn.execute(
                select(*pk_cols, *deferred_cols).where(
                    or_(*[col.is_not(None) for col in deferred_cols])
                )
            )
            rows = result.fetchall()

        if not rows:
            continue

        async with dst.begin() as dst_conn:
            for row in rows:
                pk_values = row[: len(pk_cols)]
                stmt = update(table)
                for col, value in zip(pk_cols, pk_values):
                    stmt = stmt.where(col == value)
                stmt = stmt.values(dict(zip([c.name for c in deferred_cols], row[len(pk_cols) :])))
                await dst_conn.execute(stmt)

        logger.info(
            "Restored deferred columns %s for %d rows in %s",
            sorted(names),
            len(rows),
            table.name,
        )


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
