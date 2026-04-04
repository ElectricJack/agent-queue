#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL.

Reads all rows from the SQLite database and inserts them into a PostgreSQL
database that already has the schema applied.  Tables are migrated in
foreign-key dependency order.

Usage:
    # Dry run (default) — shows what would be migrated
    python scripts/migrate_sqlite_to_pg.py \\
        --source ~/.agent-queue/agent-queue.db \\
        --target postgresql://agent_queue:agent_queue_dev@localhost:5432/agent_queue

    # Actually execute the migration
    python scripts/migrate_sqlite_to_pg.py \\
        --source ~/.agent-queue/agent-queue.db \\
        --target postgresql://agent_queue:agent_queue_dev@localhost:5432/agent_queue \\
        --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Tables in foreign-key dependency order (parents before children).
# SERIAL tables (events, chat_analyzer_suggestions) are last so we can
# reset sequences after inserting.
TABLE_ORDER = [
    "projects",
    "agent_profiles",
    "repos",
    "agents",
    "tasks",
    "task_criteria",
    "task_dependencies",
    "task_context",
    "task_tools",
    "workspaces",
    "token_ledger",
    "rate_limits",
    "task_results",
    "system_config",
    "hooks",
    "hook_runs",
    "plugins",
    "plugin_data",
    "archived_tasks",
    "events",
    "chat_analyzer_suggestions",
]

# Tables with SERIAL primary keys that need sequence resets
SERIAL_TABLES = {
    "events": "events_id_seq",
    "chat_analyzer_suggestions": "chat_analyzer_suggestions_id_seq",
}


def read_sqlite(db_path: str) -> dict[str, list[tuple]]:
    """Read all rows from all tables in the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    data: dict[str, list[tuple]] = {}
    for table in TABLE_ORDER:
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608
            rows = cursor.fetchall()
            if rows:
                columns = [desc[0] for desc in cursor.description]
                data[table] = {
                    "columns": columns,
                    "rows": [tuple(row) for row in rows],
                }
            else:
                data[table] = {"columns": [], "rows": []}
        except sqlite3.OperationalError as e:
            logger.warning("Table '%s' not found in SQLite (skipping): %s", table, e)
            data[table] = {"columns": [], "rows": []}

    conn.close()
    return data


async def migrate(source: str, target: str, execute: bool) -> None:
    """Run the migration from SQLite to PostgreSQL."""
    try:
        import asyncpg
    except ImportError:
        logger.error("asyncpg is required: pip install asyncpg")
        sys.exit(1)

    logger.info("Reading SQLite database: %s", source)
    data = read_sqlite(source)

    total_rows = sum(len(t["rows"]) for t in data.values())
    logger.info("Found %d total rows across %d tables", total_rows, len(TABLE_ORDER))

    if not execute:
        logger.info("=== DRY RUN MODE (pass --execute to apply) ===")
        for table in TABLE_ORDER:
            info = data[table]
            count = len(info["rows"])
            if count:
                logger.info("  %-35s %d rows", table, count)
            else:
                logger.info("  %-35s (empty)", table)
        logger.info("Total: %d rows would be migrated", total_rows)
        return

    logger.info("Connecting to PostgreSQL: %s", _sanitize_dsn(target))
    conn = await asyncpg.connect(target)

    try:
        # Migrate each table in order
        for table in TABLE_ORDER:
            info = data[table]
            rows = info["rows"]
            columns = info["columns"]

            if not rows:
                logger.info("  %-35s (empty, skipping)", table)
                continue

            # Check if table already has data
            existing = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            if existing > 0:
                logger.warning(
                    "  %-35s already has %d rows — skipping (drop table data first to re-migrate)",
                    table,
                    existing,
                )
                continue

            # Build INSERT statement with $1, $2, ... placeholders
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            col_list = ", ".join(columns)
            insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

            # Use COPY for bulk inserts (much faster than individual INSERTs)
            try:
                await conn.copy_records_to_table(
                    table,
                    records=rows,
                    columns=columns,
                )
                logger.info("  %-35s %d rows migrated", table, len(rows))
            except Exception as e:
                # Fall back to individual inserts if COPY fails
                logger.warning("  COPY failed for %s (%s), falling back to INSERT", table, e)
                async with conn.transaction():
                    for row in rows:
                        await conn.execute(insert_sql, *row)
                logger.info("  %-35s %d rows migrated (via INSERT)", table, len(rows))

        # Reset SERIAL sequences
        for table, seq_name in SERIAL_TABLES.items():
            info = data[table]
            if info["rows"]:
                await conn.execute(
                    f"SELECT setval('{seq_name}', (SELECT COALESCE(MAX(id), 0) FROM {table}))"
                )
                logger.info("  Reset sequence %s", seq_name)

        # Verify row counts
        logger.info("\n=== Verification ===")
        mismatches = 0
        for table in TABLE_ORDER:
            expected = len(data[table]["rows"])
            actual = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            status = "OK" if actual >= expected else "MISMATCH"
            if status == "MISMATCH":
                mismatches += 1
            if expected > 0 or actual > 0:
                logger.info("  %-35s expected=%d actual=%d %s", table, expected, actual, status)

        if mismatches:
            logger.error("%d table(s) have row count mismatches!", mismatches)
        else:
            logger.info("All tables verified successfully.")

    finally:
        await conn.close()


def _sanitize_dsn(dsn: str) -> str:
    """Remove password from DSN for logging."""
    import re

    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate agent-queue data from SQLite to PostgreSQL"
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="PostgreSQL DSN (e.g. postgresql://user:pass@host:5432/dbname)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually execute the migration (default is dry-run)",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.source, args.target, args.execute))


if __name__ == "__main__":
    main()
