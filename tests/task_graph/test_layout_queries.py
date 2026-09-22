import time

import pytest
from sqlalchemy import inspect

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph.layout.model import LayoutRow, Translation, WriteSet
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    d = Database(lease_dsn("lq.db"))
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


async def test_layout_job_ledger_index_starts_with_the_kind_filter(db):
    """The persistent rules ledger must not scan every historic job."""
    async with db._engine.connect() as conn:
        indexes = await conn.run_sync(
            lambda sync: {
                index["name"]: index["column_names"]
                for index in inspect(sync).get_indexes("layout_jobs")
            }
        )
    assert indexes["idx_layout_jobs_kind_project_variant_status"] == [
        "kind",
        "project_id",
        "variant",
        "status",
    ]


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
    assert await db.load_paths_by_prefixes("p1", "all", ["/e/"]) == {"e": "/e/", "c": "/e/c/"}
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


async def test_edges_touching_drops_rows_that_share_an_owner(db):
    """With an owner map the database returns only edges that get drawn.

    ``a``/``b`` sit inside collapsed container ``box``; ``far`` is outside it.
    The a->b edge is drawn nowhere (both endpoints dock at ``box``), so it
    must not cross the wire at all, while the edge leaving the container and
    the edge to an endpoint with no owner row both survive.
    """
    for t in ("box", "a", "b", "far", "outside"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")  # inside the collapsed container
    await db.add_dependency("far", "b")  # leaves it
    await db.add_dependency("outside", "a")  # leaves it, to an unowned endpoint
    ids = ["box", "a", "b", "far"]
    owners = {"box": "box", "a": "box", "b": "box", "far": "far"}
    assert await db.load_edges_touching(ids, owners=owners) == [
        ("far", "b", "blocks", None),
        ("outside", "a", "blocks", None),
    ]
    # Without the map the caller still gets every touching row, the b->a one
    # included -- that is the arm `remap_edges` then has to discard.
    assert len(await db.load_edges_touching(ids)) == 3


async def test_matching_ids_treats_like_metacharacters_literally(db):
    await db.create_task(Task(id="pct", project_id="p1", title="Done 50% of it", description=""))
    await db.create_task(Task(id="und", project_id="p1", title="Done 50x of it", description=""))
    ws = WriteSet(upserts=[row("pct", 0, 0, "/pct/"), row("und", 1, 0, "/und/")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(2, 1))
    assert await db.load_matching_ids("p1", "all", q="50%", status="") == {"pct"}
    assert await db.load_matching_ids("p1", "all", q="50x", status="") == {"und"}
    assert await db.load_matching_ids("p1", "all", q="50_", status="") == set()


async def test_matching_ids_escapes_metacharacters_inside_a_title(db):
    """A literal '%' in a title is matched by a literal '%' needle only."""
    await db.create_task(Task(id="pd", project_id="p1", title="100%_done", description=""))
    await db.create_task(Task(id="px", project_id="p1", title="100x1done", description=""))
    ws = WriteSet(upserts=[row("pd", 0, 0, "/pd/"), row("px", 1, 0, "/px/")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(2, 1))
    assert await db.load_matching_ids("p1", "all", q="100%", status="") == {"pd"}
    assert await db.load_matching_ids("p1", "all", q="100x", status="") == {"px"}
    # the '_' is literal too: it must not stand in for the 'x' in "100x1done"
    assert await db.load_matching_ids("p1", "all", q="100%_", status="") == {"pd"}


async def test_load_layout_rows_is_chunked(db):
    """1,000 ids resolve in one call (the IN-list is split under the hood)."""
    ids = [f"t{i:04d}" for i in range(1000)]
    for t in ids:
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    ws = WriteSet(upserts=[row(t, i, 0, f"/{t}/") for i, t in enumerate(ids)])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(1000, 1))
    rows = await db.load_layout_rows("p1", "all", ids)
    assert len(rows) == 1000 and rows["t0999"].abs_x == 999
    paths = await db.load_paths_by_ids("p1", "all", ids)
    assert len(paths) == 1000 and paths["t0500"] == "/t0500/"
    assert await db.load_paths_by_ids("p1", "all", []) == {}


async def test_matching_rows_ordered_caps_in_sql(db):
    for i, t in enumerate(("a", "b", "c")):
        await db.create_task(Task(id=t, project_id="p1", title=f"Task {t}", description=""))
    # reading order is (abs_y, abs_x, task_id) -- seeded out of that order
    ws = WriteSet(
        upserts=[row("a", 5, 9, "/a/"), row("b", 1, 1, "/b/"), row("c", 4, 1, "/c/")]
    )
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(9, 10))
    rows, truncated = await db.load_matching_rows_ordered(
        "p1", "all", q="task", status="", limit=2
    )
    assert [r.task_id for r in rows] == ["b", "c"] and truncated is True
    rows, truncated = await db.load_matching_rows_ordered(
        "p1", "all", q="task", status="", limit=3
    )
    assert [r.task_id for r in rows] == ["b", "c", "a"] and truncated is False
    rows, truncated = await db.load_matching_rows_ordered(
        "p1", "all", q="", status="DEFINED", limit=10
    )
    assert [r.task_id for r in rows] == ["b", "c", "a"] and truncated is False


async def test_snapshot_carries_phase_order(db):
    await db.create_task(Task(id="p1a", project_id="p1", title="Phase 1", description=""))
    await db.create_task(Task(id="loose", project_id="p1", title="Loose", description=""))
    await db.set_task_meta("p1a", "phase", {"order": 2, "label": "Build"})
    tasks, _ = await db.load_project_snapshot("p1")
    assert tasks["p1a"].phase_order == 2
    assert tasks["loose"].phase_order is None


async def test_snapshot_reads_metadata_in_one_statement(db):
    """The snapshot load is on the 5-second dirty path, not just the full
    layout: container flags and phase orders share ONE ``task_metadata``
    read, and the whole load stays at three statements."""
    from sqlalchemy import event

    await db.create_task(Task(id="ph", project_id="p1", title="Phase", description=""))
    await db.create_task(Task(id="c", project_id="p1", title="Child", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c", "ph", conn=conn)
    await db.set_task_meta("ph", "phase", {"order": 1, "label": "Build"})

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        tasks, _ = await db.load_project_snapshot("p1")
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert tasks["ph"].is_container and tasks["ph"].phase_order == 1
    meta_reads = [s for s in statements if "task_metadata" in s]
    assert len(meta_reads) == 1, meta_reads
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 3, selects


async def test_the_ledger_ignores_failed_and_other_kinds(db):
    """The ledger answers "converged", not "a job once existed".

    A ``failed`` job is not convergence (the rebuild never happened) but it
    does spend retry budget, and a job of a different kind — an operator
    tidy, a backfill, an older rules version — says nothing about the
    current engine rules.
    """
    kind = "rules:1"
    assert await db.layout_job_ledger(kind) == {}

    tidy = await db.enqueue_layout_job("p1", "all", "tidy")
    await db.finish_layout_job(tidy["id"], error=None)
    older = await db.enqueue_layout_job("p1", "all", "rules:0")
    await db.finish_layout_job(older["id"], error=None)
    assert await db.layout_job_ledger(kind) == {}

    failed = await db.enqueue_layout_job("p1", "all", kind)
    await db.finish_layout_job(failed["id"], error="boom")
    entry = (await db.layout_job_ledger(kind))[("p1", "all")]
    assert entry == {"settled": False, "in_flight": False, "failed": 1, "last_error": "boom"}

    queued = await db.enqueue_layout_job("p1", "all", kind)
    ledger = await db.layout_job_ledger(kind)
    assert ledger[("p1", "all")]["settled"] and ledger[("p1", "all")]["in_flight"]
    assert set(ledger) == {("p1", "all")}  # per (project, variant), nothing else

    await db.finish_layout_job(queued["id"], error=None)
    entry = (await db.layout_job_ledger(kind))[("p1", "all")]
    assert entry["settled"] and not entry["in_flight"] and entry["failed"] == 1


async def test_published_layout_variants_lists_every_published_pair(db):
    from src.task_graph.layout.driver import LayoutDriver

    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    assert await db.published_layout_variants() == set()
    await LayoutDriver(db).full_layout("p1", "all")
    assert await db.published_layout_variants() == {("p1", "all")}


async def test_reap_stale_layout_jobs_only_takes_the_long_running_ones(db):
    """Nothing else resets a ``running`` row, so a daemon killed mid-rebuild
    would leave one forever."""
    from sqlalchemy import update as sa_update

    from src.database.tables import layout_jobs

    orphan = await db.enqueue_layout_job("p1", "all", "rules:1")
    live = await db.enqueue_layout_job("p1", "active", "tidy")
    queued = await db.enqueue_layout_job("p2", "all", "rules:1")
    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            sa_update(layout_jobs)
            .where(layout_jobs.c.id == orphan["id"])
            .values(status="running", started_at=now - 500)
        )
        await conn.execute(
            sa_update(layout_jobs)
            .where(layout_jobs.c.id == live["id"])
            .values(status="running", started_at=now - 5)
        )

    reaped = await db.reap_stale_layout_jobs(started_before=now - 240, error="orphaned: boom")

    assert [r["id"] for r in reaped] == [orphan["id"]]
    assert reaped[0]["kind"] == "rules:1" and reaped[0]["variant"] == "all"
    row = await db.get_layout_job(orphan["id"])
    assert row["status"] == "failed" and row["error"] == "orphaned: boom"
    assert (await db.get_layout_job(live["id"]))["status"] == "running"
    assert (await db.get_layout_job(queued["id"]))["status"] == "queued"


async def test_a_rules_job_runs_a_full_layout(db):
    """``kind`` is a ledger label only: the job path never looks at it."""
    from src.task_graph.layout.driver import LayoutDriver

    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="b", description=""))
    drv = LayoutDriver(db)
    first = await drv.full_layout("p1", "all")
    assert (await db.get_layout_meta("p1", "all"))["layout_version"] == first

    job = await db.enqueue_layout_job("p1", "all", "rules:1")
    claimed = await db.next_layout_job()
    assert claimed["id"] == job["id"] and claimed["kind"] == "rules:1"
    await drv.full_layout(claimed["project_id"], claimed["variant"])
    await db.finish_layout_job(claimed["id"], error=None)

    meta = await db.get_layout_meta("p1", "all")
    assert meta["layout_version"] == first + 1
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert set(rows) == {"a", "b"}
    assert (await db.layout_job_ledger("rules:1"))[("p1", "all")]["settled"] is True
