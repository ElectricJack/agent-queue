"""Real disposable PostgreSQL upgrades, helper reinstall and safe rollback."""

import importlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, inspect, select

from src.database.tables import metadata, record_installation
from src.records.schema import RECORD_TABLE_NAMES


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


migration = importlib.import_module("migrations.versions.a00000000055_knowledge_records")
pytestmark = pytest.mark.migration


def run_migration(conn, action):
    with Operations.context(MigrationContext.configure(conn)):
        # A current baseline includes later additive knowledge tables. Exercise
        # the real downgrade dependency order before dropping the K01 schema.
        receipts = importlib.import_module(
            "migrations.versions.a00000000065_knowledge_index_receipts"
        )
        protection = importlib.import_module("migrations.versions.a00000000059_knowledge_protection")
        inventory = importlib.import_module(
            "migrations.versions.a00000000060_knowledge_import_inventory"
        )
        context = importlib.import_module("migrations.versions.a00000000062_knowledge_context")
        extraction = importlib.import_module("migrations.versions.a00000000063_knowledge_extraction")
        circuit = importlib.import_module("migrations.versions.a00000000064_knowledge_failure_circuit")
        if action == "downgrade":
            receipts.downgrade()
            circuit.downgrade()
            extraction.downgrade()
            context.downgrade()
            inventory.downgrade()
            protection.downgrade()
        getattr(migration, action)()
        if action == "upgrade":
            protection.upgrade()
            inventory.upgrade()
            context.upgrade()
            extraction.upgrade()
            circuit.upgrade()
            receipts.upgrade()


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


async def test_restore_validator_migration_pins_helpers_with_empty_search_path(db):
    from sqlalchemy import text
    from tests.record_helpers import snapshot
    import json

    repair = importlib.import_module("migrations.versions.a00000000061_record_validator_restore")
    async with db.immediate() as conn:
        await conn.exec_driver_sql("ALTER FUNCTION knowledge_snapshot_valid_v1(jsonb) "
                                   "RESET search_path")
        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                repair.upgrade()
                repair.upgrade()
        await conn.run_sync(upgrade)
        await conn.exec_driver_sql("SET LOCAL search_path = ''")
        assert await conn.scalar(text("SELECT public.knowledge_snapshot_valid_v1(CAST(:v AS jsonb))"),
                                 {"v": json.dumps(snapshot())})
        invalid = snapshot()
        invalid["metadata"] = {"invalid": "key"}
        assert not await conn.scalar(
            text("SELECT public.knowledge_snapshot_valid_v1(CAST(:v AS jsonb))"),
            {"v": json.dumps(invalid)},
        )


def run_context_migration(conn, action):
    context = importlib.import_module("migrations.versions.a00000000062_knowledge_context")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(context, action)()


async def test_context_upgrade_is_idempotent_on_baseline_and_creates_missing_tables(db):
    context = importlib.import_module("migrations.versions.a00000000062_knowledge_context")
    async with db.immediate() as conn:
        await conn.run_sync(lambda sync: run_context_migration(sync, "upgrade"))
        await conn.run_sync(lambda sync: run_context_migration(sync, "downgrade"))
        present = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert not set(context.CONTEXT_TABLE_NAMES) & set(present)
        await conn.run_sync(lambda sync: run_context_migration(sync, "upgrade"))
        await conn.run_sync(lambda sync: run_context_migration(sync, "upgrade"))
        present = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(context.CONTEXT_TABLE_NAMES) <= set(present)


async def test_retained_context_refuses_downgrade_without_removing_any_table(db):
    from src.database.tables import knowledge_context_bundles

    context = importlib.import_module("migrations.versions.a00000000062_knowledge_context")
    now = datetime.now(UTC)
    async with db.immediate() as conn:
        await conn.execute(insert(knowledge_context_bundles).values(
            bundle_id=uuid4(), owner_kind="supervisor_session", owner_id="retained-session",
            session_instance="instance", principal_fingerprint="a" * 64,
            request_fingerprint="b" * 64, scope_keys=[], budget={}, selection={},
            content_sha256="c" * 64, prepared_at=now, expires_at=now + timedelta(minutes=5),
        ))
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with db.immediate() as conn:
            await conn.run_sync(lambda sync: run_context_migration(sync, "downgrade"))
    async with db.immediate() as conn:
        present = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert set(context.CONTEXT_TABLE_NAMES) <= set(present)
