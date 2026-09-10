"""Async engine creation and schema lifecycle management.

PostgreSQL is the only supported backend.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import event, exc, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.database.migration_guard import VERIFY, migration_decision
from src.database.schema_key import (
    ALEMBIC_INI as _ALEMBIC_INI_SHARED,
)
from src.database.schema_key import (
    PROJECT_ROOT as _PROJECT_ROOT_SHARED,
)
from src.database.schema_key import (
    alembic_head_revisions,
    schema_inputs,
    schema_key,
)
from src.database.tables import projects, repos

logger = logging.getLogger(__name__)

# Both re-exported from ``schema_key`` so there is exactly one answer to
# "where is the repo root" shared by the engine and the test substrate.
_PROJECT_ROOT = _PROJECT_ROOT_SHARED
_ALEMBIC_INI = _ALEMBIC_INI_SHARED






#: Re-exported from :mod:`src.database.schema_key` so the SQLite template
#: cache and the PostgreSQL test template share one definition of the key.
_alembic_head_revisions = alembic_head_revisions
_schema_cache_inputs = schema_inputs
_schema_cache_key = schema_key














def _install_local_liveness_check(engine: AsyncEngine) -> None:
    """Discard a pooled connection that asyncpg already knows is dead.

    This is the ``local`` half of :data:`src.config.POOL_PRE_PING_MODES`, and
    the reason the daemon does not pay for ``pool_pre_ping=True``.

    When PostgreSQL drops a connection — a server bounce, an idle reaper, a
    ``pg_terminate_backend`` — the socket delivers EOF (or a FATAL) while that
    connection sits idle in the pool, and asyncpg's protocol marks it closed
    from the event loop's own read.  ``Connection.is_closed()`` therefore
    answers the question pre-ping asks without touching the wire at all.
    Raising :class:`~sqlalchemy.exc.DisconnectionError` from the ``checkout``
    event is SQLAlchemy's documented hook for pessimistic disconnect handling:
    the pool invalidates the entry and retries the checkout with a fresh
    connection, so the caller never sees the stale one.

    What this does *not* cover, and ``wire`` does: a connection that died so
    recently the loop has not processed the EOF yet, and a half-open socket
    where no FIN ever arrives.  Both still surface as an ``InterfaceError`` on
    first use.  Operators who cannot accept that — notably behind pgbouncer in
    transaction mode — set ``database.pre_ping: wire``.
    """
    @event.listens_for(engine.sync_engine, "checkout")
    def _discard_closed_connections(dbapi_connection, connection_record, connection_proxy):
        # ``is_closed`` is a local read on the asyncpg connection -- no wire
        # traffic, and nothing to fail.  The ``getattr`` is only there so a
        # non-asyncpg driver connection (a test double) is a no-op rather than
        # an AttributeError raised into every checkout.
        driver_connection = connection_record.driver_connection
        is_closed = getattr(driver_connection, "is_closed", None)
        if is_closed is not None and is_closed():
            raise exc.DisconnectionError(
                "asyncpg connection is already closed; discarding it from the pool"
            )


def create_postgres_engine(
    dsn: str,
    pool_min: int = 2,
    pool_max: int = 10,
    *,
    pre_ping: str = "local",
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Create an async PostgreSQL engine with connection pooling.

    Normalizes ``postgresql://`` or ``postgres://`` schemes to the
    ``postgresql+asyncpg://`` dialect required by SQLAlchemy async.

    *pre_ping* selects the connection-liveness strategy — see
    :data:`src.config.POOL_PRE_PING_MODES` for what each mode costs and
    covers.  It is a string rather than a bool because ``wire`` (SQLAlchemy's
    ``pool_pre_ping``) is three round trips per checkout on this dialect and
    ``local`` is none, and the daemon takes eight pooled transactions in a
    single ``task_claim``; an unknown value falls back to ``local`` rather
    than failing engine creation, because config validation is where a typo
    is supposed to be reported.

    *pool_recycle* retires a connection older than that many seconds at its
    next checkout (``0`` disables it).  The comparison is local, so unlike
    ``wire`` it is free, and it is what keeps a server-side idle timeout from
    being raced in the first place.
    """
    import re

    url = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", dsn)
    engine = create_async_engine(
        url,
        pool_size=pool_max,
        max_overflow=pool_max,
        pool_pre_ping=pre_ping == "wire",
        pool_recycle=pool_recycle if pool_recycle > 0 else -1,
        pool_timeout=30,
    )
    if pre_ping not in ("wire", "off"):
        _install_local_liveness_check(engine)
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
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

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


def _is_stamped_at_head(sync_connection) -> bool:
    """True when the database is already stamped at this checkout's head.

    Deliberately avoids ``ScriptDirectory`` here and reads the head set from
    the cached :func:`alembic_head_revisions` instead: this runs on every
    database construction, and parsing the whole ``migrations/versions`` tree
    per call is the cost being eliminated.  ``get_current_heads`` is a single
    read of ``alembic_version`` and returns ``()`` when the table is absent,
    which correctly falls through to the pre-Alembic stamping path.
    """
    from alembic.migration import MigrationContext

    current = set(MigrationContext.configure(sync_connection).get_current_heads())
    return bool(current) and current == set(alembic_head_revisions())


def _stamp_legacy_database(sync_connection) -> bool:
    """Stamp a pre-squash database forward, rather than replaying the baseline.

    On 2026-09-07 the 117-revision chain was collapsed into
    ``a00000000001_squashed_baseline``.  A database stamped at the pre-squash
    head already *has* that exact schema, so it must be re-stamped, never
    migrated: running the baseline would try to ``create_all`` over live tables.

    Only the pre-squash head qualifies.  A database stamped anywhere earlier
    has an incomplete schema, and this returns False so the caller raises the
    ordinary "unknown revision" diagnostic — the operator must bring it to the
    pre-squash head on the previous release first.  Guessing there would mark
    a half-migrated database as current.
    """
    from alembic.migration import MigrationContext

    from migrations.versions.a00000000001_squashed_baseline import LEGACY_HEAD

    current = set(MigrationContext.configure(sync_connection).get_current_heads())
    if current != {LEGACY_HEAD}:
        return False
    logger.info(
        "database is at the pre-squash head %s; stamping forward to the "
        "squashed baseline without replaying it",
        LEGACY_HEAD,
    )
    sync_connection.exec_driver_sql("DELETE FROM alembic_version")
    sync_connection.exec_driver_sql(
        "INSERT INTO alembic_version (version_num) VALUES ('a00000000001')"
    )
    sync_connection.commit()
    return True


def _reject_unstamped_legacy_database() -> None:
    """The squash cannot infer which migrations an unstamped schema needs."""
    raise RuntimeError(
        "Existing database has tables but no alembic_version; its migration history "
        "cannot be verified. Upgrade this database to the pre-squash head "
        "6ad7aebb8c7c using the previous release first, then retry this release. "
        "No schema or migration version has been changed."
    )


async def run_schema_setup(engine: AsyncEngine) -> None:
    """Create/migrate the database schema using Alembic.

    A database already stamped at this checkout's head returns immediately
    (see :func:`_is_stamped_at_head`); a new database runs the full chain.
    For existing pre-Alembic databases (have tables but no
    ``alembic_version``), it refuses to guess their migration history and
    directs the operator to upgrade on the previous release first.

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
    """Run the Alembic path directly."""
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

            if has_alembic:
                # Stamping adopts the squashed baseline, but later repairs
                # still need to run before this database is at current head.
                _stamp_legacy_database(sync_conn)

            if has_alembic and _is_stamped_at_head(sync_conn):
                # Already at head: `alembic upgrade head` would be a no-op, but
                # reaching that conclusion costs a ScriptDirectory build plus an
                # env.py run. Skipping it is what lets a template-cloned test
                # database (or a daemon restart with no new revisions) open in
                # milliseconds instead of hundreds of them.
                return

            if has_data_tables and not has_alembic:
                _reject_unstamped_legacy_database()
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
        legacy_repo = repos.alias("legacy_repo")
        first_repo_id = (
            select(legacy_repo.c.id)
            .where(legacy_repo.c.project_id == projects.c.id)
            .order_by(legacy_repo.c.id)
            .limit(1)
            .scalar_subquery()
        )
        result = await conn.execute(
            select(projects.c.id, repos.c.url, repos.c.default_branch)
            .select_from(projects)
            .join(repos, repos.c.id == first_repo_id)
            .where((projects.c.repo_url.is_(None)) | (projects.c.repo_url == ""))
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
