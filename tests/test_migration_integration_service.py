"""Catch-up schedule state in the current baseline."""

import pytest
from sqlalchemy import inspect, text

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


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


def _assert_catchup_contract(connection) -> None:
    _seed_schedule(connection)
    catchup_columns = {
        "catchup_trigger",
        "catchup_requested_at",
        "catchup_after_sequence",
    }
    assert catchup_columns <= _columns(connection)
    assert "ck_project_integration_schedules_catchup" in {
        constraint["name"]
        for constraint in inspect(connection).get_check_constraints("project_integration_schedules")
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


async def test_baseline_catchup_contract():
    database = Database(lease_dsn("catchup"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(_assert_catchup_contract)
    finally:
        await database.close()
