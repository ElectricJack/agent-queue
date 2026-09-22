"""Layout endpoints (spatial-layout design §5)."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, update

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.graph_layout import build_graph_layout_router
from src.database import Database
from src.database.queries.hierarchy_queries import PHASE_KEY
from src.database.tables import tasks as tasks_table
from src.models import Agent, AgentState, Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver
from tests.db_fixtures import lease_dsn, seed_task_session_attempt


@pytest.fixture
async def db(tmp_path):
    d = Database(lease_dsn("gl.db"))
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
    await mk("g0", "pkg", TaskStatus.IN_PROGRESS)
    await mk("g1", "pkg")
    await mk("z")
    await db.add_dependency("z", "c0")
    await mk("hub")
    for i in range(10):
        await mk(f"d{i}")
        await db.add_dependency(f"d{i}", "hub")
    await db.create_agent(Agent(id="a1", name="bot", profile_id="p", state=AgentState.BUSY))
    await seed_task_session_attempt(
        db,
        task_id="g0",
        project_id="p1",
        agent_id="a1",
        agent_name="bot",
        profile_id="p",
        create_task=False,
        heartbeat_age=5,
    )
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")


ALL = {"variant": "all", "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60}, "expanded": []}


@pytest.fixture
def scoped_client_factory(db):
    """A client whose requests carry a chosen ``request.state.scope``.

    ``TokenAuthMiddleware`` is what stamps the scope in production; these
    tests are wired without it, so a one-line middleware stands in.
    """

    def _make(scope=None, command_handler=None) -> AsyncClient:
        app = FastAPI()

        @app.middleware("http")
        async def _stamp(request, call_next):
            request.state.scope = scope if scope is not None else LOCAL_SCOPE
            return await call_next(request)

        app.include_router(build_graph_layout_router(db=db, command_handler=command_handler))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    return _make


class _RecordingHandler:
    """Stands in for ``CommandHandler`` and records what tidy was given."""

    def __init__(self, db):
        self.db = db
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name, args):
        self.calls.append((name, dict(args)))
        variants = [args["variant"]] if args.get("variant") else ["all", "active"]
        jobs = [await self.db.enqueue_layout_job(args["project_id"], v, "tidy") for v in variants]
        return {"success": True, "jobs": jobs}


async def test_running_target_ranks_live_leaves_and_returns_the_nested_path(db, client_factory):
    await seed(db)
    now = time.time()
    for agent_id in ("a2", "a3", "a4"):
        await db.create_agent(
            Agent(id=agent_id, name=agent_id, profile_id="p", state=AgentState.BUSY)
        )
    # An in-progress container is deliberately not a destination, even with a
    # newer/higher-priority worker marker of its own.
    await db.create_task(
        Task(
            id="container",
            project_id="p1",
            title="container",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=100,
        )
    )
    await db.create_task(Task(id="inside", project_id="p1", title="inside", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("inside", "container", conn=conn)
    await seed_task_session_attempt(
        db,
        task_id="container",
        project_id="p1",
        agent_id="a2",
        agent_name="a2",
        profile_id="p",
        now=now,
        session_started_at=now - 200,
        attempt_started_at=now - 200,
        create_task=False,
    )
    # The two root leaves tie on priority.  The oldest live attempt wins.
    # ``seed`` gives ``g0`` the model-default priority (100), which would
    # outrank this pair and hide the tie break: pin it below them.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks_table).where(tasks_table.c.id == "g0").values(priority=50)
        )
    await db.create_task(
        Task(
            id="newer",
            project_id="p1",
            title="newer",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=80,
        )
    )
    await db.create_task(
        Task(
            id="older",
            project_id="p1",
            title="older",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=80,
        )
    )
    await seed_task_session_attempt(
        db,
        task_id="newer",
        project_id="p1",
        agent_id="a3",
        agent_name="a3",
        profile_id="p",
        now=now,
        session_started_at=now - 20,
        attempt_started_at=now - 20,
        create_task=False,
    )
    await seed_task_session_attempt(
        db,
        task_id="older",
        project_id="p1",
        agent_id="a4",
        agent_name="a4",
        profile_id="p",
        now=now,
        session_started_at=now - 120,
        attempt_started_at=now - 120,
        create_task=False,
    )

    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/running-target")
    assert r.status_code == 200
    assert r.json()["task_id"] == "older"
    assert r.json()["parent_task_id"] is None
    assert r.json()["ancestors"] == []

    # Once the root leaves are no longer running, the existing nested g0
    # target includes the path the root canvas uses to pan to its outer tile.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks_table)
            .where(tasks_table.c.id.in_(["newer", "older"]))
            .values(status="COMPLETED")
        )
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/running-target")
    assert r.status_code == 200
    assert r.json()["task_id"] == "g0"
    assert r.json()["parent_task_id"] == "pkg"
    assert r.json()["ancestors"] == ["e", "pkg"]


async def test_running_target_is_null_when_no_live_leaf_exists(db, client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/running-target")
    assert r.status_code == 200
    assert r.json() is None


async def test_dashboard_running_target_ranks_across_projects(db, client_factory):
    await db.create_project(Project(id="p2", name="P2"))
    await db.create_agent(Agent(id="a2", name="a2", profile_id="p", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="p2-running",
            project_id="p2",
            title="p2-running",
            description="",
            status=TaskStatus.IN_PROGRESS,
            priority=90,
        )
    )
    await seed_task_session_attempt(
        db,
        task_id="p2-running",
        project_id="p2",
        agent_id="a2",
        agent_name="a2",
        profile_id="p",
        create_task=False,
    )

    async with client_factory() as ac:
        r = await ac.get("/api/graph/running-target")
    assert r.status_code == 200
    assert r.json()["project_id"] == "p2"
    assert r.json()["task_id"] == "p2-running"


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


async def test_tiles_includes_discovered_from_provenance_edge(db, client_factory):
    """A `discovered-from` edge between two visible nodes is annotation, not
    a dependency: it rides the same wire as `blocks`/`waits-for` (both
    endpoints stay put; no stub is manufactured) but keeps its own
    `dep_type` so the client can draw it differently.
    """
    await seed(db)
    await db.add_dependency("z", "hub", "discovered-from")
    await LayoutDriver(db).full_layout("p1", "all")
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    body = r.json()
    assert {
        "from": "z",
        "to": "hub",
        "dep_type": "discovered-from",
        "description": None,
        "count": 1,
    } in body["edges"]


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
        # a rect covering only e's box still returns e (box intersection, not
        # origin). The expanded set has to match the one `e`'s box came from:
        # collapsing `e` compacts it down to a single tile, so a rect cut from
        # its expanded corner would no longer touch it.
        r3 = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                **ALL,
                "expanded": ["e"],
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
    await db.create_agent(Agent(id="a2", name="bot2", profile_id="p", state=AgentState.BUSY))
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks_table).where(tasks_table.c.id == "c0").values(status="IN_PROGRESS")
        )
    await seed_task_session_attempt(
        db,
        task_id="c0",
        project_id="p1",
        agent_id="a2",
        agent_name="bot2",
        profile_id="p",
        create_task=False,
        heartbeat_age=5,
    )
    async with client_factory() as ac:
        first = (await ac.post("/api/projects/p1/graph/tiles", json=ALL)).json()["nodes"]
        z = next(n for n in first if n["id"] == "z")
        rect = {"x0": z["x"], "y0": z["y"], "x1": z["x"] + 0.5, "y1": z["y"] + 0.5}
        body = (await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": rect})).json()
    assert {n["id"] for n in body["nodes"]} == {"z"}
    assert body["workers"] == []  # a1 (on g0) and a2 (on c0) both dock at the culled e


async def test_tiles_a_finished_task_with_no_live_attempt_docks_no_marker(db, client_factory):
    """A stale ``agents.current_task_id`` must not resurrect a marker (B1)."""
    await db.create_task(
        Task(
            id="only",
            project_id="p1",
            title="Only",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_agent(
        Agent(id="a1", name="bot", profile_id="p", state=AgentState.IDLE, current_task_id="only")
    )
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.json()["workers"] == []


async def test_tiles_a_stopped_attempt_docks_no_marker_even_with_a_task_pointer(db, client_factory):
    """The durable attempt, not the agent pointer, decides marker liveness."""
    await seed_task_session_attempt(
        db,
        task_id="only",
        project_id="p1",
        agent_id="a1",
        agent_name="bot",
        profile_id="p",
        attempt_state="stopped",
        session_state="stopped",
    )
    await db.create_agent(
        Agent(id="a1", name="bot", profile_id="p", state=AgentState.BUSY, current_task_id="only")
    )
    await LayoutDriver(db).full_layout("p1", "all")

    async with client_factory() as client:
        response = await client.post("/api/projects/p1/graph/tiles", json=ALL)

    assert response.status_code == 200
    assert response.json()["workers"] == []


async def test_tiles_a_live_attempt_on_a_visible_task_docks_one_worker(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "expanded": ["e", "pkg"]})
    body = r.json()
    assert body["workers"] == [
        {"agent_id": "a1", "name": "bot", "docked_at": "g0", "in_collapsed": False}
    ]


async def test_tiles_a_live_attempt_in_a_collapsed_container_docks_the_container(
    db, client_factory
):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    body = r.json()
    assert body["workers"] == [
        {"agent_id": "a1", "name": "bot", "docked_at": "e", "in_collapsed": True}
    ]


async def test_tiles_root_focus_expands_the_root_and_keeps_the_active_variant(db, client_factory):
    """Entering a live container is the root view one level down.

    Focus used to force ``variant="all"`` unconditionally, so entering a
    container showed its finished children even with "Show completed" off.
    The promotion now only happens when the entered container itself is not
    in the active layout (see the finished-container test below).
    """
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
    assert ids == {"e", "c0", "pkg"}  # c1 is COMPLETED: dropped by the active variant
    assert next(n for n in body["nodes"] if n["id"] == "e")["kind"] == "container"
    assert "z" not in ids  # outside the subtree
    assert any(s["id"] == "z" for s in body["stubs"])  # z depends on c0: stub at the edge


async def test_tiles_root_focus_of_a_finished_container_promotes_to_all(db, client_factory):
    """A container the active variant dropped must still be enterable."""
    await seed(db)
    for tid, parent in (("fe", None), ("fc", "fe")):
        await db.create_task(
            Task(
                id=tid,
                project_id="p1",
                title=f"Title {tid}",
                description="",
                status=TaskStatus.DEFINED,
            )
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)
    # Finished only AFTER the parent edge: a closed container refuses children.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks_table)
            .where(tasks_table.c.id.in_(["fe", "fc"]))
            .values(status=TaskStatus.COMPLETED.value)
        )
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "fe",
            },
        )
    assert r.status_code == 200
    assert {n["id"] for n in r.json()["nodes"]} == {"fe", "fc"}


async def test_tiles_root_focus_of_finished_container_with_live_descendant_stays_active(
    db, client_factory
):
    """A finished ancestor remains a real active-layout container for live work.

    Promotion is driven by the published row's kind, not the root task's
    status.  A completed container with an unfinished descendant is a real
    ``container`` in ``active`` and entering it must not reveal finished work
    from ``all``.
    """
    for tid, parent in (("finished-parent", None), ("live-child", "finished-parent")):
        await db.create_task(
            Task(id=tid, project_id="p1", title=tid, description="", status=TaskStatus.DEFINED)
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)
    # Completion follows hierarchy setup; normal child creation correctly
    # refuses a terminal parent.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks_table)
            .where(tasks_table.c.id == "finished-parent")
            .values(status=TaskStatus.COMPLETED.value)
        )
    driver = LayoutDriver(db)
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")

    async with client_factory() as client:
        response = await client.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "finished-parent",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["variant_applied"] == "active"
    assert {node["id"] for node in body["nodes"]} == {"finished-parent", "live-child"}
    assert (
        next(node for node in body["nodes"] if node["id"] == "finished-parent")["kind"]
        == "container"
    )


async def test_tiles_focus_leaves_child_containers_collapsed(db, client_factory):
    """The client's enter-only navigation rests on this: one scope per view."""
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "root": "e", "expanded": []},
        )
    kinds = {n["id"]: n["kind"] for n in r.json()["nodes"]}
    assert kinds["e"] == "container"
    assert kinds["pkg"] == "collapsed"
    assert "g0" not in kinds and "g1" not in kinds


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


async def test_list_reports_empty_reasons_without_inferring_from_active_rows(db, client_factory):
    """Phone clients distinguish no work, finished work, and an empty filter."""
    driver = LayoutDriver(db)
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")
    async with client_factory() as ac:
        no_work = await ac.post("/api/projects/p1/graph/list", json={"variant": "active"})
    assert no_work.json()["empty_reason"] == "no_work"

    await db.create_task(
        Task(id="done", project_id="p1", title="Done", description="", status=TaskStatus.COMPLETED)
    )
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")
    async with client_factory() as ac:
        all_finished = await ac.post("/api/projects/p1/graph/list", json={"variant": "active"})
        filtered = await ac.post(
            "/api/projects/p1/graph/list", json={"variant": "active", "q": "missing"}
        )
    assert all_finished.json()["empty_reason"] == "all_finished"
    assert filtered.json()["empty_reason"] == "no_matches"


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
        r = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "all", "q": "title d", "limit": 3},
        )
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
        loc = await ac.post(
            "/api/projects/p1/graph/locate", json={"variant": "all", "q": "title e"}
        )
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


async def test_tidy_refuses_an_agent_session_scope(db, scoped_client_factory):
    """The mutation is scoped like a generated command route."""
    await seed(db)
    agent = RequestScope(kind="session", session_id="s1", project_id="p1", task_id="g0")
    handler = _RecordingHandler(db)
    async with scoped_client_factory(scope=agent, command_handler=handler) as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={})
    assert r.status_code == 403
    assert "out of scope" in r.json()["error"]
    # neither the handler nor the db fallback got to run
    assert handler.calls == []
    assert await db.list_layout_jobs("p1", "all", statuses=("queued", "running")) == []
    assert await db.list_layout_jobs("p1", "active", statuses=("queued", "running")) == []


async def test_tidy_refuses_an_agent_session_without_a_command_handler(db, scoped_client_factory):
    """The guard is on the route, not only on the command."""
    await seed(db)
    agent = RequestScope(kind="session", session_id="s1", project_id="p1", task_id="g0")
    async with scoped_client_factory(scope=agent) as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={"variant": "all"})
    assert r.status_code == 403
    assert await db.list_layout_jobs("p1", "all", statuses=("queued", "running")) == []


async def test_tidy_refuses_an_elevated_scope_for_another_project(db, scoped_client_factory):
    await seed(db)
    other = RequestScope(kind="session", session_id="s1", project_id="p2", elevated=True)
    async with scoped_client_factory(scope=other) as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={})
    assert r.status_code == 403 and "project_id mismatch" in r.json()["error"]
    assert await db.list_layout_jobs("p1", "all", statuses=("queued", "running")) == []


@pytest.mark.parametrize(
    "scope",
    [
        LOCAL_SCOPE,
        RequestScope(kind="session", session_id="s1", project_id="p1", elevated=True),
    ],
    ids=["local", "elevated-supervisor"],
)
async def test_tidy_allows_local_and_elevated_scopes(db, scoped_client_factory, scope):
    await seed(db)
    handler = _RecordingHandler(db)
    async with scoped_client_factory(scope=scope, command_handler=handler) as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={"variant": "all"})
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert [j["variant"] for j in jobs] == ["all"] and jobs[0]["status"] == "queued"
    # the server-derived scope reached the command, so its own agent-session
    # guard sees a real scope instead of None
    name, args = handler.calls[0]
    assert name == "graph_tidy"
    assert args["_scope"]["kind"] == scope.kind
    assert args["_scope"]["elevated"] is scope.elevated
    assert await db.list_layout_jobs("p1", "all", statuses=("queued", "running"))


async def test_locate_requires_a_filter(db, client_factory):
    """An unfiltered locate would scan the project; it is a 400, not a scan."""
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/locate", json={"variant": "all"})
        assert r.status_code == 400 and "requires q or status" in r.json()["detail"]
        blank = await ac.post(
            "/api/projects/p1/graph/locate", json={"variant": "all", "q": " ", "status": " "}
        )
        assert blank.status_code == 400
    # a rejected request must not enqueue a backfill either
    assert await db.next_layout_job() is None


async def test_locate_caps_and_orders_in_sql(db, client_factory, monkeypatch):
    """The page comes back capped without ever loading a row per match.

    Locate resolves the same compacted geometry the matching tiles request
    does — it has to, or a jump would land where the engine persisted the
    card rather than where the canvas draws it — but that geometry is bounded
    by the open containers, so the match set still only contributes ids.
    """
    await seed(db)
    seen: list[str] = []
    real_rows = db.load_layout_rows
    real_with_tasks = db.load_rows_with_tasks

    async def spy_rows(project_id, variant, task_ids):
        seen.extend(task_ids)
        return await real_rows(project_id, variant, task_ids)

    async def spy_with_tasks(project_id, variant, task_ids):
        seen.extend(task_ids)
        return await real_with_tasks(project_id, variant, task_ids)

    monkeypatch.setattr(db, "load_layout_rows", spy_rows)
    monkeypatch.setattr(db, "load_rows_with_tasks", spy_with_tasks)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "all", "q": "title d", "limit": 4},
        )
        exact = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "all", "q": "title d", "limit": 10},
        )
    body = r.json()
    assert len(body["hits"]) == 4 and body["truncated"] is True
    assert [h["id"] for h in body["hits"]] == ["d0", "d1", "d2", "d3"]
    assert len(exact.json()["hits"]) == 10 and exact.json()["truncated"] is False
    assert not ({f"d{i}" for i in range(10)} & set(seen)), seen


async def test_locate_is_pending_before_the_first_layout(db, client_factory):
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/locate", json={"variant": "all", "q": "x"})
    assert r.status_code == 202 and r.json()["status"] == "layout_pending"
    assert (await db.next_layout_job())["kind"] == "backfill"


async def test_node_is_pending_before_the_first_layout(db, client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/node/g0?variant=all")
    assert r.status_code == 202 and r.json()["status"] == "layout_pending"
    assert (await db.next_layout_job())["kind"] == "backfill"


async def test_node_still_404s_for_an_unknown_id_once_laid_out(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        assert (await ac.get("/api/projects/p1/graph/node/nope?variant=all")).status_code == 404


async def test_extent_reports_the_running_job_then_the_newest_queued(db, client_factory):
    """`jobs[0]` was the OLDEST queued job — the least useful thing to report."""
    import time as _time

    from sqlalchemy import insert

    from src.database.tables import layout_jobs

    async def put(job_id, status, at):
        # Straight into the table: `enqueue_layout_job` deliberately dedupes
        # onto any live job for the (project, variant), so it cannot build
        # the running-plus-backlog state this endpoint has to summarise.
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(layout_jobs).values(
                    id=job_id,
                    project_id="p1",
                    variant="all",
                    kind="tidy",
                    status=status,
                    requested_at=at,
                )
            )

    await seed(db)
    now = _time.time()
    await put("old", "queued", now - 30)
    await put("new", "queued", now - 10)
    async with client_factory() as ac:
        # newest queued, not `jobs[0]`
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.json()["job"]["id"] == "new" and r.json()["job"]["status"] == "queued"
        # a RUNNING job outranks every queued one, however old it is
        await put("busy", "running", now - 60)
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.json()["job"]["id"] == "busy" and r.json()["job"]["status"] == "running"
        # finished jobs are not reported at all
        await db.finish_layout_job("busy", error=None)
        await db.finish_layout_job("new", error=None)
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.json()["job"]["id"] == "old"
        await db.finish_layout_job("old", error=None)
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.json()["job"] is None


async def test_tiles_filter_never_loads_rows_for_the_match_set(db, client_factory, monkeypatch):
    """Matches contribute paths (for forced expansion); nothing more."""
    await seed(db)
    real = db.load_layout_rows
    seen: list[list[str]] = []

    async def spy(project_id, variant, task_ids):
        ids = list(task_ids)
        seen.append(ids)
        return await real(project_id, variant, ids)

    monkeypatch.setattr(db, "load_layout_rows", spy)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles", json={**ALL, "q": "title g", "expanded": ["e", "pkg"]}
        )
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()["nodes"]}
    assert {"g0", "g1"} <= ids  # matches shown ...
    assert {"e", "pkg"} <= ids  # ... with their ancestors as context
    assert all(n["context_only"] for n in r.json()["nodes"] if n["id"] in {"e", "pkg"})
    # the match ids themselves are never passed to `load_layout_rows`
    assert not any({"g0", "g1"} & set(batch) for batch in seen), seen


async def test_tiles_focus_does_not_load_the_whole_subtree(db, client_factory, monkeypatch):
    """Focus loads the open containers' children, not `LIKE '/e/%'`."""
    await seed(db)
    real = db.load_rows_by_prefixes
    seen: list[list[str]] = []

    async def spy(project_id, variant, prefixes):
        seen.append(list(prefixes))
        return await real(project_id, variant, prefixes)

    monkeypatch.setattr(db, "load_rows_by_prefixes", spy)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "root": "e", "expanded": ["e"]},
        )
    assert r.status_code == 200
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c0", "c1", "pkg"}
    # `pkg` is collapsed, so its own path may still be asked for (hidden
    # owner map) -- but never the root's, which would be the whole subtree.
    assert all("/e/" not in prefixes for prefixes in seen), seen


async def test_tiles_focus_expanded_child_still_shows_grandchildren(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "root": "e", "expanded": ["e", "pkg"]},
        )
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c0", "c1", "pkg", "g0", "g1"}


async def test_tiles_focus_filter_still_reaches_deep_matches(db, client_factory):
    """A filter under `root` must return the matches, not only the context.

    Regression: once focus stopped loading the root's whole subtree, a match
    deeper than the open containers had no candidate row, so the response
    was all force-expanded ancestors and no result.
    """
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "root": "e", "q": "title g"})
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert set(nodes) == {"e", "pkg", "g0", "g1"}
    # the ancestors are context; the matches are the result
    assert nodes["e"]["context_only"] and nodes["pkg"]["context_only"]
    assert not nodes["g0"]["context_only"] and not nodes["g1"]["context_only"]


async def test_tiles_focus_status_filter_still_reaches_deep_matches(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles", json={**ALL, "root": "e", "status": "COMPLETED"}
        )
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert set(nodes) == {"e", "c1"}
    assert nodes["e"]["context_only"] and not nodes["c1"]["context_only"]


async def test_tidy_checks_scope_before_project_existence(db, scoped_client_factory):
    """403, not 404: refusing a caller must not tell it what exists."""
    agent = RequestScope(kind="session", session_id="s1", project_id="p1", task_id="g0")
    async with scoped_client_factory(scope=agent) as ac:
        r = await ac.post("/api/projects/nope/graph/tidy", json={})
    assert r.status_code == 403
    # a permitted caller still gets the 404
    async with scoped_client_factory() as ac:
        assert (await ac.post("/api/projects/nope/graph/tidy", json={})).status_code == 404


async def _tiles(ac, **over):
    r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, **over})
    assert r.status_code == 200, r.text
    return {n["id"]: n for n in r.json()["nodes"]}


async def test_collapsing_a_container_reclaims_its_space_for_the_rows_below(db, client_factory):
    """The operator's complaint: siblings below a collapsed epic must move up.

    ``hub`` and ``z`` are the epic's line-mates — the root's aspect-balanced
    row target is wide enough to hold the epic and its loose cards on one
    line (reorganisation design §3.1) — so they reclaim WIDTH. The ``d*``
    dependents on the rank below reclaim HEIGHT: collapsing ``e`` shrinks it
    to one tile and they climb by exactly that delta.
    """
    await seed(db)
    async with client_factory() as ac:
        opened = await _tiles(ac, expanded=["e", "pkg"])
        closed = await _tiles(ac, expanded=[])

    delta = opened["e"]["h"] - closed["e"]["h"]
    assert delta > 0 and (closed["e"]["w"], closed["e"]["h"]) == (1.0, 1.0)
    for tid in ("d0", "d9"):
        assert closed[tid]["y"] == pytest.approx(opened[tid]["y"] - delta)
        assert closed[tid]["x"] == pytest.approx(opened[tid]["x"])
    shrink = opened["e"]["w"] - closed["e"]["w"]
    assert shrink > 0
    for tid in ("hub", "z"):
        assert closed[tid]["y"] == pytest.approx(opened[tid]["y"])
        assert closed[tid]["x"] == pytest.approx(opened[tid]["x"] - shrink)
    assert closed["e"]["y"] == pytest.approx(opened["e"]["y"])


async def test_expanding_restores_the_positions_collapsing_changed(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        first = await _tiles(ac, expanded=["e", "pkg"])
        await _tiles(ac, expanded=[])
        again = await _tiles(ac, expanded=["e", "pkg"])
    for tid, n in first.items():
        assert (again[tid]["x"], again[tid]["y"]) == (n["x"], n["y"])
        assert (again[tid]["w"], again[tid]["h"]) == (n["w"], n["h"])


async def test_tiles_positions_are_deterministic_for_one_expanded_set(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        a = await _tiles(ac, expanded=["e"])
    # A fresh client means a cold geometry cache: determinism must come from
    # the compaction itself, not from serving the same cached object twice.
    async with client_factory() as ac:
        b = await _tiles(ac, expanded=["e"])
    assert a == b


async def test_a_repeat_toggle_is_served_without_reloading_the_open_set(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        await _tiles(ac, expanded=["e"])
        real = db.load_rows_for_containers
        calls: list[list] = []

        async def spy(project_id, variant, container_ids):
            calls.append(list(container_ids))
            return await real(project_id, variant, container_ids)

        db.load_rows_for_containers = spy
        try:
            await _tiles(ac, expanded=[])  # cold for this expanded set
            assert calls
            calls.clear()
            await _tiles(ac, expanded=["e"])  # already resolved once
            assert calls == []
        finally:
            db.load_rows_for_containers = real


async def test_a_filtered_request_is_never_served_from_the_geometry_cache(db, client_factory):
    """Matches come from live titles and statuses, which change without a
    republished layout, so a filtered geometry must not be remembered."""
    await seed(db)
    async with client_factory() as ac:
        assert {"g0", "g1"} <= set(await _tiles(ac, q="title g", expanded=["e", "pkg"]))
        real = db.load_matching_ids
        calls: list[str] = []

        async def spy(project_id, variant, *, q, status):
            calls.append(q)
            return await real(project_id, variant, q=q, status=status)

        db.load_matching_ids = spy
        try:
            await _tiles(ac, q="title g", expanded=["e", "pkg"])
            assert calls == ["title g"]
        finally:
            db.load_matching_ids = real


async def test_list_reports_the_same_compacted_positions_as_tiles(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        tiles = await _tiles(ac, expanded=[])
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": [], "limit": 200},
        )
    assert r.status_code == 200
    for n in r.json()["nodes"]:
        if n["id"] in tiles:
            assert (n["x"], n["y"], n["w"], n["h"]) == (
                tiles[n["id"]]["x"],
                tiles[n["id"]]["y"],
                tiles[n["id"]]["w"],
                tiles[n["id"]]["h"],
            )


async def test_locate_reports_where_the_canvas_will_draw_the_hit(db, client_factory):
    """A jump has to land on the card, not on the hole the collapse left."""
    await seed(db)
    async with client_factory() as ac:
        tiles = await _tiles(ac, q="title d", expanded=[])
        r = await ac.post("/api/projects/p1/graph/locate", json={"variant": "all", "q": "title d"})
    assert r.status_code == 200
    hits = {h["id"]: h for h in r.json()["hits"]}
    assert hits and set(hits) <= set(tiles)
    for tid, hit in hits.items():
        assert (hit["x"], hit["y"], hit["w"], hit["h"]) == (
            tiles[tid]["x"],
            tiles[tid]["y"],
            tiles[tid]["w"],
            tiles[tid]["h"],
        )


async def test_a_collapsed_subtree_far_from_the_rect_is_not_read(db, client_factory):
    """The open set decides the geometry; the rect still decides the cost.

    `hidden_paths` reads every task inside every collapsed container it is
    given, so a project of collapsed epics would pay for all of them on every
    pan if the collapsed set were taken from the whole open set.
    """
    await seed(db)
    seen: list[list[str]] = []
    real = db.load_paths_by_prefixes

    async def spy(project_id, variant, prefixes):
        seen.append(list(prefixes))
        return await real(project_id, variant, prefixes)

    db.load_paths_by_prefixes = spy
    try:
        async with client_factory() as ac:
            everywhere = await _tiles(ac, expanded=[])
            assert any("/e/" in p for batch in seen for p in batch), seen
            seen.clear()
            e = everywhere["e"]
            far = e["y"] + e["h"] + 40
            await _tiles(ac, rect={"x0": -1, "y0": far, "x1": 20, "y1": far + 10})
    finally:
        db.load_paths_by_prefixes = real
    assert not any("/e/" in p for batch in seen for p in batch), seen


async def _finished_epic_project(db, *, anchored: bool):
    """A finished epic beside one live task, laid out in both variants.

    With *anchored*, the live task depends on the epic, so the ``active``
    variant still carries it as a stub; without, nothing unfinished needs
    it and it leaves that variant entirely.
    """

    async def create(tid, parent=None):
        await db.create_task(
            Task(id=tid, project_id="p1", title=tid, description="", status=TaskStatus.DEFINED)
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    await create("done")
    await create("child", "done")
    await create("live")
    for tid in ("child", "done"):
        await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    if anchored:
        await db.add_dependency("live", "done")
    driver = LayoutDriver(db)
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")


async def test_tiles_omit_a_finished_epic_nothing_needs_from_the_active_view(db, client_factory):
    await _finished_epic_project(db, anchored=False)

    async with client_factory() as client:
        response = await client.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60},
                "expanded": [],
            },
        )
        all_response = await client.post(
            "/api/projects/p1/graph/tiles", json={**ALL, "expanded": ["done"]}
        )

    assert response.status_code == 200
    assert {node["id"] for node in response.json()["nodes"]} == {"live"}
    # "Show completed" still shows it, children and all.
    assert all_response.status_code == 200
    assert {node["id"] for node in all_response.json()["nodes"]} == {"done", "child", "live"}


async def test_locate_does_not_find_a_dropped_finished_epic_in_the_active_view(db, client_factory):
    """The second affordance the drop rule costs, pinned deliberately.

    With "Show completed" off the client searches the ``active`` variant's
    rows, and a dropped epic has none — so a text search stops matching it
    there. Ticking "Show completed" (variant ``all``) still finds it.
    """
    await _finished_epic_project(db, anchored=False)

    async with client_factory() as client:
        active = await client.post(
            "/api/projects/p1/graph/locate", json={"variant": "active", "q": "done"}
        )
        every = await client.post(
            "/api/projects/p1/graph/locate", json={"variant": "all", "q": "done"}
        )

    assert active.status_code == 200
    assert [hit["id"] for hit in active.json()["hits"]] == []
    assert every.status_code == 200
    assert [hit["id"] for hit in every.json()["hits"]] == ["done"]


async def test_stale_expanded_id_for_a_dropped_epic_is_simply_ignored(db, client_factory):
    """A viewer whose persisted expansion still names the dropped epic.

    It has no row in ``active``, so ``_variant_for_scope`` finds no stub to
    promote on, the request stays on ``active``, and the response is the same
    one an empty ``expanded`` would get — no error, no promotion, no rows.
    """
    await _finished_epic_project(db, anchored=False)

    async with client_factory() as client:
        response = await client.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60},
                "expanded": ["done"],
            },
        )

    assert response.status_code == 200
    assert {node["id"] for node in response.json()["nodes"]} == {"live"}


async def test_tiles_expand_a_finished_epic_from_the_active_view(db, client_factory):
    await _finished_epic_project(db, anchored=True)

    async with client_factory() as client:
        response = await client.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60},
                "expanded": ["done"],
            },
        )
        list_response = await client.post(
            "/api/projects/p1/graph/list",
            json={"variant": "active", "expanded": ["done"], "limit": 50},
        )

    assert response.status_code == 200
    nodes = {node["id"]: node for node in response.json()["nodes"]}
    assert nodes["done"]["kind"] == "container"
    assert nodes["child"]["container_id"] == "done"
    assert list_response.status_code == 200
    assert {node["id"] for node in list_response.json()["nodes"]} >= {"done", "child"}


async def test_tiles_gate_lookup_is_two_statements_regardless_of_gate_count(db, client_factory):
    await seed(db)
    # An open gate over visible tasks is reported; 300 historical gates are not,
    # and they must not cost a statement each.
    open_gid, _ = await db.create_gate(
        project_id="p1", gate_type="human", title="review", waiter_task_ids=["z", "hub"]
    )
    for i in range(300):
        gid, _ = await db.create_gate(
            project_id="p1",
            gate_type="timer",
            title=f"old{i}",
            await_id=f"t{i}",
            waiter_task_ids=["hub"],
        )
        await db.resolve_gate(gid, resolved_by="test", resolution="done")

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    gates = r.json()["gates"]
    assert gates == [
        {"id": open_gid, "gate_type": "human", "status": "open", "task_ids": ["hub", "z"]}
    ]
    gate_reads = [s for s in statements if "FROM gates" in s or "task_gates" in s]
    assert len(gate_reads) <= 2, gate_reads


async def test_tiles_reports_subtask_counts_and_zero_for_none(db, client_factory):
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}, {"title": "two"}, {"title": "three"}])
    await db.update_task_subtask("z", 1, status="done")
    await db.update_task_subtask("z", 2, status="skipped")

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert nodes["z"]["subtasks_total"] == 3
    assert nodes["z"]["subtasks_settled"] == 2
    # A task with no subtasks carries zeros, not an absent field.
    assert nodes["hub"]["subtasks_total"] == 0
    assert nodes["hub"]["subtasks_settled"] == 0


async def test_tiles_container_node_reports_only_its_own_subtasks(db, client_factory):
    """Subtask counts do not roll up the hierarchy -- ``e`` has none of its own."""
    await seed(db)
    await db.add_task_subtasks("c0", "p1", [{"title": "child subtask"}])

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert nodes["e"]["subtasks_total"] == 0
    assert nodes["e"]["subtasks_settled"] == 0


async def test_tiles_subtask_lookup_is_one_statement_regardless_of_visible_count(
    db, client_factory
):
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}])

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    subtask_reads = [s for s in statements if "task_subtasks" in s]
    assert len(subtask_reads) == 1, subtask_reads


async def test_list_subtask_lookup_is_one_statement_regardless_of_page_size(db, client_factory):
    """One ``count_task_subtasks`` call over the whole page, not one per row.

    Mirrors ``test_tiles_subtask_lookup_is_one_statement_regardless_of_visible_count``
    for the ``list`` endpoint, which pages over ``page`` the same way ``tiles``
    pages over ``with_tasks``.
    """
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}])

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.post("/api/projects/p1/graph/list", json=ALL)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    assert len(r.json()["nodes"]) > 1  # a real multi-node page, not a fluke
    subtask_reads = [s for s in statements if "task_subtasks" in s]
    assert len(subtask_reads) == 1, subtask_reads


async def test_list_subtask_lookup_is_skipped_for_an_empty_page(db, client_factory):
    """No ``task_subtasks`` statement at all when the page has nothing on it."""
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}])

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.post(
                "/api/projects/p1/graph/list",
                json={**ALL, "q": "no-such-title-anywhere"},
            )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    assert r.json()["nodes"] == []
    subtask_reads = [s for s in statements if "task_subtasks" in s]
    assert subtask_reads == []


async def test_list_reports_subtask_counts(db, client_factory):
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}])
    await db.update_task_subtask("z", 1, status="done")

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/list", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert nodes["z"]["subtasks_total"] == 1
    assert nodes["z"]["subtasks_settled"] == 1
    assert nodes["hub"]["subtasks_total"] == 0


async def test_node_reports_subtask_counts(db, client_factory):
    await seed(db)
    await db.add_task_subtasks("z", "p1", [{"title": "one"}, {"title": "two"}])
    await db.update_task_subtask("z", 1, status="done")

    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/node/z?variant=all")
    assert r.status_code == 200
    node = r.json()["node"]
    assert node["subtasks_total"] == 2
    assert node["subtasks_settled"] == 1


async def test_tiles_reports_phase_fields_and_none_for_non_phase(db, client_factory):
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, {"order": 2, "label": "Build"})

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert nodes["e"]["phase_order"] == 2
    assert nodes["e"]["phase_label"] == "Build"
    # A task with no phase metadata carries None, not zero.
    assert nodes["z"]["phase_order"] is None
    assert nodes["z"]["phase_label"] is None


async def test_tiles_expose_bounded_failed_phase_hold_detail(db, client_factory):
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, {"order": 2, "label": "Build"})
    await db.transition_task("c0", TaskStatus.FAILED)

    async with client_factory() as ac:
        response = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert response.status_code == 200
    node = {item["id"]: item for item in response.json()["nodes"]}["e"]
    hold = node["phase_hold"]
    assert hold["phase_id"] == "e"
    assert hold["failed_children"] == [{"id": "c0", "status": "FAILED"}]
    assert hold["failed_children_total"] == 1
    assert hold["descendant_blocker_count"] >= 1
    assert {item["code"] for item in hold["remedies"]} == {"retry_or_reopen", "delete"}


async def test_tiles_malformed_phase_metadata_does_not_raise(db, client_factory):
    """A hand-edited or stale ``phase`` value must never 500 the response."""
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, "not-a-dict")
    await db.set_task_meta("z", PHASE_KEY, {"label": "no order"})
    await db.set_task_meta("hub", PHASE_KEY, {"order": "two"})

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    for tid in ("e", "z", "hub"):
        assert nodes[tid]["phase_order"] is None
        assert nodes[tid]["phase_label"] is None


async def test_tiles_phase_lookup_is_one_statement_regardless_of_visible_count(db, client_factory):
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, {"order": 1, "label": "Foundation"})

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    phase_reads = [s for s in statements if "task_metadata" in s]
    assert len(phase_reads) == 1, phase_reads


async def test_list_reports_phase_fields(db, client_factory):
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, {"order": 1, "label": "Foundation"})

    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/list", json=ALL)
    assert r.status_code == 200
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert nodes["e"]["phase_order"] == 1
    assert nodes["e"]["phase_label"] == "Foundation"
    assert nodes["z"]["phase_order"] is None


async def test_node_reports_phase_fields(db, client_factory):
    await seed(db)
    await db.set_task_meta("e", PHASE_KEY, {"order": 1, "label": "Foundation"})

    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/node/e?variant=all")
    assert r.status_code == 200
    node = r.json()["node"]
    assert node["phase_order"] == 1
    assert node["phase_label"] == "Foundation"


async def test_tiles_auto_expand_with_empty_expanded_returns_and_applies_active_set(
    db, client_factory
):
    """`auto_expand` with an empty `expanded` computes and applies the active set.

    In `seed`, g0 (inside pkg, inside e) is IN_PROGRESS, so both containers'
    `agg_running` rollups are nonzero -- `active_expansion` opens both,
    shallowest first.
    """
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "auto_expand": True})
    assert r.status_code == 200
    body = r.json()
    assert body["expanded_applied"] == ["e", "pkg"]
    kinds = {n["id"]: n["kind"] for n in body["nodes"]}
    # The computed set was actually used to resolve visibility: e and pkg
    # are opened, exposing their children rather than sitting collapsed.
    assert kinds["e"] == "container" and kinds["pkg"] == "container"
    assert {"c0", "c1", "g0", "g1"} <= set(kinds)


async def test_tiles_auto_expand_is_ignored_when_expanded_is_non_empty(db, client_factory):
    """A non-empty `expanded` -- even one that names nothing active -- wins.

    `expanded_applied` must be null so the client never mistakes an
    unrelated explicit expansion for one the server computed and applied.
    """
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={**ALL, "expanded": ["e"], "auto_expand": True},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["expanded_applied"] is None
    kinds = {n["id"]: n["kind"] for n in body["nodes"]}
    # Only the client's explicit expansion applied: e is open, but pkg --
    # which the active set would also have opened -- stays collapsed.
    assert kinds["e"] == "container"
    assert kinds.get("pkg") == "collapsed"


async def test_tiles_without_auto_expand_never_sets_expanded_applied(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    assert r.json()["expanded_applied"] is None


async def test_tiles_auto_expand_costs_exactly_one_statement_when_used_and_zero_otherwise(
    db, client_factory
):
    """`load_active_container_rows` runs once when `auto_expand` is honored,
    never when it isn't (design A3, fix F5) -- the same style of budget as
    `test_tiles_gate_lookup_is_two_statements_regardless_of_gate_count`.
    """
    await seed(db)

    def _active_container_reads(statements: list[str]) -> list[str]:
        # The WHERE clause SQLAlchemy renders for `load_active_container_rows`
        # is the only place in this endpoint that compares `agg_running` with
        # an inequality; every other read of `task_layouts` selects the whole
        # row (the column appears in the SELECT list) but never filters on it.
        return [s for s in statements if "agg_running >" in s]

    async with client_factory() as ac:
        # `expanded` is non-empty, so `auto_expand` must be ignored entirely --
        # zero added statements.
        statements_without: list[str] = []

        def _hook_without(conn, cursor, statement, parameters, context, executemany):
            statements_without.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", _hook_without)
        try:
            r1 = await ac.post(
                "/api/projects/p1/graph/tiles",
                json={**ALL, "auto_expand": True, "expanded": ["e"]},
            )
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", _hook_without)
        assert r1.status_code == 200
        assert r1.json()["expanded_applied"] is None
        assert _active_container_reads(statements_without) == []

        # `expanded` is empty, so `auto_expand` is honored -- exactly one
        # added statement.
        statements_with: list[str] = []

        def _hook_with(conn, cursor, statement, parameters, context, executemany):
            statements_with.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", _hook_with)
        try:
            r2 = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "auto_expand": True})
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", _hook_with)
        assert r2.status_code == 200
        assert r2.json()["expanded_applied"] is not None
        assert len(_active_container_reads(statements_with)) == 1


async def _nested_project(db):
    """``first{f0,f1}`` then ``p{a{a0,a1}, b}`` at the project root.

    Two containers ahead of what we look for, in reading order: the root
    scope's own packing moves ``p`` (and with it ``b``) when it is
    re-packed, and ``a``'s collapse moves ``b`` within ``p``. Entering
    ``p`` re-packs ``p``'s scope and leaves the root scope alone, so the
    focused geometry is genuinely a different one.
    """

    async def create(tid, parent=None):
        await db.create_task(
            Task(id=tid, project_id="p1", title=tid, description="", status=TaskStatus.DEFINED)
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    await create("first")
    await create("f0", "first")
    await create("f1", "first")
    await create("p")
    await create("a", "p")
    await create("a0", "a")
    await create("a1", "a")
    await create("b", "p")
    driver = LayoutDriver(db)
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")


async def test_tiles_report_the_variant_they_applied(db, client_factory):
    """The client cannot infer the promotion, so the response states it."""
    await seed(db)
    async with client_factory() as ac:
        root = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60},
                "expanded": [],
            },
        )
        focused = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "e",
            },
        )
        asked_for_all = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert root.json()["variant_applied"] == "active"
    assert focused.json()["variant_applied"] == "active"
    assert asked_for_all.json()["variant_applied"] == "all"


async def test_tiles_report_all_when_the_entered_container_forced_the_promotion(db, client_factory):
    """A container the active variant dropped: `active` was asked for, `all` served."""
    await _finished_epic_project(db, anchored=False)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "done",
            },
        )
    assert r.status_code == 200
    assert r.json()["variant_applied"] == "all"
    assert {node["id"] for node in r.json()["nodes"]} == {"done", "child"}


async def test_tiles_report_all_for_an_unfinished_container_the_active_view_stubbed(
    db, client_factory
):
    """The container's STATUS is not the signal, which is why this is reported.

    ``open`` is DEFINED — unfinished — but every descendant it has is
    finished, so the active layout keeps it as a stub and entering it is
    served from ``all``. A client inferring the promotion from the status
    would draw the completed children with no explanation.
    """

    async def create(tid, parent=None):
        await db.create_task(
            Task(id=tid, project_id="p1", title=tid, description="", status=TaskStatus.DEFINED)
        )
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    await create("open")
    await create("kid", "open")
    await create("live")
    await db.transition_task("kid", TaskStatus.COMPLETED, force=True)
    driver = LayoutDriver(db)
    await driver.full_layout("p1", "all")
    await driver.full_layout("p1", "active")

    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "active",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "open",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["variant_applied"] == "all"
    assert {node["id"] for node in body["nodes"]} == {"open", "kid"}
    assert next(n for n in body["nodes"] if n["id"] == "open")["status"] == "DEFINED"


async def test_list_root_pages_only_the_entered_container(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": [], "root": "e", "limit": 50},
        )
    assert r.status_code == 200
    body = r.json()
    kinds = {node["id"]: node["kind"] for node in body["nodes"]}
    assert set(kinds) == {"e", "c0", "c1", "pkg"}
    assert kinds["e"] == "container" and kinds["pkg"] == "collapsed"
    assert body["variant_applied"] == "all"


async def test_list_root_pages_every_child_across_pages(db, client_factory):
    """Paging inside a container must never truncate its own children."""
    await seed(db)
    seen: list[str] = []
    cursor = None
    async with client_factory() as ac:
        for _ in range(10):
            r = await ac.post(
                "/api/projects/p1/graph/list",
                json={"variant": "all", "expanded": [], "root": "e", "limit": 2, "cursor": cursor},
            )
            assert r.status_code == 200
            body = r.json()
            seen.extend(node["id"] for node in body["nodes"])
            cursor = body["next_cursor"]
            if not cursor:
                break
    assert set(seen) == {"e", "c0", "c1", "pkg"}
    assert "z" not in seen and "g0" not in seen


async def test_list_root_keeps_the_active_variant_and_reports_it(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "active", "expanded": [], "root": "e", "limit": 50},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["variant_applied"] == "active"
    assert {node["id"] for node in body["nodes"]} == {"e", "c0", "pkg"}


async def test_list_reports_all_for_a_finished_entered_scope(db, client_factory):
    """The phone list gets the same fallback signal as canvas tiles."""
    await _finished_epic_project(db, anchored=False)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "active", "expanded": [], "root": "done", "limit": 50},
        )
    body = r.json()
    assert body["variant_applied"] == "all"
    assert {node["id"] for node in body["nodes"]} == {"done", "child"}
    assert body["empty_reason"] is None


async def test_list_unknown_root_is_404(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/list",
            json={"variant": "all", "expanded": [], "root": "nope", "limit": 50},
        )
    assert r.status_code == 404


async def test_locate_places_a_hit_where_the_focused_view_draws_it(db, client_factory):
    """Hit boxes must come from the geometry the focused canvas draws."""
    await _nested_project(db)
    async with client_factory() as ac:
        tiles = await ac.post(
            "/api/projects/p1/graph/tiles",
            json={
                "variant": "all",
                "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                "expanded": [],
                "root": "p",
            },
        )
        focused = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "all", "q": "b", "expanded": [], "root": "p"},
        )
        rootless = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "all", "q": "b", "expanded": []},
        )
    drawn = next(node for node in tiles.json()["nodes"] if node["id"] == "b")
    hit = next(h for h in focused.json()["hits"] if h["id"] == "b")
    assert (hit["x"], hit["y"]) == (drawn["x"], drawn["y"])
    # ...and that is not where the un-focused geometry puts it, which is the
    # coordinate the canvas used to pan to.
    stale = next(h for h in rootless.json()["hits"] if h["id"] == "b")
    assert (stale["x"], stale["y"]) != (drawn["x"], drawn["y"])


async def test_locate_under_a_finished_root_searches_the_full_layout(db, client_factory):
    """Search inside an entered container the active variant dropped."""
    await _finished_epic_project(db, anchored=False)
    async with client_factory() as ac:
        r = await ac.post(
            "/api/projects/p1/graph/locate",
            json={"variant": "active", "q": "child", "expanded": [], "root": "done"},
        )
    assert r.status_code == 200
    assert [hit["id"] for hit in r.json()["hits"]] == ["child"]
