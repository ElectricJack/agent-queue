"""Viewport-bounded layout endpoints (spatial-layout design §5).

Mirrors the router-factory pattern of :mod:`src.api.graph` so tests can
wire a lightweight ``db`` without booting the full daemon.

The gates loop calls ``get_gate_waiters`` once per gate.  That is the one
non-bulk call in this module and it matches what the existing
``/api/projects/{id}/graph`` endpoint already does; gates per project are
few, so it is left as-is rather than given a bulk query of its own.

Scope policy: the read routes are un-scoped, exactly like
:mod:`src.api.graph` — they expose project graph geometry the dashboard
already renders.  The one mutation (``POST .../graph/tidy``, which queues
layout jobs) is scoped: it runs ``check_request_scope`` the way a
generated command route does and forwards the server-derived scope to
``graph_tidy`` as ``args["_scope"]``, so the command's own agent-session
guard sees a real scope instead of ``None``.
"""

from __future__ import annotations

import base64
import math
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.models.graph import GraphGate
from src.api.models.graph_layout import (
    AncestorRef,
    ExtentResponse,
    LayoutEdge,
    LayoutJob,
    LayoutNode,
    LayoutRect,
    LayoutStub,
    LayoutWorker,
    ListRequest,
    ListResponse,
    LocateHit,
    LocateRequest,
    LocateResponse,
    NodeResponse,
    StubOverflow,
    TidyRequest,
    TidyResponse,
    TilesRequest,
    TilesResponse,
)
from src.api.scope import check_request_scope
from src.task_graph.layout.compaction import Box, compact_layout
from src.task_graph.layout.constants import CELL_SIZE, FINISHED_STATUSES, VARIANTS
from src.task_graph.layout.view import (
    ancestors_of,
    cap_stubs,
    depth_first_order,
    dock_workers,
    forced_expansion_for,
    owner_map,
    remap_edges,
    resolve_visible,
)

RECT_CAP = 64.0
EXPANDED_CAP = 2000
LIST_CAP = 200
LOCATE_CAP = 200
#: Compacted geometries kept per router. A viewer toggles a handful of
#: containers back and forth, so a small cache turns a repeat toggle into a
#: dictionary hit; it is keyed by ``layout_version`` so a republished layout
#: can never be served from it.
GEOMETRY_CACHE_SIZE = 32

__all__ = ["build_graph_layout_router", "router"]


def _cells_for_rect(x0, y0, x1, y1):
    cx0, cy0 = math.floor(x0 / CELL_SIZE), math.floor(y0 / CELL_SIZE)
    cx1, cy1 = math.ceil(x1 / CELL_SIZE) - 1, math.ceil(y1 / CELL_SIZE) - 1
    return [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]


def _cell_bounds(rect) -> tuple[float, float, float, float]:
    """`rect` snapped outwards to whole cells.

    The neighbourhood a viewport request is allowed to reason about beyond
    its own rectangle — the same one the persisted cell index used to hand
    it before the geometry became a function of the expanded set.
    """
    return (
        math.floor(rect.x0 / CELL_SIZE) * CELL_SIZE,
        math.floor(rect.y0 / CELL_SIZE) * CELL_SIZE,
        math.ceil(rect.x1 / CELL_SIZE) * CELL_SIZE,
        math.ceil(rect.y1 / CELL_SIZE) * CELL_SIZE,
    )


def _intersects(b: Box, x0, y0, x1, y1) -> bool:
    return b.x < x1 and b.x + b.w > x0 and b.y < y1 and b.y + b.h > y0


@dataclass(frozen=True)
class _Geometry:
    """What a viewport request resolves to before the rect and filter cull.

    Purely a function of the published layout and the viewer's expanded set,
    which is what makes it safe to cache under ``layout_version``.
    """

    kinds: dict[str, str]
    collapsed_paths: dict[str, str]
    boxes: dict[str, Box]
    matches: frozenset[str] | None
    forced: frozenset[str]


def _box_fields(box: Box) -> dict:
    return {"x": box.x, "y": box.y, "w": box.w, "h": box.h}


def _persisted_box(row) -> Box:
    """The engine's own box for a row the compaction never placed.

    Everything reachable from a loaded scope gets a compacted box; a far
    endpoint drawn as a stub may live in a subtree this request never
    loaded, and then the persisted coordinate is the only one there is.
    """
    return Box(row.abs_x, row.abs_y, row.w, row.h)


def _node(row, task, kind, context_only=False, box=None) -> LayoutNode:
    box = box or _persisted_box(row)
    return LayoutNode(
        **task,
        x=box.x,
        y=box.y,
        w=box.w,
        h=box.h,
        depth=row.depth,
        container_id=row.container_id,
        kind=kind,
        context_only=context_only,
        agg_children=row.agg_children,
        agg_descendants=row.agg_descendants,
        agg_completed=row.agg_completed,
        agg_running=row.agg_running,
        agg_blocked=row.agg_blocked,
        agg_active=row.agg_active,
    )


def build_graph_layout_router(*, db, command_handler=None) -> APIRouter:
    router = APIRouter()
    # Per-router, so a test's throwaway app never reads another test's
    # geometry back out of a process-wide cache.
    geometry_cache: OrderedDict[tuple, _Geometry] = OrderedDict()

    async def _project_or_404(project_id: str):
        if await db.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'")

    async def _meta_or_pending(project_id: str, variant: str):
        meta = await db.get_layout_meta(project_id, variant)
        if meta is None:
            await db.enqueue_layout_job(project_id, variant, "backfill")
            return None
        return meta

    def _variant(v: str) -> str:
        if v not in VARIANTS:
            raise HTTPException(status_code=400, detail=f"variant must be one of {VARIANTS}")
        return v

    def _status(v: str | None) -> str:
        """Normalize a status filter.

        Task statuses are stored upper-case; clients may send any case.
        """
        return (v or "").strip().upper()

    async def _variant_for_expanded(
        project_id: str, variant: str, expanded: list[str]
    ) -> str:
        """Use the full layout when an expanded container is an active-view stub."""

        if variant == "all" or not expanded:
            return variant
        rows = await db.load_layout_rows(project_id, variant, expanded)
        return "all" if any(row.kind == "stub" for row in rows.values()) else variant

    @router.get(
        "/api/projects/{project_id}/graph/extent",
        response_model=ExtentResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_extent(project_id: str, variant: str = "active"):
        await _project_or_404(project_id)
        variant = _variant(variant)
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        # `list_layout_jobs` is oldest-first.  Report what is happening now:
        # the RUNNING job if one exists, else the most recently queued one —
        # `jobs[0]` was the OLDEST queued, which is the least informative
        # pick and, after a tidy, not the job the client just asked for.
        jobs = await db.list_layout_jobs(project_id, variant, statuses=("queued", "running"))
        running = [j for j in jobs if j["status"] == "running"]
        queued = [j for j in jobs if j["status"] == "queued"]
        current = running[0] if running else (queued[-1] if queued else None)
        job = LayoutJob(**current) if current else None
        return ExtentResponse(
            layout_version=meta["layout_version"],
            extent_w=meta["extent_w"],
            extent_h=meta["extent_h"],
            node_count=meta["node_count"],
            job=job,
        )

    async def _geometry(project_id: str, variant: str, req, *, status: str) -> _Geometry:
        """Visible kinds and compacted boxes for one request.

        Everything here is a pure function of the published layout and
        the viewer's own expanded set, so an unfiltered result is cached
        under ``layout_version``: toggling a container back and forth is
        then a dictionary hit rather than a re-load and a re-flow.  A
        filtered request is never cached — its match set comes from live
        task titles and statuses, which change without republishing the
        layout.
        """
        # Candidate rows: the open set (or the focused subtree's open set).
        if req.root is not None:
            root_rows = await db.load_layout_rows(project_id, variant, [req.root])
            if req.root not in root_rows:
                raise HTTPException(status_code=404, detail=f"No layout node '{req.root}'")
            # Focus does not cull by rect, but it must still not load the
            # root's whole subtree: under the visibility rules the only rows
            # that can be visible are the root itself and the direct children
            # of the root and of every expanded container.  A collapsed
            # child's subtree is never candidate material -- when it owns
            # edges it is reached through `load_paths_by_prefixes` below,
            # which fetches paths only.
            cand = {
                t: rt[0]
                for t, rt in (
                    await db.load_rows_for_containers(
                        project_id, variant, [req.root, *req.expanded]
                    )
                ).items()
            }
            cand[req.root] = root_rows[req.root]
            # The root's own ancestors: `resolve_visible` walks each row's
            # ancestor chain, and a chain with a missing link is treated as
            # not visible.
            root_anc = [a for a in ancestors_of(root_rows[req.root].path) if a not in cand]
            if root_anc:
                cand.update(await db.load_layout_rows(project_id, variant, root_anc))
        else:
            # The whole open set, NOT the rows the rect's cells happen to
            # hold. Collapsing a container reflows everything laid out after
            # it (design §3.5), so the persisted cell index no longer tells
            # us what lands in this rect -- only the compaction below can,
            # and it needs every child of every open container to run. That
            # is the same bound the `list` endpoint already accepts: a
            # response costs |open containers| worth of children, never a
            # whole project. The rect cull happens after compaction.
            cand = {
                t: rt[0]
                for t, rt in (
                    await db.load_rows_for_containers(project_id, variant, [None, *req.expanded])
                ).items()
            }
        # Ancestors of candidates are needed to decide visibility.
        anc_ids = {a for r in cand.values() for a in ancestors_of(r.path)} - set(cand)
        if anc_ids:
            cand.update(await db.load_layout_rows(project_id, variant, list(anc_ids)))

        # Filtering: matches anywhere force their ancestors open; non-matches vanish.
        matches: set[str] | None = None
        forced: set[str] = set()
        if req.q.strip() or status:
            matches = await db.load_matching_ids(
                project_id, variant, q=req.q.strip(), status=status
            )
            # Paths, not rows: `forced_expansion_for` reads nothing but each
            # match's path, and `matches` is used purely for membership
            # below.  A match set is bounded by the filter, not the viewport,
            # so a full row per match was the one load here that scaled with
            # the project.
            match_paths = await db.load_paths_by_ids(project_id, variant, list(matches))
            forced = set()
            for path in match_paths.values():
                forced.update(ancestors_of(path))
            if forced - set(cand):
                cand.update(
                    await db.load_layout_rows(project_id, variant, list(forced - set(cand)))
                )
            if forced:
                # The candidate set is the children of the open containers,
                # which is exactly the visible set -- but a filter
                # force-opens the ancestors of matches that lie deeper than
                # that, and those matches have to be candidates too or the
                # response is all context and no result.  The children of
                # the forced closure ARE those matches (`forced` is every
                # match's ancestor chain), so one container-scoped read
                # covers them without ever loading a row per match.
                cand.update(
                    {
                        t: rt[0]
                        for t, rt in (
                            await db.load_rows_for_containers(project_id, variant, sorted(forced))
                        ).items()
                    }
                )

        # Focus mode shows the whole subtree at the client's expanded state:
        # ``max_depth`` is ignored under ``root``, the same way the rect cap is.
        vis = resolve_visible(
            cand,
            expanded=set(req.expanded),
            max_depth=None if req.root is not None else req.max_depth,
            root=req.root,
            forced_expanded=forced,
        )
        # The collapsed set as RESOLVED, before any culling.  An edge into a
        # collapsed subtree that the rect (or the filter) then removes must
        # still remap onto its container -- which surfaces as a stub carrying
        # the container's title -- instead of exposing the inner task.
        collapsed_resolved = dict(vis.collapsed_paths)
        # Collapse is a structural change the viewer made, so the
        # persisted (fully expanded) geometry is not what they should
        # see: every collapsed container shrinks to one tile and what
        # follows it in reading order reclaims the space (design §3.5).
        # Only the scopes whose children were loaded in full may be
        # re-packed; the rest keep the interior the engine published. That is
        # every open container's scope -- `req.root` is None outside focus
        # mode, which is exactly the key of the project root's scope -- plus
        # the filter's forced closure, whose children were read in full too.
        scopes = {req.root, *req.expanded, *forced}
        boxes = compact_layout(cand, collapsed=set(vis.collapsed_paths), scopes_loaded=scopes)
        return _Geometry(
            kinds=dict(vis.visible),
            collapsed_paths=collapsed_resolved,
            boxes=boxes,
            matches=None if matches is None else frozenset(matches),
            forced=frozenset(forced),
        )

    async def _resolve(project_id: str, req: TilesRequest):
        """Resolve one tiles request into a :class:`TilesResponse`.

        Returns ``None`` when the variant has no published layout yet (the
        caller answers 202 and a backfill job has been enqueued).
        """
        # Every request check runs BEFORE ``_meta_or_pending``: a malformed
        # request must answer 400 and must never enqueue a backfill job.
        variant = _variant(req.variant)
        status = _status(req.status)
        rect = req.rect
        for v in (rect.x0, rect.y0, rect.x1, rect.y1):
            if not math.isfinite(v):
                raise HTTPException(status_code=400, detail="rect must be finite")
        if rect.x0 > rect.x1 or rect.y0 > rect.y1:
            raise HTTPException(status_code=400, detail="rect must be ordered")
        if req.root is None and (rect.x1 - rect.x0 > RECT_CAP or rect.y1 - rect.y0 > RECT_CAP):
            raise HTTPException(status_code=400, detail=f"rect larger than {RECT_CAP} units")
        if len(req.expanded) > EXPANDED_CAP:
            raise HTTPException(status_code=400, detail=f"expanded exceeds {EXPANDED_CAP}")
        if req.root is not None or status in FINISHED_STATUSES:
            variant = "all"
        else:
            variant = await _variant_for_expanded(project_id, variant, req.expanded)
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return None

        cache_key = (
            project_id,
            variant,
            meta["layout_version"],
            req.root,
            req.max_depth,
            tuple(sorted(set(req.expanded))),
        )
        cacheable = not (req.q.strip() or status)
        geo = geometry_cache.get(cache_key) if cacheable else None
        if geo is None:
            geo = await _geometry(project_id, variant, req, status=status)
            if cacheable:
                geometry_cache[cache_key] = geo
                geometry_cache.move_to_end(cache_key)
                while len(geometry_cache) > GEOMETRY_CACHE_SIZE:
                    geometry_cache.popitem(last=False)
        elif cacheable:
            geometry_cache.move_to_end(cache_key)
        # The cached geometry is shared, so cull into copies of it.
        visible = dict(geo.kinds)
        collapsed_resolved = dict(geo.collapsed_paths)
        boxes = geo.boxes
        matches = None if geo.matches is None else set(geo.matches)
        forced = set(geo.forced)
        # rect membership, now over the COMPACTED boxes: after a collapse the
        # persisted coordinates are not where these nodes are drawn, so the
        # rect has to be applied to where they actually land.
        if req.root is None:
            for tid in list(visible):
                if not _intersects(boxes[tid], rect.x0, rect.y0, rect.x1, rect.y1):
                    del visible[tid]
            # A collapsed container just off-screen still owns the edges into
            # its subtree, so the collapsed set is taken from a cell-aligned
            # neighbourhood rather than the rect itself. It must NOT be the
            # whole open set, though: `hidden_paths` below reads every task
            # inside every collapsed container it names, and a project of a
            # hundred collapsed epics would pay for all of them on every pan.
            near = _cell_bounds(rect)
            collapsed_resolved = {
                tid: path
                for tid, path in collapsed_resolved.items()
                if tid in boxes and _intersects(boxes[tid], *near)
            }
        context_only: set[str] = set()
        if matches is not None:
            for tid in list(visible):
                if tid in matches:
                    continue
                if tid in forced:
                    context_only.add(tid)
                else:
                    del visible[tid]

        # Edges: touching visible ids or anything inside a visible collapsed subtree.
        hidden_paths = await db.load_paths_by_prefixes(
            project_id, variant, list(collapsed_resolved.values())
        )
        hidden_owner = owner_map(hidden_paths, collapsed_resolved)
        touching = set(visible) | set(hidden_owner)
        raw_edges = await db.load_edges_touching(touching)
        wire, _orphans = remap_edges(raw_edges, visible, hidden_owner)
        # Stub candidates are every wire endpoint that is not visible: plain
        # orphans, plus containers an edge was remapped onto that the rect or
        # the filter then culled away.
        far_ids = {x for e in wire for x in (e["from"], e["to"])} - set(visible)
        stub_rows = await db.load_layout_rows(project_id, variant, list(far_ids))
        # A stub is drawn where the compaction put it when this request
        # reached it at all; a far endpoint inside a subtree we never loaded
        # has only its persisted box to offer.
        stub_boxes = {t: boxes.get(t) or _persisted_box(r) for t, r in stub_rows.items()}
        kept, stubs, more = cap_stubs(wire, stub_boxes, set(visible))
        stub_titles = await db.load_rows_with_tasks(project_id, variant, [s["id"] for s in stubs])
        stubs_out = [
            LayoutStub(
                project_id=project_id,
                title=stub_titles[s["id"]][1]["title"] if s["id"] in stub_titles else "",
                **s,
            )
            for s in stubs
        ]

        # Workers and gates.
        agents = [
            {"id": a.id, "name": a.name, "current_task_id": a.current_task_id}
            for a in await db.list_agents()
        ]
        # `hidden_owner` is the PRE-cull map (it has to be, for edge
        # remapping), so it can dock a worker at a container the rect or the
        # filter culled away.  A worker may only dock at a node we actually
        # return.
        docked = [
            d for d in dock_workers(agents, set(visible), hidden_owner) if d["docked_at"] in visible
        ]
        workers = [
            LayoutWorker(
                agent_id=d["agent"]["id"],
                name=d["agent"]["name"],
                docked_at=d["docked_at"],
                in_collapsed=d["in_collapsed"],
            )
            for d in docked
        ]
        gates_out: list[GraphGate] = []
        for g in await db.list_gates(project_id=project_id):
            waiters = await db.get_gate_waiters(g["id"])
            ids = sorted(w for w in waiters if w in visible)
            if ids:
                gates_out.append(
                    GraphGate(
                        id=g["id"], gate_type=g["gate_type"], status=g["status"], task_ids=ids
                    )
                )

        with_tasks = await db.load_rows_with_tasks(project_id, variant, list(visible))
        nodes = [
            _node(with_tasks[t][0], with_tasks[t][1], kind, t in context_only, boxes.get(t))
            for t, kind in visible.items()
            if t in with_tasks
        ]
        nodes.sort(key=lambda n: (n.depth, n.y, n.x))
        edges = [LayoutEdge(**e) for e in kept]
        return TilesResponse(
            nodes=nodes,
            edges=edges,
            stubs=stubs_out,
            stub_overflow=[StubOverflow(**m) for m in more],
            workers=workers,
            gates=gates_out,
            layout_version=meta["layout_version"],
        )

    @router.post(
        "/api/projects/{project_id}/graph/tiles",
        response_model=TilesResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_tiles(project_id: str, req: TilesRequest):
        await _project_or_404(project_id)
        res = await _resolve(project_id, req)
        if res is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        return res

    @router.post(
        "/api/projects/{project_id}/graph/list",
        response_model=ListResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_list(project_id: str, req: ListRequest):
        await _project_or_404(project_id)
        variant = _variant(req.variant)
        status = _status(req.status)
        if status in FINISHED_STATUSES:
            variant = "all"
        if not (1 <= req.limit <= LIST_CAP):
            raise HTTPException(status_code=400, detail=f"limit must be 1..{LIST_CAP}")
        if len(req.expanded) > EXPANDED_CAP:
            raise HTTPException(status_code=400, detail=f"expanded exceeds {EXPANDED_CAP}")
        # The cursor is an opaque base64 of the offset into the resolved,
        # depth-first ordering — stable for a given (variant, expanded,
        # filter) tuple within one layout version.  Decoded with the rest of
        # the request checks, before `_meta_or_pending`, so a malformed
        # request never enqueues a backfill.
        offset = 0
        if req.cursor:
            # binascii.Error, UnicodeDecodeError and int()'s own failure are
            # all ValueError subclasses.
            try:
                offset = int(base64.urlsafe_b64decode(req.cursor.encode()).decode())
            except ValueError:
                raise HTTPException(status_code=400, detail="bad cursor") from None
            if offset < 0:
                raise HTTPException(status_code=400, detail="bad cursor")
        if status not in FINISHED_STATUSES:
            variant = await _variant_for_expanded(project_id, variant, req.expanded)
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        # Only the rows that can possibly be visible: the roots plus the
        # direct children of every open container.  Under `max_depth=None,
        # root=None` that set IS the visible set, so a page costs
        # |expanded| + |matches| rather than a whole project (design §5.3).
        all_rows = await db.load_rows_for_containers(project_id, variant, [None, *req.expanded])
        matches: set[str] | None = None
        forced: set[str] = set()
        if req.q.strip() or status:
            matches = await db.load_matching_ids(
                project_id, variant, q=req.q.strip(), status=status
            )
            match_rows = await db.load_rows_with_tasks(project_id, variant, sorted(matches))
            all_rows.update(match_rows)
            forced = forced_expansion_for(matches, {t: rt[0] for t, rt in match_rows.items()})
            missing = sorted(forced - set(all_rows))
            if missing:
                all_rows.update(await db.load_rows_with_tasks(project_id, variant, missing))
        rows = {t: rt[0] for t, rt in all_rows.items()}
        vis = resolve_visible(
            rows, expanded=set(req.expanded), max_depth=None, root=None, forced_expanded=forced
        )
        # The same derived geometry the tiles endpoint serves, so the
        # coordinates this response carries are the ones the canvas draws.
        # Under a filter this endpoint loads a row per match rather than the
        # forced containers' full scopes, so those scopes are not re-packed
        # (the mobile list pages its cards rather than positioning them, so
        # only the unfiltered geometry is actually consumed).
        boxes = compact_layout(
            rows, collapsed=set(vis.collapsed_paths), scopes_loaded={None, *req.expanded}
        )
        ordered = [
            t
            for t in depth_first_order({t: rows[t] for t in vis.visible})
            if matches is None or t in matches or t in forced
        ]
        page = ordered[offset : offset + req.limit]
        nodes = [
            _node(
                rows[t],
                all_rows[t][1],
                vis.visible[t],
                matches is not None and t not in matches,
                boxes.get(t),
            )
            for t in page
        ]
        nxt = None
        if offset + req.limit < len(ordered):
            nxt = base64.urlsafe_b64encode(str(offset + req.limit).encode()).decode()
        return ListResponse(nodes=nodes, next_cursor=nxt, layout_version=meta["layout_version"])

    @router.get(
        "/api/projects/{project_id}/graph/node/{task_id}",
        response_model=NodeResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_node(project_id: str, task_id: str, variant: str = "all"):
        await _project_or_404(project_id)
        variant = _variant(variant)
        # Same pending contract as extent/tiles/list: a project whose
        # variant has never been laid out answers 202 and gets a backfill
        # queued, instead of a 404 the client cannot distinguish from a
        # genuinely unknown task id.
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        rows = await db.load_rows_with_tasks(project_id, variant, [task_id])
        if task_id not in rows:
            raise HTTPException(status_code=404, detail=f"No layout node '{task_id}'")
        row, task = rows[task_id]
        anc_ids = ancestors_of(row.path)
        anc = await db.load_rows_with_tasks(project_id, variant, anc_ids)
        ancestors = [
            AncestorRef(
                id=a,
                title=anc[a][1]["title"],
                x=anc[a][0].abs_x,
                y=anc[a][0].abs_y,
                w=anc[a][0].w,
                h=anc[a][0].h,
            )
            for a in anc_ids
            if a in anc
        ]
        # No viewport state here, so the stored kind is reported as-is: a
        # container is a container, never "collapsed".
        return NodeResponse(
            node=_node(row, task, row.kind),
            ancestors=ancestors,
            layout_version=meta["layout_version"],
        )

    @router.post(
        "/api/projects/{project_id}/graph/locate",
        response_model=LocateResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_locate(project_id: str, req: LocateRequest):
        await _project_or_404(project_id)
        variant = _variant(req.variant)
        status = _status(req.status)
        q = req.q.strip()
        # An unfiltered locate is a whole-project scan wearing a search
        # endpoint's clothes.  Rejected before `_meta_or_pending`, so a
        # malformed request never enqueues a backfill (same rule as tiles
        # and list).
        if not q and not status:
            raise HTTPException(status_code=400, detail="locate requires q or status")
        if len(req.expanded) > EXPANDED_CAP:
            raise HTTPException(status_code=400, detail=f"expanded exceeds {EXPANDED_CAP}")
        if status in FINISHED_STATUSES:
            variant = "all"
        limit = max(1, min(req.limit, LOCATE_CAP))
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        # Reading order, not `depth_first_order`: only the match rows are
        # loaded, so their ancestors are absent and the depth-first sort keys
        # would be incomplete.  Filter, order and cap all happen in SQL.
        rows, truncated = await db.load_matching_rows_ordered(
            project_id, variant, q=q, status=status, limit=limit
        )
        # Where the canvas will DRAW each hit, which after a collapse is not
        # where the engine persisted it. This is the same geometry the
        # matching tiles request resolves, filter-forced expansion included,
        # so jumping to a result lands on the card rather than on the hole it
        # left behind.
        geo = await _geometry(
            project_id,
            variant,
            TilesRequest(
                variant=variant,
                rect=LayoutRect(x0=0.0, y0=0.0, x1=0.0, y1=0.0),
                expanded=list(req.expanded),
                q=q,
                status=status,
            ),
            status=status,
        )
        hits = [
            LocateHit(
                id=r.task_id,
                **_box_fields(geo.boxes.get(r.task_id) or _persisted_box(r)),
                container_id=r.container_id,
            )
            for r in rows
        ]
        return LocateResponse(hits=hits, truncated=truncated)

    @router.post(
        "/api/projects/{project_id}/graph/tidy",
        response_model=TidyResponse,
        responses={403: {"description": "out of scope"}},
    )
    async def post_tidy(project_id: str, req: TidyRequest, request: Request = None):
        args: dict = {
            "project_id": project_id,
            **({"variant": req.variant} if req.variant else {}),
        }
        # The same gate a generated command route applies (src/api/codegen.py):
        # `graph_tidy` is not in AGENT_COMMAND_SET, so an ordinary agent
        # session is refused here; a local caller or an elevated supervisor
        # passes, with `project_id` enforced against the token's scope.
        #
        # FIRST, before the existence check: a caller who may not tidy this
        # project may not learn whether it exists either, and a 404/403 split
        # is exactly that oracle.
        scope: RequestScope = (
            getattr(request.state, "scope", LOCAL_SCOPE) if request is not None else LOCAL_SCOPE
        )
        scope_err = await check_request_scope("graph_tidy", args, scope, db=db)
        if scope_err is not None:
            return JSONResponse({"error": scope_err}, status_code=403)
        await _project_or_404(project_id)
        if req.variant is not None:
            _variant(req.variant)
        if command_handler is not None:
            # Forwarded the way /api/execute does it, so `_cmd_graph_tidy`'s
            # own agent-session guard reads a real scope rather than None.
            res = await command_handler.execute(
                "graph_tidy",
                {
                    **args,
                    "_scope": {
                        "kind": scope.kind,
                        "session_id": scope.session_id,
                        "task_id": scope.task_id,
                        "project_id": scope.project_id,
                        "elevated": scope.elevated,
                    },
                },
            )
            if not res.get("success"):
                raise HTTPException(status_code=400, detail=res.get("error", "tidy failed"))
            jobs = res.get("jobs", [])
        else:
            variants = [req.variant] if req.variant else list(VARIANTS)
            jobs = [await db.enqueue_layout_job(project_id, v, "tidy") for v in variants]
        return TidyResponse(jobs=[LayoutJob(**j) for j in jobs])

    @router.get("/api/projects/{project_id}/graph/jobs/{job_id}", response_model=LayoutJob)
    async def get_job(project_id: str, job_id: str):
        await _project_or_404(project_id)
        job = await db.get_layout_job(job_id)
        if job is None or job["project_id"] != project_id:
            raise HTTPException(status_code=404, detail=f"No job '{job_id}'")
        return LayoutJob(**job)

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db.

    Same shape as :func:`src.api.graph._build_default_router`: the routes
    are declared statically (so OpenAPI can introspect them) and delegate
    to a factory-built router bound to ``deps._orchestrator`` at request
    time.
    """
    from src.api import dependencies as deps

    router = APIRouter()

    def _inner() -> APIRouter:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        # The orchestrator keeps its handler private and lets the messaging
        # adapter swap it after startup, so read the live one the same way
        # ``deps.get_command_handler`` does.
        return build_graph_layout_router(
            db=orch.db, command_handler=getattr(orch, "_command_handler", None)
        )

    async def _call(path: str, method: str, **kwargs):
        for route in _inner().routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
                return await route.endpoint(**kwargs)
        raise HTTPException(status_code=500, detail="graph layout router misconfigured")

    @router.get(
        "/api/projects/{project_id}/graph/extent",
        response_model=ExtentResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_extent(project_id: str, variant: str = "active"):
        return await _call(
            "/api/projects/{project_id}/graph/extent",
            "GET",
            project_id=project_id,
            variant=variant,
        )

    @router.post(
        "/api/projects/{project_id}/graph/tiles",
        response_model=TilesResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_tiles(project_id: str, req: TilesRequest):
        return await _call(
            "/api/projects/{project_id}/graph/tiles", "POST", project_id=project_id, req=req
        )

    @router.post(
        "/api/projects/{project_id}/graph/list",
        response_model=ListResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_list(project_id: str, req: ListRequest):
        return await _call(
            "/api/projects/{project_id}/graph/list", "POST", project_id=project_id, req=req
        )

    @router.get(
        "/api/projects/{project_id}/graph/node/{task_id}",
        response_model=NodeResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def get_node(project_id: str, task_id: str, variant: str = "all"):
        return await _call(
            "/api/projects/{project_id}/graph/node/{task_id}",
            "GET",
            project_id=project_id,
            task_id=task_id,
            variant=variant,
        )

    @router.post(
        "/api/projects/{project_id}/graph/locate",
        response_model=LocateResponse,
        responses={202: {"description": "layout pending"}},
    )
    async def post_locate(project_id: str, req: LocateRequest):
        return await _call(
            "/api/projects/{project_id}/graph/locate", "POST", project_id=project_id, req=req
        )

    @router.post(
        "/api/projects/{project_id}/graph/tidy",
        response_model=TidyResponse,
        responses={403: {"description": "out of scope"}},
    )
    async def post_tidy(project_id: str, req: TidyRequest, request: Request = None):
        # `request` is forwarded so the factory route can read
        # `request.state.scope` -- the mutation is scope-checked.
        return await _call(
            "/api/projects/{project_id}/graph/tidy",
            "POST",
            project_id=project_id,
            req=req,
            request=request,
        )

    @router.get("/api/projects/{project_id}/graph/jobs/{job_id}", response_model=LayoutJob)
    async def get_job(project_id: str, job_id: str):
        return await _call(
            "/api/projects/{project_id}/graph/jobs/{job_id}",
            "GET",
            project_id=project_id,
            job_id=job_id,
        )

    return router


router = _build_default_router()
