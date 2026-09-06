"""Dialect coverage for normalized root-to-main promotion state."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "46f910d0dce6"
REVISION = "d4a81f0c9e72"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


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
        column["name"]
        for column in inspector.get_columns("integration_candidate_ref_mutations")
    }
    assert "prewrite_at" in mutation_columns


def _seed_root_history(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO integration_batches "
            "(id, project_id, repository_id, request_id, source_manifest_digest, base_sha, "
            "lifecycle, current_revision, integration_branch, policy_snapshot, artifact_snapshot, "
            "cleanup_state, created_at, updated_at) VALUES "
            "('batch', 'project', 'repo', 'request', 'digest', :base, 'promoting', 0, "
            "'refs/heads/integration/batch', '{}', '{}', 'pending', 1, 1)"
        ),
        {"base": "a" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_candidate_revisions "
            "(batch_id, revision, construction_base_sha, next_member_ordinal, head_sha, "
            "state, created_at, updated_at) VALUES "
            "('batch', 0, :base, 0, :sha, 'green', 1, 1)"
        ),
        {"base": "a" * 40, "sha": "b" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_promotion_intents "
            "(id, domain_key, receipt_id, source_head, source_base, repository_id, "
            "target_branch, expected_target, prepared_sha, recovery_ref, fence_owner_id, "
            "fence_token, state, created_at, updated_at, intent_kind, root_batch_id, "
            "root_candidate_revision, project_lease_owner_id, project_lease_fence_token, "
            "branch_fence_owner_id, branch_fence_token, ci_evidence_id) VALUES "
            "('root-intent', 'root:batch:0', 'root-receipt', :sha, :base, 'repo', "
            "'refs/heads/main', :base, :sha, 'refs/aq/root/root-intent', 'legacy', 1, "
            "'prepared', 1, 1, 'root', 'batch', 0, 'lease-owner', 3, "
            "'branch-owner', 7, 'ci-green')"
        ),
        {"sha": "b" * 40, "base": "a" * 40},
    )


async def test_sqlite_root_promotion_schema_and_guarded_round_trip(tmp_path):
    path = tmp_path / "root-promotion.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            _assert_root_schema(connection)
        with engine.begin() as connection:
            _seed_root_history(connection)
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="drain or reconcile root promotion history"):
                _migrate(connection, PRIOR, downgrade=True)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT intent_kind FROM integration_promotion_intents")
            ).scalar_one() == "root"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM integration_promotion_intents"))
            _migrate(connection, PRIOR, downgrade=True)
            _migrate(connection, REVISION)
        with engine.connect() as connection:
            _assert_root_schema(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_root_promotion_schema_and_guarded_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("root_promotion_d4")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_assert_root_schema)
        async with engine.begin() as connection:
            await connection.run_sync(_seed_root_history)
        with pytest.raises(RuntimeError, match="drain or reconcile root promotion history"):
            async with engine.begin() as connection:
                await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
        async with engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT intent_kind FROM integration_promotion_intents"))
            ).scalar_one() == "root"
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == REVISION
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM integration_promotion_intents"))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
            await connection.run_sync(lambda sync: _migrate(sync, REVISION))
        async with engine.connect() as connection:
            await connection.run_sync(_assert_root_schema)
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
