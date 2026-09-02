"""Database driver for the layout engine (§4.4, §4.6, §4.7)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Literal

from src.task_graph.layout.constants import (
    CARD_H, CARD_W, FINISHED_STATUSES, HEADER_H, PADDING, RANKING_DEP_TYPES, ROOT,
    RUNNING_STATUSES, VARIANTS,
)
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import (
    ContainerScope, LayoutRow, SnapTask, Translation, WriteSet,
)

logger = logging.getLogger(__name__)


class LayoutRelayDepthExceeded(ValueError):
    """A batch could not settle every moved subtree into a stable frame.

    Raised before anything is published, so the batch is a no-op and the
    dirty marks survive; the orchestrator escalates a repeat offender to a
    full tidy job.
    """


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

    # ── incremental ─────────────────────────────────────────────────────
    async def process_dirty(
        self, project_id: str, *, min_age_seconds: float
    ) -> dict[str, int | None]:
        """Fold the project's durable dirty marks into every variant.

        Returns the new layout version per variant (``None`` when there was
        nothing to do). A variant with no layout yet gets a full layout —
        the only correct answer when there is nothing to patch.
        """
        seq, marks = await self.db.pop_layout_dirty(project_id, min_age_seconds=min_age_seconds)
        out: dict[str, int | None] = {v: None for v in VARIANTS}
        if not marks:
            return out
        snapshot, edges = await self.db.load_project_snapshot(project_id)
        blocked = await self._blocked_ids(project_id)
        consumed = False
        for idx, variant in enumerate(VARIANTS):
            last = idx == len(VARIANTS) - 1
            if await self.db.get_layout_meta(project_id, variant) is None:
                out[variant] = await self.full_layout(project_id, variant)
                continue
            batch = _IncrementalBatch(self, project_id, variant, snapshot, edges, blocked, marks)
            # Only the final variant retires the marks: if a later variant
            # raises, the batch is replayed in full rather than lost. Redoing
            # an already-applied variant is idempotent — every pass recomputes
            # from the snapshot.
            out[variant] = await batch.run(seq if last else None)
            consumed = consumed or last
        if not consumed:
            # The last variant was rebuilt from scratch (``full_layout`` never
            # consumes marks), so retire them here rather than replaying them
            # forever.
            async with self.db._engine.begin() as conn:
                await self.db.clear_layout_dirty(project_id, seq, conn=conn)
        return out


class _IncrementalBatch:
    """One variant's incremental pass over a batch of dirty marks (§4.6).

    The batch keeps a *pending* row per task id, so a container re-laid
    twice (incremental, then ``resize`` from a growing child) writes one
    row, and a translation emitted twice nets out to a single delta.
    """

    MAX_DRAIN_STEPS = 10_000
    MAX_RELAY_ROUNDS = 16

    def __init__(self, driver, project_id, variant, snapshot, edges, blocked, marks):
        self.db = driver.db
        self.seed = driver.seed
        self.project_id = project_id
        self.variant = variant
        self.snapshot = snapshot
        self.marks = marks
        self.present, self.stubs = _visible(snapshot, variant)
        self.parent_of: dict[str, str | None] = {t.id: t.parent_id for t in snapshot.values()}

        all_children_of: dict[str | None, list[str]] = defaultdict(list)
        for t in snapshot.values():
            all_children_of[t.parent_id].append(t.id)
        self.aggs = _aggregates(snapshot, all_children_of, blocked)

        # Visible children per container, sorted so the pass is deterministic
        # regardless of set iteration order.
        self.children_of: dict[str | None, list[str]] = defaultdict(list)
        for tid in sorted(self.present):
            self.children_of[self.parent_of[tid]].append(tid)
        self.rank_edges: dict[str | None, list[tuple[str, str]]] = defaultdict(list)
        for d, b, typ in edges:
            if typ in RANKING_DEP_TYPES and d in self.present and b in self.present \
                    and self.parent_of[d] == self.parent_of[b]:
                self.rank_edges[self.parent_of[d]].append((d, b))

        self.ws = WriteSet()
        self.pending: dict[str, LayoutRow] = {}
        self._db_cache: dict[str, LayoutRow | None] = {}
        self.size_of: dict[str, tuple[float, float]] = {}
        self.laid_under: dict[str | None, tuple[str, tuple[float, float]]] = {}
        self.translated: dict[str, tuple[float, float]] = {}
        self.processed: set[str | None] = set()
        self.queue: list[tuple[str | None, str]] = []
        self.root_extent: tuple[float, float] | None = None
        self.dirty_tasks: set[str] = set()

    # ── row access ──────────────────────────────────────────────────────
    async def _db_row(self, tid: str) -> LayoutRow | None:
        if tid not in self._db_cache:
            rows = await self.db.load_layout_rows(self.project_id, self.variant, [tid])
            self._db_cache[tid] = rows.get(tid)
        return self._db_cache[tid]

    async def _frame_row(self, tid: str) -> LayoutRow | None:
        """The row whose position defines the frame a container's children
        are authored in.

        Children are authored in their container's *stored* frame, because
        the container's move is published as a translation over its whole
        subtree: a child laid at ``stored_origin + rel`` plus that
        translation lands exactly where the new layout wants it. Only a
        container that moved to a new path gets no such translation (its
        subtree is re-laid instead), so there the pending row — already
        expressed in the new parent's stored frame — is the right base.
        """
        stored = await self._db_row(tid)
        pending = self.pending.get(tid)
        if stored is not None and (pending is None or pending.path == stored.path):
            return stored
        return pending

    def _depth(self, cid: str | None) -> int:
        d = 0
        seen: set[str] = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            cid = self.parent_of.get(cid)
            d += 1
        return d

    async def _current_size(self, cid: str) -> tuple[float, float]:
        """The allocated size to reserve for a container child.

        The size this batch computed wins; otherwise the container's OWN
        stored row, wherever it currently sits in the tree — a container
        moved into a new parent keeps its real size instead of collapsing
        to a card slot. A container created in this batch has no stored row
        and starts at a card slot until its own pass reports an allocation.
        """
        if cid in self.size_of:
            return self.size_of[cid]
        row = await self._db_row(cid) or self.pending.get(cid)
        return (row.w, row.h) if row else (CARD_W, CARD_H)

    @staticmethod
    def _origin(row: LayoutRow) -> tuple[float, float]:
        """Absolute content origin of a container row."""
        return (row.abs_x + PADDING, row.abs_y + PADDING + HEADER_H)

    # ── dirty → containers ──────────────────────────────────────────────
    async def _seed_queue(self) -> None:
        dirty: set[str | None] = set()
        for tid, reason in self.marks:
            self.dirty_tasks.add(tid)
            if tid in self.snapshot:
                dirty.add(self.parent_of[tid])
                if self.snapshot[tid].is_container:
                    # its own children may have become (in)visible
                    dirty.add(tid)
            else:
                # Task is gone: re-lay whichever container used to hold it.
                row = await self._db_row(tid)
                if row is not None:
                    dirty.add(row.container_id)
            if reason.startswith("parent.changed:"):
                old = reason.split(":", 1)[1]
                dirty.add(None if old in ("", "-") else old)
        # a dirty container that is not present in this variant collapses to
        # its nearest present ancestor
        collapsed: set[str | None] = set()
        for cid in dirty:
            seen: set[str] = set()
            while cid is not None and cid not in self.present and cid not in seen:
                seen.add(cid)
                cid = self.parent_of.get(cid)
            collapsed.add(cid if cid is None or cid in self.present else None)
        ordered = sorted(collapsed, key=lambda c: (c is not None, c or ""))
        self.queue = [(cid, "incremental") for cid in ordered]

    # ── the engine pass over one container ──────────────────────────────
    async def _lay(self, cid: str | None, mode: str) -> None:
        if mode == "incremental" and cid in self.processed:
            return
        crow = await self._frame_row(cid) if cid is not None else None
        if cid is not None and crow is None:
            # No row yet (a container created in this very batch). Its parent's
            # pass creates the row; the relay round below then lays its children.
            return
        kids = self.children_of.get(cid, [])
        existing = await self.db.load_children_layout_rows(self.project_id, self.variant, cid)
        path = crow.path if crow else "/"
        depth = crow.depth + 1 if crow else 0
        origin = self._origin(crow) if crow else (0.0, 0.0)
        if mode == "resize" and any(k not in existing for k in kids):
            # A resize can't place a child that has no ordinal yet.
            mode = "incremental"
        child_sizes: dict[str, tuple[float, float]] = {}
        for k in kids:
            if self.snapshot[k].is_container and k not in self.stubs:
                child_sizes[k] = await self._current_size(k)
        scope = ContainerScope(
            container_id=cid, container_path=path, depth=depth,
            children={k: self.snapshot[k] for k in kids},
            existing={k: r for k, r in existing.items() if k in kids},
            sibling_edges=self.rank_edges.get(cid, []), child_sizes=child_sizes,
            stub_ids=frozenset(s for s in self.stubs if s in kids), origin=origin,
        )
        res = await asyncio.to_thread(layout_container, scope, mode=mode, seed=self.seed)
        self.processed.add(cid)
        # Remember the FRAME these children were authored in, not just the
        # path: a container framed on its pending row (moved here, or created
        # in this batch) can still move when its real size reaches its parent,
        # and its children must then be re-laid at the new origin.
        self.laid_under[cid] = (path, origin)

        # Rows that left this container: removed, archived, or hidden by the
        # variant filter. They take their whole subtree with them.
        for k, prev in sorted(existing.items()):
            if k not in res.rows and (k not in self.snapshot or k not in self.present):
                await self._delete_subtree(prev.path)

        for k, row in sorted(res.rows.items()):
            prev = existing.get(k)
            self.pending[k] = row
            if prev is None or (prev.kind == "card" and row.kind == "card"):
                continue  # a card has no subtree to carry
            if prev.path != row.path:
                # Moved to a different container: its descendants are re-laid
                # under the new path (relay round), never translated.
                continue
            total = (row.abs_x - prev.abs_x, row.abs_y - prev.abs_y)
            done = self.translated.get(row.path, (0.0, 0.0))
            dx, dy = total[0] - done[0], total[1] - done[1]
            if dx or dy:
                self.ws.translations.append(Translation(path_prefix=row.path, dx=dx, dy=dy))
                self.translated[row.path] = total

        if cid is None:
            self.root_extent = res.allocated
            return
        # A stub occupies a single card slot no matter what it contains.
        allocated = (CARD_W, CARD_H) if cid in self.stubs else res.allocated
        if allocated != self.size_of.get(cid, (crow.w, crow.h)):
            self.size_of[cid] = allocated
            self.queue.append((self.parent_of.get(cid), "resize"))

    async def _delete_subtree(self, path_prefix: str) -> None:
        self.ws.deletes.extend(
            await self.db.load_subtree_ids(self.project_id, self.variant, path_prefix)
        )

    async def _drain(self) -> None:
        steps = 0
        while self.queue:
            steps += 1
            if steps > self.MAX_DRAIN_STEPS:
                logger.warning(
                    "layout incremental drain exceeded %d steps (project=%s variant=%s)",
                    self.MAX_DRAIN_STEPS, self.project_id, self.variant,
                )
                self.queue.clear()
                break
            # Deepest container first, so a child's new size is known before
            # its parent is laid out.
            self.queue.sort(key=lambda item: -self._depth(item[0]))
            cid, mode = self.queue.pop(0)
            await self._lay(cid, mode)

    async def _needs_relay(self, tid: str) -> bool:
        """Does this container's subtree sit in a frame it no longer has?"""
        if tid not in self.present or tid in self.stubs:
            return False
        if not self.snapshot[tid].is_container:
            return False
        frame = await self._frame_row(tid)
        if frame is None:
            return False  # no row anywhere yet; its parent's pass creates one
        used = self.laid_under.get(tid)
        if used is None:
            # Never laid in this batch. Its stored children are fine as long as
            # the container keeps its path (a move gets no translation) and was
            # not a stub (a reopened stub has no child rows left at all).
            stored = await self._db_row(tid)
            return stored is None or stored.path != frame.path or stored.kind == "stub"
        return used != (frame.path, self._origin(frame))

    async def _containers_needing_relay(self) -> list[str]:
        return [tid for tid in sorted(self.pending) if await self._needs_relay(tid)]

    async def _relay_moved_containers(self) -> None:
        """Re-lay every container whose subtree is stranded in a stale frame.

        Two causes: the container moved to a new path (its descendants keep the
        old prefix and get no translation), or it was framed on its pending row
        and that row has since moved — which happens whenever a moved or newly
        created container reports its real size and its parent re-flows around
        it. Both are fixed the same way: lay its children again in the frame it
        actually has now, which is also recursive, since a re-laid child
        container moves its own subtree in turn.
        """
        for _ in range(self.MAX_RELAY_ROUNDS):
            again = await self._containers_needing_relay()
            if not again:
                return
            for tid in again:
                self.processed.discard(tid)
                self.queue.append((tid, "incremental"))
            await self._drain()
        if await self._containers_needing_relay():
            raise LayoutRelayDepthExceeded(
                f"layout relay did not converge in {self.MAX_RELAY_ROUNDS} rounds "
                f"(project={self.project_id} variant={self.variant})"
            )

    # ── aggregates ──────────────────────────────────────────────────────
    async def _refresh_aggregates(self) -> None:
        """Aggregates for every written row and every ancestor of a dirty task.

        Counts come from the snapshot (identical semantics to ``full_layout``)
        rather than from ``subtree_aggregates``: the stored rows still describe
        the *previous* version at this point, so a DB read would report the
        pre-batch tree.
        """
        targets: set[str] = set(self.pending)
        for tid in self.dirty_tasks:
            p = self.parent_of.get(tid)
            seen: set[str] = set()
            while p is not None and p not in seen:
                seen.add(p)
                targets.add(p)
                p = self.parent_of.get(p)
        for tid in sorted(targets):
            if tid not in self.present:
                continue
            agg = self.aggs.get(tid)
            if agg is None:
                continue
            row = self.pending.get(tid) or await self._db_row(tid)
            if row is None:
                continue
            row.agg_children, row.agg_descendants = agg["children"], agg["descendants"]
            row.agg_completed, row.agg_running = agg["completed"], agg["running"]
            row.agg_blocked, row.agg_active = agg["blocked"], agg["active"]
            self.pending[tid] = row

    # ── publish ─────────────────────────────────────────────────────────
    async def run(self, consumed_seq: int | None) -> int:
        await self._seed_queue()
        await self._drain()
        # Dirty tasks that vanished from this variant go, with their subtree.
        for tid in sorted(self.dirty_tasks):
            if tid in self.present:
                continue
            row = await self._db_row(tid)
            if row is not None:
                await self._delete_subtree(row.path)
        await self._relay_moved_containers()
        await self._refresh_aggregates()

        self.ws.upserts = [self.pending[k] for k in sorted(self.pending)]
        self.ws.deletes = sorted(set(self.ws.deletes) - set(self.pending))
        if self.root_extent is not None:
            self.ws.sizes[ROOT] = self.root_extent
        meta = await self.db.get_layout_meta(self.project_id, self.variant)
        extent = self.ws.sizes.get(ROOT, (meta["extent_w"], meta["extent_h"]))
        return await self.db.publish_layout(
            self.project_id, self.variant, self.ws,
            consumed_seq=consumed_seq, extent=extent, node_count_delta=None,
        )
