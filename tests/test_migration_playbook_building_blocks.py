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
    revision = importlib.import_module("migrations.versions.a00000000103_playbook_private_values")
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
    from tests.test_playbook_run_repository import (
        ARTIFACT,
        make_receipt,
        make_snapshot,
        seed_artifact,
    )
    from tests.test_playbook_wait_repository import make_wait

    revision = importlib.import_module("migrations.versions.a00000000104_playbook_iteration_paths")
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
    revision = importlib.import_module("migrations.versions.a00000000105_observation_snapshots")
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

    revision = importlib.import_module("migrations.versions.a00000000106_playbook_state")
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


async def test_message_provenance_upgrade_preserves_legacy_messages_and_readback():
    from src.models import Project

    revision = importlib.import_module("migrations.versions.a00000000107_message_provenance")
    db = Database(lease_dsn("message_provenance_migration"))
    await db.initialize()
    try:
        await db.create_project(Project(id="p", name="p"))
        original = await db.create_message(project_id="p", from_kind="system", from_id="legacy",
                                           to_kind="user", to_id="dashboard", body="retained")
        async with db.immediate() as conn:
            await conn.exec_driver_sql("ALTER TABLE messages DROP COLUMN provenance")

            def verify(bind):
                context = MigrationContext.configure(bind, opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name == "messages" if kind == "table" else True),
                })
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
                    revision.downgrade()
                    assert "provenance" in {c["name"] for c in inspect(bind).get_columns("messages")}
            await conn.run_sync(verify)
        restored = await db.get_message(original.id)
        assert restored.provenance is None and restored.created_seq == original.created_seq
        new = await db.create_message(project_id="p", from_kind="system", from_id="playbook:p",
                                      to_kind="user", to_id="dashboard", body="new",
                                      provenance={"run_id": "r", "playbook_id": "p"})
        assert (await db.get_message(new.id)).provenance == new.provenance
        assert new.created_seq > original.created_seq
    finally:
        await db.close()


async def test_playbook_jobs_upgrade_prior_and_current_metadata():
    revision = importlib.import_module("migrations.versions.a00000000108_playbook_jobs")
    db = Database(lease_dsn("playbook_jobs_migration"))
    await db.initialize()
    try:
        async with db.immediate() as conn:
            await conn.exec_driver_sql("DROP TABLE job_input_pins")
            await conn.exec_driver_sql("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_owner_fields")
            await conn.exec_driver_sql("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_owner_kind")
            for name in ("playbook_run_id", "playbook_step_id", "iteration_path", "attempt",
                         "artifact_digest", "input_root"):
                await conn.exec_driver_sql(f"ALTER TABLE jobs DROP COLUMN {name} CASCADE")
            for name in ("project_id", "workspace_id", "workspace_generation"):
                await conn.exec_driver_sql(f"ALTER TABLE jobs ALTER COLUMN {name} SET NOT NULL")
            await conn.exec_driver_sql("ALTER TABLE jobs ADD CONSTRAINT ck_jobs_owner_kind "
                                      "CHECK (owner_kind IN ('task','integration'))")

            def verify(bind):
                context = MigrationContext.configure(bind, opts={
                    "include_object": lambda obj, name, kind, reflected, compare_to: (
                        name in {"jobs", "job_input_pins"} if kind == "table" else True),
                })
                with Operations.context(context):
                    revision.upgrade()
                    revision.upgrade()
                    assert not compare_metadata(context, metadata)
                    revision.downgrade()
                    assert inspect(bind).has_table("job_input_pins")
            await conn.run_sync(verify)
    finally:
        await db.close()


BUILDING_BLOCK_TABLES = {
    "playbook_private_values", "playbook_step_receipts", "playbook_waits",
    "observation_snapshots", "observation_snapshot_pages", "playbook_state_namespaces",
    "playbook_state_entries", "playbook_state_operations", "messages", "jobs", "job_input_pins",
}
# The schema immediately before a00000000103, rebuilt around rows written by older code.
PRIOR_SCHEMA = (
    "DROP TABLE job_input_pins",
    "DROP TABLE playbook_state_operations",
    "DROP TABLE playbook_state_entries",
    "DROP TABLE playbook_state_namespaces",
    "DROP TABLE observation_snapshot_pages",
    "DROP TABLE observation_snapshots",
    "DROP TABLE playbook_private_values",
    "ALTER TABLE playbook_step_receipts DROP COLUMN iteration_path CASCADE",
    "ALTER TABLE playbook_waits DROP COLUMN iteration_path CASCADE",
    """ALTER TABLE playbook_step_receipts ADD CONSTRAINT uq_playbook_step_receipts_boundary
       UNIQUE (run_id, step_id, iteration, attempt, turn_index, receipt_kind)""",
    """CREATE INDEX idx_playbook_step_receipts_turn
       ON playbook_step_receipts (run_id, step_id, iteration, attempt, turn_index)""",
    """CREATE UNIQUE INDEX uq_playbook_waits_active_step
       ON playbook_waits (run_id, step_id, iteration) WHERE state = 'active'""",
    "ALTER TABLE messages DROP COLUMN provenance",
    "ALTER TABLE jobs DROP CONSTRAINT ck_jobs_owner_fields",
    "ALTER TABLE jobs DROP CONSTRAINT ck_jobs_owner_kind",
    *(f"ALTER TABLE jobs DROP COLUMN {name} CASCADE" for name in (
        "playbook_run_id", "playbook_step_id", "iteration_path", "attempt",
        "artifact_digest", "input_root")),
    *(f"ALTER TABLE jobs ALTER COLUMN {name} SET NOT NULL" for name in (
        "project_id", "workspace_id", "workspace_generation")),
    "ALTER TABLE jobs ADD CONSTRAINT ck_jobs_owner_kind CHECK (owner_kind IN ('task','integration'))",
)


@pytest.mark.integration
async def test_chain_rehearsal_upgrades_prior_install_and_refuses_binary_rollback(tmp_path):
    """Upgrade a pre-103 install through the real chain, then rehearse rolling code back.

    Feature rollback is disabling activations on the new code (the disposable-daemon
    acceptance covers it). Binary rollback to a release before these revisions must be
    refused by startup, never satisfied by a downgrade or a stamp.
    """
    import shutil
    from pathlib import Path

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.database import tables as t
    from src.database.additive_migrations import MigrationTree, check_upgrade
    from src.database.migration_guard import SchemaAheadCode, SchemaAheadPolicy
    from tests.pg_dsn import create_scratch_database
    from tests.test_jobs_queries import values
    from tests.test_wait_migration import ROOT, migrate

    versions = ROOT / "migrations" / "versions"
    tree = MigrationTree.from_directory(versions)
    head, = tree.heads
    engine = create_async_engine(await create_scratch_database("buildingblockschain"))
    try:
        await migrate(engine, "a00000000102")
        async with engine.begin() as conn:
            await conn.execute(sa.text(
                "INSERT INTO projects (id,name,created_at) VALUES ('p','P',0)"))
            await conn.execute(sa.text(
                "INSERT INTO tasks (id,project_id,title,description,created_at,updated_at) "
                "VALUES ('t','p','Keep','Keep',0,0)"))
            await conn.execute(sa.insert(t.messages).values(
                id="legacy-message", project_id="p", from_kind="system", from_id="legacy",
                to_kind="user", to_id="dashboard", body="retained", created_at=1))
            await conn.execute(sa.insert(t.jobs).values(values(id="legacy-job")))
            for statement in PRIOR_SCHEMA:
                await conn.execute(sa.text(statement))

        def prior(bind):
            assert MigrationContext.configure(bind).get_current_heads() == ("a00000000102",)
            assert not inspect(bind).has_table("playbook_state_entries")
            assert "provenance" not in {c["name"] for c in inspect(bind).get_columns("messages")}

        async with engine.connect() as conn:
            await conn.run_sync(prior)
        await migrate(engine, "head")
        await migrate(engine, "head")

        def upgraded(bind):
            context = MigrationContext.configure(bind, opts={
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    name in BUILDING_BLOCK_TABLES if kind == "table" else True),
            })
            assert context.get_current_heads() == (head,)
            assert not compare_metadata(context, metadata)
            for name in BUILDING_BLOCK_TABLES:
                assert all(c["name"] for c in inspect(bind).get_check_constraints(name))
            assert bind.execute(sa.text(
                "SELECT body, provenance FROM messages WHERE id='legacy-message'"
            )).one() == ("retained", None)
            assert bind.execute(sa.text(
                "SELECT owner_kind, project_id, workspace_id, playbook_run_id "
                "FROM jobs WHERE id='legacy-job'")).one() == ("task", "p", "w", None)
            # Relaxed nullability is for playbook owners; task owners keep their fields.
            with pytest.raises(sa.exc.IntegrityError), bind.begin_nested():
                bind.execute(sa.insert(t.jobs).values(values(
                    id="workspace-less", idempotency_key="other", workspace_id=None)))

        async with engine.begin() as conn:
            await conn.run_sync(upgraded)
    finally:
        await engine.dispose()

    def release(revision):
        path = tmp_path / revision
        path.mkdir()
        for ancestor in tree.ancestors(revision):
            shutil.copy(tree.revisions[ancestor].path, path)
        return path

    retained = SchemaAheadPolicy(str(versions))
    # Messages provenance alone is provably additive: older code may run ahead of it.
    assert retained.accepts(("a00000000107",), release("a00000000106"))
    assert not check_upgrade(tree.revisions["a00000000103"])
    # Constraint widening, metadata-driven creates and the job owner rewrite are not
    # provably additive, so code before them refuses this schema rather than guess.
    with pytest.raises(SchemaAheadCode, match="a00000000104_playbook_iteration_paths") as refused:
        retained.accepts((head,), release("a00000000102"))
    for revision in ("a00000000105", "a00000000106", "a00000000108"):
        assert Path(tree.revisions[revision].path).name in str(refused.value)
    assert not SchemaAheadPolicy().accepts((head,), release("a00000000101"))
