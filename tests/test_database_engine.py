"""Regression coverage for schema startup safeguards."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from src.database.engine import (
    _schema_cache_inputs,
    create_postgres_engine,
    run_schema_setup,
    run_startup_data_migrations,
)
from tests.pg_dsn import create_scratch_database


async def _scratch_engine(suffix: str):
    """An engine on an empty Postgres database of this test's own.

    These tests drive ``run_schema_setup`` itself, so they cannot use the
    template-cloned lease pool (which hands back a database already at head).
    """
    return create_postgres_engine(await create_scratch_database(suffix))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The last revision *below* the swarm hierarchy pair (A = a1b2c3d4e5f6 DDL,
#: B = b2c3d4e5f6a7 canonicalise).  Downgrading here crosses both.
BELOW_HIERARCHY_PAIR = "4e925610d7a6"


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DB_URL=db_url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_unknown_alembic_revision_fails_without_rewriting_version(tmp_path):
    engine = await _scratch_engine("engine_unknown")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('not-a-real-revision')"))
        with pytest.raises(RuntimeError, match="not-a-real-revision"):
            await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() == ("not-a-real-revision")
    finally:
        await engine.dispose()


async def test_startup_data_migrations_are_idempotent_and_preserve_same_project_links(
    tmp_path, disable_schema_cache
):
    engine = await _scratch_engine("engine_idempotent")
    try:
        await run_schema_setup(engine)
        # Re-running startup schema setup is the public idempotency contract.
        await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await engine.dispose()


async def test_startup_data_migration_copies_the_first_legacy_repo_to_project(tmp_path):
    """The legacy repo backfill is deterministic and portable to PostgreSQL."""
    engine = await _scratch_engine("legacy_repos")
    try:
        await run_schema_setup(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO projects (id, name, repo_url, created_at) VALUES ('p', 'P', '', 0)")
            )
            for repo_id, url, branch in (
                ("a", "https://example.test/first", "main"),
                ("z", "https://example.test/later", "trunk"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO repos "
                        "(id, project_id, url, default_branch, checkout_base_path) "
                        "VALUES (:id, 'p', :url, :branch, '')"
                    ),
                    {"id": repo_id, "url": url, "branch": branch},
                )

        await run_startup_data_migrations(engine)

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT repo_url, repo_default_branch FROM projects WHERE id = 'p'")
                )
            ).one()
        assert row == ("https://example.test/first", "main")
    finally:
        await engine.dispose()
















def test_schema_cache_key_covers_the_whole_migration_environment():
    """``migrations/env.py`` (batch mode, transaction-per-migration) and the
    helper module revision ``b2c3d4e5f6a7`` imports shape the migrated
    schema as much as the revision files do, so they have to key the cache."""
    inputs = _schema_cache_inputs()
    names = {os.path.relpath(path, ROOT).replace(os.sep, "/") for path in inputs}

    assert {
        "src/database/tables.py",
        "src/database/hierarchy_migration.py",
        "migrations/env.py",
    } <= names
    assert any(name.startswith("migrations/versions/") for name in names)
    assert all(os.path.isfile(path) for path in inputs)




