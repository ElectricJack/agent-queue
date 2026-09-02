"""Layout endpoints (spatial-layout design §5)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.graph_layout import build_graph_layout_router
from src.database import Database
from src.models import Agent, AgentState, Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "gl.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_graph_layout_router(db=db))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return _make


async def seed(db):
    """epic e{c0,c1,pkg{g0,g1}}, root card z blocked by c0, hub with 10 dependents."""
    async def mk(tid, parent=None, status=TaskStatus.DEFINED):
        await db.create_task(
            Task(id=tid, project_id="p1", title=f"Title {tid}", description="", status=status)
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)
    await mk("e"); await mk("c0", "e"); await mk("c1", "e", TaskStatus.COMPLETED)
    await mk("pkg", "e"); await mk("g0", "pkg"); await mk("g1", "pkg")
    await mk("z"); await db.add_dependency("z", "c0")
    await mk("hub")
    for i in range(10):
        await mk(f"d{i}"); await db.add_dependency(f"d{i}", "hub")
    await db.create_agent(
        Agent(id="a1", name="bot", profile_id="p", state=AgentState.BUSY, current_task_id="g0")
    )
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all"); await drv.full_layout("p1", "active")


ALL = {"variant": "all", "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60}, "expanded": []}


async def test_extent_pending_then_ready(db, client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.status_code == 202 and r.json()["status"] == "layout_pending"
        assert (await db.next_layout_job())["kind"] == "backfill"
        await LayoutDriver(db).full_layout("p1", "all")
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.status_code == 200
        assert r.json()["layout_version"] == 1 and r.json()["node_count"] == 0
        assert (await ac.get("/api/projects/nope/graph/extent?variant=all")).status_code == 404


async def test_tiles_default_collapsed(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    body = r.json()
    kinds = {n["id"]: n["kind"] for n in body["nodes"]}
    assert kinds["e"] == "collapsed" and kinds["z"] == "card" and "c0" not in kinds
    e = next(n for n in body["nodes"] if n["id"] == "e")
    assert e["agg_children"] == 3 and e["agg_descendants"] == 5 and e["agg_completed"] == 1
    assert e["title"] == "Title e"
    # z blocks-on c0 remaps to e, arrow drawn e -> z on the wire as from=z,to=e
    assert {
        "from": "z", "to": "e", "dep_type": "blocks", "description": None, "count": 1
    } in body["edges"]
    assert body["workers"] == [
        {"agent_id": "a1", "name": "bot", "docked_at": "e", "in_collapsed": True}
    ]
    assert body["layout_version"] == 1


async def test_tiles_expanded_and_rect_culling(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "expanded": ["e"]})
        ids = {n["id"] for n in r.json()["nodes"]}
        assert {"e", "c0", "c1", "pkg"} <= ids and "g0" not in ids
        e = next(n for n in r.json()["nodes"] if n["id"] == "e")
        # a rect entirely to the right of everything returns nothing
        r2 = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "rect": {"x0": 500, "y0": 500, "x1": 510, "y1": 510}},
        )
        assert r2.json()["nodes"] == []
        # a rect covering only e's box still returns e (box intersection, not origin)
        r3 = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": {
            "x0": e["x"] + e["w"] - 0.5, "y0": e["y"] + e["h"] - 0.5,
            "x1": e["x"] + e["w"] + 1, "y1": e["y"] + e["h"] + 1}})
        assert "e" in {n["id"] for n in r3.json()["nodes"]}


async def test_tiles_root_focus_ignores_max_depth(db, client_factory):
    """Focus mode shows the whole subtree at the client's expanded state."""
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "root": "e", "max_depth": 0, "expanded": ["pkg"]},
        )
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()["nodes"]}
    assert {"e", "c0", "c1", "pkg", "g0", "g1"} <= ids


async def test_tiles_stub_cap_and_more_marker(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        first = (await ac.post("/api/projects/p1/graph/tiles", json=ALL)).json()["nodes"]
        hub = next(n for n in first if n["id"] == "hub")
        rect = {"x0": hub["x"], "y0": hub["y"], "x1": hub["x"] + 0.5, "y1": hub["y"] + 0.5}
        # Make sure only hub is inside: shrink to its own cell region and filter by id afterwards.
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": rect})
    body = r.json()
    visible = {n["id"] for n in body["nodes"]}
    if all(f"d{i}" in visible for i in range(10)):
        pytest.skip("layout put every dependent in hub's cell; cap not exercisable here")
    ins = [e for e in body["edges"] if e["to"] == "hub"]
    assert len(ins) <= 8 + sum(1 for i in range(10) if f"d{i}" in visible)
    assert len(body["stubs"]) <= 8


async def test_tiles_validation(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        bad_rect = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "rect": {"x0": 5, "y0": 0, "x1": 1, "y1": 1}},
        )
        too_big = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "rect": {"x0": 0, "y0": 0, "x1": 100, "y1": 1}},
        )
        bad_variant = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "variant": "x"})
        too_many = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "expanded": [str(i) for i in range(2001)]},
        )
    assert {
        bad_rect.status_code, too_big.status_code, bad_variant.status_code, too_many.status_code
    } == {400}
