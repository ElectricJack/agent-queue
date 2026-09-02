"""Rank → lines → coordinates (§4.4 step 5) and cell membership (§4.10)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    CELL_SIZE,
    HEADER_H,
    LINE_GAP,
    PADDING,
    SIBLING_GAP,
    TARGET_ROW_WIDTH,
    TARGET_ROW_WIDTH_ROOT,
    band_up,
)


@dataclass
class FlowResult:
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    content: tuple[float, float] = (CARD_W, CARD_H)
    allocated: tuple[float, float] = (CARD_W, CARD_H)
    lines_per_rank: list[int] = field(default_factory=list)


def flow_container(
    ordered: list[list[str]],
    sizes: dict[str, tuple[float, float]],
    *,
    is_root: bool,
) -> FlowResult:
    """Lay out ``ordered[rank] = [ids in order]`` into wrapped lines.

    Positions are relative to the content origin (inside padding, below the
    header). Content size includes padding and header. Allocated size is the
    content size rounded up to a growth band; the root is never banded
    because nothing contains it.
    """
    target = TARGET_ROW_WIDTH_ROOT if is_root else TARGET_ROW_WIDTH
    res = FlowResult()
    if not ordered or not any(ordered):
        return res
    y = 0.0
    max_w = 0.0
    for rank in ordered:
        lines = 0
        x = 0.0
        line_h = 0.0
        started = False
        for cid in rank:
            w, h = sizes[cid]
            if started and x + w > target:
                # wrap
                y += line_h + LINE_GAP
                x = 0.0
                line_h = 0.0
                started = False
            if not started:
                lines += 1
                started = True
            res.positions[cid] = (x, y)
            line_h = max(line_h, h)
            max_w = max(max_w, x + w)
            x += w + SIBLING_GAP
        res.lines_per_rank.append(lines)
        y += line_h + LINE_GAP
    content_h = (y - LINE_GAP) + 2 * PADDING + HEADER_H
    content_w = max_w + 2 * PADDING
    res.content = (content_w, content_h)
    if is_root:
        res.allocated = res.content
    else:
        res.allocated = (band_up(content_w), band_up(content_h))
    return res


def cells_for_box(x: float, y: float, w: float, h: float) -> list[tuple[int, int]]:
    """Every CELL_SIZE cell the box [x, x+w) × [y, y+h) overlaps."""
    x0 = math.floor(x / CELL_SIZE)
    y0 = math.floor(y / CELL_SIZE)
    x1 = math.ceil((x + w) / CELL_SIZE) - 1
    y1 = math.ceil((y + h) / CELL_SIZE) - 1
    return [(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]
