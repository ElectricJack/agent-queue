"""Database connection management and initialization.

This module handles connection lifecycle, schema creation, migrations,
and startup data normalization for the SQLite backend.
"""

from __future__ import annotations

import logging
import os

import aiosqlite

from src.database.schema import INDEXES, MIGRATIONS, SCHEMA

logger = logging.getLogger(__name__)


async def create_sqlite_connection(path: str) -> aiosqlite.Connection:
    """Open a SQLite connection with WAL mode, FK enforcement, and row factory.

    Applies schema DDL, migrations, and indexes.  Returns the ready-to-use
    connection.
    """
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")

    # Idempotent column migrations
    for migration in MIGRATIONS:
        try:
            await db.execute(migration)
        except Exception:
            pass  # Column already exists

    # Create indexes
    for index_sql in INDEXES:
        await db.execute(index_sql)

    return db


async def run_startup_migrations(db: aiosqlite.Connection) -> None:
    """Run data migrations that normalize existing rows on startup.

    These are idempotent and safe to run on every startup.
    """
    await _migrate_repos_to_projects(db)
    await _normalize_workspace_paths(db)
    await _drop_legacy_agent_workspaces(db)
    await db.commit()


async def _migrate_repos_to_projects(db: aiosqlite.Connection) -> None:
    """Copy first repo's url/default_branch into project columns (idempotent)."""
    try:
        cursor = await db.execute(
            "SELECT p.id, r.url, r.default_branch "
            "FROM projects p "
            "JOIN repos r ON r.project_id = p.id "
            "WHERE (p.repo_url IS NULL OR p.repo_url = '') "
            "GROUP BY p.id"
        )
        rows = await cursor.fetchall()
        for row in rows:
            await db.execute(
                "UPDATE projects SET repo_url = ?, repo_default_branch = ? "
                "WHERE id = ? AND (repo_url IS NULL OR repo_url = '')",
                (row["url"], row["default_branch"], row["id"]),
            )
            logger.info(
                "Migration: project '%s' repo_url='%s', default_branch='%s'",
                row["id"],
                row["url"],
                row["default_branch"],
            )
    except Exception as e:
        logger.debug("Repos-to-projects migration (benign): %s", e)


async def _drop_legacy_agent_workspaces(db: aiosqlite.Connection) -> None:
    """Drop the legacy agent_workspaces table if it still exists."""
    try:
        await db.execute("DROP TABLE IF EXISTS agent_workspaces")
    except Exception as e:
        logger.debug("Drop agent_workspaces (benign): %s", e)


async def _normalize_workspace_paths(db: aiosqlite.Connection) -> None:
    """Normalize workspace paths and remove cross-project duplicates.

    1. Resolve any relative workspace_path entries to absolute paths.
    2. Remove link workspaces whose path duplicates a workspace belonging
       to a different project.

    Idempotent — safe to run on every startup.
    """
    try:
        cursor = await db.execute(
            "SELECT id, project_id, workspace_path, source_type FROM workspaces"
        )
        rows = await cursor.fetchall()

        # Phase 1: normalize relative paths to absolute
        updated = 0
        for row in rows:
            raw = row["workspace_path"]
            resolved = os.path.realpath(raw)
            if resolved != raw:
                await db.execute(
                    "UPDATE workspaces SET workspace_path = ? WHERE id = ?",
                    (resolved, row["id"]),
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
                await db.execute("DELETE FROM workspaces WHERE id = ?", (row["id"],))
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

        if updated or removed:
            await db.commit()
    except Exception as e:
        logger.debug("Workspace path normalization (benign): %s", e)
