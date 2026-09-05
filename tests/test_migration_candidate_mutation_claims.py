"""Dialect round trips for durable candidate mutation claims."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "69416e65ee21"
REVISION = "e1eab6dbc186"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert "integration_candidate_ref_mutations" in schema.get_table_names()
    assert "target_branch" in {
        column["name"] for column in schema.get_columns("integration_candidate_resolutions")
    }
    assert {
        "fk_integration_candidate_ref_mutations_revision",
        "fk_integration_candidate_ref_mutations_resolution",
    } == {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_ref_mutations")
    }
    if connection.dialect.name == "sqlite":
        guards = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_candidate_%'"
                )
            )
        )
    else:
        guards = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname IN "
                    "('integration_candidate_publication_is_monotone', "
                    "'integration_candidate_resolution_is_monotone', "
                    "'integration_candidate_mutation_is_monotone')"
                )
            )
        )
    assert "candidate PR identity is immutable" in guards or "OLD.state = 'pr_published'" in guards
    assert "target_branch" in guards
    assert "applied candidate mutation is immutable" in guards


async def test_sqlite_candidate_mutation_claim_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "candidate-mutations.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_candidate_mutation_claim_upgrade_downgrade_upgrade():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task9b1_candidate_mutations")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(lambda sync: _migrate(sync, revision, downgrade=downgrade))
                await conn.commit()

        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await engine.dispose()
        _, _, scratch_name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            POSTGRES_DSN.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        finally:
            await admin.close()
