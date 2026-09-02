# Task Graph Layout Engine (Stage 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist a stable, server-computed, nested-container layout for every project's task graph, kept current by durable dirty marks and an incremental engine, with a Tidy/backfill job path. No API or dashboard changes in this stage.

**Architecture:** A pure-Python engine in `src/task_graph/layout/` turns a bounded snapshot (dirty containers, their children, their ancestor chains) into a write set. A driver in the same package does all database IO: reads `layout_dirty`, builds scopes, runs the engine in a thread, and publishes atomically with a version bump. Five new tables hold rows, cell membership, per-variant meta, dirty marks, and jobs. Every mutation the engine depends on writes a dirty mark in its own transaction.

**Tech Stack:** Python 3.12, SQLAlchemy Core, Alembic, pytest-asyncio (auto mode), asyncio.to_thread. SQLite for unit tests, PostgreSQL for perf tests via `tests/pg_dsn.py`.

**Spec:** `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` (sections 3, 4, 8, 9, 10)

## Global Constraints

- Layout units: card 1.0 × 1.0; sibling gap 0.2; line/rank gap 0.3; container padding 0.15; header 0.35; target row width 4.0 (non-root) and 6.0 (root); growth bands 1.5, 3, 6, 12, 24, 48, then doubling.
- Cost weights: `w_cross = 10`, `w_span = 1`, `w_wrap = 2`, `w_slack = 0.5`. No displacement term.
- Incremental mode never changes an existing node's `rank` or `order_key` except the forced dependency-rank repair.
- `MAX_OPTIMIZED_SIBLINGS = 500`; incremental budget 200 evaluations or 50 ms per container; Tidy 5,000 evaluations or 2 s per container, 60 s per job.
- Finished statuses: `COMPLETED`, `CANCELED`, `CANCELLED`, `SKIPPED`.
- Layout dependency edge types considered for ranking: `blocks`, `waits-for`, `conditional-blocks`. (`discovered-from` is drawn but does not rank.)
- Cell size: 8 × 8 units.
- Containers are read from `task_metadata.container = true`, never inferred.
- Cycle breaking: drop the edge whose dependent has the newest `tasks.created_at`, ties by task id.
- Migrations must run on SQLite and PostgreSQL. Never edit `tables.py` without a migration.
- Async-first: no sync `subprocess`; engine CPU work runs via `asyncio.to_thread`.
- Commit after every task; run `pytest tests/task_graph -n auto` before each commit.

---

## File structure

| File | Responsibility |
|---|---|
| `src/database/tables.py` | Five new `Table` objects |
| `migrations/versions/d1e2f3a4b5c6_task_layouts.py` | DDL for the five tables |
| `src/database/queries/layout_queries.py` | `LayoutQueryMixin`: dirty marks, scope loads, publish, meta, jobs |
| `src/task_graph/layout/__init__.py` | Public exports |
| `src/task_graph/layout/constants.py` | Unit constants, weights, budgets, status sets |
| `src/task_graph/layout/order_key.py` | Fractional index strings |
| `src/task_graph/layout/model.py` | `SnapTask`, `LayoutRow`, `ContainerScope`, `WriteSet` |
| `src/task_graph/layout/layering.py` | Minimal feasible ranks with cycle breaking |
| `src/task_graph/layout/flow.py` | Rank → lines → coordinates, content/allocated size, cells |
| `src/task_graph/layout/cost.py` | Crossings, span, wrap, slack |
| `src/task_graph/layout/engine.py` | `layout_container`, `tidy_project`, placement search |
| `src/task_graph/layout/driver.py` | `LayoutDriver`: consume dirty marks, run engine, publish, jobs |
| `src/orchestrator/layout_step.py` | `LayoutStepMixin._run_layout_step` for `run_one_cycle` |
| `src/commands/graph_commands.py` | `_cmd_graph_layout_rebuild`, `_cmd_graph_tidy` |
| `src/config.py` | `GraphLayoutConfig` under `dashboard.graph_layout` |
| `tests/task_graph/layout/…` | Engine unit tests |
| `tests/task_graph/test_layout_driver.py` | Driver integration tests on SQLite |
| `tests/perf/test_layout_statements.py` | PostgreSQL perf assertions |

---

### Task 1: Tables and migration

**Files:**
- Modify: `src/database/tables.py` (append after `task_dependencies`, around line 202)
- Create: `migrations/versions/d1e2f3a4b5c6_task_layouts.py`
- Test: `tests/test_database.py` (append)

**Interfaces:**
- Produces: `task_layouts`, `task_layout_cells`, `project_layout_meta`, `layout_dirty`, `layout_jobs` Table objects importable from `src.database.tables`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database.py`:

```python
async def test_layout_tables_exist(tmp_path):
    from sqlalchemy import inspect
    from src.database import Database

    db = Database(str(tmp_path / "layout.db"))
    await db.initialize()
    async with db._engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    await db.close()
    for t in ("task_layouts", "task_layout_cells", "project_layout_meta",
              "layout_dirty", "layout_jobs"):
        assert t in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_database.py::test_layout_tables_exist -v`
Expected: FAIL, assertion on `task_layouts`.

- [ ] **Step 3: Add the tables**

Append to `src/database/tables.py` after the `task_dependencies` table:

```python
# ── Task graph layout (spatial-layout design §4.10) ─────────────────────────
LAYOUT_VARIANTS = ("all", "active")
LAYOUT_KINDS = ("card", "container", "stub")

task_layouts = Table(
    "task_layouts",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    Column("container_id", Text, nullable=True),
    Column("path", Text, nullable=False),
    Column("depth", Integer, nullable=False),
    Column("rank", Integer, nullable=False),
    Column("order_key", Text, nullable=False),
    Column("w", Float, nullable=False),
    Column("h", Float, nullable=False),
    Column("rel_x", Float, nullable=False),
    Column("rel_y", Float, nullable=False),
    Column("abs_x", Float, nullable=False),
    Column("abs_y", Float, nullable=False),
    Column("kind", Text, nullable=False),
    Column("agg_children", Integer, nullable=False, server_default="0"),
    Column("agg_descendants", Integer, nullable=False, server_default="0"),
    Column("agg_completed", Integer, nullable=False, server_default="0"),
    Column("agg_running", Integer, nullable=False, server_default="0"),
    Column("agg_blocked", Integer, nullable=False, server_default="0"),
    Column("agg_active", Integer, nullable=False, server_default="0"),
    CheckConstraint("variant IN ('all', 'active')", name="ck_task_layouts_variant"),
    CheckConstraint("kind IN ('card', 'container', 'stub')", name="ck_task_layouts_kind"),
    Index("idx_task_layouts_path", "project_id", "variant", "path"),
    Index("idx_task_layouts_depth", "project_id", "variant", "depth"),
    Index("idx_task_layouts_container", "project_id", "variant", "container_id"),
)

task_layout_cells = Table(
    "task_layout_cells",
    metadata,
    Column("project_id", Text, nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("cell_x", Integer, nullable=False, primary_key=True),
    Column("cell_y", Integer, nullable=False, primary_key=True),
    Column("task_id", Text, nullable=False, primary_key=True),
    Index("idx_task_layout_cells_cell", "project_id", "variant", "cell_x", "cell_y"),
    Index("idx_task_layout_cells_task", "project_id", "variant", "task_id"),
)

project_layout_meta = Table(
    "project_layout_meta",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("layout_version", Integer, nullable=False, server_default="0"),
    Column("extent_w", Float, nullable=False, server_default="0"),
    Column("extent_h", Float, nullable=False, server_default="0"),
    Column("node_count", Integer, nullable=False, server_default="0"),
    Column("updated_at", Float, nullable=False),
    Column("reconciled_at", Float, nullable=True),
)

layout_dirty = Table(
    "layout_dirty",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Index("idx_layout_dirty_project", "project_id", "seq"),
)

layout_jobs = Table(
    "layout_jobs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("variant", Text, nullable=False),
    Column("kind", Text, nullable=False),  # 'tidy' | 'backfill'
    Column("status", Text, nullable=False),  # queued | running | done | failed
    Column("requested_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    Column("error", Text, nullable=True),
    Index("idx_layout_jobs_project_status", "project_id", "status"),
)
```

- [ ] **Step 4: Generate and review the migration**

Run: `alembic revision --autogenerate -m "task layouts"`
Rename the generated file to `migrations/versions/d1e2f3a4b5c6_task_layouts.py` and set `revision = "d1e2f3a4b5c6"`, `down_revision = "009793fbb800"`. Confirm the `upgrade()` body creates exactly the five tables and their indexes and `downgrade()` drops them in reverse order. Remove anything else autogenerate emitted. Then run `alembic upgrade head`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_database.py::test_layout_tables_exist -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/database/tables.py migrations/versions/d1e2f3a4b5c6_task_layouts.py tests/test_database.py
git commit -m "feat(layout): add task layout tables and migration"
```

---

### Task 2: Constants, order keys, and model types

**Files:**
- Create: `src/task_graph/layout/__init__.py`, `constants.py`, `order_key.py`, `model.py`
- Test: `tests/task_graph/__init__.py`, `tests/task_graph/layout/__init__.py`, `tests/task_graph/layout/test_order_key.py`

**Interfaces:**
- Produces: `order_key.between(a: str | None, b: str | None) -> str`; `model.SnapTask`, `model.LayoutRow`, `model.ContainerScope`, `model.WriteSet`; constants listed below.

- [ ] **Step 1: Write the failing tests**

`tests/task_graph/layout/test_order_key.py`:

```python
from src.task_graph.layout.order_key import between


def test_between_none_none_gives_middle():
    assert between(None, None) == "U"


def test_between_orders_and_is_stable():
    a = between(None, None)
    b = between(a, None)
    c = between(a, b)
    assert a < c < b


def test_many_inserts_at_front_keep_ordering():
    keys = [between(None, None)]
    for _ in range(200):
        keys.append(between(None, keys[-1]))
    assert keys == sorted(keys, reverse=True)


def test_many_inserts_between_two_keys_keep_ordering():
    lo, hi = between(None, None), between(between(None, None), None)
    keys = []
    prev = lo
    for _ in range(200):
        prev = between(prev, hi)
        keys.append(prev)
    assert keys == sorted(keys)
    assert all(lo < k < hi for k in keys)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_order_key.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create package files**

`tests/task_graph/__init__.py` and `tests/task_graph/layout/__init__.py`: empty files.

`src/task_graph/layout/constants.py`:

```python
"""Engine constants (spatial-layout design §3.2, §4.2, §4.3)."""

from __future__ import annotations

CARD_W = 1.0
CARD_H = 1.0
SIBLING_GAP = 0.2
LINE_GAP = 0.3
PADDING = 0.15
HEADER_H = 0.35
TARGET_ROW_WIDTH = 4.0
TARGET_ROW_WIDTH_ROOT = 6.0
GROWTH_BANDS = (1.5, 3.0, 6.0, 12.0, 24.0, 48.0)
CELL_SIZE = 8.0

W_CROSS = 10.0
W_SPAN = 1.0
W_WRAP = 2.0
W_SLACK = 0.5

MAX_OPTIMIZED_SIBLINGS = 500
INCREMENTAL_EVALS = 200
INCREMENTAL_SECONDS = 0.05
TIDY_EVALS = 5000
TIDY_SECONDS = 2.0
TIDY_JOB_SECONDS = 60.0

FINISHED_STATUSES = frozenset({"COMPLETED", "CANCELED", "CANCELLED", "SKIPPED"})
RUNNING_STATUSES = frozenset({"ASSIGNED", "IN_PROGRESS"})
RANKING_DEP_TYPES = frozenset({"blocks", "waits-for", "conditional-blocks"})
VARIANTS = ("all", "active")
ROOT = "__root__"


def band_up(size: float) -> float:
    """Round a content size up to the next growth band (§3.4)."""
    for b in GROWTH_BANDS:
        if size <= b:
            return b
    b = GROWTH_BANDS[-1]
    while b < size:
        b *= 2
    return b
```

`src/task_graph/layout/order_key.py`:

```python
"""Fractional ordering keys: strings that sort lexicographically and always
admit a new key strictly between two neighbours (§4.1)."""

from __future__ import annotations

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_MIN, _MAX = _ALPHABET[0], _ALPHABET[-1]
_MID = _ALPHABET[len(_ALPHABET) // 2]  # "U"


def _digit(c: str) -> int:
    return _ALPHABET.index(c)


def between(a: str | None, b: str | None) -> str:
    """Return a key k with a < k < b. ``None`` means unbounded on that side."""
    if a is None and b is None:
        return _MID
    if a is not None and b is not None and a >= b:
        raise ValueError(f"order keys not increasing: {a!r} >= {b!r}")
    lo = a or ""
    hi = b or ""
    out: list[str] = []
    i = 0
    while True:
        lc = _digit(lo[i]) if i < len(lo) else 0
        hc = _digit(hi[i]) if i < len(hi) else (len(_ALPHABET) if b is None else 0)
        if b is not None and i >= len(hi) and i >= len(lo):
            # both exhausted: cannot happen because a < b, but guard.
            hc = len(_ALPHABET)
        if hc - lc > 1:
            out.append(_ALPHABET[(lc + hc) // 2])
            return "".join(out)
        # Digits equal or adjacent: copy the low digit and keep going.
        out.append(_ALPHABET[lc])
        i += 1
        if b is not None and i >= len(hi) and "".join(out) >= hi:
            # We have matched hi's prefix; from here hi is exhausted, so
            # treat the upper bound as open on the remaining digits.
            b = None
```

`src/task_graph/layout/model.py`:

```python
"""Engine data types (§4.1, §4.4 step 8)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SnapTask:
    id: str
    parent_id: str | None
    is_container: bool
    status: str
    created_at: float
    title: str = ""


@dataclass
class LayoutRow:
    task_id: str
    container_id: str | None
    path: str
    depth: int
    rank: int
    order_key: str
    w: float
    h: float
    rel_x: float
    rel_y: float
    abs_x: float
    abs_y: float
    kind: str  # card | container | stub
    agg_children: int = 0
    agg_descendants: int = 0
    agg_completed: int = 0
    agg_running: int = 0
    agg_blocked: int = 0
    agg_active: int = 0

    @property
    def ordinal(self) -> tuple[int, str]:
        return (self.rank, self.order_key)


@dataclass
class ContainerScope:
    """Everything the engine needs to lay out ONE container's children."""

    container_id: str | None  # None = project root
    container_path: str  # "/" for root, "/<a>/<b>/" otherwise
    depth: int  # depth of the children
    children: dict[str, SnapTask]
    existing: dict[str, LayoutRow]  # existing rows for children (by task_id)
    sibling_edges: list[tuple[str, str]]  # (dependent, blocker) among children
    child_sizes: dict[str, tuple[float, float]]  # allocated (w, h) for container children
    stub_ids: frozenset[str] = frozenset()  # container children rendered as stubs
    origin: tuple[float, float] = (0.0, 0.0)  # abs coords of container content origin


@dataclass
class Translation:
    path_prefix: str
    dx: float
    dy: float


@dataclass
class WriteSet:
    upserts: list[LayoutRow] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)  # task ids
    translations: list[Translation] = field(default_factory=list)
    # Allocated size of each re-laid container (task_id or ROOT) so the driver
    # can propagate to the parent scope.
    sizes: dict[str, tuple[float, float]] = field(default_factory=dict)
```

`src/task_graph/layout/__init__.py`:

```python
"""Server-side stable task graph layout (spatial-layout design §4)."""

from src.task_graph.layout.model import ContainerScope, LayoutRow, SnapTask, Translation, WriteSet

__all__ = ["ContainerScope", "LayoutRow", "SnapTask", "Translation", "WriteSet"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_order_key.py -v`
Expected: PASS (4 tests). If `between` fails the front-insert test, the loop's open-upper-bound handling is wrong; the invariant to keep is that when `b is None` the upper digit is `len(_ALPHABET)`.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout tests/task_graph
git commit -m "feat(layout): engine constants, order keys, and model types"
```

---

### Task 3: Layering with cycle breaking

**Files:**
- Create: `src/task_graph/layout/layering.py`
- Test: `tests/task_graph/layout/test_layering.py`

**Interfaces:**
- Produces: `minimal_ranks(children: dict[str, SnapTask], edges: list[tuple[str, str]]) -> dict[str, int]` and `break_cycles(children, edges) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.layering import break_cycles, minimal_ranks
from src.task_graph.layout.model import SnapTask


def t(i, created=0.0):
    return SnapTask(id=i, parent_id=None, is_container=False, status="READY", created_at=created)


def test_chain_gets_increasing_ranks():
    kids = {i: t(i) for i in "abc"}
    ranks = minimal_ranks(kids, [("b", "a"), ("c", "b")])  # b depends on a
    assert ranks == {"a": 0, "b": 1, "c": 2}


def test_unrelated_nodes_are_rank_zero():
    kids = {i: t(i) for i in "abc"}
    assert minimal_ranks(kids, []) == {"a": 0, "b": 0, "c": 0}


def test_longest_path_wins():
    kids = {i: t(i) for i in "abcd"}
    ranks = minimal_ranks(kids, [("d", "a"), ("b", "a"), ("c", "b"), ("d", "c")])
    assert ranks["d"] == 3


def test_cycle_drops_edge_with_newest_dependent():
    kids = {"a": t("a", 1.0), "b": t("b", 2.0), "c": t("c", 3.0)}
    edges = [("b", "a"), ("c", "b"), ("a", "c")]  # a -> c closes the cycle
    kept = break_cycles(kids, edges)
    # Dependent of ("c","b") is c, the newest. That edge is dropped.
    assert ("c", "b") not in kept
    assert len(kept) == 2
    ranks = minimal_ranks(kids, edges)
    assert set(ranks) == {"a", "b", "c"}


def test_edges_to_unknown_ids_are_ignored():
    kids = {"a": t("a")}
    assert minimal_ranks(kids, [("a", "zzz")]) == {"a": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_layering.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/task_graph/layout/layering.py`:

```python
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


def minimal_ranks(
    children: dict[str, SnapTask], edges: list[tuple[str, str]]
) -> dict[str, int]:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_layering.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/layering.py tests/task_graph/layout/test_layering.py
git commit -m "feat(layout): minimal ranks with deterministic cycle breaking"
```

---

### Task 4: Flow coordinates, sizes, and cells

**Files:**
- Create: `src/task_graph/layout/flow.py`
- Test: `tests/task_graph/layout/test_flow.py`

**Interfaces:**
- Produces:
  - `flow_container(ordered: list[list[str]], sizes: dict[str, tuple[float,float]], *, is_root: bool) -> FlowResult` where `FlowResult` has `positions: dict[str, tuple[float,float]]` (rel_x, rel_y of each child relative to the content origin), `content: tuple[float,float]`, `allocated: tuple[float,float]`, `lines_per_rank: list[int]`.
  - `cells_for_box(x, y, w, h) -> list[tuple[int,int]]`.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.constants import (
    CARD_H, CARD_W, HEADER_H, LINE_GAP, PADDING, SIBLING_GAP, band_up,
)
from src.task_graph.layout.flow import cells_for_box, flow_container


def unit(ids):
    return {i: (CARD_W, CARD_H) for i in ids}


def test_single_rank_flows_left_to_right():
    r = flow_container([["a", "b", "c"]], unit("abc"), is_root=False)
    assert r.positions["a"] == (0.0, 0.0)
    assert r.positions["b"] == (CARD_W + SIBLING_GAP, 0.0)
    assert r.positions["c"] == (2 * (CARD_W + SIBLING_GAP), 0.0)
    assert r.lines_per_rank == [1]


def test_rank_wraps_at_target_width():
    # Target 4.0 units: four cards need 4*1 + 3*0.2 = 4.6 > 4.0, so the 4th wraps.
    r = flow_container([["a", "b", "c", "d"]], unit("abcd"), is_root=False)
    assert r.positions["d"] == (0.0, CARD_H + LINE_GAP)
    assert r.lines_per_rank == [2]


def test_second_rank_starts_below_first():
    r = flow_container([["a"], ["b"]], unit("ab"), is_root=False)
    assert r.positions["b"] == (0.0, CARD_H + LINE_GAP)


def test_line_height_is_tallest_child():
    sizes = {"a": (1.0, 1.0), "b": (1.0, 3.0), "c": (1.0, 1.0)}
    r = flow_container([["a", "b"], ["c"]], sizes, is_root=False)
    assert r.positions["c"][1] == 3.0 + LINE_GAP


def test_content_and_allocated_sizes():
    r = flow_container([["a", "b"]], unit("ab"), is_root=False)
    w = 2 * CARD_W + SIBLING_GAP + 2 * PADDING
    h = CARD_H + 2 * PADDING + HEADER_H
    assert r.content == (w, h)
    assert r.allocated == (band_up(w), band_up(h))


def test_empty_container_is_card_sized():
    r = flow_container([], {}, is_root=False)
    assert r.content == (CARD_W, CARD_H)
    assert r.allocated == (CARD_W, CARD_H)


def test_cells_for_box_covers_all_overlapped_cells():
    assert cells_for_box(0.0, 0.0, 1.0, 1.0) == [(0, 0)]
    assert cells_for_box(7.5, 0.0, 1.0, 1.0) == [(0, 0), (1, 0)]
    assert cells_for_box(0.0, 0.0, 16.0, 8.0) == [(0, 0), (1, 0)]
    assert cells_for_box(-0.5, -0.5, 1.0, 1.0) == [(-1, -1), (-1, 0), (0, -1), (0, 0)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_flow.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/task_graph/layout/flow.py`:

```python
"""Rank → lines → coordinates (§4.4 step 5) and cell membership (§4.10)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.task_graph.layout.constants import (
    CARD_H, CARD_W, CELL_SIZE, HEADER_H, LINE_GAP, PADDING, SIBLING_GAP,
    TARGET_ROW_WIDTH, TARGET_ROW_WIDTH_ROOT, band_up,
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
            x += w + SIBLING_GAP
            line_h = max(line_h, h)
            max_w = max(max_w, x - SIBLING_GAP)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_flow.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/flow.py tests/task_graph/layout/test_flow.py
git commit -m "feat(layout): flow rows into lines, sizes, and cell membership"
```

---

### Task 5: Cost function

**Files:**
- Create: `src/task_graph/layout/cost.py`
- Test: `tests/task_graph/layout/test_cost.py`

**Interfaces:**
- Produces: `container_cost(ordered: list[list[str]], positions: dict[str, tuple[float,float]], edges: list[tuple[str,str]], minimal: dict[str,int], lines_per_rank: list[int]) -> float` and `count_crossings(ordered, positions, edges) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.constants import W_CROSS, W_SPAN, W_WRAP, W_SLACK
from src.task_graph.layout.cost import container_cost, count_crossings


def pos(ordered):
    return {cid: (float(i), float(r)) for r, rank in enumerate(ordered) for i, cid in enumerate(rank)}


def test_no_crossings_when_dependents_under_blockers():
    ordered = [["a", "b"], ["c", "d"]]
    edges = [("c", "a"), ("d", "b")]
    assert count_crossings(ordered, pos(ordered), edges) == 0


def test_one_crossing_when_swapped():
    ordered = [["a", "b"], ["d", "c"]]
    edges = [("c", "a"), ("d", "b")]
    assert count_crossings(ordered, pos(ordered), edges) == 1


def test_cost_components():
    ordered = [["a", "b"], ["d", "c"]]
    edges = [("c", "a"), ("d", "b")]
    minimal = {"a": 0, "b": 0, "c": 1, "d": 1}
    p = pos(ordered)
    # crossings 1; span |1-0| + |0-1| = 2; wrap (1-1)+(1-1)=0; slack 0
    expected = W_CROSS * 1 + W_SPAN * 2 + W_WRAP * 0 + W_SLACK * 0
    assert container_cost(ordered, p, edges, minimal, [1, 1]) == expected


def test_wrap_and_slack_are_charged():
    ordered = [["a"], [], ["b"]]  # b has slack 1 if minimal is 1
    p = {"a": (0.0, 0.0), "b": (0.0, 2.0)}
    cost = container_cost(ordered, p, [], {"a": 0, "b": 1}, [2, 0, 1])
    assert cost == W_WRAP * 1 + W_SLACK * 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_cost.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/task_graph/layout/cost.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_cost.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/cost.py tests/task_graph/layout/test_cost.py
git commit -m "feat(layout): container cost function"
```

---

### Task 6: Engine — incremental container layout

**Files:**
- Create: `src/task_graph/layout/engine.py`
- Test: `tests/task_graph/layout/test_engine_incremental.py`

**Interfaces:**
- Consumes: `minimal_ranks`, `flow_container`, `container_cost`, `between`, model types.
- Produces: `layout_container(scope: ContainerScope, *, mode: Literal["incremental","resize","tidy"], seed: int = 0) -> ContainerResult` where `ContainerResult` has `rows: dict[str, LayoutRow]` (all children, rel and abs coordinates filled), `allocated: tuple[float,float]`, `changed_ordinals: set[str]`.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.constants import CARD_H, CARD_W, ROOT
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask


def task(i, created=0.0, container=False, status="READY"):
    return SnapTask(id=i, parent_id=None, is_container=container, status=status, created_at=created)


def scope(children, edges=(), existing=None, sizes=None, origin=(0.0, 0.0)):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id=None, container_path="/", depth=0, children=kids,
        existing=existing or {}, sibling_edges=list(edges),
        child_sizes=sizes or {t.id: (CARD_W, CARD_H) for t in children}, origin=origin,
    )


def test_fresh_layout_places_dependents_below_blockers():
    s = scope([task("a"), task("b")], edges=[("b", "a")])
    res = layout_container(s, mode="incremental")
    assert res.rows["a"].rank == 0 and res.rows["b"].rank == 1
    assert res.rows["b"].rel_y > res.rows["a"].rel_y
    assert res.rows["a"].path == "/a/" and res.rows["a"].depth == 0


def test_insert_into_large_container_moves_no_existing_ordinal():
    ids = [f"t{i}" for i in range(1000)]
    first = layout_container(scope([task(i, created=k) for k, i in enumerate(ids)]), mode="incremental")
    before = {cid: r.ordinal for cid, r in first.rows.items()}
    kids = [task(i, created=k) for k, i in enumerate(ids)] + [task("new", created=9999)]
    second = layout_container(scope(kids, existing=first.rows), mode="incremental")
    assert second.changed_ordinals == {"new"}
    for cid, ordinal in before.items():
        assert second.rows[cid].ordinal == ordinal


def test_new_node_with_blockers_lands_under_barycenter():
    kids = [task("a"), task("b"), task("c")]
    first = layout_container(scope(kids), mode="incremental")
    kids2 = kids + [task("n", created=5)]
    s = scope(kids2, edges=[("n", "a"), ("n", "c")], existing=first.rows)
    res = layout_container(s, mode="incremental")
    assert res.rows["n"].rank == 1
    xa, xc = res.rows["a"].rel_x, res.rows["c"].rel_x
    assert xa <= res.rows["n"].rel_x <= xc + CARD_W


def test_new_edge_forces_rank_repair_of_dependent_chain_only():
    kids = [task("a"), task("b"), task("c"), task("d")]
    first = layout_container(scope(kids, edges=[("c", "b")]), mode="incremental")
    assert first.rows["c"].rank == 1
    # New edge: b depends on a → b and c must move down; a and d must not change.
    res = layout_container(
        scope(kids, edges=[("c", "b"), ("b", "a")], existing=first.rows), mode="incremental"
    )
    assert res.rows["a"].ordinal == first.rows["a"].ordinal
    assert res.rows["d"].ordinal == first.rows["d"].ordinal
    assert res.rows["b"].rank == 1 and res.rows["c"].rank == 2
    assert res.changed_ordinals == {"b", "c"}


def test_removed_node_closes_gap_without_changing_keys():
    kids = [task("a"), task("b"), task("c")]
    first = layout_container(scope(kids), mode="incremental")
    res = layout_container(scope([task("a"), task("c")], existing=first.rows), mode="incremental")
    assert res.rows["c"].order_key == first.rows["c"].order_key
    assert res.rows["c"].rel_x == first.rows["b"].rel_x
    assert "b" not in res.rows


def test_resize_mode_keeps_ordinals_and_recomputes_coordinates():
    kids = [task("a", container=True), task("b")]
    first = layout_container(scope(kids), mode="incremental")
    grown = {"a": (3.0, 3.0), "b": (CARD_W, CARD_H)}
    res = layout_container(scope(kids, existing=first.rows, sizes=grown), mode="resize")
    assert res.changed_ordinals == set()
    assert res.rows["b"].rel_x == first.rows["b"].rel_x + 2.0
    assert res.rows["a"].w == 3.0 and res.rows["a"].kind == "container"


def test_abs_coordinates_include_origin():
    res = layout_container(scope([task("a")], origin=(10.0, 20.0)), mode="incremental")
    assert (res.rows["a"].abs_x, res.rows["a"].abs_y) == (10.0, 20.0)


def test_stub_children_are_card_sized_stubs():
    s = scope([task("epic", container=True)])
    s.stub_ids = frozenset({"epic"})
    res = layout_container(s, mode="incremental")
    assert res.rows["epic"].kind == "stub"
    assert (res.rows["epic"].w, res.rows["epic"].h) == (CARD_W, CARD_H)


def test_deterministic():
    kids = [task(f"t{i}", created=i) for i in range(30)]
    edges = [(f"t{i}", f"t{i-3}") for i in range(3, 30)]
    a = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)
    b = layout_container(scope(kids, edges=edges), mode="incremental", seed=7)
    assert {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in a.rows.items()} == \
           {k: (r.ordinal, r.rel_x, r.rel_y) for k, r in b.rows.items()}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/layout/test_engine_incremental.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/task_graph/layout/engine.py`:

```python
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
    CARD_H, CARD_W, HEADER_H, INCREMENTAL_EVALS, INCREMENTAL_SECONDS,
    MAX_OPTIMIZED_SIBLINGS, PADDING, TIDY_EVALS, TIDY_SECONDS,
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
        gaps = _gap_keys(in_rank, ordinals)
        if blockers:
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
```

Note on the "repair" test: a forced push may leave two nodes in the same rank sharing an order key only if they came from different ranks with equal keys. That is tolerated: `_ordered_from` sorts by key and ties fall back to dict insertion order, which is deterministic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/layout/test_engine_incremental.py -v`
Expected: PASS (9 tests). The 1,000-node test must finish in under 5 s; if it does not, `_place_new` is evaluating too many gaps for no-blocker nodes: confirm the `if not blockers: break` and that a no-blocker node tries only the end gap first (move `(keys[-1], None)` to the front of `gaps` when `blockers` is empty).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/engine.py tests/task_graph/layout/test_engine_incremental.py
git commit -m "feat(layout): incremental container layout with immutable ordinals"
```

---

### Task 7: Engine — Tidy quality and nesting fixtures

**Files:**
- Test: `tests/task_graph/layout/test_engine_tidy.py`

**Interfaces:**
- Consumes: `layout_container(mode="tidy")`, `count_crossings`.

- [ ] **Step 1: Write the failing tests**

```python
from src.task_graph.layout.cost import count_crossings
from src.task_graph.layout.engine import _ordered_from, layout_container
from src.task_graph.layout.model import ContainerScope, SnapTask
from src.task_graph.layout.constants import CARD_H, CARD_W


def task(i, created=0.0):
    return SnapTask(id=i, parent_id=None, is_container=False, status="READY", created_at=created)


def scope(children, edges):
    kids = {t.id: t for t in children}
    return ContainerScope(
        container_id="epic", container_path="/epic/", depth=1, children=kids, existing={},
        sibling_edges=edges, child_sizes={t.id: (CARD_W, CARD_H) for t in children},
    )


def crossings_of(res, edges):
    ordinals = {c: r.ordinal for c, r in res.rows.items()}
    ordered = _ordered_from(ordinals)
    positions = {c: (r.rel_x, r.rel_y) for c, r in res.rows.items()}
    return count_crossings(ordered, positions, edges)


def test_tidy_untangles_a_reversed_ladder():
    # 4 blockers a0..a3, 4 dependents b0..b3 created in reverse order so the
    # created_at seed is maximally crossed; tidy must reach zero crossings.
    kids = [task(f"a{i}", created=i) for i in range(4)] + [task(f"b{i}", created=10 - i) for i in range(4)]
    edges = [(f"b{i}", f"a{i}") for i in range(4)]
    res = layout_container(scope(kids, edges), mode="tidy")
    assert crossings_of(res, edges) == 0


def test_tidy_pinned_bound_on_fixture():
    # Two interleaved chains plus cross links: bound pinned at 2 crossings.
    kids = [task(f"n{i}", created=(i * 7) % 12) for i in range(12)]
    edges = [(f"n{i}", f"n{i-2}") for i in range(2, 12)] + [("n5", "n0"), ("n11", "n4")]
    res = layout_container(scope(kids, edges), mode="tidy")
    assert crossings_of(res, edges) <= 2


def test_tidy_is_deterministic():
    kids = [task(f"n{i}", created=i) for i in range(20)]
    edges = [(f"n{i}", f"n{(i * 3) % 20}") for i in range(1, 20) if (i * 3) % 20 < i]
    a = layout_container(scope(kids, edges), mode="tidy", seed=1)
    b = layout_container(scope(kids, edges), mode="tidy", seed=1)
    assert {c: r.ordinal for c, r in a.rows.items()} == {c: r.ordinal for c, r in b.rows.items()}
```

- [ ] **Step 2: Run tests to verify they pass or expose a defect**

Run: `pytest tests/task_graph/layout/test_engine_tidy.py -v`
Expected: PASS. If the ladder test fails, the barycenter `reorder` is using stale `ref_pos` after the rank was re-sorted; recompute `ref_pos` inside `reorder` on every call (it already does) and confirm the down sweep runs before the up sweep.

- [ ] **Step 3: Commit**

```bash
git add tests/task_graph/layout/test_engine_tidy.py
git commit -m "test(layout): tidy quality and determinism fixtures"
```

---

### Task 8: Layout query mixin — dirty marks, meta, jobs

**Files:**
- Create: `src/database/queries/layout_queries.py`
- Modify: `src/database/adapters/sqlite.py` (import + add `LayoutQueryMixin` to the class bases), and the PostgreSQL adapter class in `src/database/adapters/` the same way (find it with `grep -ln "class .*DatabaseAdapter" src/database/adapters/*.py`)
- Test: `tests/task_graph/test_layout_queries.py`

**Interfaces:**
- Produces on `Database`:
  - `mark_layout_dirty(project_id, task_ids: Iterable[str], reason: str, *, conn) -> None`
  - `pop_layout_dirty(project_id, *, min_age_seconds: float) -> tuple[int, list[tuple[str, str]]]` returns `(max_seq, [(task_id, reason)])`, empty when the newest row is younger than `min_age_seconds`
  - `dirty_layout_projects() -> list[str]`
  - `get_layout_meta(project_id, variant) -> dict | None`
  - `enqueue_layout_job(project_id, variant, kind) -> dict` (no-op returning the existing row if one is queued/running)
  - `next_layout_job() -> dict | None`, `finish_layout_job(job_id, *, error: str | None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
import time

import pytest

from src.database import Database
from src.models import Project


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "lq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def test_dirty_marks_round_trip(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1", "t2"], "task.created", conn=conn)
    assert await db.dirty_layout_projects() == ["p1"]
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    assert seq >= 2 and sorted(r[0] for r in rows) == ["t1", "t2"]


async def test_pop_respects_debounce(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["t1"], "task.created", conn=conn)
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=60)
    assert rows == [] and seq == 0


async def test_jobs_lifecycle(db):
    job = await db.enqueue_layout_job("p1", "all", "tidy")
    again = await db.enqueue_layout_job("p1", "all", "tidy")
    assert again["id"] == job["id"]
    nxt = await db.next_layout_job()
    assert nxt["id"] == job["id"] and nxt["status"] == "running"
    await db.finish_layout_job(job["id"], error=None)
    assert await db.next_layout_job() is None
    assert (await db.get_layout_job(job["id"]))["status"] == "done"


async def test_meta_absent_until_published(db):
    assert await db.get_layout_meta("p1", "all") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_queries.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'mark_layout_dirty'`.

- [ ] **Step 3: Implement the mixin**

`src/database/queries/layout_queries.py`:

```python
"""Layout storage queries (spatial-layout design §4.6, §4.10). Expects ``self._engine``."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

from sqlalchemy import delete, func, insert, select, update

from src.database.tables import layout_dirty, layout_jobs, project_layout_meta


class LayoutQueryMixin:
    # ── dirty marks ─────────────────────────────────────────────────────
    async def mark_layout_dirty(
        self, project_id: str, task_ids: Iterable[str], reason: str, *, conn
    ) -> None:
        rows = [
            {"project_id": project_id, "task_id": t, "reason": reason, "created_at": time.time()}
            for t in dict.fromkeys(task_ids)
        ]
        if rows:
            await conn.execute(insert(layout_dirty), rows)

    async def dirty_layout_projects(self) -> list[str]:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(layout_dirty.c.project_id).distinct().order_by(layout_dirty.c.project_id)
            )
            return [r[0] for r in res.fetchall()]

    async def pop_layout_dirty(
        self, project_id: str, *, min_age_seconds: float
    ) -> tuple[int, list[tuple[str, str]]]:
        """Read (not delete) the project's dirty rows if the newest is old enough."""
        async with self._engine.begin() as conn:
            newest = (
                await conn.execute(
                    select(func.max(layout_dirty.c.created_at)).where(
                        layout_dirty.c.project_id == project_id
                    )
                )
            ).scalar_one_or_none()
            if newest is None or time.time() - newest < min_age_seconds:
                return 0, []
            res = await conn.execute(
                select(layout_dirty.c.seq, layout_dirty.c.task_id, layout_dirty.c.reason)
                .where(layout_dirty.c.project_id == project_id)
                .order_by(layout_dirty.c.seq)
            )
            rows = res.fetchall()
        return (max(r[0] for r in rows), [(r[1], r[2]) for r in rows]) if rows else (0, [])

    async def clear_layout_dirty(self, project_id: str, up_to_seq: int, *, conn) -> None:
        await conn.execute(
            delete(layout_dirty).where(
                layout_dirty.c.project_id == project_id, layout_dirty.c.seq <= up_to_seq
            )
        )

    # ── meta ────────────────────────────────────────────────────────────
    async def get_layout_meta(self, project_id: str, variant: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(project_layout_meta).where(
                        project_layout_meta.c.project_id == project_id,
                        project_layout_meta.c.variant == variant,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    # ── jobs ────────────────────────────────────────────────────────────
    async def enqueue_layout_job(self, project_id: str, variant: str, kind: str) -> dict:
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(layout_jobs).where(
                        layout_jobs.c.project_id == project_id,
                        layout_jobs.c.variant == variant,
                        layout_jobs.c.status.in_(("queued", "running")),
                    )
                )
            ).mappings().first()
            if existing:
                return dict(existing)
            row = {
                "id": uuid.uuid4().hex, "project_id": project_id, "variant": variant,
                "kind": kind, "status": "queued", "requested_at": time.time(),
            }
            await conn.execute(insert(layout_jobs).values(**row))
            return row

    async def next_layout_job(self) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(layout_jobs)
                    .where(layout_jobs.c.status == "queued")
                    .order_by(layout_jobs.c.requested_at)
                    .limit(1)
                )
            ).mappings().first()
            if not row:
                return None
            await conn.execute(
                update(layout_jobs)
                .where(layout_jobs.c.id == row["id"], layout_jobs.c.status == "queued")
                .values(status="running", started_at=time.time())
            )
            return {**dict(row), "status": "running"}

    async def finish_layout_job(self, job_id: str, *, error: str | None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(layout_jobs)
                .where(layout_jobs.c.id == job_id)
                .values(status="failed" if error else "done", finished_at=time.time(), error=error)
            )

    async def get_layout_job(self, job_id: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(select(layout_jobs).where(layout_jobs.c.id == job_id))
            ).mappings().first()
        return dict(row) if row else None
```

Add `from src.database.queries.layout_queries import LayoutQueryMixin` and `LayoutQueryMixin,` to the base list of both adapter classes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_queries.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/layout_queries.py src/database/adapters tests/task_graph/test_layout_queries.py
git commit -m "feat(layout): dirty marks, meta, and job queries"
```

---

### Task 9: Layout query mixin — scope loading and atomic publish

**Files:**
- Modify: `src/database/queries/layout_queries.py`
- Test: `tests/task_graph/test_layout_queries.py` (append)

**Interfaces:**
- Produces on `Database`:
  - `load_project_snapshot(project_id) -> tuple[dict[str, SnapTask], list[tuple[str,str,str]]]` returning all non-archived tasks and all `task_dependencies` rows as `(task_id, depends_on, dep_type)`.
  - `load_layout_rows(project_id, variant, task_ids: Iterable[str]) -> dict[str, LayoutRow]`
  - `load_children_layout_rows(project_id, variant, container_id: str | None) -> dict[str, LayoutRow]`
  - `subtree_aggregates(project_id, variant, path_prefix: str) -> dict` with keys `children` (direct), `descendants`, `completed`, `running`, `blocked`, `active`, computed from `tasks` joined to `task_layouts` of variant `all` (aggregates are over the real subtree, not the variant's visible rows).
  - `publish_layout(project_id, variant, write_set: WriteSet, *, consumed_seq: int | None, extent: tuple[float,float], node_count_delta: int | None) -> int` returning the new `layout_version`. Does upserts, deletes, translations (`UPDATE ... WHERE path LIKE prefix%` adding dx/dy), rewrites cells for every upserted or translated row, bumps meta, clears dirty rows, all in ONE transaction.

- [ ] **Step 1: Write the failing tests**

Append to `tests/task_graph/test_layout_queries.py`:

```python
from src.models import Task
from src.task_graph.layout.model import LayoutRow, Translation, WriteSet


def row(tid, x, y, path, container=None, depth=0, w=1.0, h=1.0, kind="card"):
    return LayoutRow(task_id=tid, container_id=container, path=path, depth=depth, rank=0,
                     order_key="U", w=w, h=h, rel_x=x, rel_y=y, abs_x=x, abs_y=y, kind=kind)


async def test_snapshot_reads_tasks_containers_and_edges(db):
    await db.create_task(Task(id="e", project_id="p1", title="Epic", description=""))
    await db.create_task(Task(id="c", project_id="p1", title="Child", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c", "e", conn=conn)
    await db.add_dependency("c", "e", "discovered-from")
    tasks, edges = await db.load_project_snapshot("p1")
    assert tasks["e"].is_container and not tasks["c"].is_container
    assert tasks["c"].parent_id == "e"
    assert ("c", "e", "parent-child") in edges and ("c", "e", "discovered-from") in edges


async def test_publish_is_atomic_and_bumps_version(db):
    await db.create_task(Task(id="a", project_id="p1", title="A", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="B", description=""))
    ws = WriteSet(upserts=[row("a", 0, 0, "/a/"), row("b", 9, 0, "/b/")])
    v1 = await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(10, 1), node_count_delta=None)
    assert v1 == 1
    meta = await db.get_layout_meta("p1", "all")
    assert meta["node_count"] == 2 and meta["extent_w"] == 10
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert rows["b"].abs_x == 9
    cells = await db.load_cells("p1", "all", ["b"])
    assert cells == {("b"): [(1, 0)]}


async def test_translation_moves_subtree_and_rewrites_cells(db):
    for t in ("e", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container", w=3, h=3),
                           row("c", 0.5, 0.5, "/e/c/", container="e", depth=1)])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3), node_count_delta=None)
    ws2 = WriteSet(translations=[Translation(path_prefix="/e/", dx=8.0, dy=0.0)])
    v = await db.publish_layout("p1", "all", ws2, consumed_seq=None, extent=(11, 3), node_count_delta=0)
    assert v == 2
    rows = await db.load_layout_rows("p1", "all", ["e", "c"])
    assert rows["e"].abs_x == 8.0 and rows["c"].abs_x == 8.5
    assert rows["c"].rel_x == 0.5  # rel coordinates untouched by translation
    assert (await db.load_cells("p1", "all", ["c"]))["c"] == [(1, 0)]


async def test_subtree_aggregates(db):
    from src.models import TaskStatus
    for t in ("e", "c1", "c2"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c1", "e", conn=conn)
        await db.set_parent("c2", "e", conn=conn)
    await db.transition_task("c1", TaskStatus.COMPLETED, force=True)
    ws = WriteSet(upserts=[row("e", 0, 0, "/e/", kind="container"),
                           row("c1", 0, 0, "/e/c1/", "e", 1), row("c2", 1, 0, "/e/c2/", "e", 1)])
    await db.publish_layout("p1", "all", ws, consumed_seq=None, extent=(3, 3), node_count_delta=None)
    agg = await db.subtree_aggregates("p1", "all", "/e/")
    assert agg == {"children": 2, "descendants": 2, "completed": 1, "running": 0, "blocked": 0, "active": 1}


async def test_publish_clears_consumed_dirty_rows(db):
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["x"], "task.created", conn=conn)
    seq, _ = await db.pop_layout_dirty("p1", min_age_seconds=0)
    await db.publish_layout("p1", "all", WriteSet(), consumed_seq=seq, extent=(0, 0), node_count_delta=0)
    assert await db.dirty_layout_projects() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_queries.py -v`
Expected: the five new tests FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

Append to `LayoutQueryMixin`:

```python
    # ── snapshot & rows ──────────────────────────────────────────────────
    async def load_project_snapshot(self, project_id: str):
        from src.database.queries.hierarchy_queries import CONTAINER_KEY, CONTAINER_VALUE
        from src.database.tables import task_dependencies, task_metadata, tasks
        from src.task_graph.layout.model import SnapTask

        async with self._engine.begin() as conn:
            trows = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.parent_task_id, tasks.c.status,
                           tasks.c.created_at, tasks.c.title)
                    .where(tasks.c.project_id == project_id)
                )
            ).fetchall()
            ids = [r[0] for r in trows]
            containers = set()
            if ids:
                containers = {
                    r[0] for r in (
                        await conn.execute(
                            select(task_metadata.c.task_id).where(
                                task_metadata.c.task_id.in_(ids),
                                task_metadata.c.key == CONTAINER_KEY,
                                task_metadata.c.value == CONTAINER_VALUE,
                            )
                        )
                    ).fetchall()
                }
            edges = []
            if ids:
                edges = [
                    (r[0], r[1], r[2]) for r in (
                        await conn.execute(
                            select(task_dependencies.c.task_id,
                                   task_dependencies.c.depends_on_task_id,
                                   task_dependencies.c.dep_type)
                            .where(task_dependencies.c.task_id.in_(ids))
                        )
                    ).fetchall()
                ]
        snap = {
            r[0]: SnapTask(id=r[0], parent_id=r[1], is_container=r[0] in containers,
                           status=r[2], created_at=r[3], title=r[4] or "")
            for r in trows
        }
        return snap, edges

    @staticmethod
    def _row_from_mapping(m) -> "LayoutRow":
        from src.task_graph.layout.model import LayoutRow
        return LayoutRow(
            task_id=m["task_id"], container_id=m["container_id"], path=m["path"],
            depth=m["depth"], rank=m["rank"], order_key=m["order_key"], w=m["w"], h=m["h"],
            rel_x=m["rel_x"], rel_y=m["rel_y"], abs_x=m["abs_x"], abs_y=m["abs_y"],
            kind=m["kind"], agg_children=m["agg_children"], agg_descendants=m["agg_descendants"],
            agg_completed=m["agg_completed"], agg_running=m["agg_running"],
            agg_blocked=m["agg_blocked"], agg_active=m["agg_active"],
        )

    async def load_layout_rows(self, project_id, variant, task_ids):
        from src.database.tables import task_layouts
        ids = list(task_ids)
        if not ids:
            return {}
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    task_layouts.c.task_id.in_(ids),
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_children_layout_rows(self, project_id, variant, container_id):
        from src.database.tables import task_layouts
        cond = (task_layouts.c.container_id == container_id) if container_id is not None \
            else task_layouts.c.container_id.is_(None)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant, cond,
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_cells(self, project_id, variant, task_ids) -> dict[str, list[tuple[int, int]]]:
        from src.database.tables import task_layout_cells as cells
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
                .where(cells.c.project_id == project_id, cells.c.variant == variant,
                       cells.c.task_id.in_(list(task_ids)))
                .order_by(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
            )
            out: dict[str, list[tuple[int, int]]] = {}
            for t, x, y in res.fetchall():
                out.setdefault(t, []).append((x, y))
            return out

    async def subtree_aggregates(self, project_id, variant, path_prefix) -> dict:
        from src.database.tables import task_layouts, tasks
        from src.task_graph.layout.constants import FINISHED_STATUSES, RUNNING_STATUSES
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(tasks.c.status, tasks.c.is_blocked, task_layouts.c.path)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(task_layouts.c.project_id == project_id,
                       task_layouts.c.variant == "all",
                       task_layouts.c.path.like(path_prefix + "%"),
                       task_layouts.c.path != path_prefix)
            )
            rows = res.fetchall()
        depth = path_prefix.count("/")
        return {
            "children": sum(1 for r in rows if r[2].count("/") == depth + 1),
            "descendants": len(rows),
            "completed": sum(1 for r in rows if r[0] in FINISHED_STATUSES),
            "running": sum(1 for r in rows if r[0] in RUNNING_STATUSES),
            "blocked": sum(1 for r in rows if r[1]),
            "active": sum(1 for r in rows if r[0] not in FINISHED_STATUSES),
        }

    # ── publish ─────────────────────────────────────────────────────────
    async def publish_layout(self, project_id, variant, write_set, *, consumed_seq,
                             extent, node_count_delta) -> int:
        from sqlalchemy.dialects import postgresql, sqlite
        from src.database.tables import task_layout_cells as cells, task_layouts
        from src.task_graph.layout.flow import cells_for_box

        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            meta = (
                await conn.execute(
                    select(project_layout_meta).where(
                        project_layout_meta.c.project_id == project_id,
                        project_layout_meta.c.variant == variant,
                    ).with_for_update() if dialect == "postgresql" else
                    select(project_layout_meta).where(
                        project_layout_meta.c.project_id == project_id,
                        project_layout_meta.c.variant == variant,
                    )
                )
            ).mappings().first()

            # deletes
            if write_set.deletes:
                await conn.execute(delete(task_layouts).where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant,
                    task_layouts.c.task_id.in_(write_set.deletes)))
                await conn.execute(delete(cells).where(
                    cells.c.project_id == project_id, cells.c.variant == variant,
                    cells.c.task_id.in_(write_set.deletes)))

            # upserts
            touched: list[tuple[str, float, float, float, float]] = []
            for r in write_set.upserts:
                vals = {
                    "project_id": project_id, "variant": variant, "task_id": r.task_id,
                    "container_id": r.container_id, "path": r.path, "depth": r.depth,
                    "rank": r.rank, "order_key": r.order_key, "w": r.w, "h": r.h,
                    "rel_x": r.rel_x, "rel_y": r.rel_y, "abs_x": r.abs_x, "abs_y": r.abs_y,
                    "kind": r.kind, "agg_children": r.agg_children,
                    "agg_descendants": r.agg_descendants, "agg_completed": r.agg_completed,
                    "agg_running": r.agg_running, "agg_blocked": r.agg_blocked,
                    "agg_active": r.agg_active,
                }
                ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(task_layouts).values(**vals)
                upd = {k: v for k, v in vals.items() if k not in ("project_id", "variant", "task_id")}
                await conn.execute(ins.on_conflict_do_update(
                    index_elements=["project_id", "variant", "task_id"], set_=upd))
                touched.append((r.task_id, r.abs_x, r.abs_y, r.w, r.h))

            # translations
            for t in write_set.translations:
                await conn.execute(
                    update(task_layouts)
                    .where(task_layouts.c.project_id == project_id,
                           task_layouts.c.variant == variant,
                           task_layouts.c.path.like(t.path_prefix + "%"))
                    .values(abs_x=task_layouts.c.abs_x + t.dx, abs_y=task_layouts.c.abs_y + t.dy)
                )
                moved = await conn.execute(
                    select(task_layouts.c.task_id, task_layouts.c.abs_x, task_layouts.c.abs_y,
                           task_layouts.c.w, task_layouts.c.h)
                    .where(task_layouts.c.project_id == project_id,
                           task_layouts.c.variant == variant,
                           task_layouts.c.path.like(t.path_prefix + "%"))
                )
                touched.extend(tuple(m) for m in moved.fetchall())

            # cells for every touched row
            if touched:
                ids = [t[0] for t in touched]
                await conn.execute(delete(cells).where(
                    cells.c.project_id == project_id, cells.c.variant == variant,
                    cells.c.task_id.in_(ids)))
                crow = []
                for tid, x, y, w, h in touched:
                    for cx, cy in cells_for_box(x, y, w, h):
                        crow.append({"project_id": project_id, "variant": variant,
                                     "cell_x": cx, "cell_y": cy, "task_id": tid})
                # a task may appear twice in `touched` (upsert + translation); dedupe
                seen = set()
                crow = [c for c in crow if (c["task_id"], c["cell_x"], c["cell_y"]) not in seen
                        and not seen.add((c["task_id"], c["cell_x"], c["cell_y"]))]
                if crow:
                    await conn.execute(insert(cells), crow)

            # meta
            count = (await conn.execute(
                select(func.count()).select_from(task_layouts).where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant)
            )).scalar_one()
            version = (meta["layout_version"] if meta else 0) + 1
            now = time.time()
            if meta:
                await conn.execute(update(project_layout_meta).where(
                    project_layout_meta.c.project_id == project_id,
                    project_layout_meta.c.variant == variant,
                ).values(layout_version=version, extent_w=extent[0], extent_h=extent[1],
                         node_count=count, updated_at=now))
            else:
                await conn.execute(insert(project_layout_meta).values(
                    project_id=project_id, variant=variant, layout_version=version,
                    extent_w=extent[0], extent_h=extent[1], node_count=count,
                    updated_at=now, reconciled_at=now))

            if consumed_seq is not None:
                await self.clear_layout_dirty(project_id, consumed_seq, conn=conn)
        return version
```

`node_count_delta` is accepted for interface stability but the count is always recomputed with one `COUNT(*)`; document that in the docstring.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_queries.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/layout_queries.py tests/task_graph/test_layout_queries.py
git commit -m "feat(layout): snapshot loads and atomic layout publish"
```

---

### Task 10: Driver — full project layout (backfill/Tidy path)

**Files:**
- Create: `src/task_graph/layout/driver.py`
- Test: `tests/task_graph/test_layout_driver.py`

**Interfaces:**
- Produces: `class LayoutDriver(db)` with
  - `async full_layout(project_id: str, variant: str, *, mode: Literal["tidy"] = "tidy") -> int` (new version). Loads the whole project, lays out every container bottom-up in a thread, computes aggregates, publishes.
  - Pure helper `build_full_write_set(snapshot, edges, variant, *, existing: dict[str, LayoutRow], mode) -> tuple[WriteSet, tuple[float,float]]` (runs in the thread).

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "drv.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def seed_epic(db, epic="e", n=3, completed=0):
    await db.create_task(Task(id=epic, project_id="p1", title=epic, description=""))
    kids = []
    for i in range(n):
        cid = f"{epic}-c{i}"
        await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(cid, epic, conn=conn)
        kids.append(cid)
    for cid in kids[:completed]:
        await db.transition_task(cid, TaskStatus.COMPLETED, force=True)
    return kids


async def test_full_layout_nests_children_inside_container(db):
    kids = await seed_epic(db, n=3)
    v = await LayoutDriver(db).full_layout("p1", "all")
    assert v == 1
    rows = await db.load_layout_rows("p1", "all", ["e", *kids])
    e = rows["e"]
    assert e.kind == "container" and e.depth == 0 and e.path == "/e/"
    for k in kids:
        r = rows[k]
        assert r.container_id == "e" and r.depth == 1 and r.path == f"/e/{k}/"
        assert e.abs_x <= r.abs_x and r.abs_x + r.w <= e.abs_x + e.w
        assert e.abs_y <= r.abs_y and r.abs_y + r.h <= e.abs_y + e.h
    assert e.agg_children == 3 and e.agg_descendants == 3 and e.agg_active == 3


async def test_active_variant_excludes_finished_and_stubs_finished_epics(db):
    await seed_epic(db, epic="done", n=2, completed=2)
    kids = await seed_epic(db, epic="live", n=2, completed=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    rows = await db.load_layout_rows("p1", "active", ["done", "done-c0", "live", *kids])
    assert rows["done"].kind == "stub" and "done-c0" not in rows
    assert rows["live"].kind == "container"
    assert kids[0] not in rows and kids[1] in rows


async def test_full_layout_places_top_level_dependents_below_blockers(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")
    await LayoutDriver(db).full_layout("p1", "all")
    rows = await db.load_layout_rows("p1", "all", ["a", "b"])
    assert rows["b"].rank == 1 and rows["b"].abs_y > rows["a"].abs_y


async def test_empty_project_publishes_empty_meta(db):
    v = await LayoutDriver(db).full_layout("p1", "all")
    meta = await db.get_layout_meta("p1", "all")
    assert v == 1 and meta["node_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_driver.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`src/task_graph/layout/driver.py`:

```python
"""Database driver for the layout engine (§4.4, §4.6, §4.7)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Literal

from src.task_graph.layout.constants import (
    FINISHED_STATUSES, RANKING_DEP_TYPES, ROOT, RUNNING_STATUSES, VARIANTS,
)
from src.task_graph.layout.engine import layout_container
from src.task_graph.layout.model import ContainerScope, LayoutRow, SnapTask, Translation, WriteSet

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
                present.add(t.id); stubs.add(t.id)
            elif t.status not in FINISHED_STATUSES:
                present.add(t.id)
        elif t.status not in FINISHED_STATUSES or (t.is_container and active_desc.get(t.id, 0) > 0):
            present.add(t.id)
    # a present node's ancestors must be present
    for tid in list(present):
        p = snapshot[tid].parent_id
        while p is not None and p not in present:
            present.add(p); stubs.discard(p)
            p = snapshot[p].parent_id
    # children of stubs are not present
    def prune(tid: str) -> None:
        for c in children_of.get(tid, ()):
            present.discard(c); prune(c)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_driver.py -v`
Expected: PASS (4 tests). The `place` helper mutates rows; `rel_rows` values are the same objects as `ws.upserts`, so the mutation is visible to publish.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/driver.py tests/task_graph/test_layout_driver.py
git commit -m "feat(layout): driver full layout for backfill and tidy"
```

---

### Task 11: Driver — incremental batch from dirty marks

**Files:**
- Modify: `src/task_graph/layout/driver.py`
- Test: `tests/task_graph/test_layout_driver.py` (append)

**Interfaces:**
- Produces: `LayoutDriver.process_dirty(project_id, *, min_age_seconds: float) -> dict[str, int | None]` returning the new version per variant (None if nothing to do). For each variant:
  1. Pop dirty rows. Map each dirty task id to its container (current parent) and, for reasons `parent.changed`, also the previous container recorded in the reason string as `parent.changed:<old_parent_or_->`.
  2. Build a `ContainerScope` for each dirty container from the snapshot and existing rows (children rows + sibling edges + container children's allocated sizes from their existing rows).
  3. Run `layout_container(mode="incremental")` in a thread. If the allocated size differs from the container's existing `(w, h)`, run the parent in `resize` mode, and so on up to the root.
  4. For each re-laid container, compute deltas for every child whose `(abs_x, abs_y)` changed and whose subtree exists: emit a `Translation(path_prefix, dx, dy)` for container children and direct upserts for the children rows themselves.
  5. Refresh aggregates for every ancestor of every dirty task via `subtree_aggregates`.
  6. Publish with `consumed_seq`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
async def test_incremental_adds_child_without_moving_siblings(db):
    kids = await seed_epic(db, n=3)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    before = await db.load_layout_rows("p1", "all", kids)
    await db.create_task(Task(id="e-new", project_id="p1", title="new", description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("e-new", "e", conn=conn)
        await db.mark_layout_dirty("p1", ["e-new"], "task.created", conn=conn)
    versions = await drv.process_dirty("p1", min_age_seconds=0)
    assert versions["all"] == 2
    after = await db.load_layout_rows("p1", "all", [*kids, "e-new", "e"])
    for k in kids:
        assert after[k].ordinal == before[k].ordinal
        assert (after[k].abs_x, after[k].abs_y) == (before[k].abs_x, before[k].abs_y)
    assert after["e-new"].container_id == "e" and after["e"].agg_children == 4
    assert await db.dirty_layout_projects() == []


async def test_incremental_growth_translates_later_top_level_siblings(db):
    # Epic "e" is first at root; "z" is a later root card. Grow "e" past its band.
    kids = await seed_epic(db, n=2)
    await db.create_task(Task(id="z", project_id="p1", title="z", description=""))
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    z_before = (await db.load_layout_rows("p1", "all", ["z"]))["z"]
    e_before = (await db.load_layout_rows("p1", "all", ["e"]))["e"]
    new_ids = []
    for i in range(12):  # enough to cross a growth band
        cid = f"e-x{i}"
        await db.create_task(Task(id=cid, project_id="p1", title=cid, description=""))
        async with db._engine.begin() as conn:
            await db.set_parent(cid, "e", conn=conn)
            await db.mark_layout_dirty("p1", [cid], "task.created", conn=conn)
        new_ids.append(cid)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", ["e", "z", *kids, *new_ids])
    assert rows["e"].h > e_before.h or rows["e"].w > e_before.w
    assert rows["z"].ordinal == z_before.ordinal
    # z either stayed (same line) or translated; it never overlaps e.
    assert not (rows["z"].abs_x < rows["e"].abs_x + rows["e"].w and
                rows["z"].abs_y < rows["e"].abs_y + rows["e"].h and
                rows["z"].abs_x + rows["z"].w > rows["e"].abs_x and
                rows["z"].abs_y + rows["z"].h > rows["e"].abs_y)
    for k in kids:
        assert rows[k].abs_x >= rows["e"].abs_x and rows[k].abs_y >= rows["e"].abs_y


async def test_status_change_updates_active_variant_and_aggregates(db):
    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all"); await drv.full_layout("p1", "active")
    await db.transition_task(kids[0], TaskStatus.COMPLETED, force=True)
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", [kids[0]], "status.finished", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    active = await db.load_layout_rows("p1", "active", [*kids, "e"])
    assert kids[0] not in active and kids[1] in active
    assert active["e"].agg_completed == 1 and active["e"].agg_active == 1
    allv = await db.load_layout_rows("p1", "all", [*kids, "e"])
    assert kids[0] in allv and allv["e"].agg_completed == 1


async def test_parent_change_moves_subtree_between_containers(db):
    a_kids = await seed_epic(db, epic="a", n=1)
    await seed_epic(db, epic="b", n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.set_parent(a_kids[0], "b", conn=conn)
        await db.mark_layout_dirty("p1", [a_kids[0]], "parent.changed:a", conn=conn)
    await drv.process_dirty("p1", min_age_seconds=0)
    rows = await db.load_layout_rows("p1", "all", [a_kids[0], "a", "b"])
    assert rows[a_kids[0]].container_id == "b" and rows[a_kids[0]].path == f"/b/{a_kids[0]}/"
    assert rows["a"].agg_children == 0 and rows["b"].agg_children == 2


async def test_process_dirty_respects_debounce(db):
    await seed_epic(db, n=1)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["e"], "task.updated", conn=conn)
    assert (await drv.process_dirty("p1", min_age_seconds=3600)) == {"all": None, "active": None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_driver.py -v`
Expected: the five new tests FAIL with `AttributeError: 'LayoutDriver' object has no attribute 'process_dirty'`.

- [ ] **Step 3: Implement**

Append to `driver.py`:

```python
    # ── incremental ─────────────────────────────────────────────────────
    async def process_dirty(self, project_id: str, *, min_age_seconds: float) -> dict[str, int | None]:
        seq, marks = await self.db.pop_layout_dirty(project_id, min_age_seconds=min_age_seconds)
        out: dict[str, int | None] = {v: None for v in VARIANTS}
        if not marks:
            return out
        snapshot, edges = await self.db.load_project_snapshot(project_id)
        blocked = await self._blocked_ids(project_id)
        for variant in VARIANTS:
            if await self.db.get_layout_meta(project_id, variant) is None:
                # No layout yet: a full layout is the only correct answer.
                out[variant] = await self.full_layout(project_id, variant)
                continue
            out[variant] = await self._incremental(project_id, variant, snapshot, edges, blocked, marks, seq)
        return out

    async def _incremental(self, project_id, variant, snapshot, edges, blocked, marks, seq) -> int:
        from src.task_graph.layout.constants import HEADER_H, PADDING

        present, stubs = _visible(snapshot, variant)
        parent_of = {t.id: t.parent_id for t in snapshot.values()}

        # 1. dirty containers
        dirty: set[str | None] = set()
        dirty_tasks: set[str] = set()
        for tid, reason in marks:
            dirty_tasks.add(tid)
            if tid in snapshot:
                dirty.add(parent_of[tid])
            if reason.startswith("parent.changed:"):
                old = reason.split(":", 1)[1]
                dirty.add(None if old in ("", "-") else old)
            if tid in snapshot and snapshot[tid].is_container:
                dirty.add(tid)  # its own children may have become (in)visible
        # a dirty container that is not present in this variant collapses to its
        # nearest present ancestor
        norm: set[str | None] = set()
        for c in dirty:
            while c is not None and c not in present:
                c = parent_of.get(c)
            norm.add(c)
        dirty = norm

        # 2..6 in a thread over loaded scopes
        rank_edges: dict[str | None, list[tuple[str, str]]] = defaultdict(list)
        for d, b, typ in edges:
            if typ in RANKING_DEP_TYPES and d in present and b in present \
                    and parent_of[d] == parent_of[b]:
                rank_edges[parent_of[d]].append((d, b))
        children_of: dict[str | None, list[str]] = defaultdict(list)
        for tid in present:
            children_of[parent_of[tid]].append(tid)

        ws = WriteSet()
        processed: set[str | None] = set()
        # deepest first so a child container's new size is known before its parent
        def depth_of(c: str | None) -> int:
            d = 0
            while c is not None:
                c = parent_of[c]; d += 1
            return d
        queue: list[tuple[str | None, str]] = sorted(
            ((c, "incremental") for c in dirty), key=lambda x: -depth_of(x[0]))
        new_sizes: dict[str, tuple[float, float]] = {}

        async def drain_one(cid: str | None, mode: str) -> None:
            if cid in processed and mode == "incremental":
                return
            processed.add(cid)
            kids = children_of.get(cid, [])
            existing = await self.db.load_children_layout_rows(project_id, variant, cid)
            crow = (await self.db.load_layout_rows(project_id, variant, [cid]))[cid] if cid else None
            path = crow.path if crow else "/"
            depth = crow.depth + 1 if crow else 0
            origin = (crow.abs_x + PADDING, crow.abs_y + PADDING + HEADER_H) if crow else (0.0, 0.0)
            child_sizes = {}
            for k in kids:
                if snapshot[k].is_container and k not in stubs:
                    child_sizes[k] = new_sizes.get(k) or (
                        (existing[k].w, existing[k].h) if k in existing else (1.0, 1.0))
            scope = ContainerScope(
                container_id=cid, container_path=path, depth=depth,
                children={k: snapshot[k] for k in kids},
                existing={k: r for k, r in existing.items() if k in snapshot},
                sibling_edges=rank_edges.get(cid, []), child_sizes=child_sizes,
                stub_ids=frozenset(s for s in stubs if s in kids), origin=origin,
            )
            res = await asyncio.to_thread(layout_container, scope, mode=mode, seed=self.seed)
            # rows that left this container (removed, archived, or hidden)
            for k in existing:
                if k not in res.rows and (k not in snapshot or k not in present):
                    ws.deletes.append(k)
            for k, r in res.rows.items():
                prev = existing.get(k)
                ws.upserts.append(r)
                if prev and r.kind == "container" and prev.kind == "container":
                    dx, dy = r.abs_x - prev.abs_x, r.abs_y - prev.abs_y
                    if dx or dy:
                        ws.translations.append(Translation(path_prefix=r.path, dx=dx, dy=dy))
            # propagate size
            if cid is not None:
                old = (crow.w, crow.h)
                if res.allocated != old:
                    new_sizes[cid] = res.allocated
                    queue.append((parent_of[cid], "resize"))
            else:
                ws.sizes[ROOT] = res.allocated

        while queue:
            queue.sort(key=lambda x: -depth_of(x[0]))
            cid, mode = queue.pop(0)
            await drain_one(cid, mode)

        # Removed tasks: anything with a row but not present must go, with its subtree.
        gone = [tid for tid in dirty_tasks if tid not in present]
        for tid in gone:
            rows = await self.db.load_layout_rows(project_id, variant, [tid])
            if tid in rows:
                sub = await self.db.load_subtree_ids(project_id, variant, rows[tid].path)
                ws.deletes.extend(sub)
        # Moved container subtrees: a dirty container whose path changed must have
        # its descendants re-laid under the new path. Enqueue it and drain again.
        by_id_now = {u.task_id: u for u in ws.upserts}
        moved: list[str] = []
        for tid in dirty_tasks:
            if tid not in present or not snapshot[tid].is_container or tid in stubs:
                continue
            if tid not in by_id_now:
                continue
            old_row = (await self.db.load_layout_rows(project_id, variant, [tid])).get(tid)
            if old_row is not None and old_row.path != by_id_now[tid].path:
                moved.append(tid)
        for tid in moved:
            queue.append((tid, "incremental"))
            processed.discard(tid)
        while queue:
            queue.sort(key=lambda x: -depth_of(x[0]))
            cid, mode = queue.pop(0)
            await drain_one(cid, mode)

        # 5. aggregates on every ancestor of every dirty task
        anc: set[str] = set()
        for tid in dirty_tasks:
            p = parent_of.get(tid)
            while p is not None:
                anc.add(p); p = parent_of.get(p)
        by_id = {u.task_id: u for u in ws.upserts}
        for a in anc:
            if a not in present:
                continue
            row = by_id.get(a) or (await self.db.load_layout_rows(project_id, variant, [a])).get(a)
            if row is None:
                continue
            agg = await self.db.subtree_aggregates(project_id, variant, row.path)
            row.agg_children, row.agg_descendants = agg["children"], agg["descendants"]
            row.agg_completed, row.agg_running = agg["completed"], agg["running"]
            row.agg_blocked, row.agg_active = agg["blocked"], agg["active"]
            if a not in by_id:
                ws.upserts.append(row); by_id[a] = row

        ws.deletes = sorted(set(ws.deletes) - set(by_id))
        meta = await self.db.get_layout_meta(project_id, variant)
        extent = ws.sizes.get(ROOT, (meta["extent_w"], meta["extent_h"]))
        return await self.db.publish_layout(
            project_id, variant, ws, consumed_seq=seq, extent=extent, node_count_delta=None
        )
```

One more query completes the driver. Add to `LayoutQueryMixin`:

```python
    async def load_subtree_ids(self, project_id, variant, path_prefix) -> list[str]:
        from src.database.tables import task_layouts
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id).where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant,
                    task_layouts.c.path.like(path_prefix + "%")))
            return [r[0] for r in res.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_driver.py -v`
Expected: PASS (9 tests). If `test_incremental_growth_translates_later_top_level_siblings` fails on overlap, the root `resize` pass is not being reached: confirm `res.allocated != old` compares tuples of floats produced by the same `band_up`, and that the root scope receives `new_sizes["e"]` through `child_sizes`.

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/driver.py src/database/queries/layout_queries.py tests/task_graph/test_layout_driver.py
git commit -m "feat(layout): incremental batches from durable dirty marks"
```

---

### Task 12: Dirty marks on every write path

**Files:**
- Modify: `src/database/queries/task_queries.py` (`_insert_task_row`, `_apply_transition`, `delete_task`)
- Modify: `src/database/queries/hierarchy_queries.py` (`set_parent`, `set_parent_bulk`)
- Modify: `src/database/queries/dependency_queries.py` (`add_dependency`, `remove_dependency`, `remove_all_dependencies_on`)
- Modify: `src/database/queries/archive_queries.py` (`archive_task`, and the restore method next to it)
- Test: `tests/task_graph/test_layout_dirty_marks.py`

**Interfaces:**
- Consumes: `mark_layout_dirty(project_id, task_ids, reason, *, conn)`.
- Reasons: `task.created`, `task.deleted`, `task.archived`, `task.restored`, `parent.changed:<old_parent_id or ->`, `dependency.changed`, `status.finished`, `status.reopened`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from src.database import Database
from src.models import Project, Task, TaskStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "dm.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def marks(db):
    _, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    return rows


async def test_create_marks(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    assert ("a", "task.created") in await marks(db)


async def test_set_parent_marks_with_old_parent(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c", "a", conn=conn)
        await db.set_parent("c", "b", conn=conn)
    m = await marks(db)
    assert ("c", "parent.changed:-") in m and ("c", "parent.changed:a") in m


async def test_dependency_marks_both_endpoints(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")
    m = await marks(db)
    assert ("a", "dependency.changed") in m and ("b", "dependency.changed") in m
    await db.remove_dependency("b", "a")
    assert ("b", "dependency.changed") in await marks(db)


async def test_status_marks_only_on_finished_boundary(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await marks(db)  # drain create mark
    await db.transition_task("a", TaskStatus.READY, force=True)
    assert await marks(db) == []
    await db.transition_task("a", TaskStatus.COMPLETED, force=True)
    assert ("a", "status.finished") in await marks(db)
    await db.transition_task("a", TaskStatus.READY, force=True)
    assert ("a", "status.reopened") in await marks(db)


async def test_delete_and_archive_mark(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="b", description=""))
    await marks(db)
    await db.delete_task("a")
    assert ("a", "task.deleted") in await marks(db)
    await db.transition_task("b", TaskStatus.COMPLETED, force=True)
    await marks(db)
    assert await db.archive_task("b")
    assert ("b", "task.archived") in await marks(db)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_dirty_marks.py -v`
Expected: FAIL on each assertion (no marks written).

- [ ] **Step 3: Add the marks**

In each write path, on the connection that owns the transaction, immediately after the row write:

`task_queries.py::_insert_task_row` (end of method):
```python
        await self.mark_layout_dirty(task.project_id, [task.id], "task.created", conn=conn)
```

`task_queries.py::_apply_transition`: after the status UPDATE succeeds and you know `old_status` and `new_status` (the method reads the pre-state; use the same variable it validates against), add:
```python
        from src.task_graph.layout.constants import FINISHED_STATUSES
        was = old_status.value in FINISHED_STATUSES
        now = new_status.value in FINISHED_STATUSES
        if was != now:
            await self.mark_layout_dirty(
                project_id, [task_id], "status.finished" if now else "status.reopened", conn=conn
            )
```
(`project_id` is read from the task row the method already loaded.)

`task_queries.py::delete_task`: inside the transaction, for every id in the deleted set, before the DELETE executes (so the project id is still readable):
```python
        await self.mark_layout_dirty(project_id, deleted_ids, "task.deleted", conn=connection)
```

`hierarchy_queries.py::set_parent`: after reading `task_row` (which has `parent_task_id`) and before the edge writes:
```python
        old_parent = task_row.parent_task_id or "-"
        await self.mark_layout_dirty(task_row.project_id, [task_id], f"parent.changed:{old_parent}", conn=conn)
```
`set_parent_bulk`: same per task, using each row's previous parent.

`dependency_queries.py::add_dependency` and `remove_dependency` (and `remove_all_dependencies_on` for each affected pair), inside their transactions, after the edge write; `parent-child` is delegated to `set_parent`, so only mark for other types:
```python
        if dep_type != "parent-child":
            await self.mark_layout_dirty(project_id, [task_id, depends_on], "dependency.changed", conn=conn)
```
(`project_id` comes from the task row the method already loads to validate the pair; if it does not load one, add `select(tasks.c.project_id).where(tasks.c.id == task_id)` on the same connection.)

`archive_queries.py::archive_task`: after `ids` is computed, `await self.mark_layout_dirty(project_id, ids, "task.archived", conn=conn)`; in the restore method, `"task.restored"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_dirty_marks.py -v && pytest tests/test_database.py tests/test_hierarchy*.py tests/test_dependency*.py -n auto -q`
Expected: PASS, and no regressions in the existing suites.

- [ ] **Step 5: Commit**

```bash
git add src/database/queries tests/task_graph/test_layout_dirty_marks.py
git commit -m "feat(layout): write durable layout dirty marks on every graph mutation"
```

---

### Task 13: Reconcile sweep

**Files:**
- Modify: `src/task_graph/layout/driver.py`
- Test: `tests/task_graph/test_layout_driver.py` (append)

**Interfaces:**
- Produces: `LayoutDriver.reconcile(project_id) -> int` returning the number of dirty marks it enqueued. Compares the `all` variant's rows to the snapshot: tasks with no row, rows with no task, rows whose `container_id` differs from the task's parent, and rows whose `kind` disagrees with the container flag. Each discrepancy writes a mark with reason `reconcile`, then `reconciled_at` is stamped on both variants' meta rows.

- [ ] **Step 1: Write the failing test**

```python
async def test_reconcile_repairs_a_deleted_row(db):
    kids = await seed_epic(db, n=2)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all"); await drv.full_layout("p1", "active")
    from sqlalchemy import delete
    from src.database.tables import task_layouts
    async with db._engine.begin() as conn:
        await conn.execute(delete(task_layouts).where(task_layouts.c.task_id == kids[0]))
    assert await drv.reconcile("p1") == 1
    await drv.process_dirty("p1", min_age_seconds=0)
    assert kids[0] in await db.load_layout_rows("p1", "all", kids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/task_graph/test_layout_driver.py::test_reconcile_repairs_a_deleted_row -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

```python
    async def reconcile(self, project_id: str) -> int:
        import time
        from sqlalchemy import update
        from src.database.tables import project_layout_meta

        snapshot, _ = await self.db.load_project_snapshot(project_id)
        rows = await self.db.load_layout_rows(project_id, "all", list(snapshot))
        all_rows = await self.db.load_subtree_rows(project_id, "all")  # every row in the variant
        bad: set[str] = set()
        for tid, t in snapshot.items():
            r = rows.get(tid)
            if r is None or r.container_id != t.parent_id or \
                    (r.kind == "container") != t.is_container:
                bad.add(tid)
        for tid in all_rows:
            if tid not in snapshot:
                bad.add(tid)
        if bad:
            async with self.db._engine.begin() as conn:
                await self.db.mark_layout_dirty(project_id, sorted(bad), "reconcile", conn=conn)
        async with self.db._engine.begin() as conn:
            await conn.execute(update(project_layout_meta).where(
                project_layout_meta.c.project_id == project_id).values(reconciled_at=time.time()))
        return len(bad)
```

Add `load_subtree_rows(project_id, variant) -> dict[str, LayoutRow]` to the mixin (a `select(task_layouts)` filtered by project and variant, mapped with `_row_from_mapping`).

Note: a `reconcile` mark for a task whose row is missing must make the driver lay it out. In `_incremental`, a dirty task with no existing row is simply "new" to its container's scope, which `layout_container` already handles; a stale row for a deleted task is covered by the `gone` branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_driver.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/task_graph/layout/driver.py src/database/queries/layout_queries.py tests/task_graph/test_layout_driver.py
git commit -m "feat(layout): reconcile sweep enqueues repairs for drifted rows"
```

---

### Task 14: Config section

**Files:**
- Modify: `src/config.py` (new dataclass near `SwarmConfig`; field on `AppConfig`; parse in the loader next to `config.swarm = SwarmConfig(` around line 2423)
- Test: `tests/test_config.py` (append; find the existing file with `ls tests/test_config*.py`)

**Interfaces:**
- Produces: `AppConfig.graph_layout: GraphLayoutConfig` with `enabled: bool = False`, `reconcile_interval_seconds: int = 900`, `incremental_debounce_ms: int = 500`, `tidy_job_budget_seconds: int = 60`, read from YAML key `dashboard.graph_layout`.

- [ ] **Step 1: Write the failing test**

```python
def test_graph_layout_config_defaults_and_parse(tmp_path):
    from src.config import load_config
    p = tmp_path / "c.yaml"
    p.write_text("discord:\n  bot_token: t\n  guild_id: '1'\ndashboard:\n  graph_layout:\n    enabled: true\n    incremental_debounce_ms: 250\n")
    cfg = load_config(str(p))
    assert cfg.graph_layout.enabled is True
    assert cfg.graph_layout.incremental_debounce_ms == 250
    assert cfg.graph_layout.reconcile_interval_seconds == 900
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_graph_layout_config_defaults_and_parse -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'graph_layout'`.

- [ ] **Step 3: Implement**

```python
@dataclass
class GraphLayoutConfig:
    """Server-side task graph layout (spatial-layout design §8). YAML: ``dashboard.graph_layout``."""

    enabled: bool = False
    reconcile_interval_seconds: int = 900
    incremental_debounce_ms: int = 500
    tidy_job_budget_seconds: int = 60

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for key in ("reconcile_interval_seconds", "incremental_debounce_ms", "tidy_job_budget_seconds"):
            if getattr(self, key) < 0:
                errors.append(ConfigError("dashboard.graph_layout", key, "must be >= 0"))
        return errors
```

Add `graph_layout: GraphLayoutConfig = field(default_factory=GraphLayoutConfig)` to `AppConfig`, and in the loader, next to the swarm block:

```python
        gl = (data.get("dashboard") or {}).get("graph_layout") or {}
        config.graph_layout = GraphLayoutConfig(
            enabled=bool(gl.get("enabled", False)),
            reconcile_interval_seconds=int(gl.get("reconcile_interval_seconds", 900)),
            incremental_debounce_ms=int(gl.get("incremental_debounce_ms", 500)),
            tidy_job_budget_seconds=int(gl.get("tidy_job_budget_seconds", 60)),
        )
```

Include `config.graph_layout.validate()` wherever the other sections' `validate()` results are collected.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v -k graph_layout`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(layout): dashboard.graph_layout config section"
```

---

### Task 15: Orchestrator step

**Files:**
- Create: `src/orchestrator/layout_step.py`
- Modify: `src/orchestrator/core.py` (import mixin, add to `Orchestrator` bases next to `PoolsMixin`; call `await self._run_layout_step()` in Phase 3 right after `await self._auto_archive_tasks()`)
- Test: `tests/task_graph/test_layout_step.py`

**Interfaces:**
- Produces: `LayoutStepMixin._run_layout_step()`: no-op unless `self.config.graph_layout.enabled`. Otherwise: (a) run one queued job if any (`next_layout_job` → `full_layout` → `finish_layout_job`), (b) for each dirty project call `process_dirty` with the configured debounce, (c) for each project whose `all` meta `reconciled_at` is older than the interval, call `reconcile`. Wrap each project in try/except; on the third consecutive failure for a project, enqueue a `tidy` job and log at error level. Never raise.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from src.models import Project, Task


async def test_layout_step_processes_dirty_and_jobs(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await o._run_layout_step()  # no meta yet → full layout via process_dirty
    assert (await o.db.get_layout_meta("p1", "all"))["node_count"] == 1
    job = await o.db.enqueue_layout_job("p1", "all", "tidy")
    await o._run_layout_step()
    assert (await o.db.get_layout_job(job["id"]))["status"] == "done"


async def test_layout_step_is_noop_when_disabled(orchestrator_factory):
    o = await orchestrator_factory()
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await o._run_layout_step()
    assert await o.db.get_layout_meta("p1", "all") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/task_graph/test_layout_step.py -v`
Expected: FAIL with `AttributeError: ... '_run_layout_step'`.

- [ ] **Step 3: Implement**

`src/orchestrator/layout_step.py`:

```python
"""Orchestrator cycle step for the task graph layout driver (§4.6)."""

from __future__ import annotations

import logging
import time

from src.task_graph.layout.driver import LayoutDriver

logger = logging.getLogger(__name__)


class LayoutStepMixin:
    _layout_failures: dict[str, int]

    async def _run_layout_step(self) -> None:
        cfg = getattr(self.config, "graph_layout", None)
        if not cfg or not cfg.enabled:
            return
        if not hasattr(self, "_layout_failures"):
            self._layout_failures = {}
        driver = LayoutDriver(self.db)

        job = await self.db.next_layout_job()
        if job:
            try:
                await driver.full_layout(job["project_id"], job["variant"])
                await self.db.finish_layout_job(job["id"], error=None)
            except Exception as exc:  # noqa: BLE001
                logger.error("layout job %s failed: %s", job["id"], exc)
                await self.db.finish_layout_job(job["id"], error=str(exc))

        for pid in await self.db.dirty_layout_projects():
            try:
                await driver.process_dirty(pid, min_age_seconds=cfg.incremental_debounce_ms / 1000)
                self._layout_failures.pop(pid, None)
            except Exception as exc:  # noqa: BLE001
                n = self._layout_failures.get(pid, 0) + 1
                self._layout_failures[pid] = n
                logger.warning("layout batch for %s failed (%d): %s", pid, n, exc)
                if n >= 3:
                    logger.error("layout for %s failed 3 times; enqueuing tidy", pid)
                    for variant in ("all", "active"):
                        await self.db.enqueue_layout_job(pid, variant, "tidy")
                    self._layout_failures.pop(pid, None)

        cutoff = time.time() - cfg.reconcile_interval_seconds
        for project in await self.db.list_projects():
            meta = await self.db.get_layout_meta(project.id, "all")
            if meta and (meta.get("reconciled_at") or 0) < cutoff:
                try:
                    await driver.reconcile(project.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("layout reconcile for %s failed: %s", project.id, exc)
```

Wire into `core.py`: import, add `LayoutStepMixin` to the class bases, and add in Phase 3 after `_auto_archive_tasks`:

```python
            # Task graph layout: consume durable dirty marks, run one job, reconcile.
            await self._run_layout_step()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_layout_step.py tests/test_orchestrator.py -n auto -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/layout_step.py src/orchestrator/core.py tests/task_graph/test_layout_step.py
git commit -m "feat(layout): orchestrator cycle step drives layout batches and jobs"
```

---

### Task 16: Commands — `graph_layout_rebuild` and `graph_tidy`

**Files:**
- Create: `src/commands/graph_commands.py`
- Modify: `src/commands/handler.py` (import and add `GraphCommandsMixin` to bases)
- Modify: `src/tools/definitions.py` (category map entries `"graph_layout_rebuild": "graph"`, `"graph_tidy": "graph"`, plus two tool definitions)
- Test: `tests/task_graph/test_graph_commands.py`

**Interfaces:**
- Produces: `_cmd_graph_layout_rebuild(args: {project_id})` → runs `full_layout` for both variants synchronously and returns `{"success": True, "versions": {...}}`; `_cmd_graph_tidy(args: {project_id, variant?})` → enqueues jobs (both variants when omitted) and returns `{"success": True, "jobs": [...]}`. Exposed via CLI as `aq graph layout-rebuild` and `aq graph tidy` through the auto-command generator.

- [ ] **Step 1: Write the failing test**

```python
from src.models import Project, Task


async def test_graph_layout_rebuild_and_tidy(command_handler_factory):
    h = await command_handler_factory()
    await h._db.create_project(Project(id="p1", name="P1"))
    await h._db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    r = await h.execute("graph_layout_rebuild", {"project_id": "p1"})
    assert r["success"] and r["versions"] == {"all": 1, "active": 1}
    r = await h.execute("graph_tidy", {"project_id": "p1"})
    assert r["success"] and {j["variant"] for j in r["jobs"]} == {"all", "active"}
    r = await h.execute("graph_layout_rebuild", {"project_id": "nope"})
    assert r["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/task_graph/test_graph_commands.py -v`
Expected: FAIL (unknown command).

- [ ] **Step 3: Implement**

`src/commands/graph_commands.py`:

```python
"""``graph_layout_rebuild`` / ``graph_tidy`` (spatial-layout design §5.6, §10)."""

from __future__ import annotations

from src.task_graph.layout.constants import VARIANTS
from src.task_graph.layout.driver import LayoutDriver


class GraphCommandsMixin:
    async def _cmd_graph_layout_rebuild(self, args: dict) -> dict:
        pid = args.get("project_id")
        if not pid or await self.db.get_project(pid) is None:
            return {"success": False, "error": f"No project '{pid}'"}
        driver = LayoutDriver(self.db)
        versions = {v: await driver.full_layout(pid, v) for v in VARIANTS}
        return {"success": True, "project_id": pid, "versions": versions}

    async def _cmd_graph_tidy(self, args: dict) -> dict:
        pid = args.get("project_id")
        if not pid or await self.db.get_project(pid) is None:
            return {"success": False, "error": f"No project '{pid}'"}
        variants = [args["variant"]] if args.get("variant") in VARIANTS else list(VARIANTS)
        jobs = [await self.db.enqueue_layout_job(pid, v, "tidy") for v in variants]
        return {"success": True, "project_id": pid, "jobs": jobs}
```

Tool definitions (append to the list in `src/tools/definitions.py`, mirroring the `formula_cook` entry's shape):

```python
    {
        "name": "graph_layout_rebuild",
        "description": "Rebuild the server-side task graph layout for a project (both variants) synchronously. Not available to agent sessions.",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "Project id"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "graph_tidy",
        "description": "Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project id"},
                "variant": {"type": "string", "enum": ["all", "active"], "description": "Omit for both"},
            },
            "required": ["project_id"],
        },
    },
```

Add `"graph_layout_rebuild": "graph"` and `"graph_tidy": "graph"` to the category map near the `formula` entries. If the handler has a `db` property already (check `grep -n "def db" src/commands/handler.py`), use it; otherwise use `self.orchestrator.db` in the mixin.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/task_graph/test_graph_commands.py tests/test_api_execute_contract.py -n auto -q`
Expected: PASS. If the execute-contract test complains about a missing response model, add a `GraphLayoutResponse(BaseModel)` with `success: bool`, `project_id: str | None = None`, `versions: dict[str, int] | None = None`, `jobs: list[dict] | None = None`, `error: str | None = None` to `src/api/models/graph.py` and register it the way that test's fixture expects.

- [ ] **Step 5: Commit**

```bash
git add src/commands/graph_commands.py src/commands/handler.py src/tools/definitions.py src/api/models/graph.py tests/task_graph/test_graph_commands.py
git commit -m "feat(layout): graph_layout_rebuild and graph_tidy commands"
```

---

### Task 17: PostgreSQL perf assertions

**Files:**
- Create: `tests/perf/test_layout_statements.py`
- Create: `scripts/seed_layout_perf.py`

**Interfaces:**
- Consumes: `tests/perf/conftest.py`'s `any_db` fixture (read it first: `sed -n 1,80p tests/perf/conftest.py`) and `tests/pg_dsn.py::ensure_worker_postgres_dsn`.
- Produces: a seed function `seed_project(db, project_id, *, epics=100, per_epic=40, big_epic=1000, hub_dependents=50)` reusable by Stage 2's API perf test.

- [ ] **Step 1: Write the seed script**

`scripts/seed_layout_perf.py`:

```python
"""Seed a project shaped for layout perf tests (spec §9): 100 epics with
nested packages, one 1,000-task epic, one hub with 50 dependents."""

from __future__ import annotations

import asyncio
import sys

from src.models import Project, Task, TaskStatus


async def seed_project(db, project_id: str, *, epics: int = 100, per_epic: int = 40,
                       big_epic: int = 1000, hub_dependents: int = 50) -> None:
    await db.create_project(Project(id=project_id, name=project_id))

    async def make(tid: str, parent: str | None, status=TaskStatus.DEFINED):
        await db.create_task(Task(id=tid, project_id=project_id, title=tid, description="", status=status))
        if parent:
            async with db._engine.begin() as conn:
                await db.set_parent(tid, parent, conn=conn)

    for e in range(epics):
        eid = f"epic{e}"
        await make(eid, None)
        n = big_epic if e == 0 else per_epic
        for p in range(max(1, n // 10)):
            pid = f"{eid}-pkg{p}"
            await make(pid, eid)
            for t in range(10 if n >= 10 else n):
                tid = f"{pid}-t{t}"
                await make(tid, pid, TaskStatus.COMPLETED if (t % 2 == 0 and e > 0) else TaskStatus.DEFINED)
                if t > 0:
                    await db.add_dependency(tid, f"{pid}-t{t-1}")
    await make("hub", None)
    for i in range(hub_dependents):
        await make(f"hubdep{i}", None)
        await db.add_dependency(f"hubdep{i}", "hub")


if __name__ == "__main__":
    from src.database import Database

    async def main():
        db = Database(sys.argv[1])
        await db.initialize()
        await seed_project(db, sys.argv[2] if len(sys.argv) > 2 else "perf")
        await db.close()

    asyncio.run(main())
```

- [ ] **Step 2: Write the perf test**

```python
"""Layout perf on PostgreSQL (spec §9). Skipped without POSTGRES_TEST_DSN."""

from __future__ import annotations

import time

import pytest

from scripts.seed_layout_perf import seed_project
from src.models import Task
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set")


@pytest.fixture
async def pg(any_db):
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await seed_project(any_db, "perf", epics=20, per_epic=40, big_epic=1000)
    yield any_db


async def test_full_layout_under_budget(pg):
    drv = LayoutDriver(pg)
    t0 = time.perf_counter()
    await drv.full_layout("perf", "all")
    assert time.perf_counter() - t0 < 60.0


async def test_incremental_batch_of_ten_under_200ms(pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all"); await drv.full_layout("perf", "active")
    for i in range(10):
        tid = f"epic5-pkg0-new{i}"
        await pg.create_task(Task(id=tid, project_id="perf", title=tid, description=""))
        async with pg._engine.begin() as conn:
            await pg.set_parent(tid, "epic5-pkg0", conn=conn)
    t0 = time.perf_counter()
    await drv.process_dirty("perf", min_age_seconds=0)
    assert time.perf_counter() - t0 < 0.2


async def test_root_band_crossing_publish_under_1s(pg):
    drv = LayoutDriver(pg)
    await drv.full_layout("perf", "all"); await drv.full_layout("perf", "active")
    # Grow epic0 (already the biggest) by a package of 60 tasks to force a root reflow.
    await pg.create_task(Task(id="epic0-pkgX", project_id="perf", title="x", description=""))
    async with pg._engine.begin() as conn:
        await pg.set_parent("epic0-pkgX", "epic0", conn=conn)
    for t in range(60):
        tid = f"epic0-pkgX-t{t}"
        await pg.create_task(Task(id=tid, project_id="perf", title=tid, description=""))
        async with pg._engine.begin() as conn:
            await pg.set_parent(tid, "epic0-pkgX", conn=conn)
    t0 = time.perf_counter()
    await drv.process_dirty("perf", min_age_seconds=0)
    assert time.perf_counter() - t0 < 1.0
```

- [ ] **Step 3: Run**

Run: `POSTGRES_TEST_DSN=postgresql+asyncpg://aq:aq@localhost:5533/aq_test pytest tests/perf/test_layout_statements.py -v` (use the DSN from `docs/guides/e2e-swarm.md` if it differs).
Expected: PASS. If the incremental test misses 200 ms, the driver is re-loading the whole snapshot per variant; hoist `load_project_snapshot` out of the variant loop (it already is in `process_dirty`) and check `subtree_aggregates` is called once per ancestor, not per dirty task.

- [ ] **Step 4: Commit**

```bash
git add scripts/seed_layout_perf.py tests/perf/test_layout_statements.py
git commit -m "test(layout): postgres perf assertions and seed script"
```

---

### Task 18: Stage wrap-up

- [ ] **Step 1: Full suite**

Run: `pytest tests/ -n auto -q`
Expected: PASS.

- [ ] **Step 2: Ruff**

Run: `ruff check src tests scripts && ruff format --check src/task_graph/layout src/database/queries/layout_queries.py`
Expected: clean.

- [ ] **Step 3: Update docs**

Add to `CLAUDE.md` Quick Reference a line under **Workflows**:

```
- **Graph layout:** `src/task_graph/layout/` (engine: layering, flow, cost, engine; `driver.py` consumes durable `layout_dirty` marks in the orchestrator cycle and publishes `task_layouts`/`task_layout_cells`/`project_layout_meta` atomically). Commands `graph_layout_rebuild`, `graph_tidy`. Off by default (`dashboard.graph_layout.enabled`). Spec: `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md`.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document task graph layout engine"
```

---

## Self-review against the spec

- §3.1 containers from metadata: Task 9 snapshot. Empty container keeps kind container: engine `_kind`. Root as container: `ContainerScope(container_id=None)`.
- §3.2 units and bands: Task 2 constants, Task 4 flow.
- §3.3 variants and stubs: Task 10 `_visible`.
- §3.4 immutable ordinals, forced repair, bands: Task 6 tests 2 and 4, Task 11 growth test.
- §4.2–4.3 cost, moves, sibling cap: Tasks 5, 6.
- §4.4 steps 1–8: Task 11. Step 7 aggregates: `subtree_aggregates`.
- §4.5 worst case pinned: Task 17 root band crossing.
- §4.6 durable marks, atomic publish, `FOR UPDATE` on PostgreSQL, reconcile, Tidy serialization: Tasks 8, 9, 12, 13, 15. Serialization is by the single orchestrator step running jobs and batches sequentially; the PostgreSQL row lock guards a second daemon.
- §4.7 Tidy: Tasks 7, 10, 16.
- §4.10 tables: Task 1.
- §8 config: Task 14.
- §9 engine/driver/perf tests: Tasks 6, 7, 11, 12, 13, 17. API and dashboard tests belong to Stages 2 and 3.
- §10 step 1 rollout: Tasks 15, 16.

Known simplification to record in the Stage 2 plan: `layout_container` in `tidy` mode does not implement the slack-shift move; ranks stay minimal. That satisfies every test here and can be added later without changing interfaces.
