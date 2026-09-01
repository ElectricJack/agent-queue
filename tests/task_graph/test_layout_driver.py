import pytest

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "drv.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def seed_epic(db, epic="e", n=3, completed=0):
    await db.create_task(Task(id=epic, project_id="p1", title=epic, description=""))
    kids = []
    for i in range(n):
        cid = f"{epic}-c{i}"
        await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(cid, epic, conn=conn)
        kids.append(cid)
    for cid in kids[:completed]:
        await db.transition_task(cid, TaskStatus.COMPLETED, force=True)
    return kids


async def test_full_layout_nests_children_inside_container(db):
    kids = await seed_epic(db, n=3)
    v = await LayoutDriver(db).full_layout("p1", "all")
    assert v == 1
    rows = await db.load_layout_rows("p1", "all", ["e", *kids])
    e = rows["e"]
    assert e.kind == "container" and e.depth == 0 and e.path == "/e/"
    for k in kids:
        r = rows[k]
        assert r.container_id == "e" and r.depth == 1 and r.path == f"/e/{k}/"
        assert e.abs_x <= r.abs_x and r.abs_x + r.w <= e.abs_x + e.w
        assert e.abs_y <= r.abs_y and r.abs_y + r.h <= e.abs_y + e.h
    assert e.agg_children == 3 and e.agg_descendants == 3 and e.agg_active == 3


async def test_active_variant_excludes_finished_and_stubs_finished_epics(db):
    await seed_epic(db, epic="done", n=2, completed=2)
    kids = await seed_epic(db, epic="live", n=2, completed=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    rows = await db.load_layout_rows("p1", "active", ["done", "done-c0", "live", *kids])
    assert rows["done"].kind == "stub" and "done-c0" not in rows
    assert rows["live"].kind == "container"
    assert kids[0] not in rows and kids[1] in rows


async def test_full_layout_places_top_level_dependents_below_blockers(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")
    await LayoutDriver(db).full_layout("p1", "all")
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert rows["b"].rank == 1 and rows["b"].abs_y > rows["a"].abs_y


async def test_empty_project_publishes_empty_meta(db):
    v = await LayoutDriver(db).full_layout("p1", "all")
    meta = await db.get_layout_meta("p1", "all")
    assert v == 1 and meta["node_count"] == 0
