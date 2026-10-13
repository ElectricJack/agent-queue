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


async def test_observation_snapshots_upgrade_prior_and_current_metadata():
    revision = importlib.import_module("migrations.versions.a00000000097_observation_snapshots")
    db = Database(lease_dsn("observation_snapshots_migration"))
    await db.initialize()
    try:
        async with db.immediate() as conn:
            await conn.exec_driver_sql("DROP TABLE observation_snapshot_pages")
            await conn.exec_driver_sql("DROP TABLE observation_snapshots")
            await conn.exec_driver_sql(
                "ALTER TABLE playbook_private_values DROP CONSTRAINT "
                "ck_playbook_private_values_owner_kind")
            await conn.exec_driver_sql(
                "ALTER TABLE playbook_private_values ADD CONSTRAINT "
                "ck_playbook_private_values_owner_kind CHECK (owner_kind IN ('run', 'namespace'))")

            def verify(bind):
                names = {"observation_snapshots", "observation_snapshot_pages",
                         "playbook_private_values"}
                context = MigrationContext.configure(bind, opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name in names if kind == "table" else True),
                })
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
                    for name in names:
                        assert all(c["name"] for c in inspect(bind).get_check_constraints(name))
                    revision.downgrade()
                    assert names <= set(inspect(bind).get_table_names())

            await conn.run_sync(verify)
    finally:
        await db.close()


async def test_playbook_state_upgrade_prior_and_current_preserves_journals():
    from sqlalchemy import insert, select
    from src.database import tables as t

    revision = importlib.import_module("migrations.versions.a00000000098_playbook_state")
    db = Database(lease_dsn("playbook_state_migration"))
    await db.initialize()
    try:
        async with db.immediate() as conn:
            names = {"playbook_state_namespaces", "playbook_state_entries", "playbook_state_operations"}
            for name in ("playbook_state_operations", "playbook_state_entries",
                         "playbook_state_namespaces"):
                await conn.exec_driver_sql(f"DROP TABLE {name}")

            def verify(bind):
                context = MigrationContext.configure(bind, opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name in names if kind == "table" else True),
                })
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
                    for name in names:
                        assert all(c["name"] for c in inspect(bind).get_check_constraints(name))
                        for fk in inspect(bind).get_foreign_keys(name):
                            assert fk["options"].get("ondelete") != "CASCADE"

            await conn.run_sync(verify)
            await conn.execute(insert(t.playbook_state_namespaces).values(
                namespace_id="namespace", scope_kind="system", scope_owner="system",
                playbook_id="deleted-playbook", state_schema_version=1, created_at=1, updated_at=1))
            await conn.execute(insert(t.playbook_state_operations).values(
                namespace_id="namespace", operation_key="retained", request_digest="digest",
                result_reference={"__private_value__": "retained"}, byte_count=1,
                revision=1, committed_at=1))

            def rollback_code_only(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    revision.downgrade()
            await conn.run_sync(rollback_code_only)
            assert await conn.scalar(select(t.playbook_state_operations.c.operation_key)) == "retained"
    finally:
        await db.close()
