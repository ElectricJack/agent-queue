"""Async engine creation and schema lifecycle management.

Provides factory functions for creating SQLAlchemy async engines with
appropriate configuration (WAL mode, FK enforcement for SQLite) and
running Alembic migrations on startup.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
from functools import lru_cache
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from src.database.migration_guard import VERIFY, migration_decision

logger = logging.getLogger(__name__)

# Resolve alembic.ini relative to the project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_SCHEMA_CACHE_DIRNAME = "aq-schema-cache"


def _sqlite_database_path(engine: AsyncEngine) -> Path | None:
    """Return the file path for a SQLite engine, excluding in-memory URLs."""
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:" or "mode=memory" in database:
        return None
    return Path(database).resolve()


def _schema_cache_is_enabled(database_path: Path) -> bool:
    """Return whether a SQLite database may use the disposable schema cache."""
    configured = os.environ.get("AQ_SCHEMA_CACHE")
    if configured == "0":
        return False
    if configured == "1":
        return True
    try:
        database_path.relative_to(Path(tempfile.gettempdir()).resolve())
    except ValueError:
        return False
    return True


def _alembic_head_revisions() -> tuple[str, ...]:
    """Read Alembic's current heads for cache validation and cache keys."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return tuple(sorted(ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_heads()))


def _schema_cache_directory() -> Path:
    """The per-user template directory under the temp root, created ``0700``.

    The temp root is shared, so a fixed world-readable path would let any
    local user plant a template that passes ``quick_check`` and carries the
    right ``alembic_version`` rows.  The directory is suffixed with the uid,
    created private, and refused (``OSError``, which the caller turns into
    the plain Alembic path) when it is a symlink or owned by someone else.
    """
    name = _SCHEMA_CACHE_DIRNAME
    if hasattr(os, "getuid"):
        name = f"{name}-{os.getuid()}"
    directory = Path(tempfile.gettempdir()).resolve() / name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"schema cache path {directory} is not a plain directory")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            raise OSError(f"schema cache directory {directory} is owned by uid {info.st_uid}")
        if info.st_mode & 0o077:
            os.chmod(directory, 0o700)
    return directory


def _schema_cache_inputs() -> list[Path]:
    """Every file whose content decides what a fully migrated schema looks like.

    ``migrations/env.py`` configures how revisions run (batch mode, the
    per-migration transaction) and revision ``b2c3d4e5f6a7`` imports
    ``src.database.hierarchy_migration``, so a change to either has to
    invalidate the template exactly as a changed revision file does.
    """
    database = _PROJECT_ROOT / "src" / "database"
    migrations = _PROJECT_ROOT / "migrations"
    return [
        database / "tables.py",
        database / "hierarchy_migration.py",
        migrations / "env.py",
        *sorted((migrations / "versions").glob("*.py")),
    ]


@lru_cache(maxsize=1)
def _schema_cache_key() -> tuple[str, tuple[str, ...]]:
    """Hash schema inputs so a changed migration never reuses an old template."""
    digest = hashlib.sha256()
    for source in _schema_cache_inputs():
        digest.update(str(source.relative_to(_PROJECT_ROOT)).encode())
        digest.update(b"\0")
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    heads = _alembic_head_revisions()
    return f"{'-'.join(heads)}-{digest.hexdigest()}", heads


def _cached_template_is_valid(template: Path, expected_heads: tuple[str, ...]) -> bool:
    """Reject missing, corrupt, or incorrectly stamped cache templates."""
    try:
        if template.stat().st_size == 0:
            return False
        with contextlib.closing(sqlite3.connect(str(template))) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except (OSError, sqlite3.Error):
        return False
    return {row[0] for row in rows} == set(expected_heads)


def _remove_sqlite_sidecars(database: Path) -> None:
    """Drop the ``-wal``/``-shm``/``-journal`` files that belong to *database*."""
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a checkpointed SQLite template without its transient WAL files.

    Stale sidecars next to *destination* go first: SQLite would otherwise
    replay a leftover ``-wal`` from whatever database used that path before
    into the freshly copied template.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    _remove_sqlite_sidecars(destination)
    shutil.copyfile(source, destination)


async def _build_schema_template(template: Path) -> bool:
    """Build a fully migrated SQLite template without consulting the cache.

    The build happens in a ``<key>.<pid>.building.db`` scratch file that is
    checkpointed, closed, and renamed over *template*.  The checkpoint
    connection is closed *before* the rename (a ``sqlite3`` context manager
    only commits), and the scratch file's ``-wal``/``-shm`` sidecars are
    removed whichever way the build ends -- they were piling up in the temp
    root.
    """
    temporary = template.with_suffix(f".{os.getpid()}.building.db")
    temporary.unlink(missing_ok=True)
    _remove_sqlite_sidecars(temporary)
    try:
        template_engine = create_sqlite_engine(str(temporary))
        try:
            await _run_schema_setup_without_cache(template_engine)
        finally:
            await template_engine.dispose()
        with contextlib.closing(sqlite3.connect(str(temporary))) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.replace(temporary, template)
    except Exception:
        logger.warning("Could not build SQLite schema cache template", exc_info=True)
        return False
    finally:
        temporary.unlink(missing_ok=True)
        _remove_sqlite_sidecars(temporary)
    return True


async def _restore_schema_from_cache(database_path: Path) -> bool:
    """Copy a valid migrated template into a new SQLite database when safe."""
    if database_path.exists() and database_path.stat().st_size > 0:
        return False

    try:
        cache_directory = _schema_cache_directory()
        key, heads = _schema_cache_key()
        template = cache_directory / f"{key}.db"
        lock_path = cache_directory / f"{key}.lock"
        with lock_path.open("a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if not _cached_template_is_valid(template, heads):
                    template.unlink(missing_ok=True)
                    _remove_sqlite_sidecars(template)
                    if not await _build_schema_template(template):
                        return False
                _copy_sqlite_database(template, database_path)
                return True
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, sqlite3.Error):
        logger.warning("Could not restore SQLite schema cache", exc_info=True)
        database_path.unlink(missing_ok=True)
        return False


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

    Pooling depends on whether the database is a real file or in-memory:

    * **File databases use ``NullPool``** — every transaction checks out its
      own ``sqlite3`` connection.  This matters for correctness, not just
      throughput: with ``StaticPool`` the whole process shares *one* DBAPI
      connection, so a plain ``engine.begin()`` writer running concurrently
      with an in-flight ``BEGIN IMMEDIATE`` claim transaction (see
      :mod:`src.database.queries.transaction_queries`) issues its ``COMMIT``
      on the *same* raw connection.  That commits the claim's transaction
      mid-way; the claim's own ``COMMIT`` then fails with "cannot commit -
      no transaction is active" and can leave a half-recorded holder behind.
      Separate connections make SQLite's own writer lock arbitrate instead,
      with ``PRAGMA busy_timeout`` bounding the wait.
    * **``:memory:`` databases keep ``StaticPool``** — a private in-memory
      database vanishes when its connection closes, so a shared connection
      is the only way the schema survives between checkouts.
    """
    url = f"sqlite+aiosqlite:///{path}"
    is_memory = ":memory:" in path or path == "" or "mode=memory" in path
    engine = create_async_engine(
        url,
        poolclass=StaticPool if is_memory else NullPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def _preflight_check_alembic_version(sync_connection) -> None:
    """Fail loudly if ``alembic_version`` names a revision the code lacks.

    The symptom this catches: ``aq restart`` bombs deep inside Alembic
    with ``Can't locate revision identified by 'X'`` — a phantom
    revision that was never in this branch's ``migrations/versions/``
    directory. Two common causes:

    * The DB was previously stamped/migrated by a different branch
      whose migration was later dropped or renamed.
    * The operator's real DB is at one URL but a startup path pointed
      alembic at a different one whose ``alembic_version`` row is stale.

    Rather than let Alembic's opaque KeyError propagate, we look up
    the current revision the DB claims, compare it against the
    codebase's ScriptDirectory, and raise a clear diagnostic message
    that names the resolved URL and a concrete fix (either restore
    the missing revision file or ``alembic stamp head`` after
    reconciling the schema).

    NOTE: never auto-repair — clobbering the row loses history and
    can silently skip data migrations. A clearer error is the fix.
    """
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.attributes["connection"] = sync_connection
    script = ScriptDirectory.from_config(alembic_cfg)
    ctx = MigrationContext.configure(sync_connection)
    db_revs = ctx.get_current_heads()  # ()  if unmarked, or (rev,) / (rev, rev)
    known = {s.revision for s in script.walk_revisions()}
    unknown = [r for r in db_revs if r and r not in known]
    if unknown:
        engine_url = str(sync_connection.engine.url)
        raise RuntimeError(
            "Alembic preflight failed: this database's alembic_version "
            f"references unknown revision(s) {unknown!r}. "
            f"Resolved DB URL: {engine_url}. "
            "Fix options: (a) restore the migration file(s) for those "
            "revision ids, or (b) if the schema is correct but the row "
            "is stale, reconcile by running `alembic stamp head` against "
            "this same URL (destructive to history — confirm the schema "
            "matches head first)."
        )


def _run_alembic_upgrade(sync_connection) -> None:
    """Run Alembic migrations up to head using a sync connection.

    Called via ``conn.run_sync()`` from an async context. Preflights
    the ``alembic_version`` row so an unknown revision surfaces as a
    clear diagnostic instead of Alembic's raw KeyError.
    """
    from alembic import command
    from alembic.config import Config

    _preflight_check_alembic_version(sync_connection)
    # Close the implicit transaction those reads opened.  Alembic's
    # ``begin_transaction()`` is a no-op while the connection is already
    # in a transaction, which would defeat ``transaction_per_migration``
    # (see ``run_schema_setup``).
    sync_connection.commit()
    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.attributes["connection"] = sync_connection
    command.upgrade(alembic_cfg, "head")


def _verify_schema_at_head(sync_connection) -> None:
    """Read-only counterpart to :func:`_run_alembic_upgrade`.

    Used when the caller may *not* migrate the database it is pointed at
    (see :mod:`src.database.migration_guard`).  Three outcomes:

    * stamped at the code's head — return, the process may proceed;
    * stamped at a revision this checkout does not have — the same unknown
      revision diagnostic the upgrade path raises;
    * anything else (behind head, or never stamped) — refuse and name the
      operator action, because migrating is not ours to do.
    """
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from src.database.migration_guard import SchemaBehindCode, refusal_message

    _preflight_check_alembic_version(sync_connection)
    alembic_cfg = Config(str(_ALEMBIC_INI))
    alembic_cfg.attributes["connection"] = sync_connection
    script = ScriptDirectory.from_config(alembic_cfg)
    ctx = MigrationContext.configure(sync_connection)
    current = set(ctx.get_current_heads())
    heads = set(script.get_heads())
    if current == heads:
        return
    raise SchemaBehindCode(
        refusal_message(
            str(sync_connection.engine.url),
            detail=(
                "Schema behind code: the database is stamped "
                f"{sorted(current) or 'nothing'} but this checkout's head is "
                f"{sorted(heads)}. Ask the operator to upgrade (`aq db upgrade`) or "
                "restart the daemon."
            ),
        )
    )


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

    Fresh temporary SQLite databases use a copied, fully migrated template
    when possible. Other databases always follow the normal Alembic path.

    For new databases without a cache template, this runs all migrations from
    scratch.
    For existing pre-Alembic databases (have tables but no
    ``alembic_version``), it stamps them at the baseline revision
    and then runs any newer migrations to bring the schema up to date.

    Uses ``engine.connect()`` rather than ``engine.begin()`` so that
    Alembic owns transaction boundaries: ``migrations/env.py`` configures
    ``transaction_per_migration=True`` because revision ``b2c3d4e5f6a7``
    opens a *second* connection to inspect the DDL revision
    ``a1b2c3d4e5f6`` just applied.  An outer ``engine.begin()`` would
    swallow those per-revision commits (Alembic's ``begin_transaction``
    is a no-op inside an already-open transaction), leaving that second
    connection unable to see the earlier revision's work.
    """
    if migration_decision(str(engine.url)) == VERIFY:
        await verify_schema_current(engine)
        return

    database_path = _sqlite_database_path(engine)
    if database_path and _schema_cache_is_enabled(database_path):
        if await _restore_schema_from_cache(database_path):
            return

    await _run_schema_setup_without_cache(engine)


async def verify_schema_current(engine: AsyncEngine) -> None:
    """Assert *engine*'s schema is at this checkout's head, without migrating.

    The read-only path a worker session, the CLI and pytest take when they
    open the daemon's production database: connecting is fine, silently
    changing its schema is not.
    """
    async with engine.connect() as conn:
        await conn.run_sync(_verify_schema_at_head)


async def _run_schema_setup_without_cache(engine: AsyncEngine) -> None:
    """Run the existing direct Alembic path, bypassing the SQLite cache."""
    async with engine.connect() as conn:
        # Check if this is a pre-Alembic database (has tables but no alembic_version)
        def _check_and_migrate(sync_conn):
            insp = inspect(sync_conn)
            existing_tables = set(insp.get_table_names())
            has_alembic = "alembic_version" in existing_tables
            has_data_tables = bool(existing_tables - {"alembic_version"})
            # Reflection opened an implicit transaction — end it so Alembic
            # can own the per-revision boundaries.
            sync_conn.commit()

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
        await conn.commit()


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
