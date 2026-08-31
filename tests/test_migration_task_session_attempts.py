"""Conservative legacy history import on a disposable database."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

PRIOR = "d37b821a6f04"
REVISION = "e8b39a10c572"


def test_legacy_backfill_is_conservative_and_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, PRIOR)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1.0)"))
        conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, description, created_at, updated_at) VALUES ('t', 'p', 'T', '', 1.0, 2.0)"
            )
        )
        for sid, state in (("old", "stopped"), ("live", "running")):
            conn.execute(
                text("""INSERT INTO sessions
                (id, task_id, project_id, profile_id, harness, provider, name,
                 lifecycle, state, work_dir, epoch, instance_token, started_at,
                 last_activity, model, session_key)
                VALUES (:sid, 't', 'p', 'profile', 'codex', 'fake', :sid,
                        'task', :state, '/old', 'e', 'i', 10.0, 90.0, 'legacy-model', 'actual-key')"""),
                {"sid": sid, "state": state},
            )
    command.upgrade(config, REVISION)
    command.upgrade(config, REVISION)
    with engine.connect() as conn:
        rows = (
            conn.execute(text("SELECT * FROM task_session_attempts ORDER BY session_id"))
            .mappings()
            .all()
        )
        assert len(rows) == 2
        assert {r["session_id"] for r in rows} == {"old", "live"}
        for row in rows:
            assert row["started_at"] == 10.0
            assert row["ended_at"] is None
            assert row["outcome"] is None and row["end_reason"] is None
            assert row["model"] == "legacy-model" and row["session_key"] == "actual-key"
        assert conn.execute(text("SELECT ended_at FROM sessions WHERE id='old'")).scalar() is None
    command.downgrade(config, PRIOR)
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "task_session_attempts" not in names
    engine.dispose()


def test_legacy_import_rejects_old_incarnation_and_wrong_project_but_keeps_pool_claim(tmp_path):
    path = tmp_path / "incarnation.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, PRIOR)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        for pid in ("p", "other"):
            conn.execute(
                text("INSERT INTO projects (id, name, created_at) VALUES (:pid, :pid, 1)"),
                {"pid": pid},
            )
        conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, description, created_at, updated_at) VALUES ('t', 'p', 'Current', '', 100, 100)"
            )
        )
        for sid, pid, start, lifecycle, claimed in (
            ("ancient", "p", 10.0, "task", None),
            ("foreign", "other", 110.0, "task", None),
            ("current", "p", 110.0, "task", None),
            ("pool", "p", 10.0, "pool", 120.0),
        ):
            conn.execute(
                text("""INSERT INTO sessions
                (id, task_id, project_id, profile_id, harness, provider, name,
                 lifecycle, state, work_dir, epoch, instance_token, started_at,
                 claim_phase_at, sleep_reason)
                VALUES (:sid, 't', :pid, 'profile', 'codex', 'fake', :sid,
                        :lifecycle, 'stopped', '/workspace', 'e', 'i', :start,
                        :claimed, 'restart_budget_exhausted')"""),
                dict(sid=sid, pid=pid, start=start, lifecycle=lifecycle, claimed=claimed),
            )
    command.upgrade(config, REVISION)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM task_session_attempts")).mappings().all()
        assert {r["session_id"] for r in rows} == {"current", "pool"}
        for row in rows:
            assert row["end_reason"] == "restart_budget_exhausted"
            assert row["ended_at"] is None and row["outcome"] is None
        assert (
            conn.execute(text("SELECT end_reason FROM sessions WHERE id='current'")).scalar()
            == "restart_budget_exhausted"
        )
    engine.dispose()
