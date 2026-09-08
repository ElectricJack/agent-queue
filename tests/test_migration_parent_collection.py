"""Current PostgreSQL schema contracts retained after migration squashing."""

import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_parent_schema(connection) -> None:
    schema = inspect(connection)
    assert {
        "integration_parent_episodes",
        "integration_child_dispositions",
        "integration_parent_verifications",
        "integration_parent_operation_completions",
        "integration_parent_verification_evidence",
        "integration_operation_artifact_pins",
        "integration_episode_receipt_acceptances",
    } <= set(schema.get_table_names())
    checkpoint_columns = {
        column["name"]: column for column in schema.get_columns("task_integration_checkpoints")
    }
    assert {
        "episode_id",
        "current_verification_id",
        "last_completed_operation_id",
        "last_completed_verification_id",
    } <= checkpoint_columns.keys()
    assert checkpoint_columns["episode_id"]["nullable"]
    assert "disposition_revision" in {
        column["name"] for column in schema.get_columns("task_delivery_receipts")
    }
    assert {"parent_operation_id", "parent_episode_id"} <= {
        column["name"] for column in schema.get_columns("task_delivery_receipts")
    }
    foreign_keys = {
        key["name"]
        for table in (
            "task_integration_checkpoints",
            "task_delivery_receipts",
            "integration_repair_operations",
            "integration_parent_episodes",
            "integration_parent_verifications",
            "integration_parent_operation_completions",
        )
        for key in schema.get_foreign_keys(table)
    }
    assert {
        "fk_task_integration_checkpoints_episode",
        "fk_task_integration_checkpoints_verification",
        "fk_task_integration_checkpoints_completion",
        "fk_task_delivery_receipts_parent_operation",
        "fk_task_delivery_receipts_parent_episode",
        "fk_integration_repair_operations_parent_episode",
        "fk_integration_repair_operations_verifier_task",
        "fk_integration_parent_episodes_parent_task",
        "fk_integration_parent_episodes_repository",
        "fk_integration_parent_verifications_operation",
        "fk_integration_parent_verifications_parent_task",
        "fk_integration_parent_verifications_episode",
        "fk_parent_operation_completions_operation",
        "fk_parent_operation_completions_verification",
    } <= foreign_keys
    assert {
        "verifier_task_id",
        "route_playbook_id",
        "route_scope",
        "route_scope_identifier",
        "route_activation_id",
    } <= {column["name"] for column in schema.get_columns("integration_repair_operations")}
    operation_columns = {
        column["name"]: column for column in schema.get_columns("integration_repair_operations")
    }
    assert not operation_columns["episode_id"]["nullable"]
    operation_index = next(
        index
        for index in schema.get_indexes("integration_repair_operations")
        if index["name"] == "uq_integration_repair_operations_parent_episode"
    )
    assert operation_index["unique"]
    assert operation_index["column_names"] == ["parent_task_id", "episode_id"]


async def test_baseline_parent_collection_schema():
    database = Database(lease_dsn("parent_collection"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_parent_schema)
    finally:
        await database.close()
