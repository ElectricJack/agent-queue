"""Container layout engine (§4.4, §4.7).

``layout_container`` lays out ONE container's children from a
``ContainerScope``. Ordinals of existing children are immutable in
``incremental`` and ``resize`` modes; only the forced rank repair may
change them. ``tidy`` mode treats every child as movable.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Literal

from src.task_graph.layout.constants import (
    CARD_H, CARD_W, INCREMENTAL_EVALS, INCREMENTAL_SECONDS,
    MAX_OPTIMIZED_SIBLINGS, TIDY_EVALS, TIDY_SECONDS,
)
from src.task_graph.layout.cost import container_cost
from src.task_graph.layout.flow import FlowResult, flow_container
from src.task_graph.layout.layering import break_cycles, minimal_ranks
from src.task_graph.layout.model import ContainerScope, LayoutRow
from src.task_graph.layout.order_key import between

Mode = Literal["incremental", "resize", "tidy"]


@dataclass
class ContainerResult:
    rows: dict[str, LayoutRow]
    allocated: tuple[float, float]
    changed_ordinals: set[str] = field(default_factory=set)


@dataclass
class _Budget:
    evals: int
    seconds: float
    started: float = field(default_factory=time.monotonic)
    used: int = 0

    def spent(self) -> bool:
        return self.used >= self.evals or (time.monotonic() - self.started) >= self.seconds


def _ordered_from(ordinals: dict[str, tuple[int, str]]) -> list[list[str]]:
    if not ordinals:
        return []
    nranks = max(r for r, _ in ordinals.values()) + 1
    out: list[list[str]] = [[] for _ in range(nranks)]
    for cid, (r, _) in ordinals.items():
        out[r].append(cid)
    for rank in out:
        rank.sort(key=lambda c: ordinals[c][1])
    return out


def _sizes(scope: ContainerScope) -> dict[str, tuple[float, float]]:
    sizes: dict[str, tuple[float, float]] = {}
    for cid, t in scope.children.items():
        if cid in scope.stub_ids or not t.is_container:
            sizes[cid] = (CARD_W, CARD_H)
        else:
            sizes[cid] = scope.child_sizes.get(cid, (CARD_W, CARD_H))
    return sizes


def _kind(scope: ContainerScope, cid: str) -> str:
    if cid in scope.stub_ids:
        return "stub"
    return "container" if scope.children[cid].is_container else "card"


def _rows(scope: ContainerScope, ordinals, flow: FlowResult, sizes) -> dict[str, LayoutRow]:
    ox, oy = scope.origin
    rows: dict[str, LayoutRow] = {}
    for cid, (rank, key) in ordinals.items():
        rx, ry = flow.positions[cid]
        w, h = sizes[cid]
        prev = scope.existing.get(cid)
        rows[cid] = LayoutRow(
            task_id=cid, container_id=scope.container_id,
            path=f"{scope.container_path}{cid}/", depth=scope.depth,
            rank=rank, order_key=key, w=w, h=h, rel_x=rx, rel_y=ry,
            abs_x=ox + rx, abs_y=oy + ry, kind=_kind(scope, cid),
            agg_children=prev.agg_children if prev else 0,
            agg_descendants=prev.agg_descendants if prev else 0,
            agg_completed=prev.agg_completed if prev else 0,
            agg_running=prev.agg_running if prev else 0,
            agg_blocked=prev.agg_blocked if prev else 0,
            agg_active=prev.agg_active if prev else 0,
        )
    return rows


def _evaluate(ordinals, scope, sizes, edges, minimal, is_root) -> tuple[float, FlowResult]:
    ordered = _ordered_from(ordinals)
    flow = flow_container(ordered, sizes, is_root=is_root)
    cost = container_cost(ordered, flow.positions, edges, minimal, flow.lines_per_rank)
    return cost, flow


def _gap_keys(rank_ids_sorted: list[str], ordinals) -> list[tuple[str | None, str | None]]:
    """Every (left_key, right_key) gap in a rank, including both ends."""
    keys = [ordinals[c][1] for c in rank_ids_sorted]
    gaps: list[tuple[str | None, str | None]] = [(None, keys[0] if keys else None)]
    for i in range(len(keys)):
        gaps.append((keys[i], keys[i + 1] if i + 1 < len(keys) else None))
    return gaps


def _place_new(
    cid: str, ordinals, scope, sizes, edges, minimal, is_root, budget: _Budget, rng
) -> None:
    """Choose rank (minimal, or minimal+1 if it pays) and a gap for ``cid``."""
    blockers = [b for d, b in edges if d == cid and b in ordinals]
    rank0 = minimal[cid]
    best: tuple[float, tuple[int, str]] | None = None
    for rank in (rank0, rank0 + 1):
        in_rank = sorted((c for c, (r, _) in ordinals.items() if r == rank),
                         key=lambda c: ordinals[c][1])
        if not blockers:
            # No blockers: this node just appends to the rank's end. Only
            # evaluate the end gap — iterating every gap wastes the budget
            # on large ranks for a decision the spec has already made.
            gaps = [(ordinals[in_rank[-1]][1] if in_rank else None, None)]
        else:
            gaps = _gap_keys(in_rank, ordinals)
            # Seed at barycenter: gap whose neighbours straddle the mean blocker x.
            xs = sorted(ordinals[b][1] for b in blockers)
            mid = xs[len(xs) // 2]
            gaps.sort(key=lambda g: (0 if (g[0] or "") <= mid <= (g[1] or "~") else 1))
        for lo, hi in gaps:
            if budget.spent() and best is not None:
                break
            key = between(lo, hi)
            trial = dict(ordinals)
            trial[cid] = (rank, key)
            cost, _ = _evaluate(trial, scope, sizes, edges, minimal, is_root)
            budget.used += 1
            if best is None or cost < best[0]:
                best = (cost, (rank, key))
        if not blockers:
            break  # no-blocker nodes just append; don't try sinking
    assert best is not None
    ordinals[cid] = best[1]


def _tidy_sweep(ordinals, scope, sizes, edges, minimal, is_root, budget, rng) -> None:
    """Barycenter sweeps then greedy adjacent swaps (§4.7)."""
    ordered = _ordered_from(ordinals)
    blockers_of: dict[str, list[str]] = {}
    dependents_of: dict[str, list[str]] = {}
    for d, b in edges:
        blockers_of.setdefault(d, []).append(b)
        dependents_of.setdefault(b, []).append(d)

    def reorder(rank_idx: int, neighbours: dict[str, list[str]], ref_rank: int) -> None:
        ref_pos = {c: i for i, c in enumerate(ordered[ref_rank])}
        def bary(c: str) -> float:
            ns = [ref_pos[n] for n in neighbours.get(c, ()) if n in ref_pos]
            return sum(ns) / len(ns) if ns else float(ordered[rank_idx].index(c))
        ordered[rank_idx].sort(key=bary)

    for _ in range(2):
        for r in range(1, len(ordered)):
            reorder(r, blockers_of, r - 1)
        for r in range(len(ordered) - 2, -1, -1):
            reorder(r, dependents_of, r + 1)
    # Re-key every rank fresh so keys are short and sorted.
    for r, rank in enumerate(ordered):
        prev = None
        for c in rank:
            prev = between(prev, None)
            ordinals[c] = (r, prev)
    # Greedy adjacent swaps.
    cur, _ = _evaluate(ordinals, scope, sizes, edges, minimal, is_root)
    improved = True
    while improved and not budget.spent():
        improved = False
        for r, rank in enumerate(_ordered_from(ordinals)):
            for i in range(len(rank) - 1):
                a, b = rank[i], rank[i + 1]
                trial = dict(ordinals)
                trial[a], trial[b] = (r, ordinals[b][1]), (r, ordinals[a][1])
                cost, _ = _evaluate(trial, scope, sizes, edges, minimal, is_root)
                budget.used += 1
                if cost < cur:
                    ordinals.update(trial)
                    cur = cost
                    improved = True
                if budget.spent():
                    break


def layout_container(scope: ContainerScope, *, mode: Mode, seed: int = 0) -> ContainerResult:
    is_root = scope.container_id is None
    sizes = _sizes(scope)
    edges = break_cycles(scope.children, scope.sibling_edges)
    minimal = minimal_ranks(scope.children, edges)
    rng = random.Random(seed)
    changed: set[str] = set()

    # Start from existing ordinals of children that still exist.
    ordinals: dict[str, tuple[int, str]] = {
        cid: scope.existing[cid].ordinal for cid in scope.children if cid in scope.existing
    }

    if mode == "tidy":
        ordinals = {cid: (minimal[cid], "") for cid in scope.children}
        budget = _Budget(TIDY_EVALS, TIDY_SECONDS)
        # Seed keys by created_at so the sweep has a deterministic start.
        for r in set(minimal.values()):
            prev = None
            for cid in sorted((c for c in scope.children if minimal[c] == r),
                              key=lambda c: (scope.children[c].created_at, c)):
                prev = between(prev, None)
                ordinals[cid] = (r, prev)
        if len(scope.children) <= MAX_OPTIMIZED_SIBLINGS:
            _tidy_sweep(ordinals, scope, sizes, edges, minimal, is_root, budget, rng)
        changed = set(scope.children)
    else:
        # Step 2: forced rank repair. Push down anything below its minimum;
        # cascading is implicit because minimal_ranks already includes it.
        for cid, (r, key) in list(ordinals.items()):
            if r < minimal[cid]:
                ordinals[cid] = (minimal[cid], key)
                changed.add(cid)
        if mode == "incremental":
            budget = _Budget(INCREMENTAL_EVALS, INCREMENTAL_SECONDS)
            new_ids = sorted(
                (c for c in scope.children if c not in ordinals),
                key=lambda c: (scope.children[c].created_at, c),
            )
            for cid in new_ids:
                if len(scope.children) > MAX_OPTIMIZED_SIBLINGS:
                    rank = minimal[cid]
                    last = max((ordinals[c][1] for c in ordinals if ordinals[c][0] == rank), default=None)
                    ordinals[cid] = (rank, between(last, None))
                else:
                    _place_new(cid, ordinals, scope, sizes, edges, minimal, is_root, budget, rng)
                changed.add(cid)

    _, flow = _evaluate(ordinals, scope, sizes, edges, minimal, is_root)
    rows = _rows(scope, ordinals, flow, sizes)
    return ContainerResult(rows=rows, allocated=flow.allocated, changed_ordinals=changed)
