# tests/test_hierarchy_migration.py
"""Revisions A (DDL) and B (canonicalise) — spec §17."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DB_URL=f"sqlite+aiosqlite:///{db_path}")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "mig.db")


class TestRevisionA:
    def test_upgrade_adds_columns_and_table(self, db_path):
        res = _alembic(db_path, "upgrade", "a1b2c3d4e5f6")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        task_cols = {c["name"] for c in insp.get_columns("tasks")}
        assert {"next_child_ordinal", "created_by_kind", "created_by_id", "claim_epoch",
                "filed_count"} <= task_cols
        sess_cols = {c["name"] for c in insp.get_columns("sessions")}
        assert {"claims", "agent_id", "claim_phase", "claim_phase_at",
                "last_claim_epoch", "last_claim_result"} <= sess_cols
        prof_cols = {c["name"] for c in insp.get_columns("agent_profiles")}
        assert {"min_active", "max_active", "max_claims_per_session"} <= prof_cols
        assert "hierarchy_migration_rejects" in insp.get_table_names()
        idx = {i["name"] for i in insp.get_indexes("tasks")}
        assert "idx_tasks_ready_by_profile" in idx

    def test_downgrade_round_trips(self, db_path):
        assert _alembic(db_path, "upgrade", "a1b2c3d4e5f6").returncode == 0
        res = _alembic(db_path, "downgrade", "-1")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        assert "next_child_ordinal" not in {c["name"] for c in insp.get_columns("tasks")}
        assert "hierarchy_migration_rejects" not in insp.get_table_names()
