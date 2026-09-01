import pytest

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph.layout.model import LayoutRow, Translation, WriteSet


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "lq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def test_dirty_marks_round_trip(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1", "t2"], "task.created", conn=conn)
    assert await db.dirty_layout_projects() == ["p1"]
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert seq >= 2 and sorted(r[0] for r in rows) == ["t1", "t2"]


async def test_pop_respects_debounce(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1"], "task.created", conn=conn)
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=60)
    assert rows == [] and seq == 0


async def test_jobs_lifecycle(db):
    job = await db.enqueue_layout_job("p1", "all", "tidy")
    again = await db.enqueue_layout_job("p1", "all", "tidy")
    assert again["id"] == job["id"]
    nxt = await db.next_layout_job()
    assert nxt["id"] == job["id"] and nxt["status"] == "running"
    await db.finish_layout_job(job["id"], error=None)
    assert await db.next_layout_job() is None
    assert (await db.get_layout_job(job["id"]))["status"] == "done"


async def test_meta_absent_until_published(db):
    assert await db.get_layout_meta("p1", "all") is None


def row(tid, x, y, path, container=None, depth=0, w=1.0, h=1.0, kind="card"):
    return LayoutRow(task_id=tid, container_id=container, path=path, depth=depth, rank=0,
                     order_key="U", w=w, h=h, rel_x=x, rel_y=y, abs_x=x, abs_y=y, kind=kind)


async def test_snapshot_reads_tasks_containers_and_edges(db):
    await db.create_task(Task(id="e", project_id="p1", title="Epic", description=""))
    await db.create_task(Task(id="c", project_id="p1", title="Child", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c", "e", conn=conn)
    await db.add_dependency("c", "e", "discovered-from")
    tasks, edges = await db.load_project_snapshot("p1")
    assert tasks["e"].is_container and not tasks["c"].is_container
    assert tasks["c"].parent_id == "e"
    assert ("c", "e", "parent-child") in edges and ("c", "e", "discovered-from") in edges


async def test_publish_is_atomic_and_bumps_version(db):
    await db.create_task(Task(id="a", project_id="p1", title="A", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="B", description=""))
    ws = WriteSet(upserts=[row("a", 0, 0, "/a/"), row("b", 9, 0, "/b/")])
    v1 = await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(10, 1), node_count_delta=None)
    assert v1 == 1
    meta = await db.get_layout_meta("p1", "all")
    assert meta["node_count"] == 2 and meta["extent_w"] == 10
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert rows["b"].abs_x == 9
    cells = await db.load_cells("p1", "all", ["b"])
    assert cells == {("b"): [(1, 0)]}


async def test_translation_moves_subtree_and_rewrites_cells(db):
    for t in ("e", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container", w=3, h=3),
                           row("c", 0.5, 0.5, "/e/c/", container="e", depth=1)])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3), node_count_delta=None)
    ws2 = WriteSet(upserts=[row("e", 8, 0, "/e/", kind="container", w=3, h=3)],
                   translations=[Translation(path_prefix="/e/", dx=8.0, dy=0.0)])
    v = await db.publish_layout("p1", "all", ws2, consumed_seq=None, extent=(11, 3), node_count_delta=0)
    assert v == 2
    rows = await db.load_layout_rows("p1", "all", ["e", "c"])
    assert rows["e"].abs_x == 8.0 and rows["c"].abs_x == 8.5
    assert rows["c"].rel_x == 0.5  # rel coordinates untouched by translation
    assert (await db.load_cells("p1", "all", ["c"]))["c"] == [(1, 0)]


async def test_subtree_aggregates(db):
    for t in ("e", "c1", "c2"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    # A DEFINED container withholds its children (blocked-state §3.1); release
    # it first so c1/c2 start unblocked, matching the aggregates asserted below.
    await db.transition_task("e", TaskStatus.READY, force=True)
    async with db._engine.begin() as conn:
        await db.set_parent("c1", "e", conn=conn)
        await db.set_parent("c2", "e", conn=conn)
    await db.transition_task("c1", TaskStatus.COMPLETED, force=True)
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container"),
                           row("c1", 0, 0, "/e/c1/", "e", 1), row("c2", 1, 0, "/e/c2/", "e", 1)])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3), node_count_delta=None)
    agg = await db.subtree_aggregates("p1", "all", "/e/")
    assert agg == {"children": 2, "descendants": 2, "completed": 1, "running": 0, "blocked": 0, "active": 1}


async def test_publish_clears_consumed_dirty_rows(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["x"], "task.created", conn=conn)
    seq, _ = await db.pop_layout_dirty("p1", min_age_seconds=0)
    await db.publish_layout("p1", "all", WriteSet(), consumed_seq=seq, extent=(0, 0), node_count_delta=0)
    assert await db.dirty_layout_projects() == []
