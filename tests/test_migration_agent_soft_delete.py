"""Soft deletion is additive and rollback preserves referenced history."""

import pytest
import sqlalchemy as sa
from tests.test_migration_agent_flock import migrate

pytestmark = pytest.mark.migration


def test_soft_delete_migration_preserves_references_with_foreign_keys(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'delete-migration.db'}")

    @sa.event.listens_for(engine, "connect")
    def foreign_keys(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    try:
        migrate(engine, "9b5e9f057e6e")
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO projects(id,name,created_at) VALUES('p','P',1)")
            conn.exec_driver_sql(
                "INSERT INTO agents(id,name,profile_id,state,created_at) VALUES('a','Keeper','worker','IDLE',1)"
            )
            conn.exec_driver_sql(
                "INSERT INTO tasks(id,project_id,title,description,status,assigned_agent_id,created_at,updated_at) VALUES('t','p','Done','history','COMPLETED','a',1,1)"
            )
            conn.exec_driver_sql(
                "INSERT INTO sessions(id,task_id,agent_id,project_id,profile_id,harness,provider,name,lifecycle,state,work_dir,epoch,instance_token,started_at) VALUES('s','t','a','p','worker','claude','fake','s-t','task','stopped','/work','e','token',1)"
            )
        migrate(engine, "head")
        with engine.begin() as conn:
            assert (
                conn.exec_driver_sql("SELECT deleted_at FROM agents WHERE id='a'").scalar_one()
                is None
            )
            conn.exec_driver_sql("UPDATE agents SET deleted_at=123,enabled=0 WHERE id='a'")
        migrate(engine, "9b5e9f057e6e", downgrade=True)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT id,name,enabled FROM agents").one() == (
                "a",
                "Keeper",
                0,
            )
            assert conn.exec_driver_sql("SELECT assigned_agent_id FROM tasks").scalar_one() == "a"
            assert conn.exec_driver_sql("SELECT agent_id FROM sessions").scalar_one() == "a"
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
