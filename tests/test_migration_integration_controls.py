"""Dual-dialect migration coverage for integration rollout controls."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "a10c5e1e4f03"
REVISION = "a11a5e1e4f04"
POSTGRES_DSN = ensure_worker_postgres_dsn()
CONTROL_TABLES = {
    "integration_history_waivers",
    "integration_rollout_transitions",
    "integration_history_waiver_consumptions",
    "integration_legacy_gate_applicability",
    "integration_legacy_suppression",
}
IMMUTABLE_TABLES = CONTROL_TABLES - {"integration_legacy_suppression"}


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _constraint_names(connection, table: str) -> list[str | None]:
    inspector = inspect(connection)
    values = [inspector.get_pk_constraint(table).get("name")]
    values.extend(item.get("name") for item in inspector.get_foreign_keys(table))
    values.extend(item.get("name") for item in inspector.get_unique_constraints(table))
    values.extend(item.get("name") for item in inspector.get_check_constraints(table))
    return values


def _exercise_round_trip(connection) -> None:
    _migrate(connection, PRIOR, downgrade=True)
    connection.execute(
        text(
            "INSERT INTO projects (id, name, hierarchical_integration_mode, created_at) "
            "VALUES ('existing-observe', 'existing', 'observe', 1)"
        )
    )
    _migrate(connection, REVISION)
    row = connection.execute(
        text(
            "SELECT hierarchical_integration_desired_mode, "
            "hierarchical_integration_draining, hierarchical_integration_generation "
            "FROM projects WHERE id = 'existing-observe'"
        )
    ).one()
    assert tuple(row) == ("observe", False, 0)
    assert CONTROL_TABLES <= set(inspect(connection).get_table_names())
    for table in CONTROL_TABLES:
        assert all(_constraint_names(connection, table)), table

    _migrate(connection, PRIOR, downgrade=True)
    assert CONTROL_TABLES.isdisjoint(inspect(connection).get_table_names())
    assert "hierarchical_integration_desired_mode" not in {
        item["name"] for item in inspect(connection).get_columns("projects")
    }
    _migrate(connection, REVISION)


def _exercise_live_guard_and_immutability(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO projects (id, name, created_at) "
            "VALUES ('controlled', 'controlled', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_history_waivers "
            "(id, project_id, operator_id, reason, blocker_digest, created_at) "
            "VALUES ('waiver', 'controlled', 'operator:local', 'history migration', "
            ":digest, 2)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "UPDATE projects SET hierarchical_integration_desired_mode = 'observe', "
            "hierarchical_integration_generation = 1 WHERE id = 'controlled'"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_rollout_transitions "
            "(id, project_id, generation, old_effective_mode, new_effective_mode, "
            "old_desired_mode, new_desired_mode, draining, operator_id, reason, "
            "blocker_digest, old_legacy_policy, new_legacy_policy, waiver_id, created_at) "
            "VALUES ('transition', 'controlled', 1, 'disabled', 'disabled', 'disabled', "
            "'observe', false, 'operator:local', 'observe', :digest, '{}', '{}', "
            "'waiver', 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO integration_history_waiver_consumptions "
            "(waiver_id, transition_id, project_id, blocker_digest, consumed_by, consumed_at) "
            "VALUES ('waiver', 'transition', 'controlled', :digest, 'operator:local', 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO gates (id, project_id, gate_type, title, status, created_at) "
            "VALUES ('legacy-gate', 'controlled', 'pr-merged', 'legacy', 'open', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_legacy_gate_applicability "
            "(project_id, gate_id, waiver_id, transition_id, blocker_digest, applicable, "
            "created_at) VALUES ('controlled', 'legacy-gate', 'waiver', 'transition', "
            ":digest, false, 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    timestamp_columns = {
        "integration_history_waivers": "created_at",
        "integration_rollout_transitions": "created_at",
        "integration_history_waiver_consumptions": "consumed_at",
        "integration_legacy_gate_applicability": "created_at",
    }
    for table in IMMUTABLE_TABLES:
        with pytest.raises(SQLAlchemyError), connection.begin_nested():
            connection.execute(text(f"UPDATE {table} SET {timestamp_columns[table]} = 9"))
        with pytest.raises(SQLAlchemyError), connection.begin_nested():
            connection.execute(text(f"DELETE FROM {table}"))

    with pytest.raises(
        RuntimeError,
        match="drain integration rollout/control evidence",
    ):
        _migrate(connection, PRIOR, downgrade=True)
    assert CONTROL_TABLES <= set(inspect(connection).get_table_names())


async def test_sqlite_integration_controls_upgrade_downgrade_and_live_guard(tmp_path):
    from src.database import Database

    path = tmp_path / "integration-controls-migration.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _exercise_round_trip(connection)
            _exercise_live_guard_and_immutability(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_integration_controls_upgrade_downgrade_and_live_guard():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task11a_integration_controls")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    engine = None
    try:
        await database.initialize()
        await database.close()
        engine = create_postgres_engine(dsn, 0, 1)
        async with engine.begin() as connection:
            await connection.run_sync(_exercise_round_trip)
            await connection.run_sync(_exercise_live_guard_and_immutability)
    finally:
        await database.close()
        if engine is not None:
            await engine.dispose()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()
