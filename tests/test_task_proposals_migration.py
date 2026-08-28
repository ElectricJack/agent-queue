"""Migration + schema test for task_proposals."""
from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import create_engine, inspect

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


#: migrations/versions/5ba6efdd01d0_add_task_proposals_table.py and its parent.
#: Pinned explicitly rather than using ``head``/``-1``: this test used to
#: upgrade to head and downgrade one step, which only tested *this* migration
#: while it happened to be head.  Every migration merged after it silently
#: turned the downgrade into a test of someone else's revision, and the
#: assertion below had been failing ever since.
TASK_PROPOSALS_REVISION = "5ba6efdd01d0"
PRIOR_REVISION = "c17d35836ed3"


def test_migration_creates_and_drops(tmp_path):
    """Round-trip: upgrade to the task_proposals revision, then back off it."""
    db_path = tmp_path / "aq.db"
    env = {
        **os.environ,
        "AGENT_QUEUE_DB_URL": f"sqlite+aiosqlite:///{db_path}",
    }
    subprocess.run(
        ["python3", "-m", "alembic", "upgrade", TASK_PROPOSALS_REVISION],
        check=True,
        env=env,
    )

    eng = create_engine(f"sqlite:///{db_path}")
    insp = inspect(eng)
    assert "task_proposals" in insp.get_table_names()
    eng.dispose()

    subprocess.run(
        ["python3", "-m", "alembic", "downgrade", PRIOR_REVISION],
        check=True,
        env=env,
    )
    eng = create_engine(f"sqlite:///{db_path}")
    insp = inspect(eng)
    assert "task_proposals" not in insp.get_table_names()
    eng.dispose()
