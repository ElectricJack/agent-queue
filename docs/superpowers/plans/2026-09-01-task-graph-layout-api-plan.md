# Task Graph Layout API (Stage 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the persisted layout through viewport-bounded HTTP endpoints: `extent`, `tiles`, `list`, `node`, `locate`, `tidy`, and `jobs`, with collapse, focus, level-of-detail, filtering, edge remapping, stubs, and worker docking resolved on the server.

**Architecture:** Pure resolution logic lives in `src/task_graph/layout/view.py` (visibility, edge remapping, stubs, docking, ordering) and is fed by bulk queries added to `LayoutQueryMixin`. A thin FastAPI router in `src/api/graph_layout.py` validates input, calls the queries and the view functions, and returns Pydantic models from `src/api/models/graph_layout.py`. Tidy goes through `CommandHandler` (`graph_tidy`, from Stage 1).

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy Core, httpx `ASGITransport` tests, PostgreSQL perf via `tests/perf`.

**Spec:** `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` (sections 5, 7, 9)

**Depends on:** Stage 1 plan (`2026-09-01-task-graph-layout-engine-plan.md`) merged: tables, `LayoutQueryMixin`, `LayoutDriver`, `graph_tidy` command, `scripts/seed_layout_perf.py`.

## Global Constraints

- Rect cap 64 × 64 units unless `root` is set. `expanded` at most 2,000 ids. `list` limit at most 200. `locate` cap 200. Stubs at most 8 per visible node, then `{node_id, direction, more}`.
- Node `kind` values on the wire: `card`, `container`, `collapsed`, `stub`.
- Drawn edge types: `blocks`, `waits-for`, `conditional-blocks`, `discovered-from`. Never `parent-child`, `related`, `duplicates`, `supersedes`.
- Finished statuses: `COMPLETED`, `CANCELED`, `CANCELLED`, `SKIPPED`. A finished `status` filter forces `variant = all`.
- `root` forces `variant = all` and removes the rect cap.
- No meta row: `202 {"status": "layout_pending"}` and enqueue a `backfill` job. Meta row with zero nodes: normal `200`.
- Invalid input: `400`. Unknown project: `404`.
- `tiles` and `list` are POST with JSON bodies.
- Endpoint work must be bulk queries only: no per-task loops hitting the database.

---

## File structure

| File | Responsibility |
|---|---|
| `src/database/queries/layout_queries.py` | Add: `load_rows_in_cells`, `load_rows_with_tasks`, `load_rows_by_prefixes`, `load_edges_touching`, `load_matching_ids`, `load_all_rows_with_tasks` |
| `src/task_graph/layout/view.py` | `resolve_visible`, `remap_edges`, `dock_workers`, `depth_first_order`, `filter_matches` |
| `src/api/models/graph_layout.py` | Request and response models |
| `src/api/graph_layout.py` | Router factory + default router |
| `src/api/app.py` | Register router |
| `tests/task_graph/layout/test_view.py` | Pure view logic tests |
| `tests/test_api_graph_layout.py` | Endpoint tests on SQLite |
| `tests/perf/test_layout_api_statements.py` | PostgreSQL `tiles` latency |

---

### Task 1: Bulk queries for the view

**Files:**
- Modify: `src/database/queries/layout_queries.py`
- Test: `tests/task_graph/test_layout_queries.py` (append)

**Interfaces:**
- Produces on `Database`:
  - `load_rows_in_cells(project_id, variant, cells: list[tuple[int,int]]) -> dict[str, LayoutRow]`
  - `load_rows_with_tasks(project_id, variant, task_ids) -> dict[str, tuple[LayoutRow, dict]]` where the dict has the `GraphTaskNode` fields (`id, title, status, priority, is_blocked, profile_id, intelligence_class, assigned_agent_id, branch_name, pr_url, playbook_run_id`).
  - `load_rows_by_prefixes(project_id, variant, prefixes: list[str]) -> dict[str, LayoutRow]` (rows whose `path` starts with any prefix; empty list → `{}`).
  - `load_edges_touching(task_ids) -> list[tuple[str, str, str, str | None]]` as `(task_id, depends_on, dep_type, description)` where either endpoint is in `task_ids`.
  - `load_matching_ids(project_id, variant, *, q: str, status: str) -> set[str]` using `LOWER(title) LIKE %q%` or `id LIKE %q%`, and `status = :status` when given, over tasks that have a row in the variant.
  - `load_all_rows_with_tasks(project_id, variant) -> dict[str, tuple[LayoutRow, dict]]` (for `list` and `locate`).

- [ ] **Step 1: Write the failing tests**

```python
async def test_rows_in_cells_and_prefixes(db):
    for t in ("e", "c", "far"):
        await db.create_task(Task(id=t, project_id="p1", title=t.upper(), description=""))
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container", w=3, h=3),
                           row("c", 0.5, 0.5, "/e/c/", "e", 1), row("far", 40, 40, "/far/")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(41, 41), node_count_delta=None)
    assert set(await db.load_rows_in_cells("p1", "all", [(0, 0)])) == {"e", "c"}
    assert set(await db.load_rows_in_cells("p1", "all", [(5, 5)])) == {"far"}
    assert set(await db.load_rows_by_prefixes("p1", "all", ["/e/"])) == {"e", "c"}
    with_tasks = await db.load_rows_with_tasks("p1", "all", ["c"])
    assert with_tasks["c"][1]["title"] == "C"


async def test_edges_touching_and_matching(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=f"Task {t}", description=""))
    await db.add_dependency("b", "a", description="why")
    await db.add_dependency("c", "b")
    edges = await db.load_edges_touching(["a"])
    assert edges == [("b", "a", "blocks", "why")]
    ws = WriteSet(upserts=[row(t, i, 0, f"/{t}/") for i, t in enumerate("abc")])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 1), node_count_delta=None)
    assert await db.load_matching_ids("p1", "all", q="task b", status="") == {"b"}
    assert await db.load_matching_ids("p1", "all", q="", status="DEFINED") == {"a", "b", "c"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_queries.py -v -k "cells_and_prefixes or touching"`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

Append to `LayoutQueryMixin`:

```python
    _TASK_FIELDS = ("id", "title", "status", "priority", "is_blocked", "profile_id",
                    "intelligence_class", "assigned_agent_id", "branch_name", "pr_url",
                    "playbook_run_id")

    async def load_rows_in_cells(self, project_id, variant, cells_wanted):
        from sqlalchemy import or_, and_, tuple_
        from src.database.tables import task_layout_cells as cells, task_layouts
        if not cells_wanted:
            return {}
        cond = or_(*[and_(cells.c.cell_x == cx, cells.c.cell_y == cy) for cx, cy in cells_wanted])
        async with self._engine.begin() as conn:
            ids = [r[0] for r in (await conn.execute(
                select(cells.c.task_id).distinct().where(
                    cells.c.project_id == project_id, cells.c.variant == variant, cond)
            )).fetchall()]
            if not ids:
                return {}
            res = await conn.execute(select(task_layouts).where(
                task_layouts.c.project_id == project_id, task_layouts.c.variant == variant,
                task_layouts.c.task_id.in_(ids)))
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_rows_with_tasks(self, project_id, variant, task_ids):
        from src.database.tables import task_layouts, tasks
        ids = list(task_ids)
        if not ids:
            return {}
        cols = [getattr(tasks.c, f) for f in self._TASK_FIELDS]
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts, *cols)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(task_layouts.c.project_id == project_id,
                       task_layouts.c.variant == variant, task_layouts.c.task_id.in_(ids)))
            out = {}
            for m in res.mappings():
                task = {f: m[f] for f in self._TASK_FIELDS}
                task["is_blocked"] = bool(task["is_blocked"])
                out[m["task_id"]] = (self._row_from_mapping(m), task)
            return out

    async def load_all_rows_with_tasks(self, project_id, variant):
        from src.database.tables import task_layouts, tasks
        cols = [getattr(tasks.c, f) for f in self._TASK_FIELDS]
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts, *cols)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(task_layouts.c.project_id == project_id, task_layouts.c.variant == variant))
            out = {}
            for m in res.mappings():
                task = {f: m[f] for f in self._TASK_FIELDS}
                task["is_blocked"] = bool(task["is_blocked"])
                out[m["task_id"]] = (self._row_from_mapping(m), task)
            return out

    async def load_rows_by_prefixes(self, project_id, variant, prefixes):
        from sqlalchemy import or_
        from src.database.tables import task_layouts
        if not prefixes:
            return {}
        async with self._engine.begin() as conn:
            res = await conn.execute(select(task_layouts).where(
                task_layouts.c.project_id == project_id, task_layouts.c.variant == variant,
                or_(*[task_layouts.c.path.like(p + "%") for p in prefixes])))
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_edges_touching(self, task_ids):
        from sqlalchemy import or_
        from src.database.tables import task_dependencies as td
        ids = list(task_ids)
        if not ids:
            return []
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(td.c.task_id, td.c.depends_on_task_id, td.c.dep_type, td.c.description)
                .where(or_(td.c.task_id.in_(ids), td.c.depends_on_task_id.in_(ids)))
                .order_by(td.c.task_id, td.c.depends_on_task_id, td.c.dep_type))
            return [tuple(r) for r in res.fetchall()]

    async def load_matching_ids(self, project_id, variant, *, q, status):
        from sqlalchemy import func, or_
        from src.database.tables import task_layouts, tasks
        conds = [task_layouts.c.project_id == project_id, task_layouts.c.variant == variant]
        if q:
            needle = f"%{q.lower()}%"
            conds.append(or_(func.lower(tasks.c.title).like(needle), func.lower(tasks.c.id).like(needle)))
        if status:
            conds.append(tasks.c.status == status)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(*conds))
            return {r[0] for r in res.fetchall()}
```

Large `IN` lists: SQLite caps bound parameters near 32k on older builds and 250k on current ones; PostgreSQL has no practical cap. Chunk `ids` into groups of 900 inside `load_rows_with_tasks`, `load_rows_in_cells`, and `load_edges_touching` with a small `_chunks(seq, 900)` helper at module level so a 1,000-task collapsed epic never trips the limit.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_queries.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/layout_queries.py tests/task_graph/test_layout_queries.py
git commit -m "feat(layout-api): bulk queries for viewport resolution"
```

---

### Task 2: View resolution — visibility and ordering

**Files:**
- Create: `src/task_graph/layout/view.py`
- Test: `tests/task_graph/layout/test_view.py`

**Interfaces:**
- Produces:
  - `ancestors_of(path: str) -> list[str]` (ids from the root child down to the parent, excluding self).
  - `resolve_visible(rows: dict[str, LayoutRow], *, expanded: set[str], max_depth: int | None, root: str | None, forced_expanded: set[str]) -> Visible` where `Visible` has `visible: dict[str, str]` (task_id → wire kind), `collapsed_paths: dict[str, str]` (collapsed container id → its path), `root_path: str | None`.
  - `depth_first_order(rows: dict[str, LayoutRow]) -> list[str]` ordered by each path component's `(rank, order_key)`.

Rules for `resolve_visible`:
1. If `root` is given, only rows whose path starts with `rows[root].path` are considered, and `root` itself is visible and treated as expanded.
2. A row is visible iff every ancestor id (from its path) is in `expanded ∪ forced_expanded ∪ {root}` and, when `max_depth` is set, every ancestor has `depth < max_depth`, and the row's own `depth <= max_depth`. Ancestors not present in `rows` are looked up in `rows` too; callers must include ancestor rows (Task 4 loads them).
3. Wire kind: `stub` stays `stub`; `card` stays `card`; a `container` row is `container` if it is expanded (or forced, or root) and `depth < max_depth` (when set) and `agg_children > 0`; it is `collapsed` if it has `agg_children > 0` and is not expanded or sits at `max_depth`; a container with `agg_children == 0` is `container` (empty container).

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.model import LayoutRow
from src.task_graph.layout.view import ancestors_of, depth_first_order, resolve_visible


def row(tid, path, depth, kind="card", children=0, rank=0, key="U", x=0.0, y=0.0):
    return LayoutRow(task_id=tid, container_id=ancestors_of(path)[-1] if depth else None,
                     path=path, depth=depth, rank=rank, order_key=key, w=1, h=1,
                     rel_x=x, rel_y=y, abs_x=x, abs_y=y, kind=kind, agg_children=children)


ROWS = {
    "e": row("e", "/e/", 0, "container", children=2),
    "p": row("p", "/e/p/", 1, "container", children=1),
    "t": row("t", "/e/p/t/", 2),
    "z": row("z", "/z/", 0),
    "empty": row("empty", "/empty/", 0, "container", children=0),
}


def test_ancestors_of():
    assert ancestors_of("/e/p/t/") == ["e", "p"]
    assert ancestors_of("/e/") == []


def test_default_collapsed_shows_only_top_level():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root=None, forced_expanded=set())
    assert v.visible == {"e": "collapsed", "z": "card", "empty": "container"}
    assert v.collapsed_paths == {"e": "/e/"}


def test_expanding_reveals_one_level():
    v = resolve_visible(ROWS, expanded={"e"}, max_depth=None, root=None, forced_expanded=set())
    assert v.visible["e"] == "container" and v.visible["p"] == "collapsed" and "t" not in v.visible


def test_max_depth_collapses_deeper_containers():
    v = resolve_visible(ROWS, expanded={"e", "p"}, max_depth=1, root=None, forced_expanded=set())
    assert v.visible["p"] == "collapsed" and "t" not in v.visible


def test_root_restricts_and_expands_itself():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root="e", forced_expanded=set())
    assert set(v.visible) == {"e", "p"} and v.visible["e"] == "container"
    assert v.root_path == "/e/"


def test_forced_expanded_acts_like_expanded():
    v = resolve_visible(ROWS, expanded=set(), max_depth=None, root=None, forced_expanded={"e", "p"})
    assert "t" in v.visible


def test_depth_first_order_uses_ordinals():
    rows = {
        "b": row("b", "/b/", 0, rank=0, key="A"),
        "a": row("a", "/a/", 0, rank=0, key="B"),
        "a1": row("a1", "/a/a1/", 1, rank=1, key="U"),
        "a0": row("a0", "/a/a0/", 1, rank=0, key="U"),
    }
    assert depth_first_order(rows) == ["b", "a", "a0", "a1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_view.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""Viewport resolution over persisted layout rows (spec §5.2–§5.5)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from src.task_graph.layout.model import LayoutRow


def ancestors_of(path: str) -> list[str]:
    parts = [p for p in path.split("/") if p]
    return parts[:-1]


@dataclass
class Visible:
    visible: dict[str, str] = field(default_factory=dict)
    collapsed_paths: dict[str, str] = field(default_factory=dict)
    root_path: str | None = None


def resolve_visible(
    rows: dict[str, LayoutRow], *, expanded: set[str], max_depth: int | None,
    root: str | None, forced_expanded: set[str],
) -> Visible:
    out = Visible()
    opened = set(expanded) | set(forced_expanded)
    if root is not None:
        if root not in rows:
            return out
        out.root_path = rows[root].path
        opened.add(root)
    for tid, r in rows.items():
        if out.root_path and not r.path.startswith(out.root_path):
            continue
        anc = ancestors_of(r.path)
        if out.root_path:
            anc = anc[anc.index(root) + 1:] if root in anc else anc
        ok = True
        for a in anc:
            ar = rows.get(a)
            if a not in opened or (max_depth is not None and (ar is None or ar.depth >= max_depth)):
                ok = False
                break
        if not ok or (max_depth is not None and r.depth > max_depth and tid != root):
            continue
        if r.kind != "container":
            out.visible[tid] = r.kind
            continue
        if r.agg_children == 0:
            out.visible[tid] = "container"
        elif tid in opened and (max_depth is None or r.depth < max_depth or tid == root):
            out.visible[tid] = "container"
        else:
            out.visible[tid] = "collapsed"
            out.collapsed_paths[tid] = r.path
    return out


def depth_first_order(rows: dict[str, LayoutRow]) -> list[str]:
    def key(r: LayoutRow) -> tuple:
        parts = [p for p in r.path.split("/") if p]
        return tuple((rows[p].rank, rows[p].order_key) if p in rows else (0, "") for p in parts)
    return sorted(rows, key=lambda t: key(rows[t]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_view.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/view.py tests/task_graph/layout/test_view.py
git commit -m "feat(layout-api): visibility resolution and depth-first ordering"
```

---

### Task 3: View resolution — edges, stubs, workers, filter

**Files:**
- Modify: `src/task_graph/layout/view.py`
- Test: `tests/task_graph/layout/test_view.py` (append)

**Interfaces:**
- Produces:
  - `remap_edges(edges: list[tuple[str,str,str,str|None]], visible: dict[str,str], hidden_owner: dict[str,str]) -> tuple[list[dict], set[str]]` returning wire edges `{"from", "to", "dep_type", "description", "count"}` (already remapped and deduplicated; `parent-child`, `related`, `duplicates`, `supersedes` dropped; self-loops after remap dropped) and the set of endpoint ids that are neither visible nor owned (stub candidates). `hidden_owner` maps a hidden task id → the visible collapsed container id that owns it.
  - `owner_map(rows_in_collapsed: dict[str, LayoutRow], collapsed_paths: dict[str, str]) -> dict[str, str]` picking the longest matching collapsed path.
  - `cap_stubs(edges: list[dict], stub_rows: dict[str, LayoutRow], visible: set[str], limit: int = 8) -> tuple[list[dict], list[dict], list[dict]]` returning `(kept_edges, stubs, more)`.
  - `dock_workers(agents: list[dict], visible: set[str], hidden_owner: dict[str,str]) -> list[dict]` returning `{"agent": agent, "docked_at": id, "in_collapsed": bool}`.
  - `forced_expansion_for(matches: set[str], rows: dict[str, LayoutRow]) -> set[str]` = all ancestor ids of matches.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.view import cap_stubs, dock_workers, forced_expansion_for, owner_map, remap_edges


def test_owner_map_longest_prefix():
    rows = {"t": row("t", "/e/p/t/", 2), "p": row("p", "/e/p/", 1, "container", 1)}
    assert owner_map(rows, {"e": "/e/", "p": "/e/p/"}) == {"t": "p", "p": "p"}


def test_remap_dedupes_and_drops_hierarchy_edges():
    visible = {"e": "collapsed", "z": "card"}
    owner = {"t1": "e", "t2": "e"}
    edges = [("z", "t1", "blocks", None), ("z", "t2", "blocks", None),
             ("t1", "e", "parent-child", None), ("t1", "t2", "blocks", None)]
    wire, orphans = remap_edges(edges, visible, owner)
    assert wire == [{"from": "z", "to": "e", "dep_type": "blocks", "description": None, "count": 2}]
    assert orphans == set()


def test_remap_reports_orphans_for_stubs():
    wire, orphans = remap_edges([("z", "far", "blocks", None)], {"z": "card"}, {})
    assert orphans == {"far"} and wire[0]["to"] == "far"


def test_cap_stubs_keeps_eight_then_summarizes():
    hub = {"hub": "card"}
    edges = [{"from": f"d{i}", "to": "hub", "dep_type": "blocks", "description": None, "count": 1} for i in range(12)]
    stub_rows = {f"d{i}": row(f"d{i}", f"/d{i}/", 0, x=float(i)) for i in range(12)}
    kept, stubs, more = cap_stubs(edges, stub_rows, set(hub), limit=8)
    assert len(kept) == 8 and len(stubs) == 8
    assert more == [{"node_id": "hub", "direction": "in", "more": 4}]


def test_dock_workers_on_visible_ancestor():
    agents = [{"id": "a1", "current_task_id": "t"}, {"id": "a2", "current_task_id": "z"}, {"id": "a3", "current_task_id": None}]
    docked = dock_workers(agents, {"e", "z"}, {"t": "e"})
    assert [(d["docked_at"], d["in_collapsed"]) for d in docked] == [("e", True), ("z", False)]


def test_forced_expansion_is_all_ancestors():
    assert forced_expansion_for({"t"}, ROWS) == {"e", "p"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_view.py -v`
Expected: the six new tests FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Append to `view.py`:

```python
DRAWN_TYPES = frozenset({"blocks", "waits-for", "conditional-blocks", "discovered-from"})


def owner_map(rows_in_collapsed: dict[str, LayoutRow], collapsed_paths: dict[str, str]) -> dict[str, str]:
    by_len = sorted(collapsed_paths.items(), key=lambda kv: -len(kv[1]))
    out: dict[str, str] = {}
    for tid, r in rows_in_collapsed.items():
        for cid, p in by_len:
            if r.path.startswith(p):
                out[tid] = cid
                break
    return out


def remap_edges(edges, visible, hidden_owner):
    agg: dict[tuple[str, str, str], dict] = {}
    orphans: set[str] = set()

    def target(x: str) -> str:
        if x in visible:
            return x
        if x in hidden_owner:
            return hidden_owner[x]
        orphans.add(x)
        return x

    for dep, blocker, typ, desc in edges:
        if typ not in DRAWN_TYPES:
            continue
        f, t = target(dep), target(blocker)
        if f == t:
            continue
        key = (f, t, typ)
        if key in agg:
            agg[key]["count"] += 1
            if agg[key]["description"] is None and desc:
                agg[key]["description"] = desc
        else:
            agg[key] = {"from": f, "to": t, "dep_type": typ, "description": desc, "count": 1}
    wire = sorted(agg.values(), key=lambda e: (e["from"], e["to"], e["dep_type"]))
    # An orphan that never survived (self-loop) is not a stub candidate.
    used = {e["from"] for e in wire} | {e["to"] for e in wire}
    return wire, {o for o in orphans if o in used}


def cap_stubs(edges, stub_rows, visible, limit=8):
    per_node_dir: dict[tuple[str, str], int] = defaultdict(int)
    kept: list[dict] = []
    stubs: dict[str, dict] = {}
    more: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges:
        f, t = e["from"], e["to"]
        far = None
        anchor = None
        direction = None
        if f in visible and t not in visible:
            far, anchor, direction = t, f, "out"
        elif t in visible and f not in visible:
            far, anchor, direction = f, t, "in"
        if far is None:
            kept.append(e)
            continue
        if far not in stub_rows:
            continue  # far endpoint has no row in this variant: drop
        if per_node_dir[(anchor, direction)] >= limit:
            more[(anchor, direction)] += 1
            continue
        per_node_dir[(anchor, direction)] += 1
        kept.append(e)
        r = stub_rows[far]
        stubs.setdefault(far, {"id": far, "x": r.abs_x, "y": r.abs_y, "w": r.w, "h": r.h})
    more_list = [{"node_id": n, "direction": d, "more": c} for (n, d), c in sorted(more.items())]
    return kept, list(stubs.values()), more_list


def dock_workers(agents, visible, hidden_owner):
    out = []
    for a in agents:
        cur = a.get("current_task_id")
        if not cur:
            continue
        if cur in visible:
            out.append({"agent": a, "docked_at": cur, "in_collapsed": False})
        elif cur in hidden_owner:
            out.append({"agent": a, "docked_at": hidden_owner[cur], "in_collapsed": True})
    return out


def forced_expansion_for(matches, rows):
    out: set[str] = set()
    for m in matches:
        r = rows.get(m)
        if r:
            out.update(ancestors_of(r.path))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_view.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/view.py tests/task_graph/layout/test_view.py
git commit -m "feat(layout-api): edge remapping, stub caps, worker docking, filter expansion"
```

---

### Task 4: Models and router — `extent`, `tiles`

**Files:**
- Create: `src/api/models/graph_layout.py`
- Create: `src/api/graph_layout.py`
- Modify: `src/api/app.py` (register `graph_layout_router` next to `graph_router`)
- Modify: `src/api/models/__init__.py` (export the new models if that module enumerates response models for the client generator; check with `grep -n "graph" src/api/models/__init__.py`)
- Test: `tests/test_api_graph_layout.py`

**Interfaces:**
- Produces: `build_graph_layout_router(*, db, command_handler=None) -> APIRouter` and module-level `router`.
- Routes in this task: `GET /api/projects/{project_id}/graph/extent`, `POST /api/projects/{project_id}/graph/tiles`.

- [ ] **Step 1: Write the failing tests**

```python
"""Layout endpoints (spatial-layout design §5)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.graph_layout import build_graph_layout_router
from src.database import Database
from src.models import Agent, AgentState, Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "gl.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_graph_layout_router(db=db))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return _make


async def seed(db):
    """epic e{c0,c1,pkg{g0,g1}}, root card z blocked by c0, hub with 10 dependents."""
    async def mk(tid, parent=None, status=TaskStatus.DEFINED):
        await db.create_task(Task(id=tid, project_id="p1", title=f"Title {tid}", description="", status=status))
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)
    await mk("e"); await mk("c0", "e"); await mk("c1", "e", TaskStatus.COMPLETED)
    await mk("pkg", "e"); await mk("g0", "pkg"); await mk("g1", "pkg")
    await mk("z"); await db.add_dependency("z", "c0")
    await mk("hub")
    for i in range(10):
        await mk(f"d{i}"); await db.add_dependency(f"d{i}", "hub")
    await db.create_agent(Agent(id="a1", name="bot", profile_id="p", state=AgentState.BUSY, current_task_id="g0"))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all"); await drv.full_layout("p1", "active")


ALL = {"variant": "all", "rect": {"x0": -1, "y0": -1, "x1": 60, "y1": 60}, "expanded": []}


async def test_extent_pending_then_ready(db, client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.status_code == 202 and r.json()["status"] == "layout_pending"
        assert (await db.next_layout_job())["kind"] == "backfill"
        await LayoutDriver(db).full_layout("p1", "all")
        r = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert r.status_code == 200 and r.json()["layout_version"] == 1 and r.json()["node_count"] == 0
        assert (await ac.get("/api/projects/nope/graph/extent?variant=all")).status_code == 404


async def test_tiles_default_collapsed(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json=ALL)
    assert r.status_code == 200
    body = r.json()
    kinds = {n["id"]: n["kind"] for n in body["nodes"]}
    assert kinds["e"] == "collapsed" and kinds["z"] == "card" and "c0" not in kinds
    e = next(n for n in body["nodes"] if n["id"] == "e")
    assert e["agg_children"] == 3 and e["agg_descendants"] == 5 and e["agg_completed"] == 1
    assert e["title"] == "Title e"
    # z blocks-on c0 remaps to e, arrow drawn e -> z on the wire as from=z,to=e
    assert {"from": "z", "to": "e", "dep_type": "blocks", "description": None, "count": 1} in body["edges"]
    assert body["workers"] == [{"agent_id": "a1", "name": "bot", "docked_at": "e", "in_collapsed": True}]
    assert body["layout_version"] == 1


async def test_tiles_expanded_and_rect_culling(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "expanded": ["e"]})
        ids = {n["id"] for n in r.json()["nodes"]}
        assert {"e", "c0", "c1", "pkg"} <= ids and "g0" not in ids
        e = next(n for n in r.json()["nodes"] if n["id"] == "e")
        # a rect entirely to the right of everything returns nothing
        r2 = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": {"x0": 500, "y0": 500, "x1": 510, "y1": 510}})
        assert r2.json()["nodes"] == []
        # a rect covering only e's box still returns e (box intersection, not origin)
        r3 = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": {
            "x0": e["x"] + e["w"] - 0.5, "y0": e["y"] + e["h"] - 0.5, "x1": e["x"] + e["w"] + 1, "y1": e["y"] + e["h"] + 1}})
        assert "e" in {n["id"] for n in r3.json()["nodes"]}


async def test_tiles_stub_cap_and_more_marker(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        hub = next(n for n in (await ac.post("/api/projects/p1/graph/tiles", json=ALL)).json()["nodes"] if n["id"] == "hub")
        rect = {"x0": hub["x"], "y0": hub["y"], "x1": hub["x"] + 0.5, "y1": hub["y"] + 0.5}
        # Make sure only hub is inside: shrink to its own cell region and filter by id afterwards.
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": rect})
    body = r.json()
    visible = {n["id"] for n in body["nodes"]}
    if all(f"d{i}" in visible for i in range(10)):
        pytest.skip("layout put every dependent in hub's cell; cap not exercisable here")
    ins = [e for e in body["edges"] if e["to"] == "hub"]
    assert len(ins) <= 8 + sum(1 for i in range(10) if f"d{i}" in visible)
    assert len(body["stubs"]) <= 8


async def test_tiles_validation(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        bad_rect = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": {"x0": 5, "y0": 0, "x1": 1, "y1": 1}})
        too_big = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "rect": {"x0": 0, "y0": 0, "x1": 100, "y1": 1}})
        bad_variant = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "variant": "x"})
        too_many = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "expanded": [str(i) for i in range(2001)]})
    assert {bad_rect.status_code, too_big.status_code, bad_variant.status_code, too_many.status_code} == {400}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api_graph_layout.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement models**

`src/api/models/graph_layout.py`:

```python
"""Response/request models for the layout endpoints (spatial-layout design §5)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.api.models.graph import GraphGate, GraphTaskNode


class LayoutRect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class TilesRequest(BaseModel):
    variant: str = "active"
    rect: LayoutRect
    expanded: list[str] = []
    root: str | None = None
    max_depth: int | None = None
    q: str = ""
    status: str = ""


class ListRequest(BaseModel):
    variant: str = "active"
    expanded: list[str] = []
    q: str = ""
    status: str = ""
    cursor: str | None = None
    limit: int = 50


class LayoutJob(BaseModel):
    id: str
    project_id: str
    variant: str
    kind: str
    status: str
    requested_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class ExtentResponse(BaseModel):
    layout_version: int
    extent_w: float
    extent_h: float
    node_count: int
    job: LayoutJob | None = None


class LayoutNode(GraphTaskNode):
    x: float
    y: float
    w: float
    h: float
    depth: int
    container_id: str | None = None
    kind: str
    context_only: bool = False
    agg_children: int = 0
    agg_descendants: int = 0
    agg_completed: int = 0
    agg_running: int = 0
    agg_blocked: int = 0
    agg_active: int = 0


class LayoutEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_task_id: str = Field(alias="from")
    to_task_id: str = Field(alias="to")
    dep_type: str
    description: str | None = None
    count: int = 1


class LayoutStub(BaseModel):
    id: str
    project_id: str
    x: float
    y: float
    w: float
    h: float
    title: str = ""


class StubOverflow(BaseModel):
    node_id: str
    direction: str
    more: int


class LayoutWorker(BaseModel):
    agent_id: str
    name: str
    docked_at: str
    in_collapsed: bool


class TilesResponse(BaseModel):
    nodes: list[LayoutNode] = []
    edges: list[LayoutEdge] = []
    stubs: list[LayoutStub] = []
    stub_overflow: list[StubOverflow] = []
    workers: list[LayoutWorker] = []
    gates: list[GraphGate] = []
    layout_version: int


class ListResponse(BaseModel):
    nodes: list[LayoutNode] = []
    next_cursor: str | None = None
    layout_version: int


class AncestorRef(BaseModel):
    id: str
    title: str
    x: float
    y: float
    w: float
    h: float


class NodeResponse(BaseModel):
    node: LayoutNode
    ancestors: list[AncestorRef] = []
    layout_version: int


class LocateHit(BaseModel):
    id: str
    x: float
    y: float
    w: float
    h: float
    container_id: str | None = None


class LocateResponse(BaseModel):
    hits: list[LocateHit] = []
    truncated: bool = False


class TidyRequest(BaseModel):
    variant: str | None = None


class TidyResponse(BaseModel):
    jobs: list[LayoutJob] = []
```

- [ ] **Step 4: Implement the router (extent + tiles)**

`src/api/graph_layout.py`:

```python
"""Viewport-bounded layout endpoints (spatial-layout design §5)."""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.api.models.graph import GraphGate
from src.api.models.graph_layout import (
    ExtentResponse, LayoutEdge, LayoutJob, LayoutNode, LayoutStub, LayoutWorker,
    ListRequest, ListResponse, LocateResponse, NodeResponse, StubOverflow,
    TidyRequest, TidyResponse, TilesRequest, TilesResponse,
)
from src.task_graph.layout.constants import CELL_SIZE, FINISHED_STATUSES, VARIANTS
from src.task_graph.layout.view import (
    ancestors_of, cap_stubs, depth_first_order, dock_workers, forced_expansion_for,
    owner_map, remap_edges, resolve_visible,
)

RECT_CAP = 64.0
EXPANDED_CAP = 2000
LIST_CAP = 200
LOCATE_CAP = 200

__all__ = ["build_graph_layout_router", "router"]


def _cells_for_rect(x0, y0, x1, y1):
    cx0, cy0 = math.floor(x0 / CELL_SIZE), math.floor(y0 / CELL_SIZE)
    cx1, cy1 = math.ceil(x1 / CELL_SIZE) - 1, math.ceil(y1 / CELL_SIZE) - 1
    return [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]


def _intersects(r, x0, y0, x1, y1) -> bool:
    return r.abs_x < x1 and r.abs_x + r.w > x0 and r.abs_y < y1 and r.abs_y + r.h > y0


def _node(row, task, kind, context_only=False) -> LayoutNode:
    return LayoutNode(
        **task, x=row.abs_x, y=row.abs_y, w=row.w, h=row.h, depth=row.depth,
        container_id=row.container_id, kind=kind, context_only=context_only,
        agg_children=row.agg_children, agg_descendants=row.agg_descendants,
        agg_completed=row.agg_completed, agg_running=row.agg_running,
        agg_blocked=row.agg_blocked, agg_active=row.agg_active,
    )


def build_graph_layout_router(*, db, command_handler=None) -> APIRouter:
    router = APIRouter()

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

    @router.get("/api/projects/{project_id}/graph/extent", response_model=ExtentResponse,
                responses={202: {"description": "layout pending"}})
    async def get_extent(project_id: str, variant: str = "active"):
        await _project_or_404(project_id)
        variant = _variant(variant)
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        job = None
        for j in await db.list_layout_jobs(project_id, variant, statuses=("queued", "running")):
            job = LayoutJob(**j)
        return ExtentResponse(layout_version=meta["layout_version"], extent_w=meta["extent_w"],
                              extent_h=meta["extent_h"], node_count=meta["node_count"], job=job)

    async def _resolve(project_id: str, req: TilesRequest):
        """Shared resolution for tiles: returns (visible rows+tasks, kinds, edges, stubs, overflow, workers, gates, version, context_only ids)."""
        variant = _variant(req.variant)
        if req.root is not None or (req.status and req.status in FINISHED_STATUSES):
            variant = "all"
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return None
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

        # Candidate rows: everything in the rect's cells (or the whole subtree in focus).
        if req.root is not None:
            root_rows = await db.load_layout_rows(project_id, variant, [req.root])
            if req.root not in root_rows:
                raise HTTPException(status_code=404, detail=f"No layout node '{req.root}'")
            cand = await db.load_rows_by_prefixes(project_id, variant, [root_rows[req.root].path])
        else:
            cand = await db.load_rows_in_cells(project_id, variant, _cells_for_rect(rect.x0, rect.y0, rect.x1, rect.y1))
            cand = {t: r for t, r in cand.items() if _intersects(r, rect.x0, rect.y0, rect.x1, rect.y1)}
        # Ancestors of candidates are needed to decide visibility.
        anc_ids = {a for r in cand.values() for a in ancestors_of(r.path)} - set(cand)
        if anc_ids:
            cand.update(await db.load_layout_rows(project_id, variant, list(anc_ids)))

        # Filtering: matches anywhere force their ancestors open; non-matches vanish.
        matches: set[str] | None = None
        forced: set[str] = set()
        if req.q.strip() or req.status:
            matches = await db.load_matching_ids(project_id, variant, q=req.q.strip(), status=req.status)
            match_rows = await db.load_layout_rows(project_id, variant, list(matches))
            forced = forced_expansion_for(matches, match_rows)
            if forced - set(cand):
                cand.update(await db.load_layout_rows(project_id, variant, list(forced - set(cand))))

        vis = resolve_visible(cand, expanded=set(req.expanded), max_depth=req.max_depth,
                              root=req.root, forced_expanded=forced)
        # rect membership again, now over resolved rows (ancestors may lie outside).
        if req.root is None:
            for tid in list(vis.visible):
                r = cand[tid]
                if not _intersects(r, rect.x0, rect.y0, rect.x1, rect.y1):
                    del vis.visible[tid]
                    vis.collapsed_paths.pop(tid, None)
        context_only: set[str] = set()
        if matches is not None:
            for tid in list(vis.visible):
                if tid in matches:
                    continue
                if tid in forced:
                    context_only.add(tid)
                else:
                    del vis.visible[tid]
                    vis.collapsed_paths.pop(tid, None)

        # Edges: touching visible ids or anything inside a visible collapsed subtree.
        hidden_rows = await db.load_rows_by_prefixes(project_id, variant, list(vis.collapsed_paths.values()))
        hidden_owner = owner_map(hidden_rows, vis.collapsed_paths)
        touching = set(vis.visible) | set(hidden_owner)
        raw_edges = await db.load_edges_touching(touching)
        wire, orphans = remap_edges(raw_edges, vis.visible, hidden_owner)
        stub_rows = await db.load_layout_rows(project_id, variant, list(orphans))
        kept, stubs, more = cap_stubs(wire, stub_rows, set(vis.visible))
        stub_titles = await db.load_rows_with_tasks(project_id, variant, [s["id"] for s in stubs])
        stubs_out = [LayoutStub(project_id=project_id, title=stub_titles[s["id"]][1]["title"] if s["id"] in stub_titles else "", **s) for s in stubs]

        # Workers and gates.
        agents = [{"id": a.id, "name": a.name, "current_task_id": a.current_task_id}
                  for a in await db.list_agents()]
        docked = dock_workers(agents, set(vis.visible), hidden_owner)
        workers = [LayoutWorker(agent_id=d["agent"]["id"], name=d["agent"]["name"],
                                docked_at=d["docked_at"], in_collapsed=d["in_collapsed"]) for d in docked]
        gates_out: list[GraphGate] = []
        for g in await db.list_gates(project_id=project_id):
            waiters = await db.get_gate_waiters(g["id"])
            ids = [w for w in waiters if w in vis.visible]
            if ids:
                gates_out.append(GraphGate(id=g["id"], gate_type=g["gate_type"], status=g["status"], task_ids=ids))

        with_tasks = await db.load_rows_with_tasks(project_id, variant, list(vis.visible))
        nodes = [_node(with_tasks[t][0], with_tasks[t][1], kind, t in context_only)
                 for t, kind in vis.visible.items() if t in with_tasks]
        nodes.sort(key=lambda n: (n.depth, n.y, n.x))
        edges = [LayoutEdge(**e) for e in kept]
        return TilesResponse(nodes=nodes, edges=edges, stubs=stubs_out,
                             stub_overflow=[StubOverflow(**m) for m in more], workers=workers,
                             gates=gates_out, layout_version=meta["layout_version"])

    @router.post("/api/projects/{project_id}/graph/tiles", response_model=TilesResponse,
                 responses={202: {"description": "layout pending"}})
    async def post_tiles(project_id: str, req: TilesRequest):
        await _project_or_404(project_id)
        res = await _resolve(project_id, req)
        if res is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        return res

    return router


def _build_default_router() -> APIRouter:
    from src.api import dependencies as deps

    router = APIRouter()

    def _inner():
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        return build_graph_layout_router(db=orch.db, command_handler=orch.command_handler)

    async def _call(path: str, **kwargs):
        for route in _inner().routes:
            if getattr(route, "path", None) == path:
                return await route.endpoint(**kwargs)
        raise HTTPException(status_code=500, detail="graph layout router misconfigured")

    @router.get("/api/projects/{project_id}/graph/extent", response_model=ExtentResponse,
                responses={202: {"description": "layout pending"}})
    async def get_extent(project_id: str, variant: str = "active"):
        return await _call("/api/projects/{project_id}/graph/extent", project_id=project_id, variant=variant)

    @router.post("/api/projects/{project_id}/graph/tiles", response_model=TilesResponse,
                 responses={202: {"description": "layout pending"}})
    async def post_tiles(project_id: str, req: TilesRequest):
        return await _call("/api/projects/{project_id}/graph/tiles", project_id=project_id, req=req)

    return router


router = _build_default_router()
```

Add to `LayoutQueryMixin`:

```python
    async def list_layout_jobs(self, project_id, variant, *, statuses):
        async with self._engine.begin() as conn:
            res = await conn.execute(select(layout_jobs).where(
                layout_jobs.c.project_id == project_id, layout_jobs.c.variant == variant,
                layout_jobs.c.status.in_(list(statuses))).order_by(layout_jobs.c.requested_at))
            return [dict(m) for m in res.mappings()]
```

Register in `src/api/app.py` next to `graph_router`:

```python
from src.api.graph_layout import router as graph_layout_router
...
    app.include_router(graph_layout_router)
```

The gates loop calls `get_gate_waiters` per gate. That is the one non-bulk call here and matches the current endpoint; gates per project are few. Leave it and note it in the docstring.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api_graph_layout.py -v`
Expected: PASS (5 tests; the stub-cap test may skip on SQLite depending on layout, which is acceptable because Task 3 covers the cap in isolation).

- [ ] **Step 6: Commit**

```bash
git add src/api/models/graph_layout.py src/api/graph_layout.py src/api/app.py src/database/queries/layout_queries.py tests/test_api_graph_layout.py
git commit -m "feat(layout-api): extent and tiles endpoints"
```

---

### Task 5: Focus, level of detail, and filter semantics on `tiles`

**Files:**
- Test: `tests/test_api_graph_layout.py` (append)
- Modify: `src/api/graph_layout.py` only if a test exposes a defect

- [ ] **Step 1: Write the tests**

```python
async def test_tiles_root_focus_forces_all_and_expands_root(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={"variant": "active", "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "expanded": [], "root": "e"})
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"e", "c0", "c1", "pkg"}  # c1 is COMPLETED but variant forced to all
    assert next(n for n in body["nodes"] if n["id"] == "e")["kind"] == "container"
    assert "z" not in ids  # outside the subtree
    assert any(s["id"] == "z" for s in body["stubs"])  # z depends on c0: stub at the edge


async def test_tiles_max_depth(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "expanded": ["e", "pkg"], "max_depth": 1})
    kinds = {n["id"]: n["kind"] for n in r.json()["nodes"]}
    assert kinds["e"] == "container" and kinds["pkg"] == "collapsed" and "g0" not in kinds


async def test_tiles_filter_hides_nonmatches_and_reveals_path(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "q": "g1"})
    nodes = {n["id"]: n for n in r.json()["nodes"]}
    assert set(nodes) == {"e", "pkg", "g1"}
    assert nodes["e"]["context_only"] and nodes["pkg"]["context_only"] and not nodes["g1"]["context_only"]
    assert nodes["e"]["kind"] == "container" and nodes["pkg"]["kind"] == "container"


async def test_tiles_finished_status_filter_forces_all(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tiles", json={**ALL, "variant": "active", "status": "COMPLETED"})
    assert {n["id"] for n in r.json()["nodes"]} == {"e", "c1"}
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_api_graph_layout.py -v`
Expected: PASS. If `test_tiles_root_focus...` fails on the stub for `z`: `cap_stubs` drops far endpoints without a row in the variant; `z` has a row in `all`, so confirm `_resolve` loads `stub_rows` from the forced `all` variant, not `req.variant`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_api_graph_layout.py src/api/graph_layout.py
git commit -m "test(layout-api): focus, level of detail, and filter semantics"
```

---

### Task 6: `list`, `node`, `locate`

**Files:**
- Modify: `src/api/graph_layout.py` (both the factory and the default router)
- Test: `tests/test_api_graph_layout.py` (append)

**Interfaces:**
- `POST /api/projects/{id}/graph/list` → `ListResponse`; cursor is the base64 of the offset integer.
- `GET /api/projects/{id}/graph/node/{task_id}?variant=` → `NodeResponse`.
- `GET /api/projects/{id}/graph/locate?variant=&q=&status=&limit=` → `LocateResponse`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_list_paginates_in_layout_order(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r1 = await ac.post("/api/projects/p1/graph/list", json={"variant": "all", "expanded": ["e"], "limit": 3})
        b1 = r1.json()
        assert len(b1["nodes"]) == 3 and b1["next_cursor"]
        r2 = await ac.post("/api/projects/p1/graph/list", json={"variant": "all", "expanded": ["e"], "limit": 3, "cursor": b1["next_cursor"]})
        b2 = r2.json()
        ids = [n["id"] for n in b1["nodes"] + b2["nodes"]]
        assert len(ids) == len(set(ids))
        # children follow their parent
        assert ids.index("e") < ids.index("c0") and ids.index("e") < ids.index("pkg")
        too_big = await ac.post("/api/projects/p1/graph/list", json={"variant": "all", "limit": 500})
        assert too_big.status_code == 400


async def test_node_returns_box_and_ancestors(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/node/g0?variant=all")
    body = r.json()
    assert body["node"]["id"] == "g0" and body["node"]["depth"] == 2
    assert [a["id"] for a in body["ancestors"]] == ["e", "pkg"]
    assert body["ancestors"][0]["title"] == "Title e"
    async with client_factory() as ac:
        assert (await ac.get("/api/projects/p1/graph/node/nope?variant=all")).status_code == 404


async def test_locate_returns_positions_capped(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph/locate?variant=all&q=title d&limit=3")
    body = r.json()
    assert len(body["hits"]) == 3 and body["truncated"] is True
    assert all({"id", "x", "y", "w", "h"} <= set(h) for h in body["hits"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api_graph_layout.py -v -k "list_paginates or node_returns or locate"`
Expected: FAIL with 404 / 405.

- [ ] **Step 3: Implement**

Inside `build_graph_layout_router`, after `post_tiles`:

```python
    @router.post("/api/projects/{project_id}/graph/list", response_model=ListResponse,
                 responses={202: {"description": "layout pending"}})
    async def post_list(project_id: str, req: ListRequest):
        import base64
        await _project_or_404(project_id)
        variant = _variant(req.variant)
        if req.status in FINISHED_STATUSES:
            variant = "all"
        if not (1 <= req.limit <= LIST_CAP):
            raise HTTPException(status_code=400, detail=f"limit must be 1..{LIST_CAP}")
        if len(req.expanded) > EXPANDED_CAP:
            raise HTTPException(status_code=400, detail=f"expanded exceeds {EXPANDED_CAP}")
        meta = await _meta_or_pending(project_id, variant)
        if meta is None:
            return JSONResponse(status_code=202, content={"status": "layout_pending"})
        try:
            offset = int(base64.urlsafe_b64decode(req.cursor.encode()).decode()) if req.cursor else 0
        except Exception:
            raise HTTPException(status_code=400, detail="bad cursor")
        all_rows = await db.load_all_rows_with_tasks(project_id, variant)
        rows = {t: rt[0] for t, rt in all_rows.items()}
        matches = None
        forced: set[str] = set()
        if req.q.strip() or req.status:
            matches = await db.load_matching_ids(project_id, variant, q=req.q.strip(), status=req.status)
            forced = forced_expansion_for(matches, rows)
        vis = resolve_visible(rows, expanded=set(req.expanded), max_depth=None, root=None, forced_expanded=forced)
        ordered = [t for t in depth_first_order({t: rows[t] for t in vis.visible})
                   if matches is None or t in matches or t in forced]
        page = ordered[offset: offset + req.limit]
        nodes = [_node(rows[t], all_rows[t][1], vis.visible[t], matches is not None and t not in matches) for t in page]
        nxt = None
        if offset + req.limit < len(ordered):
            nxt = base64.urlsafe_b64encode(str(offset + req.limit).encode()).decode()
        return ListResponse(nodes=nodes, next_cursor=nxt, layout_version=meta["layout_version"])

    @router.get("/api/projects/{project_id}/graph/node/{task_id}", response_model=NodeResponse)
    async def get_node(project_id: str, task_id: str, variant: str = "all"):
        await _project_or_404(project_id)
        variant = _variant(variant)
        meta = await db.get_layout_meta(project_id, variant)
        if meta is None:
            raise HTTPException(status_code=404, detail="no layout")
        rows = await db.load_rows_with_tasks(project_id, variant, [task_id])
        if task_id not in rows:
            raise HTTPException(status_code=404, detail=f"No layout node '{task_id}'")
        row, task = rows[task_id]
        anc_ids = ancestors_of(row.path)
        anc = await db.load_rows_with_tasks(project_id, variant, anc_ids)
        ancestors = [AncestorRef(id=a, title=anc[a][1]["title"], x=anc[a][0].abs_x, y=anc[a][0].abs_y,
                                 w=anc[a][0].w, h=anc[a][0].h) for a in anc_ids if a in anc]
        kind = "container" if row.kind == "container" else row.kind
        return NodeResponse(node=_node(row, task, kind), ancestors=ancestors, layout_version=meta["layout_version"])

    @router.get("/api/projects/{project_id}/graph/locate", response_model=LocateResponse)
    async def get_locate(project_id: str, variant: str = "active", q: str = "", status: str = "", limit: int = LOCATE_CAP):
        await _project_or_404(project_id)
        variant = _variant(variant)
        if status in FINISHED_STATUSES:
            variant = "all"
        limit = max(1, min(limit, LOCATE_CAP))
        ids = await db.load_matching_ids(project_id, variant, q=q.strip(), status=status)
        rows = await db.load_layout_rows(project_id, variant, sorted(ids))
        ordered = depth_first_order(rows)
        hits = [LocateHit(id=t, x=rows[t].abs_x, y=rows[t].abs_y, w=rows[t].w, h=rows[t].h,
                          container_id=rows[t].container_id) for t in ordered[:limit]]
        return LocateResponse(hits=hits, truncated=len(ordered) > limit)
```

Import `AncestorRef`, `LocateHit` at the top. Mirror the three routes in `_build_default_router` with `_call(...)` exactly as done for `extent` and `tiles`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api_graph_layout.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/graph_layout.py tests/test_api_graph_layout.py
git commit -m "feat(layout-api): list, node, and locate endpoints"
```

---

### Task 7: `tidy` and `jobs`

**Files:**
- Modify: `src/api/graph_layout.py`
- Test: `tests/test_api_graph_layout.py` (append)

**Interfaces:**
- `POST /api/projects/{id}/graph/tidy` body `{variant?}` → `TidyResponse`; delegates to `command_handler.execute("graph_tidy", {...})` when a handler is available, else enqueues directly through `db.enqueue_layout_job` (test path).
- `GET /api/projects/{id}/graph/jobs/{job_id}` → `LayoutJob`.

- [ ] **Step 1: Write the failing test**

```python
async def test_tidy_enqueues_and_jobs_reports(db, client_factory):
    await seed(db)
    async with client_factory() as ac:
        r = await ac.post("/api/projects/p1/graph/tidy", json={})
        assert r.status_code == 200
        jobs = r.json()["jobs"]
        assert {j["variant"] for j in jobs} == {"all", "active"} and all(j["status"] == "queued" for j in jobs)
        again = await ac.post("/api/projects/p1/graph/tidy", json={"variant": "all"})
        assert again.json()["jobs"][0]["id"] == next(j["id"] for j in jobs if j["variant"] == "all")
        j = await ac.get(f"/api/projects/p1/graph/jobs/{jobs[0]['id']}")
        assert j.status_code == 200 and j.json()["kind"] == "tidy"
        ext = await ac.get("/api/projects/p1/graph/extent?variant=all")
        assert ext.json()["job"]["status"] == "queued"
        assert (await ac.get("/api/projects/p1/graph/jobs/nope")).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_graph_layout.py -v -k tidy`
Expected: FAIL with 404/405.

- [ ] **Step 3: Implement**

```python
    @router.post("/api/projects/{project_id}/graph/tidy", response_model=TidyResponse)
    async def post_tidy(project_id: str, req: TidyRequest):
        await _project_or_404(project_id)
        if req.variant is not None:
            _variant(req.variant)
        if command_handler is not None:
            res = await command_handler.execute("graph_tidy", {"project_id": project_id, **({"variant": req.variant} if req.variant else {})})
            if not res.get("success"):
                raise HTTPException(status_code=400, detail=res.get("error", "tidy failed"))
            jobs = res["jobs"]
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
```

Mirror both in `_build_default_router`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api_graph_layout.py tests/test_api_graph.py tests/test_api_client_contract.py -n auto -q`
Expected: PASS. If the client-contract test enumerates routers and needs the new response models registered, add them to `src/api/models/__init__.py`'s export list.

- [ ] **Step 5: Commit**

```bash
git add src/api/graph_layout.py tests/test_api_graph_layout.py src/api/models/__init__.py
git commit -m "feat(layout-api): tidy and jobs endpoints"
```

---

### Task 8: Regenerate clients and perf test

**Files:**
- Modify: `openapi.json`, `packages/aq-ts-client/src/**`, `packages/aq-client/**` (generated)
- Create: `tests/perf/test_layout_api_statements.py`

- [ ] **Step 1: Regenerate**

Start the daemon (`./run.sh start`), then:

```bash
./scripts/regenerate-ts-client.sh
./scripts/regenerate-api-client.sh
```

Confirm `packages/aq-ts-client/src` now exports `postTilesApiProjectsProjectIdGraphTilesPost`, `getExtentApiProjectsProjectIdGraphExtentGet`, `postListApiProjectsProjectIdGraphListPost`, `getNodeApiProjectsProjectIdGraphNodeTaskIdGet`, `getLocateApiProjectsProjectIdGraphLocateGet`, `postTidyApiProjectsProjectIdGraphTidyPost`, `getJobApiProjectsProjectIdGraphJobsJobIdGet` and types `TilesRequest`, `TilesResponse`, `LayoutNode`, `LayoutEdge`, `LayoutStub`, `LayoutWorker`, `ExtentResponse`, `ListResponse`, `NodeResponse`, `LocateResponse`. If the generated names differ, record the actual names in the Stage 3 plan's Global Constraints before starting it.

- [ ] **Step 2: Perf test**

```python
"""tiles latency on PostgreSQL (spec §9)."""

from __future__ import annotations

import statistics
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scripts.seed_layout_perf import seed_project
from src.api.graph_layout import build_graph_layout_router
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set")


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=100, per_epic=40, big_epic=1000, hub_dependents=50)
    drv = LayoutDriver(any_db)
    await drv.full_layout("perf", "all"); await drv.full_layout("perf", "active")
    yield any_db


async def test_tiles_p95_under_100ms_with_big_collapsed_epic_visible(pg):
    app = FastAPI(); app.include_router(build_graph_layout_router(db=pg))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        big = (await ac.get("/api/projects/perf/graph/node/epic0?variant=all")).json()["node"]
        rect = {"x0": big["x"] - 1, "y0": big["y"] - 1, "x1": big["x"] + 15, "y1": big["y"] + 15}
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            r = await ac.post("/api/projects/perf/graph/tiles", json={"variant": "all", "rect": rect, "expanded": []})
            times.append(time.perf_counter() - t0)
            assert r.status_code == 200
        p95 = statistics.quantiles(times, n=20)[18]
        assert p95 < 0.1, f"p95 {p95:.3f}s"
```

- [ ] **Step 3: Run**

Run: `POSTGRES_TEST_DSN=<dsn> pytest tests/perf/test_layout_api_statements.py -v`
Expected: PASS. If it misses, the likely culprit is `load_rows_by_prefixes` returning 1,000 rows plus `load_edges_touching` on 1,000 ids: confirm the `idx_task_layouts_path` index is used (`EXPLAIN` the LIKE with a trailing `%`), and that `_TASK_FIELDS` joins are not being run for hidden rows.

- [ ] **Step 4: Commit**

```bash
git add openapi.json packages tests/perf/test_layout_api_statements.py
git commit -m "feat(layout-api): regenerate clients; postgres tiles latency test"
```

---

### Task 9: Stage wrap-up

- [ ] **Step 1:** `pytest tests/ -n auto -q` → PASS.
- [ ] **Step 2:** `ruff check src tests` → clean.
- [ ] **Step 3:** Add to `CLAUDE.md` under the graph layout line: "API: `src/api/graph_layout.py` (`extent`, `tiles`, `list`, `node`, `locate`, `tidy`, `jobs`); resolution logic in `src/task_graph/layout/view.py`."
- [ ] **Step 4:** Commit `docs: document layout API`.

---

## Self-review against the spec

- §5.1 extent: Task 4 (header-band content is not returned; the dashboard already fetches playbooks via `usePlaybooks`, so the spec's header content is served by the existing hook — record this in the Stage 3 plan).
- §5.2 tiles: Tasks 4, 5. Rect cap, expanded cap, root forcing `all`, max_depth, `q`/`status` with context-only ancestors, remapped edges with count, stubs capped with overflow, workers, gates, version.
- §5.3 list: Task 6. §5.4 node: Task 6. §5.5 locate: Task 6. §5.6 tidy + jobs: Task 7.
- §7 error handling: 202 pending + backfill enqueue (Task 4), 400 validation (Tasks 4, 6), 404s.
- §9 API tests: Tasks 4–7; perf: Task 8.
- Cross-project stubs (§5.2 last bullet): a far endpoint in another project has no row in this project's variant and is dropped by `cap_stubs`. Cross-project edges therefore render only when both projects are loaded on the client (Stage 3, §6.8), which the spec allows; the labeled-port stub for an unloaded peer project is deferred and noted for Stage 3.
