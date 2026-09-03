"""``agents.origin`` backfill: only rows that pools alone ever ran become pool-origin."""

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa

from tests.test_migration_task_comments import upgrade

pytestmark = pytest.mark.migration

PREVIOUS = "e6a1b2c3d4f5"

_SESSION = (
    "INSERT INTO sessions(id,project_id,profile_id,harness,provider,name,lifecycle,"
    "work_dir,epoch,instance_token,started_at,state,desired_state,agent_id) "
    "VALUES (:id,'p','worker','claude','tmux',:id,:lifecycle,'/w','e',:id,1,'stopped',"
    "'running',:agent)"
)


def test_agent_origin_backfill_marks_only_pool_only_rows(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'agent-origin.db'}")
    try:
        upgrade(engine, PREVIOUS)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
            for agent in ("idle-manual", "pool-only", "pool-and-task", "task-only"):
                conn.execute(sa.text(
                    "INSERT INTO agents(id,name,profile_id,state,created_at) "
                    "VALUES (:id,:id,'worker','IDLE',1)"
                ), {"id": agent})
            for sid, lifecycle, agent in (
                ("s1", "pool", "pool-only"),
                ("s2", "pool", "pool-only"),
                ("s3", "pool", "pool-and-task"),
                ("s4", "task", "pool-and-task"),
                ("s5", "task", "task-only"),
                ("s6", "pool", None),
            ):
                conn.execute(sa.text(_SESSION), {"id": sid, "lifecycle": lifecycle, "agent": agent})
        upgrade(engine, "head")
        upgrade(engine, "head")
        with engine.connect() as conn:
            rows = dict(conn.execute(sa.text("SELECT id, origin FROM agents")).all())
        assert rows == {
            "idle-manual": "manual",
            "pool-only": "pool",
            "pool-and-task": "manual",
            "task-only": "manual",
        }
    finally:
        engine.dispose()


def test_agent_origin_backfill_on_postgres():
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN not set")
    # Separate from the parity fixtures' resettable database, never a daemon DB.
    database = "aq_agent_origin_" + uuid.uuid4().hex

    async def check():
        import asyncpg
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.ext.asyncio import create_async_engine

        admin = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
        await admin.execute(f'CREATE DATABASE "{database}"')
        engine = create_async_engine(
            dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
            + "/" + database
        )
        try:
            def exercise(conn):
                cfg = Config("alembic.ini")
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, PREVIOUS)
                conn.commit()
                conn.execute(sa.text("INSERT INTO projects(id,name,created_at) VALUES ('p','P',1)"))
                for agent in ("idle-manual", "pool-only", "pool-and-task"):
                    conn.execute(sa.text(
                        "INSERT INTO agents(id,name,profile_id,state,created_at) "
                        "VALUES (:id,:id,'worker','IDLE',1)"
                    ), {"id": agent})
                for sid, lifecycle, agent in (
                    ("s1", "pool", "pool-only"),
                    ("s3", "pool", "pool-and-task"),
                    ("s4", "task", "pool-and-task"),
                ):
                    conn.execute(sa.text(_SESSION), {"id": sid, "lifecycle": lifecycle, "agent": agent})
                conn.commit()
                command.upgrade(cfg, "head")
                conn.commit()
                assert dict(conn.execute(sa.text("SELECT id, origin FROM agents")).all()) == {
                    "idle-manual": "manual", "pool-only": "pool", "pool-and-task": "manual",
                }
                command.downgrade(cfg, PREVIOUS)
                conn.commit()
                columns = {row[0] for row in conn.execute(sa.text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='agents'"
                )).all()}
                assert "origin" not in columns
            async with engine.connect() as conn:
                await conn.run_sync(exercise)
        finally:
            await engine.dispose()
            await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()
    asyncio.run(check())
