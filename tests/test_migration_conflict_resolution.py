"""Dialect round trips for conflict resolution reservations revision 8b4d2f7c1a90."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "7a1d5e9f0b2c"
REVISION = "8b4d2f7c1a90"
POSTGRES_DSN = ensure_worker_postgres_dsn()
RESOLUTION_COLUMNS = {
    "resolution_head_sha",
    "resolution_tree_sha",
    "resolution_commit_shas",
    "resolution_operation_id",
    "resolution_stage_ordinal",
    "resolution_task_id",
    "resolution_session_id",
    "resolution_session_instance_token",
    "resolution_workspace_id",
    "resolution_fence_owner_id",
    "resolution_fence_token",
    "resolution_push_evidence",
}
RESOLUTION_CONSTRAINTS = {
    "ck_integration_promotion_intents_resolution_binding",
    "ck_integration_promotion_intents_resolution_stage",
    "ck_integration_promotion_intents_resolution_fence",
}


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_resolution_schema(connection) -> None:
    schema = inspect(connection)
    columns = {
        column["name"]
        for column in schema.get_columns("integration_promotion_intents")
    }
    assert RESOLUTION_COLUMNS <= columns
    constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in schema.get_check_constraints("integration_promotion_intents")
    }
    assert RESOLUTION_CONSTRAINTS <= constraints.keys()
    assert "resolution_reserved" in constraints["ck_integration_promotion_intents_state"]
    assert "resolution_session_instance_token" in constraints[
        "ck_integration_promotion_intents_resolution_binding"
    ]


async def test_sqlite_conflict_resolution_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "conflict-resolution-migration.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_resolution_schema(conn)
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
        with engine.connect() as conn:
            columns = {
                column["name"]
                for column in inspect(conn).get_columns("integration_promotion_intents")
            }
            assert not RESOLUTION_COLUMNS & columns
        with engine.begin() as conn:
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_resolution_schema(conn)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_conflict_resolution_upgrade_downgrade_upgrade():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task7b_conflict_resolution")
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
            await conn.run_sync(_assert_resolution_schema)
        await migrate(PRIOR, downgrade=True)
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns(
                        "integration_promotion_intents"
                    )
                }
            )
            assert not RESOLUTION_COLUMNS & columns
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_resolution_schema)
    finally:
        await engine.dispose()
