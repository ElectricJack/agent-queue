"""Settling schedules, epic dependencies, and GitHub review evidence survive upgrades."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.tables import (
    epic_dependencies,
    integration_review_evidence,
    project_integration_schedules,
)
from src.models import Project
from tests.db_fixtures import lease_dsn
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
async def db():
    database = Database(lease_dsn("epic-migration"))
    await database.initialize()
    await database.create_project(Project(id="p", name="migration project"))
    yield database
    await database.close()


async def test_settling_columns_exist_and_default_to_null(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p", interval_seconds=60, next_due_at=60.0, updated_at=1.0
            )
        )
        row = (
            await conn.execute(
                select(
                    project_integration_schedules.c.settling_first_approval_at,
                    project_integration_schedules.c.settling_fires_at,
                ).where(project_integration_schedules.c.project_id == "p")
            )
        ).one()
    assert row == (None, None)


async def test_epic_dependencies_round_trip_and_reject_self_edges(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(epic_dependencies).values(
                dependent_task_id="e2", dependency_task_id="e1", declared_at=10.0
            )
        )
        rows = (await conn.execute(select(epic_dependencies))).mappings().all()
    assert [dict(row) for row in rows] == [
        {"dependent_task_id": "e2", "dependency_task_id": "e1", "declared_at": 10.0}
    ]

    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                insert(epic_dependencies).values(
                    dependent_task_id="e1", dependency_task_id="e1", declared_at=10.0
                )
            )


def _upgrade(dsn: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.migration
@pytest.mark.integration
@pytest.mark.parametrize("legacy_schema", [False, True], ids=["fresh", "existing"])
async def test_upgrade_applies_to_fresh_and_existing_schemas(legacy_schema):
    if not ensure_worker_postgres_dsn():
        pytest.skip("POSTGRES_TEST_DSN is not set")

    dsn = await create_scratch_database("epic_pull_requests")
    _upgrade(dsn, "a0000000001a" if legacy_schema else "head")

    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        if legacy_schema:
            await conn.execute("DROP TABLE IF EXISTS epic_dependencies")
            await conn.execute(
                "ALTER TABLE project_integration_schedules "
                "DROP COLUMN IF EXISTS settling_first_approval_at, "
                "DROP COLUMN IF EXISTS settling_fires_at"
            )
            await conn.execute(
                "ALTER TABLE integration_review_evidence "
                "DROP COLUMN IF EXISTS reviewer_identity, "
                "ALTER COLUMN reviewer_task_id SET NOT NULL"
            )
    finally:
        await conn.close()

    if legacy_schema:
        _upgrade(dsn, "head")

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        applied_revision = await conn.fetchval("SELECT version_num FROM alembic_version")
        script = ScriptDirectory.from_config(Config(os.path.join(ROOT, "alembic.ini")))
        assert applied_revision == script.get_current_head()
        applied_history = {
            revision.revision for revision in script.iterate_revisions(applied_revision, "base")
        }
        assert "a0000000001b" in applied_history
        columns = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            "project_integration_schedules",
        )
        assert {row["column_name"] for row in columns} >= {
            "settling_first_approval_at", "settling_fires_at"
        }
        review_columns = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            integration_review_evidence.name,
        )
        review_nullability = {
            row["column_name"]: row["is_nullable"] for row in review_columns
        }
        assert review_nullability["reviewer_task_id"] == "YES"
        assert review_nullability["reviewer_identity"] == "YES"
        await conn.execute(
            "INSERT INTO epic_dependencies (dependent_task_id, dependency_task_id, declared_at) "
            "VALUES ('e2', 'e1', 10)"
        )
        assert await conn.fetchval("SELECT count(*) FROM epic_dependencies") == 1
    finally:
        await conn.close()
