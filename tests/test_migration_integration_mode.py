"""The integration_mode migration: backfill, preflight, and rollback.

Revision ``c4d5e6f7a8b9`` replaces ``requires_approval``/``auto_approve_plan``
with ``integration_mode`` on active and archived tasks (plus a project-level
policy column).  These tests drive a real SQLite database through alembic:

* backfill maps ``requires_approval`` 1 → ``pull_request``, 0 → ``direct``
  on BOTH tables, and drops the legacy columns;
* the preflight refuses to upgrade while any active task sits in a retired
  AWAITING_* status, and names the rows with remediation SQL;
* downgrade restores ``requires_approval`` from the mode.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

INTEGRATION_MODE_REVISION = "c4d5e6f7a8b9"
PRIOR_REVISION = "f1d7a9c20b64"

pytestmark = pytest.mark.migration


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _seed(conn, *, statuses=None) -> None:
    now = time.time()
    conn.execute(
        sa.text("INSERT INTO projects (id, name, created_at) VALUES ('p-im', 'im', :now)"),
        {"now": now},
    )
    rows = [
        ("t-pr", 1, "READY"),
        ("t-direct", 0, "READY"),
    ]
    for extra_id, status in (statuses or {}).items():
        rows.append((extra_id, 1, status))
    for tid, approval, status in rows:
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, project_id, title, description, priority,"
                " status, verification_type, retry_count, max_retries,"
                " requires_approval, is_plan_subtask, auto_approve_plan, attachments,"
                " created_at, updated_at)"
                " VALUES (:id, 'p-im', :id, '', 100, :status, 'auto_test', 0, 3,"
                " :approval, 0, 0, '[]', :now, :now)"
            ),
            {"id": tid, "approval": approval, "status": status, "now": now},
        )
    for tid, approval in (("a-pr", 1), ("a-direct", 0)):
        conn.execute(
            sa.text(
                "INSERT INTO archived_tasks (id, project_id, title, description,"
                " priority, status, verification_type, retry_count, max_retries,"
                " requires_approval, is_plan_subtask, auto_approve_plan, attachments,"
                " created_at, updated_at, archived_at)"
                " VALUES (:id, 'p-im', :id, '', 100, 'COMPLETED', 'auto_test', 0, 3,"
                " :approval, 0, 0, '[]', :now, :now, :now)"
            ),
            {"id": tid, "approval": approval, "now": now},
        )


def _fresh_db(tmp_path: Path, *, statuses=None):
    db_path = tmp_path / "mig.db"
    url = f"sqlite:///{db_path}"
    cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(cfg, PRIOR_REVISION)
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn, statuses=statuses)
    return url, cfg, engine


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def test_upgrade_backfills_modes_and_drops_flags(tmp_path):
    url, cfg, engine = _fresh_db(tmp_path)
    command.upgrade(cfg, INTEGRATION_MODE_REVISION)

    with engine.connect() as conn:
        active = dict(
            conn.execute(sa.text("SELECT id, integration_mode FROM tasks")).fetchall()
        )
        archived = dict(
            conn.execute(
                sa.text("SELECT id, integration_mode FROM archived_tasks")
            ).fetchall()
        )
    assert active == {"t-pr": "pull_request", "t-direct": "direct"}
    assert archived == {"a-pr": "pull_request", "a-direct": "direct"}

    for table in ("tasks", "archived_tasks"):
        cols = _columns(engine, table)
        assert "integration_mode" in cols
        assert "requires_approval" not in cols
        assert "auto_approve_plan" not in cols
    assert "integration_mode" in _columns(engine, "projects")


@pytest.mark.parametrize("status", ["AWAITING_APPROVAL", "AWAITING_PLAN_APPROVAL"])
def test_preflight_refuses_stranded_approval_rows(tmp_path, status):
    url, cfg, engine = _fresh_db(tmp_path, statuses={"t-stranded": status})

    with pytest.raises(Exception) as excinfo:
        command.upgrade(cfg, INTEGRATION_MODE_REVISION)
    msg = str(excinfo.value)
    assert "t-stranded" in msg
    assert "UPDATE tasks SET status=" in msg  # exact remediation is included

    # The upgrade must not have applied: legacy columns intact, no new column.
    cols = _columns(engine, "tasks")
    assert "requires_approval" in cols
    assert "integration_mode" not in cols


def test_downgrade_restores_requires_approval(tmp_path):
    url, cfg, engine = _fresh_db(tmp_path)
    command.upgrade(cfg, INTEGRATION_MODE_REVISION)
    command.downgrade(cfg, PRIOR_REVISION)

    with engine.connect() as conn:
        active = dict(
            conn.execute(sa.text("SELECT id, requires_approval FROM tasks")).fetchall()
        )
        archived = dict(
            conn.execute(
                sa.text("SELECT id, requires_approval FROM archived_tasks")
            ).fetchall()
        )
    assert active == {"t-pr": 1, "t-direct": 0}
    assert archived == {"a-pr": 1, "a-direct": 0}
    for table in ("tasks", "archived_tasks", "projects"):
        assert "integration_mode" not in _columns(engine, table)
