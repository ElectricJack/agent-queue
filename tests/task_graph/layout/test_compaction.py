"""Derived compaction of a persisted layout for one expanded set (§3.5)."""

import pytest

from src.task_graph.layout.compaction import COLLAPSED_SIZE, compact_layout
from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    HEADER_H,
    LINE_GAP,
    PADDING,
    SIBLING_GAP,
    band_up,
)
from src.task_graph.layout.driver import LayoutDriver
from src.task_graph.layout.model import LayoutRow


def row(
    tid, *, parent, path, depth, rank, rel, size=(CARD_W, CARD_H), kind="card", origin=(0.0, 0.0)
):
    w, h = size
    return LayoutRow(
        task_id=tid,
        container_id=parent,
        path=path,
        depth=depth,
        rank=rank,
        order_key=tid,
        w=w,
        h=h,
        rel_x=rel[0],
        rel_y=rel[1],
        abs_x=origin[0] + rel[0],
        abs_y=origin[1] + rel[1],
        kind=kind,
    )


def content_origin(box):
    """Absolute content origin of a container (driver ``_origin``)."""
    x = getattr(box, "abs_x", None)
    y = getattr(box, "abs_y", None)
    if x is None:
        x, y = box.x, box.y
    return (x + PADDING, y + PADDING + HEADER_H)


def stacked_root(epic_h):
    """Root scope: container ``e`` on line 0, cards ``y`` and ``z`` below it.

    ``e`` holds two cards side by side, so collapsing it is the interesting
    case: the epic shrinks to one tile and the two root cards below have to
    climb.
    """
    e_size = (band_up(2 * CARD_W + SIBLING_GAP + 2 * PADDING), epic_h)
    rows = {
        "e": row(
            "e",
            parent=None,
            path="/e/",
            depth=0,
            rank=0,
            rel=(0.0, 0.0),
            size=e_size,
            kind="container",
        ),
        "y": row("y", parent=None, path="/y/", depth=0, rank=1, rel=(0.0, e_size[1] + LINE_GAP)),
        "z": row(
            "z",
            parent=None,
            path="/z/",
            depth=0,
            rank=1,
            rel=(CARD_W + SIBLING_GAP, e_size[1] + LINE_GAP),
        ),
    }
    eo = content_origin(rows["e"])
    rows["c0"] = row("c0", parent="e", path="/e/c0/", depth=1, rank=0, rel=(0.0, 0.0), origin=eo)
    rows["c1"] = row(
        "c1", parent="e", path="/e/c1/", depth=1, rank=0, rel=(CARD_W + SIBLING_GAP, 0.0), origin=eo
    )
    return rows


def test_nothing_collapsed_reproduces_the_persisted_positions():
    rows = stacked_root(band_up(CARD_H + 2 * PADDING + HEADER_H))
    boxes = compact_layout(rows, collapsed=set(), scopes_loaded={None, "e"})
    assert set(boxes) == set(rows)
    for tid, r in rows.items():
        assert (boxes[tid].x, boxes[tid].y) == (r.abs_x, r.abs_y)
        assert (boxes[tid].w, boxes[tid].h) == (r.w, r.h)


def test_collapsing_a_container_shrinks_it_and_lifts_the_rows_below_by_that_delta():
    epic_h = band_up(CARD_H + 2 * PADDING + HEADER_H)
    rows = stacked_root(epic_h)
    before = compact_layout(rows, collapsed=set(), scopes_loaded={None, "e"})
    after = compact_layout(rows, collapsed={"e"}, scopes_loaded={None, "e"})

    assert (after["e"].w, after["e"].h) == COLLAPSED_SIZE
    delta = epic_h - CARD_H
    assert delta > 0
    for tid in ("y", "z"):
        assert after[tid].y == before[tid].y - delta
        assert after[tid].x == before[tid].x  # same line, same column
    # Nothing inside a collapsed container is placed: it has no position on
    # the canvas until the viewer expands it again.
    assert "c0" not in after and "c0" in before
    assert after["e"].y == before["e"].y


def test_expanding_restores_every_original_position():
    rows = stacked_root(band_up(CARD_H + 2 * PADDING + HEADER_H))
    before = compact_layout(rows, collapsed=set(), scopes_loaded={None, "e"})
    compact_layout(rows, collapsed={"e"}, scopes_loaded={None, "e"})
    again = compact_layout(rows, collapsed=set(), scopes_loaded={None, "e"})
    assert again == before


def test_compaction_is_deterministic_across_calls():
    rows = stacked_root(band_up(CARD_H + 2 * PADDING + HEADER_H))
    a = compact_layout(rows, collapsed={"e"}, scopes_loaded={None, "e"})
    b = compact_layout(rows, collapsed={"e"}, scopes_loaded={None, "e"})
    assert a == b


def test_siblings_on_the_same_line_slide_left_by_the_reclaimed_width():
    """A collapsed container's line-mates move left, keeping SIBLING_GAP."""
    e_w = band_up(2 * CARD_W + SIBLING_GAP + 2 * PADDING)
    e_h = band_up(CARD_H + 2 * PADDING + HEADER_H)
    rows = {
        "e": row(
            "e",
            parent=None,
            path="/e/",
            depth=0,
            rank=0,
            rel=(0.0, 0.0),
            size=(e_w, e_h),
            kind="container",
        ),
        "t": row("t", parent=None, path="/t/", depth=0, rank=0, rel=(e_w + SIBLING_GAP, 0.0)),
    }
    eo = content_origin(rows["e"])
    rows["c0"] = row("c0", parent="e", path="/e/c0/", depth=1, rank=0, rel=(0.0, 0.0), origin=eo)
    boxes = compact_layout(rows, collapsed={"e"}, scopes_loaded={None, "e"})
    assert boxes["e"].x == 0.0
    assert boxes["t"].x == CARD_W + SIBLING_GAP
    assert boxes["t"].y == 0.0


def test_a_collapsed_grandchild_shrinks_its_parent_and_lifts_the_parent_s_siblings():
    """Compaction propagates up: the epic re-bands, the root card climbs."""
    pkg_h = band_up(3 * CARD_H + 2 * LINE_GAP + 2 * PADDING + HEADER_H)
    epic_h = band_up(pkg_h + 2 * PADDING + HEADER_H)
    rows = {
        "e": row(
            "e",
            parent=None,
            path="/e/",
            depth=0,
            rank=0,
            rel=(0.0, 0.0),
            size=(band_up(3.0), epic_h),
            kind="container",
        ),
        "z": row("z", parent=None, path="/z/", depth=0, rank=1, rel=(0.0, epic_h + LINE_GAP)),
    }
    eo = content_origin(rows["e"])
    rows["pkg"] = row(
        "pkg",
        parent="e",
        path="/e/pkg/",
        depth=1,
        rank=0,
        rel=(0.0, 0.0),
        size=(band_up(CARD_W + 2 * PADDING), pkg_h),
        kind="container",
        origin=eo,
    )
    po = content_origin(rows["pkg"])
    for i in range(3):
        rows[f"g{i}"] = row(
            f"g{i}",
            parent="pkg",
            path=f"/e/pkg/g{i}/",
            depth=2,
            rank=i,
            rel=(0.0, i * (CARD_H + LINE_GAP)),
            origin=po,
        )

    scopes = {None, "e", "pkg"}
    before = compact_layout(rows, collapsed=set(), scopes_loaded=scopes)
    assert (before["e"].w, before["e"].h) == (rows["e"].w, rows["e"].h)

    after = compact_layout(rows, collapsed={"pkg"}, scopes_loaded=scopes)
    assert (after["pkg"].w, after["pkg"].h) == COLLAPSED_SIZE
    assert after["e"].h == band_up(CARD_H + 2 * PADDING + HEADER_H) < epic_h
    assert after["z"].y == pytest.approx(after["e"].h + LINE_GAP)
    assert after["z"].y < before["z"].y


def test_a_scope_that_was_not_loaded_in_full_keeps_its_persisted_interior():
    """Focus mode loads a node's ancestors but not their siblings, so those
    scopes must not be re-packed off a partial child list."""
    e_h = band_up(2 * CARD_H + LINE_GAP + 2 * PADDING + HEADER_H)
    rows = {
        "e": row(
            "e",
            parent=None,
            path="/e/",
            depth=0,
            rank=0,
            rel=(3.0, 4.0),
            size=(band_up(CARD_W + 2 * PADDING), e_h),
            kind="container",
        ),
    }
    eo = content_origin(rows["e"])
    # Only the SECOND child of ``e`` is present.
    rows["c1"] = row(
        "c1", parent="e", path="/e/c1/", depth=1, rank=1, rel=(0.0, CARD_H + LINE_GAP), origin=eo
    )
    boxes = compact_layout(rows, collapsed=set(), scopes_loaded={"e"} - {"e"})
    assert (boxes["e"].x, boxes["e"].y) == (3.0, 4.0)
    assert (boxes["c1"].x, boxes["c1"].y) == (rows["c1"].abs_x, rows["c1"].abs_y)


async def _seed_real_project(db):
    """An epic with a wrapping rank, a nested package, a long serial chain and
    two root cards below — enough shapes that a naive re-flow would drift."""
    from src.models import Project, Task

    await db.create_project(Project(id="p1", name="P1"))

    async def mk(tid, parent=None):
        await db.create_task(Task(id=tid, project_id="p1", title=tid, description=""))
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    await mk("e")
    for i in range(7):  # wraps: 7 cards do not fit one comfortable line
        await mk(f"c{i}", "e")
    await mk("pkg", "e")
    chain = [f"s{i}" for i in range(8)]  # long enough to serpentine
    for i, sid in enumerate(chain):
        await mk(sid, "pkg")
        if i:
            await db.add_dependency(sid, chain[i - 1])
    await mk("y")
    await mk("z")
    await db.add_dependency("z", "y")


async def test_compaction_of_a_real_layout_is_the_identity_when_nothing_is_collapsed(tmp_path):
    """The engine's own output has to survive a compaction round trip, wraps
    and serpentine folds included — otherwise expanding would make the graph
    jump instead of returning it to where it was."""
    from src.database import Database

    db = Database(str(tmp_path / "compact.db"))
    await db.initialize()
    try:
        await _seed_real_project(db)
        await LayoutDriver(db).full_layout("p1", "all")
        rows = await db.load_subtree_rows("p1", "all")
        boxes = compact_layout(
            rows,
            collapsed=set(),
            scopes_loaded={None, *(t for t, r in rows.items() if r.kind == "container")},
        )
        # The fixture must actually exercise both folds, or this test would
        # pass on a layout with nothing interesting in it.
        assert len({rows[f"c{i}"].rel_y for i in range(7)}) > 1  # rank wrapped
        assert len({rows[f"s{i}"].rel_y for i in range(8)}) > 1  # chain folded
        assert set(boxes) == set(rows)
        for tid, r in rows.items():
            assert (boxes[tid].x, boxes[tid].y) == pytest.approx((r.abs_x, r.abs_y)), tid
            assert (boxes[tid].w, boxes[tid].h) == pytest.approx((r.w, r.h)), tid
    finally:
        await db.close()


async def test_collapsing_the_epic_of_a_real_layout_reclaims_the_space(tmp_path):
    from src.database import Database

    db = Database(str(tmp_path / "compact2.db"))
    await db.initialize()
    try:
        await _seed_real_project(db)
        await LayoutDriver(db).full_layout("p1", "all")
        rows = await db.load_subtree_rows("p1", "all")
        scopes = {None, *(t for t, r in rows.items() if r.kind == "container")}
        before = compact_layout(rows, collapsed=set(), scopes_loaded=scopes)
        after = compact_layout(rows, collapsed={"e"}, scopes_loaded=scopes)

        delta = before["e"].h - COLLAPSED_SIZE[1]
        assert delta > 0
        for tid in ("y", "z"):
            assert after[tid].y == pytest.approx(before[tid].y - delta)
            assert after[tid].x == pytest.approx(before[tid].x)
        assert compact_layout(rows, collapsed=set(), scopes_loaded=scopes) == before
    finally:
        await db.close()
