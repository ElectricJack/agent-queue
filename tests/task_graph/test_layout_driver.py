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


async def _make_container(db, cid, parent=None, n=0):
    """Create ``cid`` (optionally under ``parent``) with ``n`` card children."""
    await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
    if parent is not None:
        async with db._engine.begin() as conn:
            await db.set_parent(cid, parent, conn=conn)
    kids = []
    for i in range(n):
        kid = f"{cid}-k{i}"
        await db.create_task(Task(id=kid, project_id="p1", title=kid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(kid, cid, conn=conn)
        kids.append(kid)
    return kids


def _assert_nested(rows, container_id):
    """Every child row of ``container_id`` sits inside its box and under its path."""
    parent = rows[container_id]
    children = [r for r in rows.values() if r.container_id == container_id]
    assert children, f"{container_id} has no child rows"
    for child in children:
        assert child.path.startswith(parent.path), (child.task_id, child.path, parent.path)
        assert _inside(parent, child), (child.task_id, (child.abs_x, child.abs_y, child.w,
                                                        child.h), (parent.abs_x, parent.abs_y,
                                                                   parent.w, parent.h))


async def test_moved_big_container_keeps_its_children_inside_it(db):
    # b is a root container holding two cards; m is a fat container elsewhere.
    await _make_container(db, "b", n=2)
    await _make_container(db, "s", n=0)
    await _make_container(db, "m", parent="s", n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.set_parent("m", "b", conn=conn)
        await db.mark_layout_dirty("p1", ["m"], "parent.changed:s", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    ids = ["b", "b-k0", "b-k1", "m", "m-k0", "m-k1", "m-k2"]
    rows = await db.load_layout_rows("p1", "all", ids)
    assert rows["m"].path == "/b/m/"
    _assert_nested(rows, "m")
    _assert_nested(rows, "b")


async def test_container_created_with_children_in_one_batch(db):
    await _make_container(db, "b", n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    kids = await _make_container(db, "m", parent="b", n=3)
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["m", *kids], "task.created", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", ["b", "b-k0", "b-k1", "m", *kids])
    assert rows["m"].path == "/b/m/" and rows["m"].kind == "container"
    _assert_nested(rows, "m")
    _assert_nested(rows, "b")


async def test_dirty_marks_survive_a_failure_in_a_later_variant(db, monkeypatch):
    from src.task_graph.layout import driver as driver_mod

    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await db.create_task(Task(id="e-new", project_id="p1", title="new", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-new", "e", conn=conn)
        await db.mark_layout_dirty("p1", ["e-new"], "task.created", conn=conn)

    real_run = driver_mod._IncrementalBatch.run

    async def boom(self, consumed_seq):
        if self.variant == "active":
            raise RuntimeError("active exploded")
        return await real_run(self, consumed_seq)

    monkeypatch.setattr(driver_mod._IncrementalBatch, "run", boom)
    with pytest.raises(RuntimeError, match="active exploded"):
        await drv.process_dirty("p1", min_age_seconds=0)
    assert await db.dirty_layout_projects() == ["p1"]

    monkeypatch.undo()
    versions = await drv.process_dirty("p1", min_age_seconds=0)
    assert versions["all"] is not None and versions["active"] is not None
    assert await db.dirty_layout_projects() == []
    rows = await db.load_layout_rows("p1", "active", ["e-new", *kids])
    assert "e-new" in rows


async def test_relay_depth_exceeded_aborts_without_publishing(db, monkeypatch):
    from src.database.queries import hierarchy_queries
    from src.task_graph.layout import driver as driver_mod

    # A 3-level moved subtree needs more headroom than the product cap allows.
    monkeypatch.setattr(hierarchy_queries, "MAX_STRUCTURAL_DEPTH", 6)
    await _make_container(db, "b", n=1)
    await _make_container(db, "a", n=0)
    await _make_container(db, "m", parent="a", n=0)
    await _make_container(db, "c", parent="m", n=0)
    await _make_container(db, "g", parent="c", n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    before = await db.get_layout_meta("p1", "all")
    async with db._engine.begin() as conn:
        await db.set_parent("m", "b", conn=conn)
        await db.mark_layout_dirty("p1", ["m"], "parent.changed:a", conn=conn)
    monkeypatch.setattr(driver_mod._IncrementalBatch, "MAX_RELAY_ROUNDS", 1)
    with pytest.raises(driver_mod.LayoutRelayDepthExceeded):
        await drv.process_dirty("p1", min_age_seconds=0)
    after = await db.get_layout_meta("p1", "all")
    assert after["layout_version"] == before["layout_version"]
    assert await db.dirty_layout_projects() == ["p1"]


async def test_load_subtree_ids_escapes_like_wildcards(db):
    await _make_container(db, "a_b", n=1)
    await _make_container(db, "aXb", n=1)
    await LayoutDriver(db).full_layout("p1", "all")
    assert sorted(await db.load_subtree_ids("p1", "all", "/a_b/")) == ["a_b", "a_b-k0"]
    assert sorted(await db.load_subtree_ids("p1", "all", "/aXb/")) == ["aXb", "aXb-k0"]


async def test_reconcile_repairs_a_deleted_row(db):
    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    from sqlalchemy import delete

    from src.database.tables import task_layouts
    async with db._engine.begin() as conn:
        await conn.execute(delete(task_layouts).where(task_layouts.c.task_id == kids[0]))
    assert await drv.reconcile("p1") == 1
    await drv.process_dirty("p1", min_age_seconds=0)
    assert kids[0] in await db.load_layout_rows("p1", "all", kids)


async def test_reconcile_with_no_layout_yet_is_a_noop(db):
    await seed_epic(db, n=2)
    assert await LayoutDriver(db).reconcile("p1") == 0


async def test_full_layout_purges_rows_left_by_a_different_project(db):
    from src.models import Project, Task
    from src.task_graph.layout.model import LayoutRow, WriteSet

    await db.create_project(Project(id="p2", name="P2"))
    await db.create_task(Task(id="x", project_id="p2", title="x", description=""))

    kids = await seed_epic(db, n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")

    # Publish a stray row for a task that belongs to p2 under p1's layout —
    # the project_id/variant key allows it even though "x" is not in p1.
    stray = LayoutRow(
        task_id="x", container_id=None, path="/x/", depth=0, rank=0, order_key="0",
        w=1, h=1, rel_x=0, rel_y=0, abs_x=0, abs_y=0, kind="card",
    )
    await db.publish_layout(
        "p1", "all", WriteSet(upserts=[stray]), consumed_seq=None, extent=(1, 1),
    )
    assert "x" in await db.load_subtree_rows("p1", "all")

    await drv.full_layout("p1", "all")
    rows = await db.load_subtree_rows("p1", "all")
    assert "x" not in rows
    for k in ("e", *kids):
        assert k in rows


async def test_reparenting_freshly_created_task_does_not_relay_root(db, monkeypatch):
    """A task created then reparented under an epic in one un-drained batch
    (``parent.changed:-``) must not force a full root re-lay: the task never
    had a stored layout row at root (nothing to clean up there), so root's
    other siblings shouldn't be touched. Regression test for the ``_seed_queue``
    fix that stopped trusting the dirty-mark reason string's old-parent id."""
    from src.task_graph.layout import driver as driver_mod

    await seed_epic(db, n=3)
    await db.create_task(Task(id="z", project_id="p1", title="z", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    # full_layout doesn't consume dirty marks — drain the ones left behind
    # by seed_epic's task creations before installing the spy, so only the
    # e-new create+reparent below is observed.
    await drv.process_dirty("p1", min_age_seconds=0)

    calls: list[str | None] = []
    orig = driver_mod.layout_container

    def spy(scope, *, mode, seed):
        calls.append(scope.container_id)
        return orig(scope, mode=mode, seed=seed)

    monkeypatch.setattr(driver_mod, "layout_container", spy)

    await db.create_task(Task(id="e-new", project_id="p1", title="new", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-new", "e", conn=conn)

    versions = await drv.process_dirty("p1", min_age_seconds=0)
    assert versions["all"] is not None
    assert None not in calls, f"root was re-laid unnecessarily: {calls}"
    assert "e" in calls


async def test_deleting_middle_child_closes_the_gap_and_updates_aggregates(db):
    """Delete drops the task's layout rows in its own transaction, so the
    driver can no longer find the former container from a stored row — the
    delete path has to mark the surviving PARENT as well. Without that the
    remaining children keep their old coordinates (a hole where the deleted
    card was) and the epic's aggregates stay at the pre-delete counts."""
    kids = await seed_epic(db, n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)

    await db.delete_task(kids[1])
    await drv.process_dirty("p1", min_age_seconds=0)

    rows = await db.load_layout_rows("p1", "all", ["e", *kids])
    assert kids[1] not in rows
    assert rows[kids[0]].rel_x == 0.0
    assert rows[kids[2]].rel_x == pytest.approx(1.2)
    assert rows["e"].agg_children == 2 and rows["e"].agg_descendants == 2


async def test_archiving_a_completed_child_closes_the_gap_and_updates_aggregates(db):
    kids = await seed_epic(db, n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)

    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)
    assert await db.archive_task(kids[1])
    await drv.process_dirty("p1", min_age_seconds=0)

    rows = await db.load_layout_rows("p1", "all", ["e", *kids])
    assert kids[1] not in rows
    assert rows[kids[0]].rel_x == 0.0
    assert rows[kids[2]].rel_x == pytest.approx(1.2)
    assert rows["e"].agg_children == 2 and rows["e"].agg_descendants == 2


async def test_deleting_a_root_task_closes_the_gap(db):
    """Root-level tasks have no parent, so ``_layout_parent_ids`` marks
    nothing but the deleted task itself — the driver has to fall back to
    dirtying root directly, or the surviving siblings keep their stale
    rel_x and root's extent never shrinks. Regression test for the
    ``_seed_queue`` "task is gone" branch."""
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)
    meta_before = await db.get_layout_meta("p1", "all")

    await db.delete_task("a")
    await drv.process_dirty("p1", min_age_seconds=0)

    for variant in ("all", "active"):
        rows = await db.load_layout_rows("p1", variant, ["b", "c"])
        assert rows["b"].rel_x == 0.0
        assert rows["c"].rel_x == pytest.approx(1.2)
    meta_after = await db.get_layout_meta("p1", "all")
    assert meta_after["extent_w"] < meta_before["extent_w"]


async def test_archiving_a_completed_root_task_closes_the_gap(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)
    meta_before = await db.get_layout_meta("p1", "all")

    await db.transition_task("a", TaskStatus.COMPLETED, force=True)
    assert await db.archive_task("a")
    await drv.process_dirty("p1", min_age_seconds=0)

    rows = await db.load_layout_rows("p1", "all", ["b", "c"])
    assert rows["b"].rel_x == 0.0
    assert rows["c"].rel_x == pytest.approx(1.2)
    meta_after = await db.get_layout_meta("p1", "all")
    assert meta_after["extent_w"] < meta_before["extent_w"]


async def test_deleting_a_nested_container_updates_its_parents_aggregates(db):
    await seed_epic(db, n=1)  # e / e-c0
    await db.create_task(Task(id="pkg", project_id="p1", title="pkg", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("pkg", "e", conn=conn)
    for i in range(2):
        tid = f"pkg-c{i}"
        await db.create_task(Task(id=tid, project_id="p1", title=tid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(tid, "pkg", conn=conn)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)
    before = (await db.load_layout_rows("p1", "all", ["e"]))["e"]
    assert (before.agg_children, before.agg_descendants) == (2, 4)

    await db.delete_task("pkg", cascade=True)
    await drv.process_dirty("p1", min_age_seconds=0)

    rows = await db.load_subtree_rows("p1", "all")
    assert "pkg" not in rows and "pkg-c0" not in rows
    assert rows["e"].agg_children == 1 and rows["e"].agg_descendants == 1


async def test_tidy_job_budget_falls_back_to_placement_only(db, caplog):
    """An already-expired job budget still lays out every node (§4.7): the
    remaining containers just get placement without the improvement loop,
    and the driver says so exactly once."""
    kids = await seed_epic(db, n=3)
    with caplog.at_level("WARNING", logger="src.task_graph.layout.driver"):
        await LayoutDriver(db, tidy_job_seconds=0).full_layout("p1", "all")
    assert set(await db.load_layout_rows("p1", "all", ["e", *kids])) == {"e", *kids}
    assert len([r for r in caplog.records if "tidy job budget exhausted" in r.getMessage()]) == 1


async def test_generous_tidy_job_budget_does_not_warn(db, caplog):
    kids = await seed_epic(db, n=3)
    with caplog.at_level("WARNING", logger="src.task_graph.layout.driver"):
        await LayoutDriver(db, tidy_job_seconds=600).full_layout("p1", "all")
    assert not [r for r in caplog.records if "tidy job budget" in r.getMessage()]
    assert set(await db.load_layout_rows("p1", "all", ["e", *kids])) == {"e", *kids}
