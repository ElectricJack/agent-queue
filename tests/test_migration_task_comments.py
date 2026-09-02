"""Additive comments migration retains tasks, legacy notes and claim state."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.migration


def upgrade(engine, revision):
    with engine.connect() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
        conn.commit()


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_comments_migration_preserves_notes_and_assignment(tmp_path, foreign_keys):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'comments-migration.db'}")
    if foreign_keys:

        @sa.event.listens_for(engine, "connect")
        def enable_fk(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

    try:
        upgrade(engine, "f81a93bd0264")
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
            conn.execute(
                sa.text(
                    "INSERT INTO agents(id,name,profile_id,state,created_at) VALUES ('a','Worker','worker','BUSY',1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO tasks(id,project_id,title,description,status,assigned_agent_id,claim_epoch,created_at,updated_at) VALUES ('t','p','Work','Original requirements','IN_PROGRESS','a',7,1,1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_context(id,task_id,type,label,content) VALUES ('note','t','note','Old note','Keep this note')"
                )
            )
        upgrade(engine, "head")
        upgrade(engine, "head")
        with engine.begin() as conn:
            assert conn.execute(
                sa.text(
                    "SELECT description,status,assigned_agent_id,claim_epoch FROM tasks WHERE id='t'"
                )
            ).one() == ("Original requirements", "IN_PROGRESS", "a", 7)
            assert (
                conn.execute(
                    sa.text("SELECT content FROM task_context WHERE id='note'")
                ).scalar_one()
                == "Keep this note"
            )
            assert conn.execute(sa.text("SELECT count(*) FROM task_comments")).scalar_one() == 0
            conn.execute(
                sa.text(
                    "INSERT INTO task_comments(id,task_id,body,author_kind,author_id,created_at) VALUES ('c','t','Finding','user','local',2)"
                )
            )
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("prior_revision, existing_table", [
    ("7ac492b83fd1", "task_comments"),
    ("c8f4a1d2e6b9", "task_completion_records"),
])
def test_merged_head_upgrades_from_either_deployed_branch(tmp_path, prior_revision, existing_table):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'merged-migration.db'}")
    try:
        upgrade(engine, prior_revision)
        with engine.begin() as conn:
            if existing_table == "task_comments":
                conn.execute(sa.text("INSERT INTO task_comments VALUES ('c','t','Finding','user','local',1)"))
            else:
                conn.execute(sa.text("INSERT INTO task_completion_records(id,task_id,outcome,completed_at) VALUES ('c','t','pass',1)"))
        upgrade(engine, "head")
        upgrade(engine, "head")
        with engine.connect() as conn:
            assert {"task_comments", "task_completion_records"} <= set(sa.inspect(conn).get_table_names())
            assert conn.execute(sa.text(f"SELECT count(*) FROM {existing_table}")).scalar_one() == 1
            assert conn.execute(sa.text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
    finally:
        engine.dispose()
