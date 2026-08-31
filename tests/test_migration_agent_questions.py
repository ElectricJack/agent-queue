"""Question persistence migration keeps live task/session assignments intact."""
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def upgrade(engine, revision):
    with engine.connect() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
        conn.commit()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_question_migration_preserves_current_worker_and_task(tmp_path, foreign_keys):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'questions-migration.db'}")
    if foreign_keys:
        @sa.event.listens_for(engine, "connect")
        def enable_fk(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")
    try:
        upgrade(engine, "e7a2b9c41d05")
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
            conn.execute(sa.text("INSERT INTO agents(id,name,profile_id,state,created_at) VALUES ('a','Sol','worker','BUSY',1)"))
            conn.execute(sa.text("INSERT INTO tasks(id,project_id,title,description,status,assigned_agent_id,claim_epoch,created_at,updated_at) VALUES ('t','p','Work','Keep','IN_PROGRESS','a',7,1,1)"))
            conn.execute(sa.text("UPDATE agents SET current_task_id='t' WHERE id='a'"))
            conn.execute(sa.text("INSERT INTO sessions(id,task_id,project_id,agent_id,profile_id,harness,provider,name,lifecycle,work_dir,epoch,instance_token,started_at,state,desired_state) VALUES ('s','t','p','a','worker','codex','tmux','s-t','task','/work','e','tok',1,'running','running')"))
            conn.execute(sa.text("INSERT INTO task_metadata(task_id,key,value) VALUES ('t','stall_nudges','3')"))
        upgrade(engine, "head")
        upgrade(engine, "head")  # normal restart must be idempotent
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT status,assigned_agent_id,claim_epoch FROM tasks WHERE id='t'")).one() == ("IN_PROGRESS", "a", 7)
            assert conn.execute(sa.text("SELECT current_task_id,state FROM agents WHERE id='a'")).one() == ("t", "BUSY")
            assert conn.execute(sa.text("SELECT id,task_id,instance_token,state FROM sessions")).one() == ("s", "t", "tok", "running")
            assert conn.execute(sa.text("SELECT value FROM task_metadata WHERE task_id='t' AND key='stall_nudges'")).scalar_one() == "3"
            assert conn.execute(sa.text("SELECT count(*) FROM agent_questions")).scalar_one() == 0
            assert conn.execute(sa.text("SELECT count(*) FROM message_discord_receipts")).scalar_one() == 0
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()
