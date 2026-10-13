"""Additive encrypted-value and iteration-path migrations on disposable PostgreSQL."""

import importlib

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from src.database import Database
from src.database.tables import metadata
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


async def test_private_values_upgrade_from_prior_schema_and_current_metadata():
    revision = importlib.import_module("migrations.versions.a00000000095_playbook_private_values")
    db = Database(lease_dsn("private_values_migration"))
    await db.initialize()
    try:
        async with db.immediate() as conn:
            await conn.exec_driver_sql("DROP TABLE playbook_private_values")

            def verify(bind):
                context = MigrationContext.configure(
                    bind,
                    opts={
                        "include_object": lambda obj, name, kind, reflected, compare_to: (
                            name == "playbook_private_values" if kind == "table" else True
                        ),
                    },
                )
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
                    assert all(
                        item["name"]
                        for item in inspect(bind).get_check_constraints("playbook_private_values")
                    )
                    revision.downgrade()
                    assert inspect(bind).has_table("playbook_private_values")

            await conn.run_sync(verify)
    finally:
        await db.close()


async def test_iteration_paths_upgrade_preserves_legacy_rows_and_matches_metadata():
    from tests.test_playbook_run_repository import ARTIFACT, make_receipt, make_snapshot, seed_artifact
    from tests.test_playbook_wait_repository import make_wait

    revision = importlib.import_module("migrations.versions.a00000000096_playbook_iteration_paths")
    db = Database(lease_dsn("iteration_paths_migration"))
    await db.initialize()
    try:
        await seed_artifact(db, ARTIFACT)
        snapshot = await db.create_run(make_snapshot())
        receipt = make_receipt(snapshot)
        snapshot = await db.commit_boundary(snapshot, receipt)
        await db.register(make_wait(), snapshot.version + 1)
        async with db.immediate() as conn:
            # Reconstruct the actual prior schema around real legacy rows.
            for table in ("playbook_step_receipts", "playbook_waits"):
                await conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN iteration_path CASCADE")
            await conn.exec_driver_sql("""ALTER TABLE playbook_step_receipts ADD CONSTRAINT
                uq_playbook_step_receipts_boundary UNIQUE
                (run_id, step_id, iteration, attempt, turn_index, receipt_kind)""")
            await conn.exec_driver_sql("""CREATE INDEX idx_playbook_step_receipts_turn
                ON playbook_step_receipts (run_id, step_id, iteration, attempt, turn_index)""")
            await conn.exec_driver_sql("""CREATE UNIQUE INDEX uq_playbook_waits_active_step
                ON playbook_waits (run_id, step_id, iteration) WHERE state = 'active'""")

            def verify(bind):
                context = MigrationContext.configure(bind, opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name in {"playbook_step_receipts", "playbook_waits"}
                        if kind == "table" else True
                    ),
                })
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
            await conn.run_sync(verify)
        [restored] = await db.list_receipts(snapshot.run_id)
        assert restored.iteration_path == ""
        assert restored.idempotency_key == receipt.idempotency_key
        [wait] = await db.list_active(snapshot.run_id)
        assert wait.iteration_path == ""
    finally:
        await db.close()
