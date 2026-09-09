# tests/test_migration_postgres_upgrade_head.py
"""Fresh PostgreSQL baseline creation, usable defaults, and schema parity.

The pre-squash revision paths are retired. These tests run the supported
baseline from an empty scratch database, independently of the template cache.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()


async def _assert_integration_guards(conn):
    from migrations.integration_guards import FUNCTIONS, TRIGGERS

    expected_functions = {
        re.search(r"FUNCTION\s+(\w+)\(", statement).group(1) for statement in FUNCTIONS
    }
    installed_functions = {
        row["proname"]
        for row in await conn.fetch(
            "SELECT proname FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace "
            "WHERE n.nspname='public'"
        )
    }
    installed_triggers = {
        (row["tgname"], row["relname"])
        for row in await conn.fetch(
            "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid=tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE NOT tgisinternal AND n.nspname='public'"
        )
    }
    assert expected_functions <= installed_functions
    assert {(name, table) for name, table, _ in TRIGGERS} <= installed_triggers


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


async def test_upgrade_head_applies_the_baseline_on_postgres():
    """Empty database -> head, on real PostgreSQL, with no manual repair."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("uphead")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    heads = _alembic_pg(dsn, "heads")
    assert heads.returncode == 0, heads.stderr
    head_revision = heads.stdout.split()[0]

    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == head_revision
        await _assert_integration_guards(conn)
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
        assert (await conn.fetchval("SELECT hooks_provisioned FROM sessions WHERE id='s'")) is False
    finally:
        await conn.close()


async def test_autogenerate_against_a_fresh_head_database_is_empty():
    """``tables.py`` and the migrated schema agree — no autogenerate drift.

    The documented workflow for a schema change is "edit ``tables.py``,
    then ``alembic revision --autogenerate``".  That only works if a
    database at head already matches the metadata exactly: any standing
    drift is silently folded into the *next* developer's revision.  One
    such drift shipped for real — ``task_dependencies``' self-dependency
    guard was declared *unnamed* in ``tables.py``, which makes it
    invisible to autogenerate's comparison, so every run wanted to drop
    the ``task_dependencies_check`` it found in the live schema.  A later
    merge adopted the migration that creates ``agent_profiles.overlay_config``
    without adopting its metadata declaration, so autogenerate then wanted
    to remove that live column.

    This runs the same comparison ``alembic revision --autogenerate``
    runs (same ``compare_type`` opt as ``migrations/env.py``) and
    demands it produce nothing.
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.database.tables import metadata

    dsn = await create_scratch_database("updrift")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    def _diff(sync_conn) -> list:
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, metadata)

    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(_diff)
    finally:
        await engine.dispose()

    assert diffs == [], (
        "alembic autogenerate is not empty against a database at head — "
        "src/database/tables.py has drifted from the migration chain:\n"
        + "\n".join(f"  {d}" for d in diffs)
    )


async def test_baseline_downgrade_is_refused_without_losing_data():
    dsn = await create_scratch_database("baselineguard")
    upgraded = _alembic_pg(dsn, "upgrade", "a00000000001")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
        original_revision = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    refused = _alembic_pg(dsn, "downgrade", "base")
    assert refused.returncode != 0
    assert "the squashed baseline has no downgrade" in refused.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == original_revision
    finally:
        await conn.close()


async def test_upgrade_repairs_guards_on_original_squashed_database():
    from migrations.integration_guards import TRIGGERS

    dsn = await create_scratch_database("repairguards")
    upgraded = _alembic_pg(dsn, "upgrade", "a00000000001")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        # Reproduce the first baseline release: tables existed without triggers.
        for name, table, _ in TRIGGERS:
            await conn.execute(f'DROP TRIGGER "{name}" ON "{table}"')
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        await _assert_integration_guards(conn)
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
    finally:
        await conn.close()
