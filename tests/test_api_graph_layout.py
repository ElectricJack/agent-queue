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

    await mk("e")
    await mk("c0", "e")
    await mk("c1", "e", TaskStatus.COMPLETED)
    await mk("pkg", "e")
    await mk("g0", "pkg")
    await mk("g1", "pkg")
    await mk("z")
    await db.add_dependency("z", "c0")
    await mk("hub")
    for i in range(10):
        await mk(f"d{i}")
        await db.add_dependency(f"d{i}", "hub")
    await db.create_agent(
        Agent(id="a1", name="bot", profile_id="p", state=AgentState.BUSY, current_task_id="g0")
    )
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")


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
    assert {"from": "z", "to": "e", "dep_type": "blocks", "description": None, "count": 1} in body[
        "edges"
    ]
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
        r3 = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                **ALL,
                "rect": {
                    "x0": e["x"] + e["w"] - 0.5,
                    "y0": e["y"] + e["h"] - 0.5,
                    "x1": e["x"] + e["w"] + 1,
                    "y1": e["y"] + e["h"] + 1,
                },
            },
        )
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
        bad_rect.status_code,
        too_big.status_code,
        bad_variant.status_code,
        too_many.status_code,
    } == {400}


async def test_tiles_validation_runs_before_any_backfill(db, client_factory):
    """A malformed request must 400 without enqueueing a backfill job."""
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "rect": {"x0": 5, "y0": 0, "x1": 1, "y1": 1}},
        )
    assert r.status_code == 400
    assert await db.next_layout_job() is None


async def test_tiles_culled_collapsed_container_becomes_the_stub(db, client_factory):
    """z -> c0 must remap to the collapsed container e even when e is culled.

    The rect holds only z; e is collapsed and off-screen.  The wire edge is
    z -> e and the stub carries e's title, never the inner task c0.
    """
    await seed(db)
    async with client_factory() as ac:
        first = (await ac.post("/api/projects/p1/graph/tiles", json=ALL)).json()["nodes"]
        z = next(n for n in first if n["id"] == "z")
        rect = {"x0": z["x"], "y0": z["y"], "x1": z["x"] + 0.5, "y1": z["y"] + 0.5}
        body = (await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": rect})).json()
    assert {n["id"] for n in body["nodes"]} == {"z"}
    assert [(e["from"], e["to"]) for e in body["edges"]] == [("z", "e")]
    assert [(s["id"], s["title"]) for s in body["stubs"]] == [("e", "Title e")]


async def test_tiles_no_worker_docks_at_a_culled_container(db, client_factory):
    """The pre-cull owner map must not dock a worker at an absent node."""
    await seed(db)
    await db.create_agent(
        Agent(id="a2", name="bot2", profile_id="p", state=AgentState.BUSY, current_task_id="c0")
    )
    async with client_factory() as ac:
        first = (await ac.post("/api/projects/p1/graph/tiles", json=ALL)).json()["nodes"]
        z = next(n for n in first if n["id"] == "z")
        rect = {"x0": z["x"], "y0": z["y"], "x1": z["x"] + 0.5, "y1": z["y"] + 0.5}
        body = (await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": rect})).json()
    assert {n["id"] for n in body["nodes"]} == {"z"}
    assert body["workers"] == []  # a1 (on g0) and a2 (on c0) both dock at the culled e


async def test_tiles_root_focus_forces_all_and_expands_root(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "e",
            },
        )
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"e", "c0", "c1", "pkg"}  # c1 is COMPLETED but variant forced to all
    assert next(n for n in body["nodes"] if n["id"] == "e")["kind"] == "container"
    assert "z" not in ids  # outside the subtree
    assert any(s["id"] == "z" for s in body["stubs"])  # z depends on c0: stub at the edge


async def test_tiles_max_depth(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "expanded": ["e", "pkg"], "max_depth": 1},
        )
    kinds = {n["id"]: n["kind"] for n in r.json()["nodes"]}
    assert kinds["e"] == "container" and kinds["pkg"] == "collapsed" and "g0" not in kinds


async def test_tiles_filter_hides_nonmatches_and_reveals_path(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "q": "g1"})
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert set(nodes) == {"e", "pkg", "g1"}
    assert nodes["e"]["context_only"] and nodes["pkg"]["context_only"]
    assert not nodes["g1"]["context_only"]
    assert nodes["e"]["kind"] == "container" and nodes["pkg"]["kind"] == "container"


async def test_tiles_finished_status_filter_forces_all(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles", json={**ALL, "variant": "active", "status": "COMPLETED"}
        )
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c1"}


async def test_tiles_status_filter_is_case_insensitive(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles", json={**ALL, "variant": "active", "status": "completed"}
        )
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c1"}


async def test_list_paginates_in_layout_order(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r1 = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": ["e"], "limit": 3},
        )
        b1 = r1.json()
        assert len(b1["nodes"]) == 3 and b1["next_cursor"]
        r2 = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": ["e"], "limit": 3, "cursor": b1["next_cursor"]},
        )
        b2 = r2.json()
        ids = [n["id"] for n in b1["nodes"] + b2["nodes"]]
        assert len(ids) == len(set(ids))
        # children follow their parent
        assert ids.index("e") < ids.index("c0") and ids.index("e") < ids.index("pkg")
        too_big = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "all", "limit": 500}
        )
        assert too_big.status_code == 400


async def test_list_validation_runs_before_any_backfill(db, client_factory):
    async with client_factory() as ac:
        bad_limit = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "all", "limit": 0}
        )
        bad_cursor = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "all", "cursor": "!!not-base64!!"}
        )
    assert bad_limit.status_code == 400 and bad_cursor.status_code == 400
    assert await db.next_layout_job() is None


async def test_node_returns_box_and_ancestors(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/node/g0?variant=all")
    body = r.json()
    assert body["node"]["id"] == "g0" and body["node"]["depth"] == 2
    assert [a["id"] for a in body["ancestors"]] == ["e", "pkg"]
    assert body["ancestors"][0]["title"] == "Title e"
    async with client_factory() as ac:
        assert (await ac.get("/api/projects/p1/graph/node/nope?variant=all")).status_code == 404


async def test_locate_returns_positions_capped(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/locate?variant=all&q=title d&limit=3")
    body = r.json()
    assert len(body["hits"]) == 3 and body["truncated"] is True
    assert all({"id", "x", "y", "w", "h"} <= set(h) for h in body["hits"])
    # reading order: top-to-bottom, then left-to-right
    assert [(h["y"], h["x"]) for h in body["hits"]] == sorted(
        (h["y"], h["x"]) for h in body["hits"]
    )
    assert [h["id"] for h in body["hits"]] == ["d0", "d1", "d2"]


async def test_list_status_filter_is_case_insensitive(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "active", "status": "completed", "limit": 50},
        )
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c1"}


async def test_list_never_loads_the_whole_project(db, client_factory, monkeypatch):
    """`list` pages over open containers only, never the whole variant."""
    for n in (1, 2, 3):
        await db.create_task(Task(id=f"e{n}", project_id="p1", title=f"Epic {n}", description=""))
        for k in range(5):
            kid = f"e{n}c{k}"
            await db.create_task(
                Task(id=kid, project_id="p1", title=f"Child {kid}", description="")
            )
            async with db._engine.begin() as conn:
                await db.set_parent(kid, f"e{n}", conn=conn)
    await LayoutDriver(db).full_layout("p1", "all")

    def boom(*a, **kw):
        raise AssertionError("list must not load the whole variant")

    monkeypatch.setattr(db, "load_all_rows_with_tasks", boom)

    async with client_factory() as ac:
        collapsed = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "all", "expanded": [], "limit": 50}
        )
        opened = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": ["e1"], "limit": 50},
        )
    assert {n["id"] for n in collapsed.json()["nodes"]} == {"e1", "e2", "e3"}
    ids = {n["id"] for n in opened.json()["nodes"]}
    assert {f"e1c{k}" for k in range(5)} <= ids
    assert not any(i.startswith("e2c") for i in ids)


async def test_tidy_enqueues_and_jobs_reports(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={})
        assert r.status_code == 200
        jobs = r.json()["jobs"]
        assert {j["variant"] for j in jobs} == {"all", "active"}
        assert all(j["status"] == "queued" for j in jobs)
        again = await ac.post("/api/projects/p1/graph/tidy", json={"variant": "all"})
        assert again.json()["jobs"][0]["id"] == next(j["id"] for j in jobs if j["variant"] == "all")
        j = await ac.get(f"/api/projects/p1/graph/jobs/{jobs[0]['id']}")
        assert j.status_code == 200 and j.json()["kind"] == "tidy"
        ext = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert ext.json()["job"]["status"] == "queued"
        assert (await ac.get("/api/projects/p1/graph/jobs/nope")).status_code == 404


async def test_default_router_delegates_to_the_orchestrator_db(db, monkeypatch):
    """The statically declared router resolves the live db at request time."""
    from src.api import dependencies as deps
    from src.api.graph_layout import router as default_router

    class _Orch:
        pass

    orch = _Orch()
    orch.db = db
    # The real Orchestrator keeps its handler private and exposes no
    # `command_handler` attribute, so the fake must not invent one.
    orch._command_handler = None
    monkeypatch.setattr(deps, "_orchestrator", orch, raising=False)

    app = FastAPI()
    app.include_router(default_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        pending = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert pending.status_code == 202
        await seed(db)
        ready = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert ready.status_code == 200
        tiles = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
        assert tiles.status_code == 200
        assert "e" in {n["id"] for n in tiles.json()["nodes"]}
        # every route, so `_call`'s path+method dispatch is exercised
        lst = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "all", "expanded": [], "limit": 10}
        )
        assert lst.status_code == 200 and "e" in {n["id"] for n in lst.json()["nodes"]}
        node = await ac.get("/api/projects/p1/graph/node/g0?variant=all")
        assert node.status_code == 200 and node.json()["node"]["id"] == "g0"
        loc = await ac.get("/api/projects/p1/graph/locate?variant=all&q=title e")
        assert loc.status_code == 200 and [h["id"] for h in loc.json()["hits"]] == ["e"]
        tidy = await ac.post("/api/projects/p1/graph/tidy", json={"variant": "all"})
        assert tidy.status_code == 200
        job_id = tidy.json()["jobs"][0]["id"]
        job = await ac.get(f"/api/projects/p1/graph/jobs/{job_id}")
    # the extent 202 above already queued a backfill for `all`, and
    # `enqueue_layout_job` dedupes onto it — hence no `kind` assertion here
    # (`test_tidy_enqueues_and_jobs_reports` covers that).
    assert job.status_code == 200
    assert job.json()["id"] == job_id and job.json()["status"] == "queued"
