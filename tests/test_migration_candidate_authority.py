"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert {
        "integration_candidate_publications",
        "integration_candidate_resolutions",
    } <= set(schema.get_table_names())
    publication_fks = {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_publications")
    }
    resolution_fks = {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_resolutions")
    }
    assert publication_fks == {"fk_integration_candidate_publications_revision"}
    assert {
        "fk_integration_candidate_resolutions_member",
        "fk_integration_candidate_resolutions_stage",
        "fk_integration_candidate_resolutions_task",
        "fk_integration_candidate_resolutions_session",
        "fk_integration_candidate_resolutions_workspace",
    } <= resolution_fks


async def test_baseline_candidate_authority_schema():
    database = Database(lease_dsn("candidate_authority"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()
