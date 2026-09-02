"""Schema migration coverage for durable task completion records."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


COMPLETION_REVISION = "c8f4a1d2e6b9"
PRIOR_REVISION = "882b77dc8495"

pytestmark = pytest.mark.migration


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def test_upgrade_creates_completion_record_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "completion.db"
    command.upgrade(_alembic_config(db_path), COMPLETION_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        columns = {
            row[1]: row[3]
            for row in conn.execute(sa.text("PRAGMA table_info(task_completion_records)"))
        }
        indexes = {
            row[1]
            for row in conn.execute(sa.text("PRAGMA index_list(task_completion_records)"))
        }
    engine.dispose()

    assert set(columns) == {
        "id", "task_id", "outcome", "work_outcome", "failure_class",
        "changes", "verification", "tests", "commands", "branch", "commits",
        "pr_url", "summary", "notes", "completed_at",
    }
    assert columns["task_id"] == 1
    assert "idx_task_completion_records_task_time" in indexes


def test_downgrade_drops_completion_records(tmp_path: Path) -> None:
    db_path = tmp_path / "completion.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, COMPLETION_REVISION)
    command.downgrade(cfg, PRIOR_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        table = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='task_completion_records'"
            )
        ).scalar_one_or_none()
    engine.dispose()

    assert table is None
