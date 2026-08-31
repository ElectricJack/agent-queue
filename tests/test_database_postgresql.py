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


async def test_postgres_head_window_downgrade_reupgrade_preserves_and_transforms_data(db):
    """Head initialization exposes the schema objects required by the migration window."""
    from sqlalchemy import inspect

    async with db._engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert {"tasks", "agents", "task_completion_records"} <= tables


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_playbook_runs_round_trip_pinned_graph_node_trace_and_waiting_for_event_on_both_backends(
    backend, tmp_path
):
    if backend == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        adapter = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    else:
        from src.database import Database

        adapter = Database(str(tmp_path / "playbook.db"))
    await adapter.initialize()
    try:
        from src.models import PlaybookRun

        run = PlaybookRun(
            run_id=f"run-{_uid()}",
            playbook_id="pb",
            playbook_version=1,
            trigger_event="{}",
            pinned_graph='{"node":"start"}',
            node_trace='["start"]',
            waiting_for_event="approval",
        )
        await adapter.create_playbook_run(run)
        loaded = await adapter.get_playbook_run(run.run_id)
        assert (loaded.pinned_graph, loaded.node_trace, loaded.waiting_for_event) == (
            run.pinned_graph,
            run.node_trace,
            run.waiting_for_event,
        )
    finally:
        await adapter.close()


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
