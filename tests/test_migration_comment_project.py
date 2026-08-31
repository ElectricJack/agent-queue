"""Backfill comment ownership only where historical task IDs are unambiguous."""
import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
from tests.test_migration_task_comments import upgrade


def test_comment_project_migration_preserves_ambiguous_history(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'comment-project.db'}")
    try:
        upgrade(engine, "e8b39a10c572")
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1),('other','Other',1)"))
            for table, rows in (
                ("tasks", [("active", "p"), ("same", "p"), ("collision", "other")]),
                ("archived_tasks", [("archived", "p"), ("same", "p"), ("collision", "p")]),
            ):
                for tid, pid in rows:
                    extra_columns = ",archived_at" if table == "archived_tasks" else ""
                    extra_values = ",2" if table == "archived_tasks" else ""
                    conn.execute(sa.text(f"INSERT INTO {table}(id,project_id,title,description,status,created_at,updated_at{extra_columns}) VALUES (:tid,:pid,'Task','','COMPLETED',1,1{extra_values})"), {"tid": tid, "pid": pid})
            for tid in ("active", "archived", "same", "collision", "orphan"):
                conn.execute(sa.text("INSERT INTO task_comments(id,task_id,body,author_kind,author_id,created_at) VALUES (:tid,:tid,'Keep','user','local',3)"), {"tid": tid})
        upgrade(engine, "head")
        upgrade(engine, "head")
        with engine.connect() as conn:
            rows = dict(conn.execute(sa.text("SELECT task_id,project_id FROM task_comments")).all())
            assert rows == {"active": "p", "archived": "p", "same": "p", "collision": None, "orphan": None}
            assert conn.execute(sa.text("SELECT count(*) FROM task_comments WHERE body='Keep'")).scalar_one() == 5
    finally:
        engine.dispose()


def test_comment_project_backfill_on_postgres():
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN not set")
    # Separate from the parity fixtures' resettable database, never a daemon DB.
    database = "aq_comment_migration_" + uuid.uuid4().hex

    async def check():
        import asyncpg
        from sqlalchemy.ext.asyncio import create_async_engine
        from alembic import command
        from alembic.config import Config

        admin = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
        await admin.execute(f'CREATE DATABASE "{database}"')
        engine = create_async_engine(dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + "/" + database)
        try:
            def exercise(conn):
                cfg = Config("alembic.ini")
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "e8b39a10c572")
                conn.commit()
                conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1),('other','Other',1)"))
                conn.execute(sa.text("INSERT INTO tasks(id,project_id,title,description,status,created_at,updated_at) VALUES ('known','p','Task','','READY',1,1),('collision','other','Task','','READY',1,1)"))
                conn.execute(sa.text("INSERT INTO archived_tasks(id,project_id,title,description,status,created_at,updated_at,archived_at) VALUES ('collision','p','Task','','COMPLETED',1,1,2)"))
                conn.execute(sa.text("INSERT INTO task_comments(id,task_id,body,author_kind,author_id,created_at) VALUES ('known','known','Keep','user','local',3),('collision','collision','Private','user','local',3)"))
                conn.commit()
                command.upgrade(cfg, "head")
                conn.commit()
                assert dict(conn.execute(sa.text("SELECT id,project_id FROM task_comments")).all()) == {"known": "p", "collision": None}
            async with engine.connect() as conn:
                await conn.run_sync(exercise)
        finally:
            await engine.dispose()
            await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()
    asyncio.run(check())
