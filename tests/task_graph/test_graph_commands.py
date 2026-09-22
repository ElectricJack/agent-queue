"""``graph_layout_rebuild`` / ``graph_tidy`` commands (spatial-layout design §5.6, §10)."""

from __future__ import annotations

from src.models import Project, Task
from src.task_graph.layout.driver import LayoutDriver


async def test_graph_layout_rebuild_and_tidy(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    await h._db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    r = await h.execute("graph_layout_rebuild", {"project_id": "p1"})
    assert r["success"] and r["versions"] == {"all": 1, "active": 1}
    r = await h.execute("graph_tidy", {"project_id": "p1"})
    assert r["success"] and {j["variant"] for j in r["jobs"]} == {"all", "active"}
    r = await h.execute("graph_layout_rebuild", {"project_id": "nope"})
    assert r["success"] is False


async def test_graph_layout_rebuild_refuses_unelevated_session(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
    r = await h._cmd_graph_layout_rebuild({"project_id": "p1"})
    assert r["success"] is False
    assert "not available to agent sessions" in r["error"]


async def test_graph_layout_rebuild_allows_elevated_session(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": True}
    r = await h._cmd_graph_layout_rebuild({"project_id": "p1"})
    assert r["success"] is True


async def test_graph_tidy_refuses_unelevated_session(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
    r = await h._cmd_graph_tidy({"project_id": "p1"})
    assert r["success"] is False
    assert "not available to agent sessions" in r["error"]


async def test_graph_tidy_allows_elevated_session(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": True}
    r = await h._cmd_graph_tidy({"project_id": "p1"})
    assert r["success"] is True


async def test_graph_tidy_all_creates_one_durable_request(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    await h._db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    driver = LayoutDriver(h._db)
    for variant in ("active", "all"):
        await driver.full_layout("p1", variant)

    first = await h.execute("graph_tidy", {"all": True})
    again = await h.execute("graph_tidy", {"all": True})

    assert first["success"] and first["request"]["total"] == 2
    assert first["request"]["pending"] == 2
    assert again["request"]["id"] == first["request"]["id"]
    assert (await h.execute("graph_tidy", {"project_id": "p1", "all": True}))["success"] is False
    assert (await h.execute("graph_tidy", {"all": True, "variant": "active"}))["success"] is False
