"""Dialect migration proof for exclusive attestation publication reservations."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "ed46f4aec7be"
REVISION = "f0a1b2c3d4e5"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_schema(connection) -> None:
    inspector = inspect(connection)
    columns = {
        column["name"]
        for column in inspector.get_columns("integration_attestation_publications")
    }
    assert {
        "project_id", "batch_id", "revision", "operation_id", "head_sha",
        "ci_evidence_id", "external_id", "execution_nonce", "state",
        "prewrite_at", "check_run_id", "expires_at",
    } <= columns
    uniques = {
        item["name"]
        for item in inspector.get_unique_constraints("integration_attestation_publications")
    }
    assert {
        "uq_integration_attestation_publications_subject",
        "uq_integration_attestation_publications_external",
    } <= uniques
    foreign_keys = {
        item["name"]
        for item in inspector.get_foreign_keys("integration_attestation_publications")
    }
    assert {
        "fk_integration_attestation_publications_revision",
        "fk_integration_attestation_publications_project",
        "fk_integration_attestation_publications_operation",
        "fk_integration_attestation_publications_evidence",
    } <= foreign_keys


async def test_sqlite_attestation_publication_schema_round_trip(tmp_path):
    path = tmp_path / "attestation-publication.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            _assert_schema(connection)
        with engine.begin() as connection:
            _migrate(connection, PRIOR, downgrade=True)
            assert "integration_attestation_publications" not in inspect(connection).get_table_names()
            _migrate(connection, REVISION)
            _assert_schema(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_attestation_publication_schema_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("attestation_publication_f0")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_assert_schema)
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
            await connection.run_sync(lambda sync: _migrate(sync, REVISION))
            await connection.run_sync(_assert_schema)
    finally:
        await engine.dispose()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()
