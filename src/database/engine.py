"""Async engine creation and schema lifecycle management.

Provides factory functions for creating SQLAlchemy async engines with
appropriate configuration (WAL mode, FK enforcement for SQLite) and
running Alembic migrations on startup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# Resolve alembic.ini relative to the project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"


def create_postgres_engine(dsn: str, pool_min: int = 2, pool_max: int = 10) -> AsyncEngine:
    """Create an async PostgreSQL engine with connection pooling.

    Normalizes ``postgresql://`` or ``postgres://`` schemes to the
    ``postgresql+asyncpg://`` dialect required by SQLAlchemy async.
    """
    import re

    url = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", dsn)
    return create_async_engine(
        url,
        pool_size=pool_max,
        max_overflow=pool_max,
        pool_pre_ping=True,
        pool_timeout=30,
    )


def create_sqlite_engine(path: str) -> AsyncEngine:
    """Create an async SQLite engine with WAL mode and FK enforcement.

    Uses ``StaticPool`` to keep a single connection open, matching the
    previous aiosqlite single-connection behavior.
    """
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(
        url,
        poolclass=StaticPool,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _run_alembic_upgrade(sync_connection) -> None:
    """Run Alembic migrations up to head using a sync connection.

    Called via ``conn.run_sync()`` from an async context.
    """
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.attributes["connection"] = sync_connection
    command.upgrade(alembic_cfg, "head")


def _stamp_alembic_baseline(sync_connection) -> None:
    """Stamp an existing database at the baseline migration.

    Used for pre-Alembic databases that already have the core schema
    but no ``alembic_version`` table.  By stamping at the baseline
    (instead of head), any post-baseline migrations (e.g. new tables
    like ``task_metadata``) are applied on the subsequent upgrade call.
    """
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.attributes["connection"] = sync_connection
    command.stamp(alembic_cfg, "311e98c39ffa")


async def run_schema_setup(engine: AsyncEngine) -> None:
    """Create/migrate the database schema using Alembic.

    For new databases, this runs all migrations from scratch.
    For existing pre-Alembic databases (have tables but no
    ``alembic_version``), it stamps them at the baseline revision
    and then runs any newer migrations to bring the schema up to date.
    """
    async with engine.begin() as conn:
        # Check if this is a pre-Alembic database (has tables but no alembic_version)
        def _check_and_migrate(sync_conn):
            insp = inspect(sync_conn)
            existing_tables = set(insp.get_table_names())
            has_alembic = "alembic_version" in existing_tables
            has_data_tables = bool(existing_tables - {"alembic_version"})

            if has_data_tables and not has_alembic:
                # Existing DB from before Alembic — stamp at baseline,
                # then upgrade so post-baseline migrations are applied.
                logger.info("Pre-Alembic database detected, stamping at baseline")
                _stamp_alembic_baseline(sync_conn)
                _run_alembic_upgrade(sync_conn)
            else:
                # New DB or already-Alembic DB — run migrations normally
                _run_alembic_upgrade(sync_conn)

        await conn.run_sync(_check_and_migrate)


async def run_startup_data_migrations(engine: AsyncEngine) -> None:
    """Run data migrations that normalize existing rows on startup.

    These are idempotent and safe to run on every startup.
    """
    async with engine.begin() as conn:
        await _migrate_repos_to_projects(conn)
        await _normalize_workspace_paths(conn)
        await _drop_legacy_agent_workspaces(conn)
        await _drop_legacy_workspace_locks(conn)


async def _migrate_repos_to_projects(conn) -> None:
    """Copy first repo's url/default_branch into project columns (idempotent)."""
    try:
        result = await conn.execute(
            text(
                "SELECT p.id, r.url, r.default_branch "
                "FROM projects p "
                "JOIN repos r ON r.project_id = p.id "
                "WHERE (p.repo_url IS NULL OR p.repo_url = '') "
                "GROUP BY p.id"
            )
        )
        rows = result.mappings().fetchall()
        for row in rows:
            await conn.execute(
                text(
                    "UPDATE projects SET repo_url = :url, repo_default_branch = :branch "
                    "WHERE id = :id AND (repo_url IS NULL OR repo_url = '')"
                ),
                {"url": row["url"], "branch": row["default_branch"], "id": row["id"]},
            )
            logger.info(
                "Migration: project '%s' repo_url='%s', default_branch='%s'",
                row["id"],
                row["url"],
                row["default_branch"],
            )
    except Exception as e:
        logger.debug("Repos-to-projects migration (benign): %s", e)


async def _drop_legacy_agent_workspaces(conn) -> None:
    """Drop the legacy agent_workspaces table if it still exists."""
    try:
        await conn.execute(text("DROP TABLE IF EXISTS agent_workspaces"))
    except Exception as e:
        logger.debug("Drop agent_workspaces (benign): %s", e)


async def _drop_legacy_workspace_locks(conn) -> None:
    """Drop the legacy workspace_locks table if it still exists.

    This table has FK constraints to tasks.id that can block task deletion.
    The codebase uses workspaces.locked_by_task_id instead.
    """
    try:
        await conn.execute(text("DROP TABLE IF EXISTS workspace_locks"))
    except Exception as e:
        logger.debug("Drop workspace_locks (benign): %s", e)


async def _normalize_workspace_paths(conn) -> None:
    """Normalize workspace paths and remove cross-project duplicates.

    1. Resolve any relative workspace_path entries to absolute paths.
    2. Remove link workspaces whose path duplicates a workspace belonging
       to a different project.

    Idempotent — safe to run on every startup.
    """
    try:
        result = await conn.execute(
            text("SELECT id, project_id, workspace_path, source_type FROM workspaces")
        )
        rows = result.mappings().fetchall()

        # Phase 1: normalize relative paths to absolute
        updated = 0
        for row in rows:
            raw = row["workspace_path"]
            resolved = os.path.realpath(raw)
            if resolved != raw:
                await conn.execute(
                    text("UPDATE workspaces SET workspace_path = :path WHERE id = :id"),
                    {"path": resolved, "id": row["id"]},
                )
                logger.info(
                    "Normalized workspace %s path: %r -> %r",
                    row["id"],
                    raw,
                    resolved,
                )
                updated += 1
        if updated:
            logger.info("Normalized %d workspace paths to absolute", updated)

        # Phase 2: remove link workspaces that duplicate another project's path.
        path_owners: dict[str, str] = {}
        for row in rows:
            ws_path = os.path.realpath(row["workspace_path"])
            if row["source_type"] == "clone" and ws_path not in path_owners:
                path_owners[ws_path] = row["project_id"]

        removed = 0
        for row in rows:
            if row["source_type"] != "link":
                continue
            ws_path = os.path.realpath(row["workspace_path"])
            owner = path_owners.get(ws_path)
            if owner and owner != row["project_id"]:
                await conn.execute(
                    text("DELETE FROM workspaces WHERE id = :id"),
                    {"id": row["id"]},
                )
                logger.warning(
                    "Removed bogus workspace %s: path %s belongs to project "
                    "'%s' but was linked to project '%s'",
                    row["id"],
                    ws_path,
                    owner,
                    row["project_id"],
                )
                removed += 1
        if removed:
            logger.info("Removed %d cross-project duplicate workspaces", removed)
    except Exception as e:
        logger.debug("Workspace path normalization (benign): %s", e)
