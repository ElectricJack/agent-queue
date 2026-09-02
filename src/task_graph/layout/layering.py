"""Minimal feasible ranks over sibling dependency edges (§4.4 step 2).

Edges are ``(dependent, blocker)``: the blocker must sit in a lower rank.
"""

from __future__ import annotations

from collections import defaultdict

from src.task_graph.layout.model import SnapTask


def break_cycles(
    children: dict[str, SnapTask], edges: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Return ``edges`` restricted to known ids with cycles removed.

    Within each cycle found, drop the edge whose *dependent* has the newest
    ``created_at`` (ties by task id), then re-check until acyclic.
    """
    kept = [(d, b) for d, b in edges if d in children and b in children and d != b]
    while True:
        cycle = _find_cycle(kept)
        if cycle is None:
            return kept
        victim = max(cycle, key=lambda e: (children[e[0]].created_at, e[0]))
        kept.remove(victim)


def _find_cycle(edges: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
    """DFS over blocker → dependent; return the edges of one cycle or None."""
    out: dict[str, list[str]] = defaultdict(list)
    for d, b in edges:
        out[b].append(d)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    parent: dict[str, str] = {}

    def visit(u: str) -> list[tuple[str, str]] | None:
        color[u] = GREY
        for v in sorted(out[u]):
            if color[v] == GREY:
                # Walk back from u to v collecting edges (dependent, blocker).
                cyc = [(v, u)]
                x = u
                while x != v:
                    p = parent[x]
                    cyc.append((x, p))
                    x = p
                return cyc
            if color[v] == WHITE:
                parent[v] = u
                found = visit(v)
                if found:
                    return found
        color[u] = BLACK
        return None

    for node in sorted(set(out) | {d for d, _ in edges}):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def minimal_ranks(children: dict[str, SnapTask], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layering: rank(dependent) >= rank(blocker) + 1."""
    acyclic = break_cycles(children, edges)
    blockers: dict[str, list[str]] = defaultdict(list)
    for d, b in acyclic:
        blockers[d].append(b)
    memo: dict[str, int] = {}

    def rank(x: str) -> int:
        if x in memo:
            return memo[x]
        r = 0
        for b in blockers.get(x, ()):
            r = max(r, rank(b) + 1)
        memo[x] = r
        return r

    return {cid: rank(cid) for cid in sorted(children)}
