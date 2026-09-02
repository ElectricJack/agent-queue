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
    serpentine_chains: tuple[tuple[str, ...], ...] = (),
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
    chains_by_first = {chain[0]: chain for chain in serpentine_chains if chain}
    rank_index = 0
    while rank_index < len(ordered):
        rank = ordered[rank_index]
        chain = chains_by_first.get(rank[0]) if len(rank) == 1 else None
        chain_end = _chain_end_index(ordered, rank_index, chain) if chain else None
        if (
            chain
            and chain_end is not None
            and _chain_overflows(chain, sizes, target)
        ):
            y, chain_width, chain_lines = _flow_serpentine_chain(chain, sizes, target, res.positions, y)
            max_w = max(max_w, chain_width)
            # Each original rank still has one logical line.  This keeps the
            # cost model keyed by rank while letting the visual flow fold.
            chain_ids = set(chain)
            res.lines_per_rank.extend(
                1 if row and row[0] in chain_ids else 0
                for row in ordered[rank_index:chain_end + 1]
            )
            rank_index = chain_end + 1
            continue
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
        rank_index += 1
    content_h = (y - LINE_GAP) + 2 * PADDING + HEADER_H
    content_w = max_w + 2 * PADDING
    res.content = (content_w, content_h)
    if is_root:
        res.allocated = res.content
    else:
        res.allocated = (band_up(content_w), band_up(content_h))
    return res


def _chain_overflows(chain: tuple[str, ...], sizes: dict[str, tuple[float, float]], target: float) -> bool:
    """Leave short dependency chains in their normal layered positions."""
    return sum(sizes[cid][0] for cid in chain) + SIBLING_GAP * (len(chain) - 1) > target


def _chain_end_index(
    ordered: list[list[str]], start: int, chain: tuple[str, ...]
) -> int | None:
    """Locate a chain while allowing empty ordinal ranks used as slack."""
    expected = iter(chain)
    current = next(expected, None)
    index = start
    while index < len(ordered) and current is not None:
        row = ordered[index]
        if row:
            if len(row) != 1 or row[0] != current:
                return None
            current = next(expected, None)
        index += 1
    return index - 1 if current is None else None


def _flow_serpentine_chain(
    chain: tuple[str, ...],
    sizes: dict[str, tuple[float, float]],
    target: float,
    positions: dict[str, tuple[float, float]],
    y: float,
) -> tuple[float, float, int]:
    """Fold a serial dependency run into alternating horizontal lines.

    The first line reads left-to-right.  The next starts at the right, so the
    dependency at a turn is a short vertical connector; subsequent links run
    horizontally back across the line.  Rank/order ordinals remain untouched.
    """
    lines: list[list[str]] = [[]]
    line_widths = [0.0]
    for cid in chain:
        w, _ = sizes[cid]
        current = lines[-1]
        proposed = line_widths[-1] + (SIBLING_GAP if current else 0.0) + w
        if current and proposed > target:
            lines.append([])
            line_widths.append(0.0)
        if lines[-1]:
            line_widths[-1] += SIBLING_GAP
        lines[-1].append(cid)
        line_widths[-1] += w

    max_w = 0.0
    for line_number, line in enumerate(lines):
        line_h = max(sizes[cid][1] for cid in line)
        line_w = line_widths[line_number]
        max_w = max(max_w, line_w)
        if line_number % 2 == 0:
            x = 0.0
            for cid in line:
                positions[cid] = (x, y)
                x += sizes[cid][0] + SIBLING_GAP
        else:
            # Right-align reverse lines.  At the preceding turn the next
            # dependency therefore drops almost straight down instead of
            # drawing a diagonal back to a short partial line.
            x = target
            for cid in line:
                w, _ = sizes[cid]
                x -= w
                positions[cid] = (x, y)
                x -= SIBLING_GAP
        max_w = max(max_w, target if line_number % 2 else line_w)
        y += line_h + LINE_GAP
    return y, max_w, len(lines)


def cells_for_box(x: float, y: float, w: float, h: float) -> list[tuple[int, int]]:
    """Every CELL_SIZE cell the box [x, x+w) × [y, y+h) overlaps."""
    x0 = math.floor(x / CELL_SIZE)
    y0 = math.floor(y / CELL_SIZE)
    x1 = math.ceil((x + w) / CELL_SIZE) - 1
    y1 = math.ceil((y + h) / CELL_SIZE) - 1
    return [(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]
