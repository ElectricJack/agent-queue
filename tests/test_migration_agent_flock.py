"""Flock migration adds settings without rewriting existing worker identities."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def migrate(engine, target, downgrade=False):
    with engine.connect() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        (command.downgrade if downgrade else command.upgrade)(cfg, target)
        conn.commit()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_flock_migration_preserves_workers_assignments_and_history(tmp_path, foreign_keys):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    if foreign_keys:

        @sa.event.listens_for(engine, "connect")
        def enable_foreign_keys(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

    try:
        migrate(engine, "5f37c424acde")
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
            conn.execute(
                sa.text(
                    "INSERT INTO agents(id,name,profile_id,state,created_at) VALUES ('a','Alice','old-profile','BUSY',1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO tasks(id,project_id,title,description,status,assigned_agent_id,created_at,updated_at) VALUES ('t','p','Task','Keep','IN_PROGRESS','a',1,1)"
                )
            )
            conn.execute(sa.text("UPDATE agents SET current_task_id='t' WHERE id='a'"))
            conn.execute(
                sa.text(
                    "INSERT INTO workspaces(id,project_id,workspace_path,source_type,name,locked_by_agent_id,locked_by_task_id,created_at) VALUES ('w','p','/work','link','main','a','t',1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO sessions(id,task_id,project_id,profile_id,harness,provider,name,lifecycle,work_dir,epoch,instance_token,started_at) VALUES ('s','t','p','old-profile','claude','tmux','s-t','task','/work','e','tok',1)"
                )
            )
        migrate(engine, "head")
        with engine.connect() as conn:
            agent = conn.execute(sa.text("SELECT * FROM agents WHERE id='a'")).mappings().one()
            assert agent["name"] == "Alice" and agent["current_task_id"] == "t"
            assert agent["role"] == "worker" and agent["enabled"]
            assert agent["profile_id"] == "old-profile"
            assert (
                agent["harness"] is None
                and agent["model"] is None
                and agent["intelligence_class"] is None
            )
            record = conn.execute(sa.text("SELECT * FROM sessions WHERE id='s'")).mappings().one()
            assert record["agent_id"] == "a"
            assert record["llm_provider"] is None and record["model"] is None
            assert record["intelligence_class"] is None
            assert record["project_id"] == "p" and record["instance_token"] == "tok"
        migrate(engine, "5f37c424acde", downgrade=True)
        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT name,current_task_id FROM agents WHERE id='a'")
            ).one() == ("Alice", "t")
            assert conn.execute(sa.text("SELECT id,agent_id FROM sessions")).one() == ("s", "a")
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == int(foreign_keys)
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute(
                sa.text("SELECT locked_by_agent_id,locked_by_task_id FROM workspaces")
            ).one() == ("a", "t")
    finally:
        engine.dispose()
