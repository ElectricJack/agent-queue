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


async def test_incremental_adds_child_without_moving_siblings(db):
    kids = await seed_epic(db, n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    before = await db.load_layout_rows("p1", "all", kids)
    await db.create_task(Task(id="e-new", project_id="p1", title="new", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-new", "e", conn=conn)
        await db.mark_layout_dirty("p1", ["e-new"], "task.created", conn=conn)
    versions = await drv.process_dirty("p1", min_age_seconds=0)
    assert versions["all"] == 2
    after = await db.load_layout_rows("p1", "all", [*kids, "e-new", "e"])
    for k in kids:
        assert after[k].ordinal == before[k].ordinal
        assert (after[k].abs_x, after[k].abs_y) == (before[k].abs_x, before[k].abs_y)
    assert after["e-new"].container_id == "e" and after["e"].agg_children == 4
    assert await db.dirty_layout_projects() == []


async def test_incremental_growth_translates_later_top_level_siblings(db):
    # Epic "e" is first at root; "z" is a later root card. Grow "e" past its band.
    kids = await seed_epic(db, n=2)
    await db.create_task(Task(id="z", project_id="p1", title="z", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    z_before = (await db.load_layout_rows("p1", "all", ["z"]))["z"]
    e_before = (await db.load_layout_rows("p1", "all", ["e"]))["e"]
    new_ids = []
    for i in range(12):  # enough to cross a growth band
        cid = f"e-x{i}"
        await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(cid, "e", conn=conn)
            await db.mark_layout_dirty("p1", [cid], "task.created", conn=conn)
        new_ids.append(cid)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", ["e", "z", *kids, *new_ids])
    assert rows["e"].h > e_before.h or rows["e"].w > e_before.w
    assert rows["z"].ordinal == z_before.ordinal
    # z either stayed (same line) or translated; it never overlaps e.
    assert not (rows["z"].abs_x < rows["e"].abs_x + rows["e"].w and
                rows["z"].abs_y < rows["e"].abs_y + rows["e"].h and
                rows["z"].abs_x + rows["z"].w > rows["e"].abs_x and
                rows["z"].abs_y + rows["z"].h > rows["e"].abs_y)
    for k in kids:
        assert rows[k].abs_x >= rows["e"].abs_x and rows[k].abs_y >= rows["e"].abs_y


async def test_status_change_updates_active_variant_and_aggregates(db):
    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await db.transition_task(kids[0], TaskStatus.COMPLETED, force=True)
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", [kids[0]], "status.finished", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    active = await db.load_layout_rows("p1", "active", [*kids, "e"])
    assert kids[0] not in active and kids[1] in active
    assert active["e"].agg_completed == 1 and active["e"].agg_active == 1
    allv = await db.load_layout_rows("p1", "all", [*kids, "e"])
    assert kids[0] in allv and allv["e"].agg_completed == 1


async def test_parent_change_moves_subtree_between_containers(db):
    a_kids = await seed_epic(db, epic="a", n=1)
    await seed_epic(db, epic="b", n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.set_parent(a_kids[0], "b", conn=conn)
        await db.mark_layout_dirty("p1", [a_kids[0]], "parent.changed:a", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", [a_kids[0], "a", "b"])
    assert rows[a_kids[0]].container_id == "b" and rows[a_kids[0]].path == f"/b/{a_kids[0]}/"
    assert rows["a"].agg_children == 0 and rows["b"].agg_children == 2


async def test_process_dirty_respects_debounce(db):
    await seed_epic(db, n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["e"], "task.updated", conn=conn)
    assert (await drv.process_dirty("p1", min_age_seconds=3600)) == {"all": None, "active": None}


def _inside(outer, inner):
    return (outer.abs_x <= inner.abs_x and inner.abs_x + inner.w <= outer.abs_x + outer.w
            and outer.abs_y <= inner.abs_y and inner.abs_y + inner.h <= outer.abs_y + outer.h)


async def test_moved_container_relays_its_whole_subtree(db):
    await seed_epic(db, epic="a", n=1)
    await seed_epic(db, epic="b", n=1)
    await db.create_task(Task(id="deep", project_id="p1", title="deep", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("deep", "a-c0", conn=conn)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.set_parent("a-c0", "b", conn=conn)
        await db.mark_layout_dirty("p1", ["a-c0"], "parent.changed:a", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", ["a", "b", "a-c0", "deep"])
    assert rows["a-c0"].path == "/b/a-c0/" and rows["deep"].path == "/b/a-c0/deep/"
    assert _inside(rows["b"], rows["a-c0"]) and _inside(rows["a-c0"], rows["deep"])
    assert not _inside(rows["a"], rows["a-c0"])


async def test_container_collapses_to_stub_then_reopens_in_active(db):
    kids = await seed_epic(db, epic="e", n=2)
    await db.create_task(Task(id="gc", project_id="p1", title="gc", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("gc", kids[0], conn=conn)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    for tid in ("gc", kids[0], kids[1], "e"):
        await db.transition_task(tid, TaskStatus.COMPLETED, force=True)
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["gc", *kids, "e"], "status.finished", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "active", ["e", *kids, "gc"])
    assert set(rows) == {"e"}
    assert rows["e"].kind == "stub" and (rows["e"].w, rows["e"].h) == (1.0, 1.0)

    await db.transition_task("gc", TaskStatus.READY, force=True)
    await db.transition_task(kids[0], TaskStatus.READY, force=True)
    await db.transition_task("e", TaskStatus.READY, force=True)
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["gc", kids[0], "e"], "status.reopened", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "active", ["e", *kids, "gc"])
    assert set(rows) == {"e", kids[0], "gc"}
    assert rows["e"].kind == "container"
    assert _inside(rows["e"], rows[kids[0]]) and _inside(rows[kids[0]], rows["gc"])
