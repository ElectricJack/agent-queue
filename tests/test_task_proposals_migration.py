"""Migration + schema test for task_proposals."""
from __future__ import annotations

from sqlalchemy import inspect

from src.database import Database
from tests.db_fixtures import lease_dsn

from src.database.tables import metadata, task_proposals


def test_table_registered_on_metadata():
    assert "task_proposals" in metadata.tables


def test_columns_and_check_constraint():
    cols = {c.name: c for c in task_proposals.columns}
    assert set(cols) == {
        "id",
        "project_id",
        "source",
        "payload",
        "status",
        "created_at",
        "updated_at",
    }
    assert cols["id"].primary_key is True
    assert cols["project_id"].nullable is False
    assert cols["source"].nullable is False
    assert cols["payload"].nullable is False
    assert cols["status"].nullable is False
    # status CHECK constraint present.
    check_names = [
        c.name for c in task_proposals.constraints if getattr(c, "name", None)
    ]
    assert any("status" in (n or "") for n in check_names)


async def test_baseline_creates_task_proposals():
    """The squashed PostgreSQL baseline retains proposal status enforcement."""
    database = Database(lease_dsn("task-proposals-schema"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync: inspect(sync).get_columns("task_proposals")
            )
            checks = await conn.run_sync(
                lambda sync: inspect(sync).get_check_constraints("task_proposals")
            )
        assert {col["name"] for col in columns} == set(task_proposals.c.keys())
        assert any("status" in check["sqltext"] for check in checks)
    finally:
        await database.close()
