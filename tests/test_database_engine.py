"""Regression coverage for schema startup safeguards."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import event, text
from sqlalchemy import exc as sa_exc

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





class TestConnectionLiveness:
    """``database.pre_ping`` — what a pooled checkout costs, and what it covers.

    ``create_postgres_engine`` used to pass ``pool_pre_ping=True``
    unconditionally.  On the asyncpg dialect that is three round trips per
    checkout, not one (``_async_ping`` opens a transaction, runs ``;`` and
    rolls back so it stays correct under pgbouncer), which measured 1.26-1.56
    ms of every pooled transaction the daemon takes — ~15 ms of a single
    ``task_claim`` + ``release_claim`` round trip, and the same tax on every
    other path.  The default is now ``local``: ask asyncpg whether it already
    knows the connection is dead, for free.  These tests pin both halves of
    that trade — the wiring, and the failure it still has to absorb.
    """

    #: A DSN good enough to build an engine from.  Nothing here connects.
    UNCONNECTED = "postgresql://u:p@localhost:5432/does-not-matter"

    def test_local_is_the_default_and_keeps_sqlalchemys_wire_ping_off(self):
        pool = create_postgres_engine(self.UNCONNECTED).sync_engine.pool
        assert pool._pre_ping is False
        assert bool(pool.dispatch.checkout), "local mode installs a checkout listener"

    def test_wire_asks_sqlalchemy_for_the_three_round_trip_ping(self):
        pool = create_postgres_engine(self.UNCONNECTED, pre_ping="wire").sync_engine.pool
        assert pool._pre_ping is True
        assert not bool(pool.dispatch.checkout), "wire mode must not also pay for the local check"

    def test_off_checks_nothing(self):
        pool = create_postgres_engine(self.UNCONNECTED, pre_ping="off").sync_engine.pool
        assert pool._pre_ping is False
        assert not bool(pool.dispatch.checkout)

    def test_an_unknown_mode_falls_back_to_local_rather_than_failing_to_build(self):
        """Config validation reports the typo; the engine still comes up safe."""
        pool = create_postgres_engine(self.UNCONNECTED, pre_ping="lcoal").sync_engine.pool
        assert pool._pre_ping is False
        assert bool(pool.dispatch.checkout)

    def test_pool_recycle_is_plumbed_and_zero_disables_it(self):
        assert create_postgres_engine(self.UNCONNECTED).sync_engine.pool._recycle == 1800
        recycled = create_postgres_engine(self.UNCONNECTED, pool_recycle=60).sync_engine.pool
        assert recycled._recycle == 60
        assert create_postgres_engine(self.UNCONNECTED, pool_recycle=0).sync_engine.pool._recycle == -1

    async def test_local_replaces_a_backend_the_server_terminated(self):
        """The case ``wire`` was being paid for, handled without a round trip."""
        engine, pid, closed = await _park_a_terminated_connection("liveness_local", "local")
        assert closed, "asyncpg should have observed the termination while idle in the pool"
        try:
            async with engine.begin() as conn:
                fresh = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
            assert fresh != pid
        finally:
            await engine.dispose()

    async def test_off_lets_the_dead_connection_reach_the_caller(self):
        """The control: without a liveness check the same kill is an error."""
        engine, _pid, closed = await _park_a_terminated_connection("liveness_off", "off")
        assert closed
        try:
            with pytest.raises(sa_exc.DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(text("SELECT pg_backend_pid()"))
        finally:
            await engine.dispose()


async def _park_a_terminated_connection(suffix: str, pre_ping: str):
    """Leave one pooled connection whose backend the server has killed.

    Returns ``(engine, dead pid, asyncpg noticed)``.  The wait is on
    ``Connection.is_closed()`` rather than a sleep because that flag *is* the
    thing ``pre_ping: local`` reads: polling it makes the test deterministic
    instead of a race with the event loop's next read.
    """
    dsn = await create_scratch_database(suffix)
    engine = create_postgres_engine(dsn, pool_max=1, pre_ping=pre_ping)

    parked: list = []

    @event.listens_for(engine.sync_engine, "checkin")
    def _capture(dbapi_connection, connection_record):
        parked.append(connection_record.driver_connection)

    async with engine.begin() as conn:
        pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()

    killer = create_postgres_engine(dsn, pre_ping="off")
    try:
        async with killer.begin() as conn:
            await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    finally:
        await killer.dispose()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if parked and parked[-1] is not None and parked[-1].is_closed():
            return engine, pid, True
        await asyncio.sleep(0.05)
    return engine, pid, False
