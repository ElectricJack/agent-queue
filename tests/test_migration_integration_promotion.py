"""Dialect round trips for prepared promotion evidence revision b91."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "f02a4a4a3010"
REVISION = "b91e4d7a2c10"
POSTGRES_DSN = ensure_worker_postgres_dsn()
REVIEW_INDEX_COLUMNS = [
    "source_task_id",
    "repository_id",
    "source_base",
    "reviewed_head_sha",
    "generation",
    "created_at",
    "id",
]


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


async def test_sqlite_promotion_revision_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "promotion-migration.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            assert "integration_review_evidence" in inspect(conn).get_table_names()
            review_index = next(
                index
                for index in inspect(conn).get_indexes("integration_review_evidence")
                if index["name"] == "idx_integration_review_evidence_current"
            )
            assert review_index["column_names"] == REVIEW_INDEX_COLUMNS
            assert "authors" in {
                column["name"]
                for column in inspect(conn).get_columns("integration_promotion_intents")
            }
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
        with engine.connect() as conn:
            assert "integration_review_evidence" not in inspect(conn).get_table_names()
        with engine.begin() as conn:
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            assert "integration_review_evidence" in inspect(conn).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_promotion_revision_upgrade_downgrade_upgrade():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("promotion_b91")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:

        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(lambda sync: _migrate(sync, revision, downgrade=downgrade))
                await conn.commit()

        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "integration_review_evidence" in tables
            review_indexes = await conn.run_sync(
                lambda sync: inspect(sync).get_indexes("integration_review_evidence")
            )
            review_index = next(
                index
                for index in review_indexes
                if index["name"] == "idx_integration_review_evidence_current"
            )
            assert review_index["column_names"] == REVIEW_INDEX_COLUMNS
        raw_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        evidence_conn = await asyncpg.connect(raw_dsn)
        try:
            await evidence_conn.execute(
                "INSERT INTO integration_review_evidence "
                "(id, source_task_id, repository_id, source_base, reviewed_head_sha, "
                "reviewed_tree_sha, reviewer_task_id, review_kind, generation, verdict, "
                "evidence, created_at) VALUES "
                "('immutable', 'task', 'repo', $1, $2, $3, 'review', 'leaf', 0, "
                "'approved', '{}'::json, 1)",
                "a" * 40,
                "b" * 40,
                "c" * 40,
            )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await evidence_conn.execute(
                    "UPDATE integration_review_evidence SET verdict='rejected' WHERE id='immutable'"
                )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await evidence_conn.execute(
                    "DELETE FROM integration_review_evidence WHERE id='immutable'"
                )
        finally:
            await evidence_conn.close()
        await migrate(PRIOR, downgrade=True)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "integration_review_evidence" not in tables
        await migrate(REVISION)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "integration_review_evidence" in tables
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
