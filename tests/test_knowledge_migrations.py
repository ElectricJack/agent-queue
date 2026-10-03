"""Real disposable PostgreSQL upgrades, helper reinstall and safe rollback."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from src.database.tables import metadata, record_installation
from src.records.schema import RECORD_TABLE_NAMES


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


migration = importlib.import_module("migrations.versions.a00000000055_knowledge_records")
pytestmark = pytest.mark.migration


def run_migration(conn, action):
    with Operations.context(MigrationContext.configure(conn)):
        getattr(migration, action)()


async def test_baseline_created_tables_reinstall_guards_and_keep_installation(db):
    async with db.immediate() as conn:
        before = await conn.scalar(select(record_installation.c.installation_id))
        await conn.exec_driver_sql("DROP TRIGGER tr_records_identity_v1 ON records")
        await conn.run_sync(lambda sync: run_migration(sync, "upgrade"))
        await conn.run_sync(lambda sync: run_migration(sync, "upgrade"))
        after = await conn.scalar(select(record_installation.c.installation_id))
        assert before == after
        from sqlalchemy import text

        assert (
            await conn.scalar(
                text("SELECT count(*) FROM pg_trigger WHERE tgname = 'tr_records_identity_v1'")
            )
            == 1
        )


async def test_empty_downgrade_then_upgrade_creates_all_tables(db):
    async with db.immediate() as conn:
        await conn.run_sync(lambda sync: run_migration(sync, "downgrade"))
        remaining = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert not set(RECORD_TABLE_NAMES) & set(remaining)
        await conn.run_sync(lambda sync: run_migration(sync, "upgrade"))
        created = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(RECORD_TABLE_NAMES) <= set(created)
        assert await conn.scalar(select(record_installation.c.installation_id))


async def test_nonempty_downgrade_refuses_before_any_ddl(db):
    async with db.immediate() as conn:
        await db.ensure_record_scope_on(project_id=None, conn=conn)
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with db.immediate() as conn:
            await conn.run_sync(lambda sync: run_migration(sync, "downgrade"))
    async with db.immediate() as conn:
        present = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(RECORD_TABLE_NAMES) <= set(present)


async def test_metadata_create_all_installs_functions_before_payload_checks(db):
    async with db.immediate() as conn:
        await conn.run_sync(lambda sync: run_migration(sync, "downgrade"))
        await conn.run_sync(
            lambda sync: metadata.create_all(
                sync, tables=[metadata.tables[name] for name in RECORD_TABLE_NAMES]
            )
        )
        await conn.run_sync(lambda sync: run_migration(sync, "upgrade"))
        assert await conn.scalar(select(record_installation.c.installation_id))
