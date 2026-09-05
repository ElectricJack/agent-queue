"""Dialect round trips for durable repair stages revision 7a1d5e9f0b2c."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "e4c6a8b20d31"
REVISION = "7a1d5e9f0b2c"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert "integration_repair_stage_evidence" in schema.get_table_names()
    columns = {
        column["name"]: column
        for column in schema.get_columns("integration_repair_stages")
    }
    assert {
        "writer_kind",
        "trigger_id",
        "current_subject",
        "deadline_event_id",
        "success_subject",
        "success_evidence_id",
        "retained_workspace_id",
        "retained_handoff",
    } <= columns.keys()
    assert columns["intelligence_class"]["nullable"]
    unique_names = {
        constraint["name"]
        for constraint in schema.get_unique_constraints("integration_repair_stages")
    }
    assert "uq_integration_repair_stages_deadline_event" in unique_names
    operation_unique = {
        constraint["name"]
        for constraint in schema.get_unique_constraints("integration_repair_operations")
    }
    assert "uq_integration_repair_operations_batch_episode" in operation_unique


async def test_sqlite_repair_stage_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "repair-stage-migration.db"
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
async def test_postgres_repair_stage_upgrade_downgrade_upgrade():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task7a_repair_stages")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(
                    lambda sync: _migrate(sync, revision, downgrade=downgrade)
                )
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
