"""Durable session history uses disposable SQLite databases only."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from src.database.adapters.sqlite import SQLiteDatabaseAdapter
from src.database.tables import agents, tasks
from src.models import Agent, Project, SessionRecord, Task, TaskStatus
from tests.perf.test_hierarchy_statements import count_statements


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "history.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_agent(Agent(id="a", name="Original worker", profile_id="worker"))
    for tid in ("t", "other"):
        await database.create_task(Task(id=tid, project_id="p", title=tid, description=""))
    async with database._engine.begin() as conn:
        await conn.execute(update(tasks).values(created_at=1.0))
    yield database
    await database.close()


def session(**kwargs):
    values = dict(
        id="s",
        project_id="p",
        profile_id="worker",
        harness="codex",
        provider="fake",
        name="worker",
        lifecycle="task",
        work_dir="/disposable",
        epoch="epoch",
        instance_token="token",
        started_at=100.0,
        task_id="t",
        agent_id="a",
        state="running",
        model="model-at-launch",
        intelligence_class="reasoning",
        session_key="conversation-one",
    )
    values.update(kwargs)
    return SessionRecord(**values)


async def test_retry_keeps_distinct_attempts_and_launch_identity(db):
    await db.create_session(session())
    await db.update_session("s", state="stopped", end_reason="restart_budget_exhausted")
    async with db._engine.begin() as conn:
        await conn.execute(update(agents).values(name="Renamed worker", model="new-model"))
    await db.create_session(session(id="s2", started_at=200.0))
    rows = await db.list_task_session_attempts("t")
    assert [r["session_id"] for r in rows] == ["s2", "s"]
    assert rows[1]["agent_name"] == "Original worker"
    assert rows[1]["model"] == "model-at-launch"
    assert rows[1]["end_reason"] == "restart_budget_exhausted"
    assert rows[1]["ended_at"] is not None
    assert rows[0]["ended_at"] is None
    assert rows[0]["id"] != rows[1]["id"]
    assert await db.get_task_session_attempt(rows[1]["id"]) == {
        **rows[1],
        "transcript_end_at": None,
    }
    assert await db.get_task_session_attempt("missing") is None


async def test_pool_claim_release_and_reuse_are_atomic_and_keep_a(db):
    await db.create_session(session(task_id=None, lifecycle="pool"))
    async with db.immediate() as conn:
        await db.record_holder(
            conn, session_id="s", task_id="t", agent_id="a", work_dir="/task-a", now=150.0
        )
    await db.record_task_session_outcome("t", "success", session_id="s")
    await db.release_claim(
        "s", task_status=TaskStatus.COMPLETED, context="session_close", now=170.0
    )
    async with db.immediate() as conn:
        await db.record_holder(
            conn, session_id="s", task_id="other", agent_id="a", work_dir="/task-b", now=180.0
        )
    await db.update_session("s", session_key="conversation-two")
    (a,) = await db.list_task_session_attempts("t")
    (b,) = await db.list_task_session_attempts("other")
    assert (a["started_at"], a["ended_at"], a["outcome"]) == (150.0, 170.0, "success")
    assert a["state"] == "stopped" and a["end_reason"] == "session_close"
    assert a["work_dir"] == "/task-a" and a["session_key"] == "conversation-one"
    assert b["work_dir"] == "/task-b" and b["session_key"] == "conversation-two"
    assert b["session_started_at"] == 100.0 and b["ended_at"] is None
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await db.release_claim(
                "s", task_status=TaskStatus.READY, context="rollback", now=190.0, conn=conn
            )
            raise RuntimeError("rollback")
    assert (await db.get_session("s")).task_id == "other"
    assert (await db.get_task_session_attempt(b["id"]))["ended_at"] is None


async def test_pool_holder_snapshots_history_in_one_database_statement(db):
    """A pool claim keeps its durable attempt record within the claim budget."""
    await db.create_session(session(task_id=None, lifecycle="pool"))
    async with count_statements(db) as counted:
        async with db.immediate() as conn:
            await db.record_holder(
                conn, session_id="s", task_id="t", agent_id="a", work_dir="/task-a", now=150.0
            )

    # BEGIN, holder's session/agent/workspace/metadata writes, one attempt
    # snapshot INSERT ... SELECT, COMMIT.  Keep the durable audit record
    # without restoring the four round trips it originally needed.
    assert counted["n"] <= 7
    (attempt,) = await db.list_task_session_attempts("t")
    assert (attempt["agent_name"], attempt["work_dir"], attempt["started_at"]) == (
        "Original worker",
        "/task-a",
        150.0,
    )


async def test_attempt_survives_archive_and_session_deletion(db):
    await db.create_session(session())
    await db.update_session("s", state="stopped", ended_at=140.0, end_reason="exited")
    await db.update_task("t", status=TaskStatus.COMPLETED)
    before = await db.list_task_session_attempts("t")
    assert await db.archive_task("t")
    await db.delete_session("s")
    assert await db.list_task_session_attempts("t") == before


async def test_terminal_repeat_does_not_rewrite_exit_timestamp(db):
    await db.create_session(session())
    await db.update_session("s", state="stopped", ended_at=140.0, end_reason="exited")
    await db.update_session("s", state="stopped")
    row = await db.get_session("s")
    (attempt,) = await db.list_task_session_attempts("t")
    assert row.ended_at == attempt["ended_at"] == 140.0
    assert row.end_reason == attempt["end_reason"] == "exited"


async def test_attempt_api_scope_and_archived_task(db, monkeypatch):
    from src.api import dependencies as deps
    from src.api.auth import RequestScope
    from src.api.task_sessions import router

    await db.create_session(session())
    monkeypatch.setattr(deps, "_orchestrator", SimpleNamespace(db=db))
    app = FastAPI()
    scope = RequestScope(kind="session", project_id="wrong", elevated=True)

    @app.middleware("http")
    async def set_scope(request, call_next):
        request.state.scope = scope
        return await call_next(request)

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/tasks/t/sessions")
        assert response.status_code == 403
        scope = RequestScope(kind="session", project_id=None)
        assert (await client.get("/api/tasks/t/sessions")).status_code == 403
        scope = RequestScope(kind="session", project_id="p", task_id="other")
        assert (await client.get("/api/tasks/t/sessions")).status_code == 403
        scope = RequestScope(kind="session", project_id="p")
        response = await client.get("/api/tasks/t/sessions")
        assert response.status_code == 200
        assert response.json()["sessions"][0]["task_id"] == "t"
        assert response.json()["sessions"][0]["session_started_at"] == 100.0
        await db.update_session("s", state="stopped")
        await db.update_task("t", status=TaskStatus.COMPLETED)
        await db.archive_task("t")
        assert (await client.get("/api/tasks/t/sessions")).status_code == 200
        assert (await client.get("/api/tasks/missing/sessions")).status_code == 404
        # Soft audit references must never expose an archived project's
        # history if another project later reuses the same task identifier.
        await db.create_project(Project(id="new-project", name="New"))
        with pytest.raises(ValueError, match="another project"):
            await db.create_task(
                Task(id="t", project_id="new-project", title="Reused", description="")
            )
        scope = RequestScope(kind="session", project_id="new-project")
        assert (await client.get("/api/tasks/t/sessions")).status_code == 403
        await db.create_task(Task(id="t", project_id="p", title="New incarnation", description=""))
        scope = RequestScope(kind="session", project_id="p")
        assert (await client.get("/api/tasks/t/sessions")).json()["sessions"] == []
        assert len(await db.list_task_session_attempts("t")) == 1  # Old audit record retained.


async def test_get_show_and_explain_expose_operational_block(db, tmp_path):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.orchestrator import Orchestrator

    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "workspace"))
    orch = Orchestrator(config)
    orch.db = db
    handler = CommandHandler(orch, config)
    await db.set_task_meta("t", "needs_attention", "restart_budget_exhausted")
    for command in ("get_task", "task_show"):
        result = await handler.execute(command, {"task_id": "t"})
        assert result["needs_attention"] == "restart_budget_exhausted"
    result = await handler.execute("explain_task", {"task_id": "t"})
    assert {"code": "needs_attention", "detail": "restart_budget_exhausted", "ref": "t"} in result[
        "reasons"
    ]


@pytest.mark.parametrize(
    "later",
    [
        {"session_key": "conversation-one", "work_dir": "/different-workspace"},
        {"session_key": "different-conversation", "work_dir": "/disposable"},
    ],
)
async def test_legacy_attempt_transcript_bound_uses_next_known_launch(db, later):
    await db.create_session(session(state="stopped"))
    (old,) = await db.list_task_session_attempts("t")
    await db.create_session(
        session(id="later", name="later", task_id="other", started_at=200.0, **later)
    )
    await db.create_session(
        session(id="latest", name="latest", task_id="other", started_at=300.0, **later)
    )
    bounded = await db.get_task_session_attempt(old["id"])
    assert bounded["transcript_end_at"] == 200.0
    assert bounded["ended_at"] is None  # Boundary is not an invented exit time.


async def test_legacy_attempt_without_safe_later_launch_has_no_transcript_bound(db):
    await db.create_session(session(state="stopped", session_key=None, work_dir=""))
    (old,) = await db.list_task_session_attempts("t")
    await db.create_session(
        session(
            id="later",
            name="later",
            task_id="other",
            started_at=200.0,
            session_key=None,
            work_dir="",
        )
    )
    bounded = await db.get_task_session_attempt(old["id"])
    assert bounded["transcript_end_at"] is None
    assert bounded["ended_at"] is None
