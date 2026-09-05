"""GET /api/projects/{project_id}/graph — aggregate graph payload."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.graph import build_graph_router
from src.database import Database
from src.models import Agent, AgentState, Project, Task


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "g.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_graph_router(db=db))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return _make


async def test_empty_project_returns_empty_arrays(client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph")
    assert r.status_code == 200
    body = r.json()
    assert body == {"tasks": [], "edges": [], "gates": [], "agents": []}


async def test_unknown_project_is_404(client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/nope/graph")
    assert r.status_code == 404


async def test_tasks_edges_gates_agents_are_included(db, client_factory):
    await db.create_task(Task(id="t1", project_id="p1", title="One", description=""))
    await db.create_task(Task(id="t2", project_id="p1", title="Two", description=""))
    await db.add_dependency(
        "t2", "t1", description="The second task consumes the first task's schema"
    )  # t2 blocks-on t1

    await db.create_agent(Agent(
        id="a1", name="claude-1", profile_id="claude-agent",
        state=AgentState.IDLE, current_task_id="t1",
    ))
    gid, _ = await db.create_gate(
        project_id="p1", gate_type="human", title="review",
        waiter_task_ids=["t1"],
    )

    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph")
    assert r.status_code == 200
    body = r.json()

    ids = {t["id"] for t in body["tasks"]}
    assert ids == {"t1", "t2"}
    assert {(e["from"], e["to"], e["dep_type"]) for e in body["edges"]} \
        == {("t2", "t1", "blocks")}
    assert body["edges"][0]["description"] == (
        "The second task consumes the first task's schema"
    )
    assert body["gates"][0]["task_ids"] == ["t1"]
    assert body["agents"][0]["current_task_id"] == "t1"


async def test_playbook_run_root_exposes_run_identity(db, client_factory):
    await db.create_task(
        Task(
            id="run-root",
            project_id="p1",
            title="Playbook run: release",
            description="",
            dedup_key="playbook-run:run-123",
        )
    )

    async with client_factory() as ac:
        response = await ac.get("/api/projects/p1/graph")

    assert response.status_code == 200
    assert response.json()["tasks"][0]["playbook_run_id"] == "run-123"


async def test_list_gate_waiters_for_project_groups_waiters_by_gate(db):
    await db.create_task(Task(id="t1", project_id="p1", title="One", description=""))
    await db.create_task(Task(id="t2", project_id="p1", title="Two", description=""))
    g1, _ = await db.create_gate(
        project_id="p1", gate_type="human", title="review", waiter_task_ids=["t2", "t1"]
    )
    g2, _ = await db.create_gate(project_id="p1", gate_type="timer", title="wait", await_id="x")

    waiters = await db.list_gate_waiters_for_project("p1")

    assert waiters == {g1: ["t1", "t2"]}
    assert g2 not in waiters
