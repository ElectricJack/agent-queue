"""Revision ``d4e5f6a7b8c9`` — the two ``use_alter`` foreign keys.

The baseline migration declared ``agents.current_task_id -> tasks.id`` and
``tasks.preferred_workspace_id -> workspaces.id`` inside ``op.create_table``.
Alembic never emits ``use_alter`` constraints from a ``create_table``, so on
PostgreSQL both were silently missing and autogenerate kept reporting them.

These tests pin the two properties the revision promises:

* dangling references are nullified *before* the constraint is created (a
  pre-existing orphan row must not make the upgrade fail);
* after ``upgrade head`` the constraints exist on PostgreSQL.

The SQLite branch always runs.  The PostgreSQL branch runs only when
``POSTGRES_TEST_DSN`` is set, and builds its own throwaway database so the
alembic chain never touches a database another suite is using.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

#: The revision just before the foreign keys are added.
PRE_REVISION = "c3d4e5f6a7b8"
FK_REVISION = "d4e5f6a7b8c9"

AGENTS_FK = "fk_agents_current_task"
TASKS_FK = "fk_tasks_preferred_workspace"


def _alembic_config(async_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


def _seed_orphan_agent(sync_url: str) -> None:
    """Insert an agent pointing at a task id that does not exist."""
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents (id, name, profile_id, state, current_task_id, "
                    "total_tokens_used, session_tokens_used, created_at) "
                    "VALUES ('a1', 'orphan', 'worker', 'IDLE', 'no-such-task', 0, 0, 1.0)"
                )
            )
    finally:
        engine.dispose()


def _current_task_id(sync_url: str, agent_id: str = "a1"):
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.text("SELECT current_task_id FROM agents WHERE id = :i"), {"i": agent_id}
            ).scalar_one()
    finally:
        engine.dispose()


# ─────────────────────────────── SQLite ──────────────────────────────────


def test_orphan_current_task_id_is_nullified_sqlite(tmp_path: Path):
    """An agent referencing a missing task survives the upgrade, with NULL."""
    db_path = tmp_path / "fk.db"
    cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
    sync_url = f"sqlite:///{db_path}"

    command.upgrade(cfg, PRE_REVISION)
    _seed_orphan_agent(sync_url)
    assert _current_task_id(sync_url) == "no-such-task"

    command.upgrade(cfg, "head")

    assert _current_task_id(sync_url) is None


def test_upgrade_head_is_clean_sqlite(tmp_path: Path):
    """A full chain to head still works with no rows to repair."""
    db_path = tmp_path / "clean.db"
    cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            agent_fks = inspector.get_foreign_keys("agents")
            assert any(
                fk.get("referred_table") == "tasks"
                and list(fk.get("constrained_columns") or []) == ["current_task_id"]
                for fk in agent_fks
            )
            task_fks = inspector.get_foreign_keys("tasks")
            assert any(
                fk.get("referred_table") == "workspaces"
                and list(fk.get("constrained_columns") or []) == ["preferred_workspace_id"]
                for fk in task_fks
            )
    finally:
        engine.dispose()


# ───────────────────────────── PostgreSQL ────────────────────────────────


def _worker_suffix() -> str:
    raw = os.environ.get("PYTEST_XDIST_WORKER", "master")
    return re.sub(r"[^a-zA-Z0-9_]", "_", raw) or "master"


async def _recreate_database(base_dsn: str, target_db: str) -> None:
    import asyncpg

    prefix, _, dbname = base_dsn.rpartition("/")
    admin_dsn = f"{prefix}/{dbname}".replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{target_db}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()


async def _drop_database(base_dsn: str, target_db: str) -> None:
    import asyncpg

    prefix, _, dbname = base_dsn.rpartition("/")
    admin_dsn = f"{prefix}/{dbname}".replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{target_db}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture
def pg_migration_dsn():
    """A throwaway, empty PostgreSQL database for one alembic chain run."""
    base = os.environ.get("POSTGRES_TEST_DSN")
    if not base:
        pytest.skip("POSTGRES_TEST_DSN not set")
    # Its own database, never one of the suites' — this fixture drops it.
    target_db = f"aq_fkmig_{_worker_suffix()}"
    prefix, _, _ = base.rpartition("/")
    asyncio.run(_recreate_database(base, target_db))
    try:
        yield f"{prefix}/{target_db}"
    finally:
        asyncio.run(_drop_database(base, target_db))


async def _pg_fetchval(dsn: str, sql: str, *args):
    """One-shot query over raw asyncpg.

    Only ``asyncpg`` is installed — there is no sync PostgreSQL driver in
    this environment, so the SQLAlchemy ``create_engine`` helpers above
    cannot be reused for the Postgres branch.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _pg_execute(dsn: str, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def test_orphan_current_task_id_is_nullified_postgres(pg_migration_dsn: str):
    cfg = _alembic_config(pg_migration_dsn)

    command.upgrade(cfg, PRE_REVISION)
    asyncio.run(
        _pg_execute(
            pg_migration_dsn,
            "INSERT INTO agents (id, name, profile_id, state, current_task_id, "
            "total_tokens_used, session_tokens_used, created_at) "
            "VALUES ('a1', 'orphan', 'worker', 'IDLE', 'no-such-task', 0, 0, 1.0)",
        )
    )
    assert (
        asyncio.run(
            _pg_fetchval(pg_migration_dsn, "SELECT current_task_id FROM agents WHERE id = 'a1'")
        )
        == "no-such-task"
    )

    command.upgrade(cfg, "head")

    assert (
        asyncio.run(
            _pg_fetchval(pg_migration_dsn, "SELECT current_task_id FROM agents WHERE id = 'a1'")
        )
        is None
    )


@pytest.mark.parametrize("constraint,table", [(AGENTS_FK, "agents"), (TASKS_FK, "tasks")])
def test_named_foreign_keys_exist_postgres(pg_migration_dsn: str, constraint: str, table: str):
    command.upgrade(_alembic_config(pg_migration_dsn), "head")

    found = asyncio.run(
        _pg_fetchval(
            pg_migration_dsn,
            # ``confdeltype`` is a ``"char"`` column; asyncpg hands those back
            # as bytes, so cast in SQL rather than comparing to b"n".
            "SELECT confdeltype::text FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            f"WHERE c.conname = '{constraint}' AND t.relname = '{table}'",
        )
    )
    assert found == "n", f"{constraint} missing or not ON DELETE SET NULL (got {found!r})"
