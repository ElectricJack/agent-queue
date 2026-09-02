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


async def test_next_layout_job_loses_race_cleanly(db):
    """A concurrent winner leaves the only queued row already 'running'.

    Interleaving two coroutines mid-transaction to hit next_layout_job's
    SELECT/UPDATE window directly isn't practical against SQLite's
    single-writer model, so this pre-flips the row the way a winning
    claimant would have left it, then confirms the loser gets None and
    does not touch started_at -- i.e. it never double-claims."""
    from sqlalchemy import update as sa_update

    from src.database.tables import layout_jobs

    job = await db.enqueue_layout_job("p1", "all", "tidy")
    async with db._engine.begin() as conn:
        result = await conn.execute(
            sa_update(layout_jobs)
            .where(layout_jobs.c.id == job["id"], layout_jobs.c.status == "queued")
            .values(status="running", started_at=0.0)
        )
        assert result.rowcount == 1
    assert await db.next_layout_job() is None
    row = await db.get_layout_job(job["id"])
    assert row["status"] == "running" and row["started_at"] == 0.0


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
    v1 = await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(10, 1))
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
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3))
    ws2 = WriteSet(upserts=[row("e", 8, 0, "/e/", kind="container", w=3, h=3)],
                   translations=[Translation(path_prefix="/e/", dx=8.0, dy=0.0)])
    v = await db.publish_layout("p1", "all", ws2, consumed_seq=None, extent=(11, 3))
    assert v == 2
    rows = await db.load_layout_rows("p1", "all", ["e", "c"])
    assert rows["e"].abs_x == 8.0 and rows["c"].abs_x == 8.5
    assert rows["c"].rel_x == 0.5  # rel coordinates untouched by translation
    assert (await db.load_cells("p1", "all", ["c"]))["c"] == [(1, 0)]


async def test_subtree_aggregates(db):
    for t in ("e", "c1", "c2", "g", "blocker"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    # A DEFINED container withholds its children (blocked-state §3.1); release
    # it first so c1/c2/g start unblocked unless independently blocked below.
    await db.transition_task("e", TaskStatus.READY, force=True)
    async with db._engine.begin() as conn:
        await db.set_parent("c1", "e", conn=conn)
        await db.set_parent("c2", "e", conn=conn)
        await db.set_parent("g", "c2", conn=conn)
    await db.transition_task("c1", TaskStatus.COMPLETED, force=True)
    await db.transition_task("c2", TaskStatus.IN_PROGRESS, force=True)
    # g blocks on a DEFINED root task -> g.is_blocked == 1, independent of
    # the parent-child withholding above (c2 is IN_PROGRESS, so g is not
    # withheld by its container; the "blocks" edge is what flips it).
    await db.add_dependency("g", "blocker", "blocks")
    ws = WriteSet(upserts=[
        row("e", 0, 0, "/e/", kind="container"),
        row("c1", 0, 0, "/e/c1/", "e", 1),
        row("c2", 1, 0, "/e/c2/", "e", 1, kind="container"),
        row("g", 0, 0, "/e/c2/g/", "c2", 2),
    ])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3))
    agg = await db.subtree_aggregates("p1", "/e/")
    assert agg == {
        "children": 2, "descendants": 3, "completed": 1, "running": 1,
        "blocked": 1, "active": 2,
    }


async def test_publish_clears_consumed_dirty_rows(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["x"], "task.created", conn=conn)
    seq, _ = await db.pop_layout_dirty("p1", min_age_seconds=0)
    await db.publish_layout("p1", "all", WriteSet(), consumed_seq=seq, extent=(0, 0))
    assert await db.dirty_layout_projects() == []


async def test_upsert_then_translation_of_the_same_node_leaves_no_ghost_cells(db):
    """A node can be both upserted (at its pre-translation absolute frame)
    and covered by a translation whose post-UPDATE re-SELECT reports its new
    frame. Recording both positions would insert two sets of cells, and the
    stale set would keep answering tile queries for a box nothing occupies."""
    from src.task_graph.layout.flow import cells_for_box

    for t in ("x", "b", "bc"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    ws = WriteSet(upserts=[
        row("x", 0, 0, "/x/", kind="container", w=40, h=40),
        row("b", 0, 0, "/x/b/", container="x", depth=1, kind="container", w=20, h=20),
        row("bc", 1, 1, "/x/b/bc/", container="b", depth=2),
    ])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(40, 40))
    assert (await db.load_cells("p1", "all", ["bc"]))["bc"] == cells_for_box(1, 1, 1, 1)

    # The child is upserted at its OLD frame while "/x/b/" translates by
    # (+30, +20): the translation is what carries it to its real position.
    ws2 = WriteSet(
        upserts=[row("bc", 1, 1, "/x/b/bc/", container="b", depth=2)],
        translations=[Translation(path_prefix="/x/b/", dx=30.0, dy=20.0)],
    )
    await db.publish_layout("p1", "all", ws2, consumed_seq=None, extent=(80, 80))
    rows = await db.load_layout_rows("p1", "all", ["bc"])
    assert (rows["bc"].abs_x, rows["bc"].abs_y) == (31.0, 21.0)
    assert (await db.load_cells("p1", "all", ["bc"]))["bc"] == cells_for_box(31.0, 21.0, 1.0, 1.0)


async def test_pop_layout_dirty_is_capped_and_leaves_the_rest(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", [f"t{i}" for i in range(1500)], "task.created", conn=conn)
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert len(rows) == 1000
    # The returned seq covers exactly the rows returned, so consuming it
    # retires those and no more.
    async with db._engine.begin() as conn:
        await db.clear_layout_dirty("p1", seq, conn=conn)
    seq2, rest = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert len(rest) == 500 and seq2 > seq
    assert {r[0] for r in rows} | {r[0] for r in rest} == {f"t{i}" for i in range(1500)}


async def test_trim_layout_dirty_discards_everything(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["a", "b", "c"], "task.created", conn=conn)
    assert await db.trim_layout_dirty() == 3
    assert await db.dirty_layout_projects() == []


async def test_rows_in_cells_and_prefixes(db):
    for t in ("e", "c", "far"):
        await db.create_task(Task(id=t, project_id="p1", title=t.upper(), description=""))
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container", w=3, h=3),
                           row("c", 0.5, 0.5, "/e/c/", "e", 1), row("far", 40, 40, "/far/")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(41, 41))
    assert set(await db.load_rows_in_cells("p1", "all", [(0, 0)])) == {"e", "c"}
    assert set(await db.load_rows_in_cells("p1", "all", [(5, 5)])) == {"far"}
    assert set(await db.load_rows_by_prefixes("p1", "all", ["/e/"])) == {"e", "c"}
    with_tasks = await db.load_rows_with_tasks("p1", "all", ["c"])
    assert with_tasks["c"][1]["title"] == "C"


async def test_edges_touching_and_matching(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=f"Task {t}", description=""))
    await db.add_dependency("b", "a", description="why")
    await db.add_dependency("c", "b")
    edges = await db.load_edges_touching(["a"])
    assert edges == [("b", "a", "blocks", "why")]
    ws = WriteSet(upserts=[row(t, i, 0, f"/{t}/") for i, t in enumerate("abc")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 1))
    assert await db.load_matching_ids("p1", "all", q="task b", status="") == {"b"}
    assert await db.load_matching_ids("p1", "all", q="", status="DEFINED") == {"a", "b", "c"}


async def test_matching_ids_treats_like_metacharacters_literally(db):
    await db.create_task(Task(id="pct", project_id="p1", title="Done 50% of it", description=""))
    await db.create_task(Task(id="und", project_id="p1", title="Done 50x of it", description=""))
    ws = WriteSet(upserts=[row("pct", 0, 0, "/pct/"), row("und", 1, 0, "/und/")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(2, 1))
    assert await db.load_matching_ids("p1", "all", q="50%", status="") == {"pct"}
    assert await db.load_matching_ids("p1", "all", q="50x", status="") == {"und"}
    assert await db.load_matching_ids("p1", "all", q="50_", status="") == set()
