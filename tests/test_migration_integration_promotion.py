"""Prepared promotion evidence remains indexed and append-only on PostgreSQL."""

import asyncpg
import pytest
from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration

REVIEW_INDEX_COLUMNS = [
    "source_task_id",
    "repository_id",
    "source_base",
    "reviewed_head_sha",
    "generation",
    "created_at",
    "id",
]


async def test_baseline_promotion_evidence_is_append_only():
    dsn = lease_dsn("promotion")
    database = Database(dsn)
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
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
    finally:
        await database.close()
