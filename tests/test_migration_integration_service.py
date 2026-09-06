"""Dual-dialect migration coverage for integration schedule catch-up state."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "e9b2f1b7c3d5"
REVISION = "ed46f4aec7be"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _columns(connection) -> set[str]:
    return {
        column["name"]
        for column in inspect(connection).get_columns("project_integration_schedules")
    }


def _seed_schedule(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO project_integration_schedules "
            "(project_id, enabled, interval_seconds, next_due_at, request_sequence, "
            "outstanding_request_id, outstanding_trigger, outstanding_requested_at, updated_at) "
            "VALUES ('project-z', TRUE, 30, 30, 4, 'request-4', 'manual', 10, 10)"
        )
    )


def _exercise_round_trip(connection) -> None:
    _migrate(connection, PRIOR, downgrade=True)
    _seed_schedule(connection)
    _migrate(connection, REVISION)
    catchup_columns = {
        "catchup_trigger",
        "catchup_requested_at",
        "catchup_after_sequence",
    }
    assert catchup_columns <= _columns(connection)
    assert "ck_project_integration_schedules_catchup" in {
        constraint["name"]
        for constraint in inspect(connection).get_check_constraints(
            "project_integration_schedules"
        )
    }
    row = connection.execute(
        text(
            "SELECT catchup_trigger, catchup_requested_at, catchup_after_sequence "
            "FROM project_integration_schedules WHERE project_id = 'project-z'"
        )
    ).one()
    assert row == (None, None, None)
    connection.execute(
        text(
            "UPDATE project_integration_schedules SET catchup_trigger = 'periodic', "
            "catchup_requested_at = 20, catchup_after_sequence = 4 "
            "WHERE project_id = 'project-z'"
        )
    )
    with pytest.raises(RuntimeError, match="project-z.*live catch-up state"):
        _migrate(connection, PRIOR, downgrade=True)
    assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
    connection.execute(
        text(
            "UPDATE project_integration_schedules SET catchup_trigger = NULL, "
            "catchup_requested_at = NULL, catchup_after_sequence = NULL "
            "WHERE project_id = 'project-z'"
        )
    )
    _migrate(connection, PRIOR, downgrade=True)
    assert not (catchup_columns & _columns(connection))
    outstanding = connection.execute(
        text(
            "SELECT outstanding_request_id, outstanding_trigger, request_sequence "
            "FROM project_integration_schedules WHERE project_id = 'project-z'"
        )
    ).one()
    assert outstanding == ("request-4", "manual", 4)
    _migrate(connection, REVISION)
    assert catchup_columns <= _columns(connection)


async def test_sqlite_catchup_migration_guarded_round_trip(tmp_path):
    path = tmp_path / "catchup.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _exercise_round_trip(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_catchup_migration_guarded_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task10a_schedule_catchup")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_exercise_round_trip)
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
