"""Dialect round trips for conflict resolution reservations revision 8b4d2f7c1a90."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Column, Integer, MetaData, Table, Text, create_engine, inspect, select

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "7a1d5e9f0b2c"
REVISION = "8b4d2f7c1a90"
POSTGRES_DSN = ensure_worker_postgres_dsn()
RESOLUTION_COLUMNS = {
    "resolution_head_sha",
    "resolution_tree_sha",
    "resolution_commit_shas",
    "resolution_operation_id",
    "resolution_stage_ordinal",
    "resolution_task_id",
    "resolution_session_id",
    "resolution_session_instance_token",
    "resolution_workspace_id",
    "resolution_fence_owner_id",
    "resolution_fence_token",
    "resolution_push_evidence",
}
RESOLUTION_CONSTRAINTS = {
    "ck_integration_promotion_intents_resolution_binding",
    "ck_integration_promotion_intents_resolution_stage",
    "ck_integration_promotion_intents_resolution_fence",
}
SESSION_INSTANCE_COLUMN = "session_instance_token"


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_resolution_schema(connection) -> None:
    schema = inspect(connection)
    assert SESSION_INSTANCE_COLUMN in {
        column["name"] for column in schema.get_columns("api_session_tokens")
    }
    columns = {
        column["name"]
        for column in schema.get_columns("integration_promotion_intents")
    }
    assert RESOLUTION_COLUMNS <= columns
    constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in schema.get_check_constraints("integration_promotion_intents")
    }
    assert RESOLUTION_CONSTRAINTS <= constraints.keys()
    assert "resolution_reserved" in constraints["ck_integration_promotion_intents_state"]
    assert "resolution_session_instance_token" in constraints[
        "ck_integration_promotion_intents_resolution_binding"
    ]


def _seed_live_resolution(connection) -> None:
    intent = Table(
        "integration_promotion_intents",
        MetaData(),
        Column("id", Text),
        Column("domain_key", Text),
        Column("operation_key", Text),
        Column("receipt_id", Text),
        Column("source_head", Text),
        Column("source_base", Text),
        Column("repository_id", Text),
        Column("target_branch", Text),
        Column("expected_target", Text),
        Column("fence_owner_id", Text),
        Column("fence_token", Integer),
        Column("state", Text),
        Column("resolution_head_sha", Text),
        Column("resolution_tree_sha", Text),
        Column("resolution_commit_shas", JSON),
        Column("resolution_operation_id", Text),
        Column("resolution_stage_ordinal", Integer),
        Column("resolution_task_id", Text),
        Column("resolution_session_id", Text),
        Column("resolution_session_instance_token", Text),
        Column("resolution_workspace_id", Text),
        Column("resolution_fence_owner_id", Text),
        Column("resolution_fence_token", Integer),
        Column("created_at", Integer),
        Column("updated_at", Integer),
    )
    connection.execute(
        intent.insert().values(
            id="live-resolution",
            domain_key="live-resolution-domain",
            operation_key="operation",
            receipt_id="receipt",
            source_head="a" * 40,
            source_base="b" * 40,
            repository_id="repo",
            target_branch="aq/parent",
            expected_target="c" * 40,
            fence_owner_id="operation",
            fence_token=1,
            state="resolution_reserved",
            resolution_head_sha="d" * 40,
            resolution_tree_sha="e" * 40,
            resolution_commit_shas=["d" * 40],
            resolution_operation_id="operation",
            resolution_stage_ordinal=0,
            resolution_task_id="repair",
            resolution_session_id="session",
            resolution_session_instance_token="instance",
            resolution_workspace_id="workspace",
            resolution_fence_owner_id="repair",
            resolution_fence_token=2,
            created_at=1,
            updated_at=1,
        )
    )


def _assert_live_resolution_state(connection, expected: str) -> None:
    intent = Table(
        "integration_promotion_intents",
        MetaData(),
        Column("id", Text),
        Column("state", Text),
    )
    state = connection.execute(
        select(intent.c.state).where(intent.c.id == "live-resolution")
    ).scalar_one()
    assert state == expected


def _drain_live_resolution(connection) -> None:
    intent = Table(
        "integration_promotion_intents",
        MetaData(),
        Column("id", Text),
    )
    connection.execute(intent.delete().where(intent.c.id == "live-resolution"))


def _assert_live_resolution_absent(connection) -> None:
    intent = Table(
        "integration_promotion_intents",
        MetaData(),
        Column("id", Text),
    )
    assert connection.execute(
        select(intent.c.id).where(intent.c.id == "live-resolution")
    ).scalar_one_or_none() is None


async def test_sqlite_conflict_resolution_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "conflict-resolution-migration.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_resolution_schema(conn)
        with engine.begin() as conn:
            _seed_live_resolution(conn)
        with pytest.raises(RuntimeError, match="reconcile/drain resolution reservations"):
            with engine.begin() as conn:
                _migrate(conn, PRIOR, downgrade=True)
        with engine.connect() as conn:
            _assert_resolution_schema(conn)
            _assert_live_resolution_state(conn, "resolution_reserved")
        with engine.begin() as conn:
            _drain_live_resolution(conn)
            _migrate(conn, PRIOR, downgrade=True)
        with engine.connect() as conn:
            columns = {
                column["name"]
                for column in inspect(conn).get_columns("integration_promotion_intents")
            }
            assert not RESOLUTION_COLUMNS & columns
            assert SESSION_INSTANCE_COLUMN not in {
                column["name"] for column in inspect(conn).get_columns("api_session_tokens")
            }
            _assert_live_resolution_absent(conn)
        with engine.begin() as conn:
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_resolution_schema(conn)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_conflict_resolution_upgrade_downgrade_upgrade():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task7b_conflict_resolution")
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
            await conn.run_sync(_assert_resolution_schema)
        async with engine.begin() as conn:
            await conn.run_sync(_seed_live_resolution)
        with pytest.raises(RuntimeError, match="reconcile/drain resolution reservations"):
            await migrate(PRIOR, downgrade=True)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_resolution_schema)
            await conn.run_sync(
                lambda sync: _assert_live_resolution_state(sync, "resolution_reserved")
            )
        async with engine.begin() as conn:
            await conn.run_sync(_drain_live_resolution)
        await migrate(PRIOR, downgrade=True)
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns(
                        "integration_promotion_intents"
                    )
                }
            )
            assert not RESOLUTION_COLUMNS & columns
            token_columns = await conn.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("api_session_tokens")
                }
            )
            assert SESSION_INSTANCE_COLUMN not in token_columns
            await conn.run_sync(
                _assert_live_resolution_absent
            )
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_resolution_schema)
    finally:
        await engine.dispose()
