"""Layout cost (§4.2). Edges are (dependent, blocker)."""

from __future__ import annotations

from src.task_graph.layout.constants import W_CROSS, W_SLACK, W_SPAN, W_WRAP


def _rank_of(ordered: list[list[str]]) -> dict[str, int]:
    return {cid: r for r, rank in enumerate(ordered) for cid in rank}


def count_crossings(
    ordered: list[list[str]],
    positions: dict[str, tuple[float, float]],
    edges: list[tuple[str, str]],
) -> int:
    """Straight-line crossings between edges spanning the same rank pair.

    Two edges (d1,b1), (d2,b2) with the same (rank(b), rank(d)) cross when
    x(b1) < x(b2) and x(d1) > x(d2) or vice versa.
    """
    rank = _rank_of(ordered)
    groups: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for d, b in edges:
        if d not in positions or b not in positions:
            continue
        key = (rank[b], rank[d])
        groups.setdefault(key, []).append((positions[b][0], positions[d][0]))
    total = 0
    for segs in groups.values():
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                (b1, d1), (b2, d2) = segs[i], segs[j]
                if (b1 - b2) * (d1 - d2) < 0:
                    total += 1
    return total


def container_cost(
    ordered: list[list[str]],
    positions: dict[str, tuple[float, float]],
    edges: list[tuple[str, str]],
    minimal: dict[str, int],
    lines_per_rank: list[int],
) -> float:
    rank = _rank_of(ordered)
    crossings = count_crossings(ordered, positions, edges)
    span = sum(
        abs(positions[d][0] - positions[b][0])
        for d, b in edges
        if d in positions and b in positions
    )
    wrap = sum(max(0, n - 1) for n in lines_per_rank)
    slack = sum(rank[cid] - minimal.get(cid, 0) for cid in rank)
    return W_CROSS * crossings + W_SPAN * span + W_WRAP * wrap + W_SLACK * slack
