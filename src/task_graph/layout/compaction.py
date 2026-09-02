"""Derived compaction of a persisted layout for one expanded set (§3.5).

``task_layouts`` stores the FULLY EXPANDED geometry: every container carries
the footprint it needs with all of its descendants drawn.  A viewer who
collapses a container should not keep staring at that footprint — the
collapsed container shrinks to a single tile and everything laid out after it
in reading order slides up and left to reclaim the space.

This module derives those positions.  Nothing here is persisted: the expanded
set is the viewer's own state (localStorage, per project), so compaction is a
pure function of ``(persisted rows, collapsed set)`` recomputed per request.

Three properties the spec's determinism rules depend on:

* **Reading order and line membership survive.**  A child never changes which
  visual line it sits on nor which siblings it sits between; it only slides
  along its line, and lines only slide up.  The graph stays recognisable
  across a toggle, which is the whole point of animating the reflow.
* **Gaps survive.**  Positions move by *accumulated size deltas*, so
  ``SIBLING_GAP`` and ``LINE_GAP`` come through untouched rather than being
  re-derived.
* **Identity.**  With nothing collapsed every delta is zero, so the compacted
  positions are exactly the persisted ones.  Expanding therefore restores the
  layout the engine published, and a live update that does not change
  structure cannot make anything jump.

Re-packing a scope requires *every* child of that scope: a sibling missing
from ``rows`` would silently shrink the line it belonged to.  Callers say
which scopes they loaded in full via ``scopes_loaded``; every other scope
keeps its persisted relative offsets (it still moves, because its container
moved, but its interior is left alone).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    HEADER_H,
    PADDING,
    band_up,
)
from src.task_graph.layout.model import LayoutRow

__all__ = ["COLLAPSED_SIZE", "Box", "compact_layout"]

#: A collapsed container renders as a single card-sized tile — the same size
#: the dashboard already draws it at (``flowNodes.ts`` gives any node whose
#: kind is not ``container`` a 1×1 box).
COLLAPSED_SIZE = (CARD_W, CARD_H)

#: Persisted coordinates are floats written and read back through the DB.
#: Line membership is decided by equality of ``rel_y``, so compare rounded.
_Y_PRECISION = 6


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float


def compact_layout(
    rows: Mapping[str, LayoutRow],
    *,
    collapsed: Collection[str],
    scopes_loaded: Collection[str | None],
) -> dict[str, Box]:
    """Absolute boxes for every row in ``rows`` under this collapsed set.

    ``collapsed`` is the set of containers rendering as a single tile — the
    ids ``resolve_visible`` returned as kind ``collapsed``, so level-of-detail
    culling shrinks a container exactly like an explicit collapse does.

    ``scopes_loaded`` names the containers (``None`` for the project root)
    whose children are *all* present in ``rows`` and may therefore be
    re-packed.
    """
    collapsed = frozenset(collapsed)
    repackable = frozenset(scopes_loaded)
    by_parent: dict[str | None, list[LayoutRow]] = {}
    for row in rows.values():
        by_parent.setdefault(row.container_id, []).append(row)

    sizes: dict[str, tuple[float, float]] = {}

    def size_of(tid: str) -> tuple[float, float]:
        """Allocated size of ``tid`` once its collapsed descendants shrink."""
        if tid in sizes:
            return sizes[tid]
        row = rows[tid]
        # Guard against a malformed parent chain: a container that contains
        # itself would recurse forever. Claim the persisted size first, so a
        # re-entrant call terminates instead of blowing the stack.
        sizes[tid] = (row.w, row.h)
        if tid in collapsed:
            sizes[tid] = COLLAPSED_SIZE
        elif row.kind == "container" and tid in repackable and by_parent.get(tid):
            _, content_w, content_h = _pack(by_parent[tid], size_of)
            sizes[tid] = (band_up(content_w), band_up(content_h))
        return sizes[tid]

    boxes: dict[str, Box] = {}

    def place(parent: str | None, origin: tuple[float, float]) -> None:
        kids = by_parent.get(parent, ())
        if not kids:
            return
        if parent in repackable:
            rel, _, _ = _pack(kids, size_of)
        else:
            rel = {k.task_id: (k.rel_x, k.rel_y) for k in kids}
        ox, oy = origin
        for kid in kids:
            tid = kid.task_id
            if tid in boxes:
                continue  # defensive: a cyclic parent chain must not recurse
            w, h = size_of(tid)
            rx, ry = rel[tid]
            boxes[tid] = Box(ox + rx, oy + ry, w, h)
            if tid not in collapsed:
                place(tid, (ox + rx + PADDING, oy + ry + PADDING + HEADER_H))

    # Placement roots: the project root, plus any scope whose container row
    # was not loaded (focus mode loads the ancestors of the focused node, not
    # their siblings). Such a scope is anchored where the engine put it.
    for parent, kids in sorted(by_parent.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
        if parent is not None and parent in rows:
            continue
        anchor = min(kids, key=lambda r: r.task_id)
        place(parent, (anchor.abs_x - anchor.rel_x, anchor.abs_y - anchor.rel_y))
    return boxes


def _pack(
    kids: Iterable[LayoutRow],
    size_of,
) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Re-pack one scope's children, returning ``(rel positions, w, h)``.

    Children keep their line and their order within it; each one slides left
    by the width its earlier line-mates gave up, and each line slides up by
    the height its earlier lines gave up.  ``w``/``h`` are the container's
    content size, measured exactly the way :func:`flow.flow_container`
    measures it, so an uncompacted scope reproduces the persisted allocation.
    """
    lines: dict[float, list[LayoutRow]] = {}
    for kid in kids:
        lines.setdefault(round(kid.rel_y, _Y_PRECISION), []).append(kid)

    rel: dict[str, tuple[float, float]] = {}
    dy = 0.0
    right = 0.0
    bottom = 0.0
    for key in sorted(lines):
        line = sorted(lines[key], key=lambda r: (r.rel_x, r.task_id))
        dx = 0.0
        new_y = key - dy
        line_h = 0.0
        for kid in line:
            w, h = size_of(kid.task_id)
            new_x = kid.rel_x - dx
            rel[kid.task_id] = (new_x, new_y)
            right = max(right, new_x + w)
            line_h = max(line_h, h)
            dx += kid.w - w
        bottom = max(bottom, new_y + line_h)
        dy += max(k.h for k in line) - line_h
    return rel, right + 2 * PADDING, bottom + 2 * PADDING + HEADER_H
