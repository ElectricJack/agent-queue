"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration

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
    "resolution_push_started_at",
    "resolution_push_evidence",
}
RESOLUTION_CONSTRAINTS = {
    "ck_integration_promotion_intents_resolution_binding",
    "ck_integration_promotion_intents_resolution_stage",
    "ck_integration_promotion_intents_resolution_fence",
}
SESSION_INSTANCE_COLUMN = "session_instance_token"


def _assert_resolution_schema(connection) -> None:
    schema = inspect(connection)
    assert SESSION_INSTANCE_COLUMN in {
        column["name"] for column in schema.get_columns("api_session_tokens")
    }
    columns = {column["name"] for column in schema.get_columns("integration_promotion_intents")}
    assert RESOLUTION_COLUMNS <= columns
    constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in schema.get_check_constraints("integration_promotion_intents")
    }
    assert RESOLUTION_CONSTRAINTS <= constraints.keys()
    assert "resolution_reserved" in constraints["ck_integration_promotion_intents_state"]
    assert (
        "resolution_session_instance_token"
        in constraints["ck_integration_promotion_intents_resolution_binding"]
    )


async def test_baseline_conflict_resolution_schema():
    database = Database(lease_dsn("conflict_resolution"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_resolution_schema)
    finally:
        await database.close()
