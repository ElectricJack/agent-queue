"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_schema(connection) -> None:
    inspector = inspect(connection)
    columns = {
        column["name"] for column in inspector.get_columns("integration_attestation_publications")
    }
    assert {
        "project_id",
        "batch_id",
        "revision",
        "operation_id",
        "head_sha",
        "ci_evidence_id",
        "external_id",
        "execution_nonce",
        "state",
        "prewrite_at",
        "check_run_id",
        "expires_at",
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
        item["name"] for item in inspector.get_foreign_keys("integration_attestation_publications")
    }
    assert {
        "fk_integration_attestation_publications_revision",
        "fk_integration_attestation_publications_project",
        "fk_integration_attestation_publications_operation",
        "fk_integration_attestation_publications_evidence",
    } <= foreign_keys


async def test_baseline_attestation_publications_schema():
    database = Database(lease_dsn("attestation_publications"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()
