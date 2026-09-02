"""The desired_state backfill.

The column's server_default is ``running``, which is right for a live row
and wrong for every dead one already in the table.  Without the backfill the
first reconciler tick after the runtime is enabled would read a graveyard of
stopped sessions as "wanted" and try to start them all.

Spec: docs/superpowers/specs/2026-08-27-session-desired-state-design.md
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

DESIRED_STATE_REVISION = "4e925610d7a6"
PRIOR_REVISION = "2ea52ac3da6c"

pytestmark = pytest.mark.migration


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _insert_session(conn, sid: str, state: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO sessions (id, project_id, profile_id, harness, provider, "
            "name, lifecycle, state, work_dir, epoch, instance_token, started_at) "
            "VALUES (:id, 'p1', 'pr', 'claude', 'tmux', :name, 'named', :state, "
            "'/wd', 'e', 'tok', 1.0)"
        ),
        {"id": sid, "name": f"n-{sid}", "state": state},
    )


def test_backfill_maps_observed_state_to_intent():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        sync_url = f"sqlite:///{db_path}"

        command.upgrade(cfg, PRIOR_REVISION)

        engine = create_engine(sync_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO projects (id, name, created_at) VALUES ('p1', 'P1', 1.0)")
            )
            for sid, state in (
                ("live", "running"),
                ("starting", "starting"),
                ("gone", "stopped"),
                ("bad", "quarantined"),
                ("napping", "sleeping"),
            ):
                _insert_session(conn, sid, state)

        command.upgrade(cfg, DESIRED_STATE_REVISION)

        with engine.connect() as conn:
            got = dict(
                conn.execute(
                    sa.text("SELECT id, desired_state FROM sessions")
                ).fetchall()
            )
        assert got == {
            "live": "running",
            "starting": "running",
            # History is not wanted back.
            "gone": "stopped",
            "bad": "stopped",
            "napping": "sleeping",
        }
        engine.dispose()


def test_downgrade_drops_the_column():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        command.upgrade(cfg, DESIRED_STATE_REVISION)
        command.downgrade(cfg, PRIOR_REVISION)

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            cols = {
                r[1]
                for r in conn.execute(sa.text("PRAGMA table_info(sessions)")).fetchall()
            }
        assert "desired_state" not in cols
        engine.dispose()
