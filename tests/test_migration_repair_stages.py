"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert "integration_repair_stage_evidence" in schema.get_table_names()
    columns = {column["name"]: column for column in schema.get_columns("integration_repair_stages")}
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


async def test_baseline_repair_stages_schema():
    database = Database(lease_dsn("repair_stages"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()
