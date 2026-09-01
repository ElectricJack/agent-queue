"""Database driver for the layout engine (§4.4, §4.6, §4.7)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Literal

from src.task_graph.layout.constants import (
    FINISHED_STATUSES, RANKING_DEP_TYPES, ROOT, RUNNING_STATUSES,
)
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import ContainerScope, LayoutRow, SnapTask, WriteSet

logger = logging.getLogger(__name__)


def _visible(snapshot: dict[str, SnapTask], variant: str) -> tuple[set[str], set[str]]:
    """Return (ids present in the variant, container ids rendered as stubs)."""
    if variant == "all":
        return set(snapshot), set()
    children_of: dict[str | None, list[str]] = defaultdict(list)
    for t in snapshot.values():
        children_of[t.parent_id].append(t.id)
    active_desc: dict[str, int] = {}

    def count(tid: str) -> int:
        n = 0
        for c in children_of.get(tid, ()):
            n += (0 if snapshot[c].status in FINISHED_STATUSES else 1) + count(c)
        active_desc[tid] = n
        return n

    for t in snapshot.values():
        if t.parent_id is None:
            count(t.id)
    present: set[str] = set()
    stubs: set[str] = set()
    for t in snapshot.values():
        if t.is_container and active_desc.get(t.id, 0) == 0:
            # finished container: stub if it has any descendants, else keep as
            # empty container only if it is itself unfinished
            if children_of.get(t.id):
                present.add(t.id)
                stubs.add(t.id)
            elif t.status not in FINISHED_STATUSES:
                present.add(t.id)
        elif t.status not in FINISHED_STATUSES or (t.is_container and active_desc.get(t.id, 0) > 0):
            present.add(t.id)
    # a present node's ancestors must be present
    for tid in list(present):
        p = snapshot[tid].parent_id
        while p is not None and p not in present:
            present.add(p)
            stubs.discard(p)
            p = snapshot[p].parent_id
    # children of stubs are not present
    def prune(tid: str) -> None:
        for c in children_of.get(tid, ()):
            present.discard(c)
            prune(c)
    for s in stubs:
        prune(s)
    return present, stubs


def _aggregates(snapshot, children_of, blocked: set[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}

    def walk(tid: str) -> dict[str, int]:
        agg = {"children": 0, "descendants": 0, "completed": 0, "running": 0, "blocked": 0, "active": 0}
        for c in children_of.get(tid, ()):
            sub = walk(c)
            t = snapshot[c]
            agg["children"] += 1
            agg["descendants"] += 1 + sub["descendants"]
            agg["completed"] += (t.status in FINISHED_STATUSES) + sub["completed"]
            agg["running"] += (t.status in RUNNING_STATUSES) + sub["running"]
            agg["blocked"] += (c in blocked) + sub["blocked"]
            agg["active"] += (t.status not in FINISHED_STATUSES) + sub["active"]
        out[tid] = agg
        return agg

    for t in snapshot.values():
        if t.parent_id is None:
            walk(t.id)
    return out


def build_full_write_set(
    snapshot: dict[str, SnapTask],
    edges: list[tuple[str, str, str]],
    variant: str,
    *,
    blocked: set[str],
    mode: Literal["tidy"] = "tidy",
    seed: int = 0,
) -> tuple[WriteSet, tuple[float, float]]:
    present, stubs = _visible(snapshot, variant)
    children_of: dict[str | None, list[str]] = defaultdict(list)
    for tid in present:
        children_of[snapshot[tid].parent_id].append(tid)
    all_children_of: dict[str | None, list[str]] = defaultdict(list)
    for t in snapshot.values():
        all_children_of[t.parent_id].append(t.id)
    rank_edges: dict[str | None, list[tuple[str, str]]] = defaultdict(list)
    for d, b, typ in edges:
        if typ in RANKING_DEP_TYPES and d in present and b in present \
                and snapshot[d].parent_id == snapshot[b].parent_id:
            rank_edges[snapshot[d].parent_id].append((d, b))
    aggs = _aggregates(snapshot, all_children_of, blocked)

    sizes: dict[str, tuple[float, float]] = {}
    rel_rows: dict[str, LayoutRow] = {}

    # Bottom-up: lay out a container after all its container children.
    def lay(container_id: str | None, path: str, depth: int) -> tuple[float, float]:
        kids = children_of.get(container_id, [])
        for k in kids:
            if snapshot[k].is_container and k not in stubs:
                sizes[k] = lay(k, f"{path}{k}/", depth + 1)
        scope = ContainerScope(
            container_id=container_id, container_path=path, depth=depth,
            children={k: snapshot[k] for k in kids}, existing={},
            sibling_edges=rank_edges.get(container_id, []),
            child_sizes={k: sizes[k] for k in kids if k in sizes},
            stub_ids=frozenset(s for s in stubs if s in kids),
        )
        res = layout_container(scope, mode=mode, seed=seed)
        rel_rows.update(res.rows)
        return res.allocated

    extent = lay(None, "/", 0)

    # Top-down: absolute coordinates.
    from src.task_graph.layout.constants import HEADER_H, PADDING

    def place(container_id: str | None, ox: float, oy: float) -> None:
        for k in children_of.get(container_id, []):
            r = rel_rows[k]
            r.abs_x, r.abs_y = ox + r.rel_x, oy + r.rel_y
            a = aggs.get(k)
            if a:
                r.agg_children, r.agg_descendants = a["children"], a["descendants"]
                r.agg_completed, r.agg_running = a["completed"], a["running"]
                r.agg_blocked, r.agg_active = a["blocked"], a["active"]
            if r.kind == "container":
                place(k, r.abs_x + PADDING, r.abs_y + PADDING + HEADER_H)

    place(None, 0.0, 0.0)
    ws = WriteSet(upserts=list(rel_rows.values()), sizes={ROOT: extent})
    return ws, extent


class LayoutDriver:
    def __init__(self, db, *, seed: int = 0):
        self.db = db
        self.seed = seed

    async def _blocked_ids(self, project_id: str) -> set[str]:
        tasks = await self.db.list_tasks(project_id=project_id)
        return {t.id for t in tasks if getattr(t, "is_blocked", False)}

    async def full_layout(self, project_id: str, variant: str, *, mode: Literal["tidy"] = "tidy") -> int:
        snapshot, edges = await self.db.load_project_snapshot(project_id)
        blocked = await self._blocked_ids(project_id)
        ws, extent = await asyncio.to_thread(
            build_full_write_set, snapshot, edges, variant, blocked=blocked, mode=mode, seed=self.seed
        )
        # Replace everything: rows no longer present are deleted.
        existing = await self.db.load_layout_rows(project_id, variant, list(snapshot))
        keep = {r.task_id for r in ws.upserts}
        ws.deletes = [tid for tid in existing if tid not in keep]
        return await self.db.publish_layout(
            project_id, variant, ws, consumed_seq=None, extent=extent, node_count_delta=None
        )
