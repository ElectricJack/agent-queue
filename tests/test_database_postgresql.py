"""Integration tests for the PostgreSQL database adapter.

These tests require a running PostgreSQL instance.  They are skipped
automatically when the ``POSTGRES_TEST_DSN`` environment variable is
not set.

To run locally::

    docker compose up -d
    POSTGRES_TEST_DSN=postgresql://agent_queue:agent_queue_dev@localhost:5533/agent_queue \
        pytest tests/test_database_postgresql.py -v
"""

from __future__ import annotations

import uuid

import pytest

from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from tests.pg_dsn import ensure_worker_postgres_dsn

#: Per-xdist-worker DSN (tests/pg_dsn.py) -- this suite's own database,
#: separate from tests/perf and tests/test_claim_queries.py's, so
#: concurrent truncates under ``-n auto`` don't race each other.
POSTGRES_DSN = ensure_worker_postgres_dsn() or ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set"),
]


def _uid() -> str:
    return str(uuid.uuid4())[:8]


@pytest.fixture
async def db():
    """Provide an initialized PostgreSQLDatabaseAdapter with a clean schema."""
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    adapter = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await adapter.initialize()

    yield adapter

    # Clean up all tables after each test (reverse FK order)
    if adapter._engine:
        from sqlalchemy import text

        async with adapter._engine.begin() as conn:
            await conn.execute(
                text(
                    "DO $$ DECLARE r RECORD; BEGIN "
                    "FOR r IN (SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename != 'alembic_version') LOOP "
                    "EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE'; "
                    "END LOOP; END $$;"
                )
            )

    await adapter.close()


async def _make_project(db, pid=None):
    pid = pid or f"p-{_uid()}"
    await db.create_project(Project(id=pid, name=f"project-{pid}"))
    return pid


async def _make_agent(db, aid=None):
    aid = aid or f"a-{_uid()}"
    await db.create_agent(Agent(id=aid, name=f"agent-{aid}", profile_id="claude"))
    return aid


async def _make_task(db, project_id, tid=None, **kwargs):
    tid = tid or f"t-{_uid()}"
    task = Task(
        id=tid,
        project_id=project_id,
        title=f"task-{tid}",
        description="test task",
        **kwargs,
    )
    await db.create_task(task)
    return tid


def _alembic_pg(dsn: str, *args: str):
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, AGENT_QUEUE_DB_URL=dsn)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_postgres_head_window_downgrade_reupgrade_preserves_and_transforms_data():
    """head -> below the hierarchy pair -> head on a real PostgreSQL database.

    Runs the actual alembic chain on a scratch database of this worker's
    own: the downgrade must remove the swarm DDL, and data seeded at the
    old schema (a column-only parent pointer) must survive the re-upgrade
    and come out canonicalised (edge row, container flag, partial unique
    index) — the same contract the SQLite twin asserts, but on the dialect
    whose DDL paths (no batch_alter rebuild) actually differ.
    """
    import asyncpg

    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("window")
    plain_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    assert _alembic_pg(dsn, "upgrade", "head").returncode == 0
    res = _alembic_pg(dsn, "downgrade", "4e925610d7a6")
    assert res.returncode == 0, res.stderr

    conn = await asyncpg.connect(plain_dsn)
    try:
        assert (
            await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='tasks' AND column_name='claim_epoch'"
            )
            is None
        )
        assert await conn.fetchval("SELECT to_regclass('hierarchy_migration_rejects')") is None
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('x','x',0)")
        await conn.execute(
            "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
            "status, created_at, updated_at) VALUES "
            "('p','x',NULL,'p','p','IN_PROGRESS',0,0), "
            "('c','x','p','c','c','READY',0,0)"
        )
    finally:
        await conn.close()

    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr
    conn = await asyncpg.connect(plain_dsn)
    try:
        assert (
            await conn.fetchval("SELECT parent_task_id FROM tasks WHERE id='c'") == "p"
        )
        assert (
            await conn.fetchval(
                "SELECT depends_on_task_id FROM task_dependencies "
                "WHERE task_id='c' AND dep_type='parent-child'"
            )
            == "p"
        )
        assert (
            await conn.fetchval(
                "SELECT value FROM task_metadata WHERE task_id='p' AND key='container'"
            )
            == "true"
        )
        assert (
            await conn.fetchval("SELECT to_regclass('uq_task_deps_single_parent')")
            is not None
        )
        assert (
            await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='tasks' AND column_name='claim_epoch'"
            )
            == 1
        )
    finally:
        await conn.close()


# --- Project CRUD ---


class TestProjectCRUD:
    async def test_create_and_get(self, db):
        pid = await _make_project(db)
        project = await db.get_project(pid)
        assert project is not None
        assert project.id == pid

    async def test_list_projects(self, db):
        await _make_project(db, "p1")
        await _make_project(db, "p2")
        projects = await db.list_projects()
        assert len(projects) >= 2

    async def test_update_project(self, db):
        pid = await _make_project(db)
        await db.update_project(pid, name="updated")
        project = await db.get_project(pid)
        assert project.name == "updated"

    async def test_delete_project(self, db):
        pid = await _make_project(db)
        await db.delete_project(pid)
        assert await db.get_project(pid) is None


# --- Task CRUD ---


class TestTaskCRUD:
    async def test_create_and_get(self, db):
        pid = await _make_project(db)
        tid = await _make_task(db, pid)
        task = await db.get_task(tid)
        assert task is not None
        assert task.project_id == pid

    async def test_list_tasks(self, db):
        pid = await _make_project(db)
        await _make_task(db, pid)
        await _make_task(db, pid)
        tasks = await db.list_tasks(project_id=pid)
        assert len(tasks) >= 2

    async def test_transition_task(self, db):
        pid = await _make_project(db)
        tid = await _make_task(db, pid)
        await db.transition_task(tid, TaskStatus.READY)
        task = await db.get_task(tid)
        assert task.status == TaskStatus.READY


# --- Agent CRUD ---


class TestAgentCRUD:
    async def test_create_and_get(self, db):
        aid = await _make_agent(db)
        agent = await db.get_agent(aid)
        assert agent is not None
        assert agent.id == aid


# --- Workspace Operations ---


class TestWorkspaces:
    async def test_create_and_list(self, db):
        pid = await _make_project(db)
        ws = Workspace(
            id=f"ws-{_uid()}",
            project_id=pid,
            workspace_path="/tmp/test-ws",
            source_type=RepoSourceType.CLONE,
        )
        await db.create_workspace(ws)
        workspaces = await db.list_workspaces(project_id=pid)
        assert len(workspaces) == 1
        assert workspaces[0].workspace_path == "/tmp/test-ws"


# --- Assign Task to Agent (atomic transaction) ---


class TestAtomicOperations:
    async def test_assign_task_to_agent(self, db):
        pid = await _make_project(db)
        aid = await _make_agent(db)
        tid = await _make_task(db, pid)
        await db.transition_task(tid, TaskStatus.READY)
        await db.assign_task_to_agent(tid, aid)

        task = await db.get_task(tid)
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigned_agent_id == aid

        agent = await db.get_agent(aid)
        assert agent.state == AgentState.BUSY


# --- Profile CRUD ---


class TestProfiles:
    async def test_create_and_get(self, db):
        profile = AgentProfile(id=f"prof-{_uid()}", name=f"profile-{_uid()}")
        await db.create_profile(profile)
        result = await db.get_profile(profile.id)
        assert result is not None
        assert result.name == profile.name


# --- Events ---


class TestEvents:
    async def test_log_event(self, db):
        pid = await _make_project(db)
        await db.log_event("test_event", project_id=pid)
        events = await db.get_recent_events(limit=50)
        assert len(events) >= 1
