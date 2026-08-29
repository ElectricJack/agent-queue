"""Per-xdist-worker Postgres database derivation.

Three suites each parametrize/fixture a live Postgres connection against
``POSTGRES_TEST_DSN``: ``tests/perf`` (``any_db``), ``tests/test_claim_queries.py``
(its ``db`` fixture), and ``tests/test_database_postgresql.py``. CI runs the
whole suite under ``pytest -n auto`` (pytest-xdist) — if every worker process
pointed at the *same* Postgres database, one worker's ``reset_for_tests()``
truncate would race against another worker's in-flight seed/assert, corrupting
both. Giving each xdist worker its own database (named after the worker id)
makes the suites independent again, the same way each SQLite branch already
gets its own ``tmp_path`` file per test.

Usage: call :func:`ensure_worker_postgres_dsn` once per module (at import
time is fine — it's a no-op when ``POSTGRES_TEST_DSN`` isn't set, which is
the common case on a dev machine with no Postgres at all) and use its return
value instead of reading ``POSTGRES_TEST_DSN`` directly. It rewrites the
``POSTGRES_TEST_DSN`` env var in this worker process to the per-worker DSN,
so ``PostgreSQLDatabaseAdapter.reset_for_tests()``'s own guard (which compares
against ``os.environ["POSTGRES_TEST_DSN"]``) keeps working unmodified.
"""

from __future__ import annotations

import asyncio
import os
import re

_WORKER_ENV = "PYTEST_XDIST_WORKER"


def _worker_id() -> str:
    """xdist worker id (``gw0``, ``gw1``, ...), or ``master`` outside ``-n``."""
    raw = os.environ.get(_WORKER_ENV, "master")
    # Never trust environment input blindly for a value that ends up in a
    # SQL identifier, even though pytest-xdist's own ids are already alnum.
    return re.sub(r"[^a-zA-Z0-9_]", "_", raw) or "master"


def _derive_worker_dsn(base_dsn: str) -> str:
    prefix, _, dbname = base_dsn.rpartition("/")
    return f"{prefix}/{dbname}_{_worker_id()}"


async def _create_database_if_missing(base_dsn: str, target_db: str) -> None:
    import asyncpg

    prefix, _, dbname = base_dsn.rpartition("/")
    # asyncpg's own connect() wants the plain "postgresql://" scheme, not
    # SQLAlchemy's "+asyncpg" driver suffix.
    admin_dsn = f"{prefix}/{dbname}".replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target_db)
        if not exists:
            # CREATE DATABASE cannot run inside a transaction on Postgres;
            # asyncpg connections are autocommit by default for a bare
            # execute() outside an explicit transaction block, so this is
            # already fine as written.
            try:
                await conn.execute(f'CREATE DATABASE "{target_db}"')
            except asyncpg.exceptions.DuplicateDatabaseError:
                pass  # another worker won the race to create it first
    finally:
        await conn.close()


def ensure_worker_postgres_dsn() -> str | None:
    """Rewrite ``POSTGRES_TEST_DSN`` to this worker's own database in-place.

    Creates that database first if it doesn't exist yet. Returns the
    (possibly unchanged) DSN, or ``None`` when ``POSTGRES_TEST_DSN`` isn't
    set at all (the common local-dev case — this never touches the network
    then). Idempotent within one worker process: a module that's already
    been rewritten by an earlier call in the same process is a no-op.
    """
    base = os.environ.get("POSTGRES_TEST_DSN")
    if not base:
        return None
    worker_dsn = _derive_worker_dsn(base)
    if os.environ.get("POSTGRES_TEST_DSN") == worker_dsn:
        return worker_dsn
    _, _, target_db = worker_dsn.rpartition("/")
    asyncio.run(_create_database_if_missing(base, target_db))
    os.environ["POSTGRES_TEST_DSN"] = worker_dsn
    return worker_dsn
