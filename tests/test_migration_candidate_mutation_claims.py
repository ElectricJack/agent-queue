"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect, text

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert "integration_candidate_ref_mutations" in schema.get_table_names()
    assert "target_branch" in {
        column["name"] for column in schema.get_columns("integration_candidate_resolutions")
    }
    assert {"repair_workspace_path", "target_kind", "handoff_owner_id", "handoff_fence_token"} <= {
        column["name"] for column in schema.get_columns("integration_candidate_resolutions")
    }
    assert {
        "fk_integration_candidate_ref_mutations_revision",
        "fk_integration_candidate_ref_mutations_resolution",
    } == {fk["name"] for fk in schema.get_foreign_keys("integration_candidate_ref_mutations")}
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
    assert "repair_workspace_path" in guards
    assert "target_kind" in guards
    assert "applied candidate mutation is immutable" in guards


async def test_baseline_candidate_mutation_claims_schema():
    database = Database(lease_dsn("candidate_mutation_claims"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()
