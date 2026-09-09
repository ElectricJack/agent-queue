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
    finally:
        await database.close()
