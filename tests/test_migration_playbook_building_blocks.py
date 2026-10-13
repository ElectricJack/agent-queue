"""A3's additive encrypted-value migration on disposable PostgreSQL."""

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


async def test_observation_snapshots_upgrade_prior_and_current_metadata():
    revision = importlib.import_module("migrations.versions.a00000000096_observation_snapshots")
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
