"""Container layout engine (§4.4, §4.7).

``layout_container`` lays out ONE container's children from a
``ContainerScope``. Ordinals of existing children are immutable in
``incremental`` and ``resize`` modes; only the forced rank repair may
change them. ``tidy`` mode treats every child as movable.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Literal

from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    INCREMENTAL_EVALS,
    INCREMENTAL_SECONDS,
    MAX_OPTIMIZED_SIBLINGS,
    TIDY_EVALS,
    TIDY_SECONDS,
)
from src.task_graph.layout.cost import container_cost
from src.task_graph.layout.flow import FlowResult, flow_container
from src.task_graph.layout.layering import break_cycles, minimal_ranks_acyclic
from src.task_graph.layout.model import ContainerScope, LayoutRow
from src.task_graph.layout.order_key import between

logger = logging.getLogger(__name__)

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
    _warned: bool = field(default=False, repr=False)

    def spent(self) -> bool:
        """Eval-count budget only. Wall-clock never feeds this decision, so
        layout output stays deterministic for a given input+seed regardless
        of how fast (or slow) evaluation happens to run."""
        return self.used >= self.evals

    def check_safety(self) -> None:
        """Wall-clock safety valve, decoupled from ``spent()``. Guards
        against a pathological case where eval count doesn't bound wall
        time; trips at 10x the nominal seconds budget, logs once, and then
        forces ``spent()`` to report exhausted from here on."""
        if self.used >= self.evals:
            return
        if time.monotonic() - self.started >= self.seconds:
            if not self._warned:
                logger.warning(
                    "layout budget wall-clock safety valve tripped after %.2fs (evals used=%d/%d)",
                    self.seconds,
                    self.used,
                    self.evals,
                )
                self._warned = True
            self.used = self.evals


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
            task_id=cid,
            container_id=scope.container_id,
            path=f"{scope.container_path}{cid}/",
            depth=scope.depth,
            rank=rank,
            order_key=key,
            w=w,
            h=h,
            rel_x=rx,
            rel_y=ry,
            abs_x=ox + rx,
            abs_y=oy + ry,
            kind=_kind(scope, cid),
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
    """Every (left_key, right_key) gap in a rank, including both ends.

    Degenerate gaps (lo == hi, which ``between`` can't split) are dropped.
    """
    keys = [ordinals[c][1] for c in rank_ids_sorted]
    gaps: list[tuple[str | None, str | None]] = [(None, keys[0] if keys else None)]
    for i in range(len(keys)):
        gaps.append((keys[i], keys[i + 1] if i + 1 < len(keys) else None))
    return [(lo, hi) for lo, hi in gaps if lo is None or hi is None or lo != hi]


def _barycenter_gap(
    rank_ids_sorted: list[str],
    ordinals,
    positions: dict[str, tuple[float, float]],
    blockers: list[str],
) -> tuple[str | None, str | None]:
    """The gap in the target rank whose neighbours' x straddle the
    blockers' median x (front gap if the median is below everything in the
    rank, end gap if above everything).

    ``positions`` comes from a single prior ``_evaluate`` of the current
    (pre-insertion) ordinals, so blocker x (typically in the rank above)
    and target-rank x share one coordinate space.

    For an even blocker count this uses the *lower* median (the smaller of
    the two middle x's) rather than averaging, so the comparison is a pure
    positional test with no float-averaging edge cases.
    """
    xs = sorted(positions[b][0] for b in blockers if b in positions)
    if not rank_ids_sorted:
        return (None, None)
    keys = [ordinals[c][1] for c in rank_ids_sorted]
    if not xs:
        return (keys[-1], None)
    mid = xs[(len(xs) - 1) // 2]
    xpos = [positions[c][0] for c in rank_ids_sorted]
    if mid <= xpos[0]:
        return (None, keys[0])
    if mid >= xpos[-1]:
        return (keys[-1], None)
    for i in range(len(rank_ids_sorted) - 1):
        if xpos[i] <= mid <= xpos[i + 1]:
            return (keys[i], keys[i + 1])
    return (keys[-1], None)


def _place_new(
    cid: str, ordinals, scope, sizes, edges, minimal, is_root, budget: _Budget, rng
) -> None:
    """Choose rank (minimal, or minimal+1 if it pays) and a gap for ``cid``."""
    budget.check_safety()
    blockers = [b for d, b in edges if d == cid and b in ordinals]
    rank0 = minimal[cid]
    best: tuple[float, tuple[int, str]] | None = None
    positions0: dict[str, tuple[float, float]] | None = None
    if blockers:
        _, flow0 = _evaluate(ordinals, scope, sizes, edges, minimal, is_root)
        positions0 = flow0.positions
    for rank in (rank0, rank0 + 1):
        in_rank = sorted(
            (c for c, (r, _) in ordinals.items() if r == rank), key=lambda c: ordinals[c][1]
        )
        if not blockers:
            # No blockers: this node just appends to the rank's end. Only
            # evaluate the end gap — iterating every gap wastes the budget
            # on large ranks for a decision the spec has already made.
            gaps = [(ordinals[in_rank[-1]][1] if in_rank else None, None)]
        else:
            gaps = _gap_keys(in_rank, ordinals)
            # Try the barycenter gap first (cheap, usually near-optimal);
            # the cost loop below still evaluates every other gap and picks
            # whichever truly minimizes cost.
            bary_gap = _barycenter_gap(in_rank, ordinals, positions0, blockers)
            if bary_gap in gaps:
                gaps.remove(bary_gap)
            if bary_gap[0] is None or bary_gap[1] is None or bary_gap[0] != bary_gap[1]:
                gaps.insert(0, bary_gap)
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
    if best is None:
        raise RuntimeError("no placement candidate")
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
        pos = {c: i for i, c in enumerate(ordered[rank_idx])}

        def bary(c: str) -> float:
            ns = [ref_pos[n] for n in neighbours.get(c, ()) if n in ref_pos]
            return sum(ns) / len(ns) if ns else float(pos[c])

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
        budget.check_safety()
        if budget.spent():
            break
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
                    break  # ordering changed — restart the pass fresh
                if budget.spent():
                    break
            if improved or budget.spent():
                break


def layout_container(scope: ContainerScope, *, mode: Mode, seed: int = 0) -> ContainerResult:
    is_root = scope.container_id is None
    sizes = _sizes(scope)
    edges = break_cycles(scope.children, scope.sibling_edges)
    # ``edges`` is already acyclic — don't make ``minimal_ranks`` repeat the
    # cycle search over the same list.
    minimal = minimal_ranks_acyclic(scope.children, edges)
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
            for cid in sorted(
                (c for c in scope.children if minimal[c] == r),
                key=lambda c: (scope.children[c].created_at, c),
            ):
                prev = between(prev, None)
                ordinals[cid] = (r, prev)
        if len(scope.children) <= MAX_OPTIMIZED_SIBLINGS:
            _tidy_sweep(ordinals, scope, sizes, edges, minimal, is_root, budget, rng)
        changed = set(scope.children)
    else:
        # Step 2: forced rank repair. A node whose old rank falls below its
        # newly-computed minimum is pushed down. It takes a FRESH key at
        # the end of its new rank — never its old key — so it can't
        # collide with whatever already occupies that rank (every rank's
        # first-ever key is "U", so keeping the old key easily collides).
        # Pushed nodes are processed in a deterministic order (old rank,
        # old key, id) so multi-node cascades stay stable.
        pushed = sorted(
            (cid for cid, (r, _) in ordinals.items() if r < minimal[cid]),
            key=lambda cid: (ordinals[cid][0], ordinals[cid][1], cid),
        )
        for cid in pushed:
            new_rank = minimal[cid]
            last = max(
                (key for c, (r, key) in ordinals.items() if r == new_rank and c != cid),
                default=None,
            )
            ordinals[cid] = (new_rank, between(last, None))
            changed.add(cid)
        if mode == "incremental":
            budget = _Budget(INCREMENTAL_EVALS, INCREMENTAL_SECONDS)
            new_ids = sorted(
                (c for c in scope.children if c not in ordinals),
                key=lambda c: (scope.children[c].created_at, c),
            )
            for cid in new_ids:
                if len(scope.children) > MAX_OPTIMIZED_SIBLINGS:
                    blockers = [b for d, b in edges if d == cid and b in ordinals]
                    rank = minimal[cid]
                    in_rank = sorted(
                        (c for c, (r, _) in ordinals.items() if r == rank),
                        key=lambda c: ordinals[c][1],
                    )
                    if blockers:
                        _, flow0 = _evaluate(ordinals, scope, sizes, edges, minimal, is_root)
                        lo, hi = _barycenter_gap(in_rank, ordinals, flow0.positions, blockers)
                    else:
                        last = ordinals[in_rank[-1]][1] if in_rank else None
                        lo, hi = last, None
                    ordinals[cid] = (rank, between(lo, hi))
                else:
                    _place_new(cid, ordinals, scope, sizes, edges, minimal, is_root, budget, rng)
                changed.add(cid)

    missing = set(scope.children) - set(ordinals)
    if missing:
        raise ValueError(f"unplaced children: {sorted(missing)}")

    _, flow = _evaluate(ordinals, scope, sizes, edges, minimal, is_root)
    rows = _rows(scope, ordinals, flow, sizes)
    return ContainerResult(rows=rows, allocated=flow.allocated, changed_ordinals=changed)
