# tests/test_migration_postgres_upgrade_head.py
"""``alembic upgrade head`` must succeed on PostgreSQL, not just SQLite.

PostgreSQL is the production backend (SQLite is the dev default), but
the revision chain is almost always exercised against SQLite, which is
far more forgiving about types in DDL — it happily accepts
``BOOLEAN DEFAULT 0`` where Postgres raises

    DatatypeMismatchError: column "..." is of type boolean but default
    expression is of type integer

Revision ``33bdb059ceff`` shipped exactly that and took every
Postgres-parametrised test down at fixture setup.  This module is the
smoke check for that whole class: run the real chain from an empty
database to head on a real Postgres server and then use the schema.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.pg_dsn import ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _alembic_pg(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


async def _pg_conn(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


async def test_upgrade_head_applies_the_whole_chain_on_postgres():
    """Empty database -> head, on real PostgreSQL, with no manual repair."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("uphead")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    heads = _alembic_pg(dsn, "heads")
    assert heads.returncode == 0, heads.stderr
    head_revision = heads.stdout.split()[0]

    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == head_revision
    finally:
        await conn.close()


async def test_boolean_columns_added_by_migrations_default_correctly_on_postgres():
    """The migrated schema is usable: boolean server defaults actually apply.

    A ``BOOLEAN DEFAULT 0`` column is rejected outright by Postgres, so
    this both re-proves the upgrade and pins the semantics of the
    defaults it installs (``false``, not "some integer that happens to
    be falsy on SQLite").
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("upheadbool")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    conn = await _pg_conn(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('p','P',0)")
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "created_at, updated_at) VALUES ('t','p','T','T','READY',0,0)"
        )
        # Every column the default is meant to cover is omitted here.
        await conn.execute(
            "INSERT INTO sessions (id, task_id, project_id, profile_id, harness, "
            "provider, name, lifecycle, work_dir, epoch, instance_token, started_at) "
            "VALUES ('s','t','p','prof','claude','tmux','n-s','task','/w','e','tok',0)"
        )
        assert (
            await conn.fetchval("SELECT hooks_provisioned FROM sessions WHERE id='s'")
        ) is False
    finally:
        await conn.close()
