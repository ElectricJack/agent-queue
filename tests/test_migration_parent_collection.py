"""Dialect round trips for parent collection revision e4c6a8b20d31."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "c7a1e5d92f40"
REVISION = "e4c6a8b20d31"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


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
        column["name"]: column
        for column in schema.get_columns("task_integration_checkpoints")
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
    } <= {
        column["name"] for column in schema.get_columns("integration_repair_operations")
    }
    operation_columns = {
        column["name"]: column
        for column in schema.get_columns("integration_repair_operations")
    }
    assert not operation_columns["episode_id"]["nullable"]
    operation_index = next(
        index
        for index in schema.get_indexes("integration_repair_operations")
        if index["name"] == "uq_integration_repair_operations_parent_episode"
    )
    assert operation_index["unique"]
    assert operation_index["column_names"] == ["parent_task_id", "episode_id"]


async def test_sqlite_parent_collection_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "parent-collection-migration.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_parent_schema(conn)
            conn.execute(
                text("INSERT INTO projects (id,name,created_at) VALUES ('p','p',1)")
            )
            conn.execute(
                text(
                    "INSERT INTO repos (id,project_id,url,checkout_base_path) "
                    "VALUES ('repo','p','','/tmp')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO tasks (id,project_id,title,description,created_at,updated_at) "
                    "VALUES ('parent','p','parent','parent',1,1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO integration_parent_episodes "
                    "(id,parent_task_id,repository_id,generation,"
                    "pre_collection_checkpoint_sha,created_at) "
                    "VALUES ('episode','parent','repo',0,:sha,1)"
                ),
                {"sha": "a" * 40},
            )
            with pytest.raises(Exception, match="append-only"):
                conn.execute(
                    text(
                        "UPDATE integration_parent_episodes SET generation=1 "
                        "WHERE id='episode'"
                    )
                )
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
        with engine.connect() as conn:
            assert "integration_parent_episodes" not in inspect(conn).get_table_names()
        with engine.begin() as conn:
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_parent_schema(conn)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_parent_collection_upgrade_downgrade_upgrade():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task6_parent_collection")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:

        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(
                    lambda sync: _migrate(sync, revision, downgrade=downgrade)
                )
                await conn.commit()

        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_parent_schema)
        raw_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        evidence_conn = await asyncpg.connect(raw_dsn)
        try:
            await evidence_conn.execute(
                "INSERT INTO projects (id,name,created_at) VALUES ($1,$2,1)", "p", "p"
            )
            await evidence_conn.execute(
                "INSERT INTO repos (id,project_id,url,checkout_base_path) "
                "VALUES ($1,$2,$3,$4)", "repo", "p", "", "/tmp"
            )
            await evidence_conn.execute(
                "INSERT INTO tasks (id,project_id,title,description,created_at,updated_at) "
                "VALUES ($1,$2,$3,$4,1,1)", "parent", "p", "parent", "parent"
            )
            await evidence_conn.execute(
                "INSERT INTO integration_parent_episodes "
                "(id,parent_task_id,repository_id,generation,"
                "pre_collection_checkpoint_sha,created_at) VALUES ($1,$2,$3,0,$4,1)",
                "episode",
                "parent",
                "repo",
                "a" * 40,
            )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await evidence_conn.execute(
                    "DELETE FROM integration_parent_episodes WHERE id='episode'"
                )
        finally:
            await evidence_conn.close()
        await migrate(PRIOR, downgrade=True)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "integration_parent_episodes" not in tables
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_parent_schema)
    finally:
        await engine.dispose()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()
