"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_root_schema(connection) -> None:
    inspector = inspect(connection)
    assert "integration_root_intent_members" in inspector.get_table_names()
    intent_columns = {
        column["name"] for column in inspector.get_columns("integration_promotion_intents")
    }
    assert {
        "intent_kind",
        "root_batch_id",
        "root_candidate_revision",
        "project_lease_owner_id",
        "project_lease_fence_token",
        "branch_fence_owner_id",
        "branch_fence_token",
        "ci_evidence_id",
    } <= intent_columns
    mutation_columns = {
        column["name"] for column in inspector.get_columns("integration_candidate_ref_mutations")
    }
    assert "prewrite_at" in mutation_columns
    unique_names = {
        item["name"]
        for table in (
            "integration_promotion_intents",
            "integration_batch_members",
            "integration_candidate_member_results",
            "integration_review_evidence",
        )
        for item in (inspector.get_unique_constraints(table) + inspector.get_indexes(table))
    }
    assert {
        "uq_integration_promotion_intents_root_identity",
        "uq_integration_batch_members_root_identity",
        "uq_integration_candidate_results_root_identity",
        "uq_integration_review_evidence_root_identity",
    } <= unique_names
    root_member_fks = {
        item["name"] for item in inspector.get_foreign_keys("integration_root_intent_members")
    }
    assert {
        "fk_integration_root_intent_members_exact_intent",
        "fk_integration_root_intent_members_exact_member",
        "fk_integration_root_intent_members_exact_result",
        "fk_integration_root_intent_members_exact_review",
    } <= root_member_fks
    receipt_indexes = {item["name"] for item in inspector.get_indexes("task_delivery_receipts")}
    assert "uq_task_delivery_receipts_root_tuple" in receipt_indexes


async def test_baseline_root_main_promotion_schema():
    database = Database(lease_dsn("root_main_promotion"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_root_schema)
    finally:
        await database.close()
