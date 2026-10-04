"""PostgreSQL baseline contract for durable task completion records."""

import pytest
import sqlalchemy as sa

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


async def test_upgrade_creates_completion_record_contract() -> None:
    database = Database(lease_dsn("completion-records"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync: {
                    col["name"]: col
                    for col in sa.inspect(sync).get_columns("task_completion_records")
                }
            )
            indexes = await conn.run_sync(
                lambda sync: {
                    index["name"]
                    for index in sa.inspect(sync).get_indexes("task_completion_records")
                }
            )
        assert set(columns) == {
            "id", "task_id", "outcome", "work_outcome", "failure_class",
            "changes", "verification", "tests", "commands", "branch", "commits",
            "pr_url", "summary", "notes", "completed_at", "deliverables",
        }
        assert columns["task_id"]["nullable"] is False
        assert "idx_task_completion_records_task_time" in indexes
        async with database._engine.connect() as conn:
            for table in ("tasks", "archived_tasks"):
                legacy = await conn.run_sync(
                    lambda sync, table=table: next(
                        col for col in sa.inspect(sync).get_columns(table)
                        if col["name"] == "legacy_completion_id"
                    )
                )
                assert legacy["nullable"] is False
                assert "gen_random_uuid()" in legacy["default"]
    finally:
        await database.close()


async def test_legacy_identity_upgrade_preserves_old_archived_locator() -> None:
    import hashlib
    import json
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision = import_module("migrations.versions.a00000000062_legacy_completion_identity")
    database = Database(lease_dsn("legacy-archived-identity"))
    await database.initialize()
    try:
        def exercise(conn):
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()
                revision.downgrade()
                conn.execute(sa.text("""
                    INSERT INTO archived_tasks
                        (id, project_id, repo_id, title, description, status,
                         created_at, updated_at, archived_at)
                    VALUES ('gone', 'p', 'r', 'Gone', '', 'COMPLETED', 1, 2, 3)
                """))
                revision.upgrade()
                identity = conn.execute(sa.text(
                    "SELECT legacy_completion_id FROM archived_tasks WHERE id = 'gone'"
                )).scalar_one()
                expected = "legacy:" + hashlib.sha256(json.dumps(
                    ["p", "r", "gone", 2.0], separators=(",", ":"),
                ).encode()).hexdigest()
                assert identity == expected
                revision.upgrade()
                assert conn.execute(sa.text(
                    "SELECT legacy_completion_id FROM archived_tasks WHERE id = 'gone'"
                )).scalar_one() == identity
        async with database._engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await database.close()
