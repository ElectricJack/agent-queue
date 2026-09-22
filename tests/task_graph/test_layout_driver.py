from unittest.mock import patch

import pytest
from sqlalchemy import event

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph.layout import driver as driver_module
from src.task_graph.layout.constants import CARD_W, SIBLING_GAP
from src.task_graph.layout.driver import LayoutDriver
from src.task_graph.layout.flow import clamp_row_target, row_target
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    d = Database(lease_dsn("drv.db"))
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


async def test_late_phase_without_a_gate_has_a_rank_floor_in_both_variants(db):
    """Completed predecessor phases do not gate a later phase, but layout
    preserves their declared delivery order in both persisted variants.
    """
    await db.create_task(Task(id="p1", project_id="p1", title="Phase 1", description=""))
    async with db._engine.begin() as conn:
        await db.mark_container("p1", conn=conn)
    await db.set_task_meta("p1", "phase", {"order": 1, "label": "Phase 1"})
    await db.transition_task("p1", TaskStatus.COMPLETED, force=True)

    # This is the late-creation case: p1 is already complete, so phase_create
    # would attach no blocks edge to p2.
    await db.create_task(
        Task(id="p2", project_id="p1", title="Phase 2", description="", status=TaskStatus.DEFINED)
    )
    async with db._engine.begin() as conn:
        await db.mark_container("p2", conn=conn)
    await db.set_task_meta("p2", "phase", {"order": 2, "label": "Phase 2"})
    await db.create_task(Task(id="work", project_id="p1", title="Work", description=""))
    await db.add_dependency("work", "p2")
    _, edges = await db.load_project_snapshot("p1")
    assert edges == [("work", "p2", "blocks")]

    driver = LayoutDriver(db)
    first: dict[str, dict[str, tuple]] = {}
    for variant in ("all", "active"):
        await driver.full_layout("p1", variant)
        rows = await db.load_layout_rows("p1", variant, ["p1", "p2", "work"])
        assert rows["p2"].rank == 2
        assert rows["work"].rank == 3  # ordinary dependency rank above the floor
        if variant == "all":
            assert rows["p1"].rank == 1
        else:
            assert "p1" not in rows
        first[variant] = {
            task_id: (row.ordinal, row.rel_x, row.rel_y, row.abs_x, row.abs_y)
            for task_id, row in rows.items()
        }

    # A second full layout has the same phase ranks, ordinals, and geometry.
    for variant in ("all", "active"):
        await driver.full_layout("p1", variant)
        rows = await db.load_layout_rows("p1", variant, ["p1", "p2", "work"])
        assert {
            task_id: (row.ordinal, row.rel_x, row.rel_y, row.abs_x, row.abs_y)
            for task_id, row in rows.items()
        } == first[variant]


async def test_full_layout_ignores_discovered_from_for_ranking(db):
    """`discovered-from` is a provenance annotation, not a ranking edge

    (`RANKING_DEP_TYPES` in `src/task_graph/layout/constants.py` excludes
    it): unlike a `blocks` edge, it must not pull the dependent below its
    origin, so geometry is identical to the no-edge case.
    """
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a", "discovered-from")
    await LayoutDriver(db).full_layout("p1", "all")
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert rows["a"].rank == 0 and rows["b"].rank == 0
    assert rows["a"].abs_y == rows["b"].abs_y


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


async def test_container_leaves_active_when_it_finishes_then_reopens(db):
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
    # `e` finished too, and nothing unfinished points into it, so the whole
    # subtree leaves the variant rather than lingering as a stub.
    assert await db.load_layout_rows("p1", "active", ["e", *kids, "gc"]) == {}

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
    assert rows[kids[2]].rel_x == pytest.approx(CARD_W + SIBLING_GAP)
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
    assert rows[kids[2]].rel_x == pytest.approx(CARD_W + SIBLING_GAP)
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
        assert rows["c"].rel_x == pytest.approx(CARD_W + SIBLING_GAP)
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
    assert rows["c"].rel_x == pytest.approx(CARD_W + SIBLING_GAP)
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


async def test_status_flip_in_all_variant_updates_aggregates_without_relaying(db):
    kids = await seed_epic(db, n=4)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    # Drain the "task.created"/"parent.changed" marks seed_epic's creation
    # left behind: full_layout never consumes the dirty table, and those
    # marks (not status.* reasons) would otherwise ride along in the same
    # batch as the status flip below and dirty "e" through the ordinary
    # path, defeating the point of the assertion at the end of this test.
    await drv.process_dirty("p1", min_age_seconds=0)
    before = await db.load_layout_rows("p1", "all", ["e", *kids])

    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)  # marks "status.finished"

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        with patch("src.task_graph.layout.driver.VARIANTS", ("all",)):
            versions = await drv.process_dirty("p1", min_age_seconds=0)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert versions["all"] == 3  # full_layout (1), the drain above (2), this fold (3)
    after = await db.load_layout_rows("p1", "all", ["e", *kids])
    for k in kids:  # nobody moved
        assert (after[k].abs_x, after[k].abs_y, after[k].w, after[k].h) == (
            before[k].abs_x, before[k].abs_y, before[k].w, before[k].h)
    assert after["e"].agg_completed == 1 and after["e"].agg_active == 3
    # The engine pass reads a container's children with load_children_layout_rows,
    # whose SELECT (layout_queries.py:387-404) ends its WHERE clause with this
    # exact fragment; a pure status flip must not trigger it for the `all`
    # variant. (A plain "container_id = " substring also matches the routine
    # upsert's "SET container_id = excluded.container_id", which fires on
    # every publish including the aggregates-only path, so it can't be used
    # to distinguish a container relay from an ordinary row write.)  The
    # ``?`` is SQLite's paramstyle — this fixture is SQLite-only; on
    # PostgreSQL (``$1``) the fragment would never match and the assertion
    # would pass vacuously.
    assert not any("task_layouts.container_id = ?" in s for s in statements), statements


async def test_finished_leaf_leaves_active_variant_without_relaying_siblings(db):
    kids = await seed_epic(db, n=4)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    # full_layout never consumes the dirty table; drain the "task.created"/
    # "parent.changed" marks left behind by seed_epic's creation before
    # taking the "before" snapshot, so only the status flip below rides in
    # the batch under test (mirrors the D2 all-variant test above).
    await drv.process_dirty("p1", min_age_seconds=0)
    before = await db.load_layout_rows("p1", "active", ["e", *kids])

    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)
    versions = await drv.process_dirty("p1", min_age_seconds=0)

    assert versions["active"] == 3  # full_layout (1), the drain above (2), this fold (3)
    after = await db.load_layout_rows("p1", "active", ["e", *kids])
    assert kids[1] not in after
    for k in (kids[0], kids[2], kids[3]):  # siblings did not close up
        assert (after[k].abs_x, after[k].abs_y) == (before[k].abs_x, before[k].abs_y)
    assert after["e"].agg_completed == 1 and after["e"].agg_active == 3
    assert (after["e"].w, after["e"].h) == (before["e"].w, before["e"].h)


async def test_last_finished_child_still_collapses_its_container_to_a_stub(db):
    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)  # drain creation marks

    await db.transition_task(kids[0], TaskStatus.COMPLETED, force=True)
    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)
    await drv.process_dirty("p1", min_age_seconds=0)

    rows = await db.load_layout_rows("p1", "active", ["e", *kids])
    assert rows["e"].kind == "stub"
    for k in kids:
        assert k not in rows


# ── active variant: dropping finished containers nothing needs ───────────
#
# A finished container is kept in ``active`` only as *context*: the spec's
# stub exists "so the epic remains findable" (§3.3/§4.8) and to give a drawn
# edge from unfinished work a visible far endpoint (``view.cap_stubs`` drops
# an edge whose far end has no row in the variant). When nothing unfinished
# points into its subtree and it holds no unfinished descendant, it is
# context for nothing and leaves the variant entirely.


async def _finish(db, *ids):
    for tid in ids:
        await db.transition_task(tid, TaskStatus.COMPLETED, force=True)


async def test_finished_container_nothing_needs_leaves_the_active_variant(db):
    kids = await seed_epic(db, epic="done", n=2, completed=2)
    await _finish(db, "done")
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    active = await db.load_layout_rows("p1", "active", ["done", *kids, "live"])
    assert set(active) == {"live"}
    # ``all`` is untouched: the epic is still there, as a real container.
    all_rows = await db.load_layout_rows("p1", "all", ["done", *kids, "live"])
    assert set(all_rows) == {"done", *kids, "live"}
    assert all_rows["done"].kind == "container"


async def test_finished_container_an_unfinished_task_depends_on_stays_a_stub(db):
    kids = await seed_epic(db, epic="done", n=2, completed=2)
    await _finish(db, "done")
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    await db.add_dependency("live", "done")
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    rows = await db.load_layout_rows("p1", "active", ["done", *kids, "live"])
    assert rows["done"].kind == "stub"
    assert set(rows) == {"done", "live"}


async def test_finished_container_a_deep_edge_points_into_stays_a_stub(db):
    """The edge lands on a *child*: the whole subtree is the anchor."""
    kids = await seed_epic(db, epic="done", n=2, completed=2)
    await _finish(db, "done")
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    await db.add_dependency("live", kids[0], dep_type="discovered-from")
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    rows = await db.load_layout_rows("p1", "active", ["done", *kids, "live"])
    assert rows["done"].kind == "stub"


async def test_edge_between_two_finished_tasks_does_not_anchor_a_container(db):
    kids = await seed_epic(db, epic="done", n=2, completed=2)
    await _finish(db, "done")
    await db.create_task(Task(id="gone", project_id="p1", title="gone", description=""))
    await db.add_dependency("gone", kids[0])
    await _finish(db, "gone")
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    assert set(await db.load_layout_rows("p1", "active", ["done", *kids, "gone"])) == set()


async def test_finished_ancestor_of_unfinished_work_stays_present(db):
    kids = await seed_epic(db, epic="done", n=2, completed=1)
    await _finish(db, "done")
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    rows = await db.load_layout_rows("p1", "active", ["done", *kids])
    assert rows["done"].kind == "container"
    assert kids[1] in rows and kids[0] not in rows


async def test_anchored_nested_epic_keeps_its_finished_ancestor_as_a_container(db):
    """A dropped ancestor comes back as a real container for an anchored child."""
    await _make_container(db, "outer", n=0)
    await _make_container(db, "inner", parent="outer", n=1)
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    await db.add_dependency("live", "inner")
    await _finish(db, "inner-k0", "inner", "outer")
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")

    rows = await db.load_layout_rows("p1", "active", ["outer", "inner", "inner-k0", "live"])
    assert rows["outer"].kind == "container"
    assert rows["inner"].kind == "stub"
    assert "inner-k0" not in rows


async def test_finishing_the_container_itself_drops_it_from_active_incrementally(db):
    kids = await seed_epic(db, epic="done", n=2)
    await db.create_task(Task(id="live", project_id="p1", title="live", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await drv.process_dirty("p1", min_age_seconds=0)

    await _finish(db, *kids, "done")
    await drv.process_dirty("p1", min_age_seconds=0)

    assert set(await db.load_layout_rows("p1", "active", ["done", *kids])) == set()


async def test_reconcile_heals_an_active_variant_stub_the_rules_no_longer_keep(db):
    """An install laid out under the old rules converges with no operator action.

    The rows are published exactly as the pre-change engine published them
    (a finished epic kept as a stub), then the reconcile sweep notices the
    drift and ``process_dirty`` retires it.
    """
    kids = await seed_epic(db, epic="done", n=2, completed=2)
    await _finish(db, "done")
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    # The old rule was "every finished container is kept as a stub", i.e. every
    # candidate counted as anchored.
    with patch(
        "src.task_graph.layout.driver._edge_anchored",
        lambda snapshot, candidates, edges: set(candidates),
    ):
        await drv.full_layout("p1", "active")
    assert (await db.load_layout_rows("p1", "active", ["done"]))["done"].kind == "stub"

    assert await drv.reconcile("p1") == 1
    await drv.process_dirty("p1", min_age_seconds=0)
    assert set(await db.load_layout_rows("p1", "active", ["done", *kids])) == set()


async def _drain(db, drv):
    await drv.process_dirty("p1", min_age_seconds=0)


def _record_ordinals(records):
    """Patch the driver's engine entry point so a batch's per-container
    ``changed_ordinals`` can be inspected from the outside."""
    real = driver_module.layout_container

    def wrapper(scope, **kw):
        res = real(scope, **kw)
        records.append((scope.container_id, set(res.changed_ordinals)))
        return res

    return patch.object(driver_module, "layout_container", wrapper)


async def _positions(db, variant):
    rows = await db.load_subtree_rows("p1", variant)
    return {tid: (r.abs_x, r.abs_y) for tid, r in rows.items()}


def _total_movement(before, after):
    return sum(
        abs(after[t][0] - before[t][0]) + abs(after[t][1] - before[t][1])
        for t in before.keys() & after.keys()
    )


async def test_incremental_trajectory_is_stable(db):
    """create → start → finish → create-next, on both variants.

    Starting work writes no dirty mark at all, a leaf's status change never
    re-keys anything, and a new sibling moves no existing ordinal. The
    aspect-balanced row target must not have loosened any of that.
    """
    kids = await seed_epic(db, n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    await _drain(db, drv)

    # ── create ────────────────────────────────────────────────────────
    before = {v: await db.load_subtree_rows("p1", v) for v in ("all", "active")}
    await db.create_task(Task(id="e-n1", project_id="p1", title="n1", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-n1", "e", conn=conn)
        await db.mark_layout_dirty("p1", ["e-n1"], "task.created", conn=conn)
    records: list[tuple[str | None, set[str]]] = []
    with _record_ordinals(records):
        await _drain(db, drv)
    assert records and all(changed <= {"e-n1"} for _, changed in records)
    for variant, rows in before.items():
        after = await db.load_subtree_rows("p1", variant)
        for tid, r in rows.items():
            assert after[tid].ordinal == r.ordinal, (variant, tid)

    # ── start ─────────────────────────────────────────────────────────
    await db.transition_task("e-n1", TaskStatus.ASSIGNED, force=True)
    await db.transition_task("e-n1", TaskStatus.IN_PROGRESS, force=True)
    _, marks = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert marks == []

    # ── finish ────────────────────────────────────────────────────────
    # A finishing leaf is ``_aggregates_only``: ``all`` rewrites counters in
    # place and ``active`` deletes the row, so NO container is re-laid at
    # all. Assert that, not "every pass that ran changed nothing" — there
    # are no passes, and that assertion would hold vacuously.
    positions = {v: await _positions(db, v) for v in ("all", "active")}
    await _finish(db, "e-n1")
    records.clear()
    with _record_ordinals(records):
        await _drain(db, drv)
    assert records == []
    for variant, before_xy in positions.items():
        after_xy = await _positions(db, variant)
        assert {t: xy for t, xy in after_xy.items() if t in before_xy} == {
            t: xy for t, xy in before_xy.items() if t in after_xy
        }, variant
        assert _total_movement(before_xy, after_xy) == 0.0, variant
        # ``active`` drops the finished leaf; ``all`` keeps it in place.
        assert (set(before_xy) - set(after_xy)) == (
            {"e-n1"} if variant == "active" else set()
        ), variant

    # ── create-next ───────────────────────────────────────────────────
    before = {v: await db.load_subtree_rows("p1", v) for v in ("all", "active")}
    await db.create_task(Task(id="e-n2", project_id="p1", title="n2", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-n2", "e", conn=conn)
        await db.mark_layout_dirty("p1", ["e-n2"], "task.created", conn=conn)
    records.clear()
    with _record_ordinals(records):
        await _drain(db, drv)
    assert records and all(changed <= {"e-n2"} for _, changed in records)
    for variant, rows in before.items():
        after = await db.load_subtree_rows("p1", variant)
        for tid, r in rows.items():
            assert after[tid].ordinal == r.ordinal, (variant, tid)
    assert set(kids) <= set(await db.load_subtree_rows("p1", "all"))


async def test_published_positions_move_only_on_a_band_crossing(db):
    """Σ|Δabs_x| + |Δabs_y| over the whole canvas is 0 unless the scope's
    row target or growth band actually changed, and then only the crossing
    scope and its later siblings move."""
    await seed_epic(db, n=3)
    await db.create_task(Task(id="z", project_id="p1", title="z", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await _drain(db, drv)

    def scope_shape(rows):
        """What actually governs the epic's wrapping: the CLAMPED target
        (the ideal can move without the published one moving, and vice
        versa), plus the box the epic is allocated."""
        kids = {k: (r.w, r.h) for k, r in rows.items() if r.container_id == "e"}
        ordered: dict[int, list[str]] = {}
        for k, r in sorted(rows.items()):
            if r.container_id == "e":
                ordered.setdefault(r.rank, []).append(k)
        ideal = row_target(kids, is_root=False)
        return (
            clamp_row_target(
                [ordered[r] for r in sorted(ordered)], kids, is_root=False, target=ideal
            ),
            (rows["e"].w, rows["e"].h),
        )

    rows = await db.load_subtree_rows("p1", "all")
    shape = scope_shape(rows)
    before = {tid: (r.abs_x, r.abs_y) for tid, r in rows.items()}

    crossings = 0
    for i in range(14):
        cid = f"e-x{i}"
        await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(cid, "e", conn=conn)
            await db.mark_layout_dirty("p1", [cid], "task.created", conn=conn)
        await _drain(db, drv)

        rows = await db.load_subtree_rows("p1", "all")
        after = {tid: (r.abs_x, r.abs_y) for tid, r in rows.items()}
        new_shape = scope_shape(rows)
        moved = {
            tid for tid in before.keys() & after.keys() if before[tid] != after[tid]
        }
        if new_shape == shape:
            assert _total_movement(before, after) == 0.0, (i, sorted(moved))
        else:
            crossings += 1
            # Confined to the crossing scope (``e`` and its children) and
            # ``z``, the later root sibling that the wider epic pushes.
            assert moved <= {"e", "z", *(t for t in rows if rows[t].container_id == "e")}
        before, shape = after, new_shape

    # The fixture must exercise BOTH branches, or one of them is vacuous.
    assert 1 <= crossings < 14


# ── activity-aware tidy seed (reorganisation design §3.2) ───────────────


async def _seed_activity_fixture(db):
    """Three root epics created oldest-first: finished, open, running."""
    done = await seed_epic(db, epic="e-done", n=2, completed=2)
    await seed_epic(db, epic="e-open", n=2)
    running = await seed_epic(db, epic="e-run", n=1)
    for status in (TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
        await db.transition_task(running[0], status)
    return done, running


def _reading_order(rows, ids):
    present = [i for i in ids if i in rows]
    return sorted(present, key=lambda i: (rows[i].rel_y, rows[i].rel_x))


@pytest.mark.parametrize("variant", ["all", "active"])
async def test_a_finished_epic_sorts_first_and_running_next_after_a_full_layout(db, variant):
    await _seed_activity_fixture(db)
    await LayoutDriver(db).full_layout("p1", variant)
    rows = await db.load_layout_rows("p1", variant, ["e-done", "e-open", "e-run"])
    assert _reading_order(rows, ["e-done", "e-open", "e-run"]) == ["e-done", "e-run", "e-open"]


async def test_full_layout_seeds_containers_with_fresh_aggregates(db):
    """The seed classes an epic from the SNAPSHOT, not from the aggregate
    columns of the previously published row (which the incremental path
    refreshes only after its pass, so they can be stale)."""
    from sqlalchemy import update

    from src.database.tables import task_layouts

    await _seed_activity_fixture(db)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    # Lie in the stored rows: the finished epic claims a running descendant
    # and the running epic claims none.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(task_layouts)
            .where(task_layouts.c.project_id == "p1", task_layouts.c.task_id == "e-done")
            .values(agg_running=9, agg_active=9)
        )
        await conn.execute(
            update(task_layouts)
            .where(task_layouts.c.project_id == "p1", task_layouts.c.task_id == "e-run")
            .values(agg_running=0, agg_active=0)
        )
    await drv.full_layout("p1", "all")
    rows = await db.load_layout_rows("p1", "all", ["e-done", "e-open", "e-run"])
    assert _reading_order(rows, ["e-done", "e-open", "e-run"]) == ["e-done", "e-run", "e-open"]


async def test_incremental_work_still_appends_between_tidies(db):
    """The first slice reorders at the tidy seed only: a sibling created after
    a tidy still appends at the end of its rank and moves nothing."""
    await _seed_activity_fixture(db)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    before = await db.load_layout_rows("p1", "all", ["e-done", "e-open", "e-run"])
    await db.create_task(Task(id="z-new", project_id="p1", title="z", description=""))
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["z-new"], "task.created", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    after = await db.load_layout_rows("p1", "all", ["e-done", "e-open", "e-run", "z-new"])
    for tid, row in before.items():
        assert after[tid].ordinal == row.ordinal
    assert after["z-new"].rank == 0
    assert after["z-new"].order_key > max(r.order_key for r in before.values())
