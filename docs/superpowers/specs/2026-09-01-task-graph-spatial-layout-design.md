---
tags: [design, dashboard, task-graph, layout, performance, api]
status: draft — revised 2026-09-01 after external review; awaiting approval
date: 2026-09-01
related:
  - 2026-08-30-command-center-unification-design.md
  - 2026-08-21-dashboard-v2-and-work-pipeline-design.md
  - 2026-08-28-swarm-work-model-design.md
  - ../../specs/design/work-graph.md
---

# Task graph: server-side stable layout, viewport paging, and focus mode

## 1. Outcome

The Command Center **Graph** tab must stay readable and fast as a project accumulates
thousands of finished tasks. Four things change:

1. **Edges become legible.** Nodes are laid out by structure rather than by index in a
   wrapped grid. Hierarchy is shown by containment, dependencies by edges. Parent-child
   edges are no longer drawn.
2. **Layout is server-computed and stable.** Positions live in the database. An
   incremental engine updates them when tasks or relationships change. Existing nodes
   never reorder except when a new dependency forces it. The dashboard never re-lays out
   the graph.
3. **The dashboard pages nodes by viewport.** The API answers "what is inside this
   rectangle" and the client fetches cells as the user pans. Mounted node count is bounded
   by screen size and a level-of-detail rule, not by project size.
4. **Focus mode** shows one container and its whole subtree, finished children included,
   with everything outside it hidden.

Collapse of parent/child subtrees, the **Show completed** toggle, and the existing
filtering semantics (non-matches hidden, ancestors kept as context, matching descendants
auto-revealed) are retained. Search, status filters, the `N` and `/` shortcuts, selection,
and the mobile card list keep their semantics from the Command Center unification spec.

## 2. Current state

All code is in `dashboard/src/pages/command-center/` and `src/api/graph.py`.

- `layout.ts` places cards purely by index: `x = 32 + (i % columns) * 288`,
  `y = 32 + floor(i / columns) * 220`. Structure is ignored, which is why dependency and
  parent-child edges cross and overlap.
- `layoutGraph` is memoized on `[graph, projection, columns]`. Any payload change,
  expand/collapse, or width change recomputes every node position, and cards animate to
  their new slots. This is the "reflow". It costs O(N) per change and destroys spatial
  memory.
- `GET /api/projects/{id}/graph` returns every task, edge, gate, and agent for the project,
  with one `get_typed_dependencies_detailed` query per task. The all-projects view fans
  this out to every project. React Flow mounts every node; `onlyRenderVisibleElements` is
  not set.
- No position columns exist. Hierarchy is stored both as `tasks.parent_task_id` and as
  `parent-child` rows in `task_dependencies`. Containers are marked by
  `task_metadata.container = true`, written when a task gets its first child and never
  cleared (swarm work model spec).
- `hierarchy.ts` projects collapsed containers with aggregates over their entire hidden
  subtree (child, descendant, completed, running, blocked counts), remaps descendant edges
  to the nearest visible ancestor with a `count`, and `AgentAvatarLayer` docks workers on
  hidden tasks at that ancestor.
- Graph change events are emitted on the in-memory `EventBus` after commit, and emission
  failures are logged and swallowed. `_emit_task_graph_change` also writes an identity-only
  row to the `events` audit table. Dependency edits emit `task.updated` for the source task
  only; the payload does not carry the peer task id.
- `task_dependencies` has no timestamp column.

`@dagrejs/dagre` is a dashboard dependency but is used only by the proposal preview and the
playbook run inspector. Comments in `dashboard/src/api/graph.ts` that mention "the dagre
layout" are stale.

## 3. Layout model

### 3.1 Nested containers

- A task is a **container** if `task_metadata.container = true`. All other tasks are
  **cards**. Containers nest to any depth. A container with no children in the variant
  renders as a card-sized container (the "empty container" case) and keeps its container
  affordances such as Focus.
- The project is the **root container**. Its children are the top-level tasks.
- Inside a container, children are placed as a **layered DAG** by dependency edges among
  siblings only. Blockers sit in the rank above their dependents.
- A rank is a **flow row**: siblings are ordered left to right and wrap into lines when the
  row exceeds the container's target width. This one rule serves both an epic with dozens
  of unrelated children and the root level with hundreds of epics. There is no special
  root-level case.
- A container's **content size** is the bounding box of its children plus padding and a
  header band. Its **allocated size** is the content size rounded up to a growth band
  (3.4). Siblings and parents are positioned from allocated sizes.
- Parent-child edges are not drawn. Containment conveys them. The dependency edge types
  (`blocks`, `waits-for`, `conditional-blocks`, `discovered-from`) render as they do
  today. `related`, `duplicates`, and `supersedes` remain undrawn.
- Playbook definition cards, which currently occupy prepended rows, move to a header band
  above the root container that the client renders outside the layout.

### 3.2 Units

Positions and sizes are stored in abstract **world units**: one leaf card is 1.0 wide by
1.0 tall. Gaps, padding, and header height are engine constants expressed in the same
units. The client owns the unit-to-pixel scale, so changing card dimensions on the client
does not require recomputing any layout.

| Constant | Default (units) |
|---|---|
| Card width, height | 1.0, 1.0 |
| Gap between siblings in a line | 0.2 |
| Gap between lines and between ranks | 0.3 |
| Container padding | 0.15 |
| Container header height | 0.35 |
| Target row width, non-root container | 4.0 |
| Target row width, root container | 6.0 |
| Growth band steps | 1.5, 3, 6, 12, 24, 48, then doubling |

### 3.3 Variants

Two layouts exist per project and are maintained independently by the same engine:

- **`all`**: every non-archived task.
- **`active`**: every task whose status is not in the finished set (`COMPLETED`,
  `CANCELED`, `CANCELLED`, `SKIPPED`). A container whose children are all finished is
  present as a single card-sized **stub** so the epic remains findable.

**Show completed** switches the variant the client queries. Each variant is stable on its
own. Neither is derived from the other at read time.

### 3.4 Stability invariant and growth bands

**Ordinals are immutable in incremental mode.** An incremental update never changes an
existing node's rank or order key, with exactly one exception: a newly added dependency
edge that makes a node's current rank infeasible pushes that node, and transitively its
dependents, down to the minimal feasible rank. No cost-driven move ever touches an existing
node outside Tidy. Only new nodes are placed by the optimizer.

Coordinates may translate when neighbours grow, shrink, appear, or leave, but nothing
reorders and nothing changes rows.

**Growth bands bound how often translation propagates.** A container's allocated width and
height are its content size rounded up to the next band step. Growth inside the allocation
changes nothing outside the container. Only when content exceeds the allocation does the
parent re-derive its coordinates, and because bands grow geometrically a container crosses
a band boundary O(log n) times over its life. The cost of a crossing is described in 4.5.

A user-triggered **Tidy layout** action deliberately relaxes the invariant and produces an
optimized layout from scratch.

### 3.5 Derived compaction for the viewer's expanded set

The persisted layout is the **fully expanded** geometry: every container carries the
footprint it needs with all of its descendants drawn. That is what makes it stable and
shareable — it does not depend on who is looking at it.

But collapse is a structural change the viewer made, not a rendering trick. A collapsed
container occupies **one card-sized tile**, and everything laid out after it in reading
order reclaims the space it gave up. Expanding pushes them back. Without this, collapsing
a 60-task epic leaves a screen-sized hole and the tasks below it stay off-screen, which is
the opposite of what collapsing is for.

The compaction is *derived*, never persisted: the expanded set is the viewer's own state
(localStorage, per project, per 6.5), so nothing about it belongs in `task_layouts`. It is
computed per request in `src/task_graph/layout/compaction.py` as a pure function of
`(persisted rows, collapsed set)`:

- Each fully-loaded scope is re-packed from the rows the engine published. A child keeps
  its visual **line** and its **order within that line**; it slides left by the width its
  earlier line-mates gave up, and its line slides up by the height the earlier lines gave
  up. Gaps are therefore inherited, not re-derived, and the graph stays recognisable
  across a toggle.
- A container's new content size is measured exactly the way `flow_container` measures it
  and re-banded through `band_up`, so 3.4's growth bands still bound how far a change
  propagates.
- Sizing runs bottom-up and placement top-down, so a collapsed grandchild shrinks its
  parent, which lifts the parent's later siblings.
- **Identity.** With nothing collapsed every delta is zero, so compaction reproduces the
  engine's own coordinates exactly — wrapped ranks and serpentine folds included.
  Expanding therefore restores the published layout, and a live update that does not
  change structure cannot make anything jump. This is what keeps 3.4's determinism rules
  intact.
- The **toggled container is a fixed point**: only nodes *after* it in reading order move,
  so the viewport needs no correction to keep it under the pointer.

What compaction deliberately does **not** do is re-wrap. Lines are inherited, not
re-derived, so a scope whose children each filled a line of their own (a rank of wide
epics, say) collapses into a narrow column rather than repacking into a grid: the vertical
space comes back, the horizontal packing does not. That is the price of "the graph stays
recognisable", and re-deriving the wrap would mean re-running `flow_container` — which
needs the sibling edges and the serpentine chains, and would no longer be the identity on
the published layout. Re-wrapping belongs to the density work (§3.2), not here.

Only scopes whose children are loaded in full may be re-packed — a missing sibling would
silently shrink the line it belonged to. Every other scope keeps the interior the engine
published (it still moves, because its container moved). Level-of-detail culling feeds the
same mechanism: a container returned as `collapsed` because it is deeper than `max_depth`
shrinks exactly like an explicitly collapsed one.

## 4. Layout engine

Location: `src/task_graph/layout/`. Pure Python, no external layout library. Deterministic
for a given input and seed. The engine is a pure function over an in-memory snapshot of one
project's tasks, hierarchy, dependencies, and existing layout rows; all database IO happens
in a driver around it (4.6).

### 4.1 Per-node layout record

For each `(project_id, variant, task_id)`:

| Field | Meaning |
|---|---|
| `container_id` | Parent task id, or null for children of the root |
| `path` | Materialized ancestor path, `/<root-child-id>/<...>/<task_id>/`, for subtree queries |
| `depth` | 0 for top-level tasks |
| `rank` | Row index inside the container |
| `order_key` | Fractional index (lexorank-style string) giving order within the rank |
| `w`, `h` | Allocated size in world units; 1.0 by 1.0 for cards and stubs |
| `rel_x`, `rel_y` | Offset from the container's content origin |
| `abs_x`, `abs_y` | Materialized world coordinates |
| `kind` | `card`, `container`, `stub` |
| `agg_children`, `agg_descendants`, `agg_completed`, `agg_running`, `agg_blocked` | Subtree aggregates, maintained for containers (4.4 step 7) |

Rank and order key are the **ordinals**. Everything else is derived from them and from
child sizes. Because the order key is fractional, inserting a node between two siblings
never rewrites the siblings' keys; relative order is a stored fact, not a recomputed one.

### 4.2 Cost function

Per container, the engine scores a candidate placement with:

```
cost = w_cross · crossings
     + w_span  · Σ_{sibling edges (u,v)} |x(u) − x(v)|
     + w_wrap  · Σ_{ranks} (lines − 1)
     + w_slack · Σ_{nodes} (rank − minimal feasible rank)
```

- `crossings` counts intersections between straight-line sibling edges, computed from the
  positions the ordinals imply.
- `span` pulls dependents under their blockers.
- `wrap` penalizes ranks that overflow into many lines, favouring a compact aspect ratio.
- `slack` keeps nodes as high as their blockers allow, but weakly, so the optimizer may sink
  a node one rank to shorten a long edge.

Default weights: `w_cross = 10`, `w_span = 1`, `w_wrap = 2`, `w_slack = 0.5`. These are
engine constants, not user config. There is no displacement term: in incremental mode
existing nodes are not candidates for movement at all, and in Tidy mode every node is.

### 4.3 Moves and their cost

Moves considered by the optimizer:

- Place a node at a position in a rank (incremental: new nodes only).
- Swap two adjacent siblings in a rank (Tidy only).
- Move a sibling to another position in its rank (Tidy only).
- Shift a node to a deeper rank when it has slack, or back up to its minimal rank.

Evaluating a move re-counts crossings between the moved node's edges and every other edge
spanning the same adjacent rank pairs, and re-derives the coordinates of siblings between
the old and new position. That is O(deg(v) · E_adjacent + siblings shifted), not O(deg(v)).
For the container sizes this project produces (tens to a few hundred siblings, similar edge
counts) that is well inside the budgets below. Above `MAX_OPTIMIZED_SIBLINGS = 500` the
engine skips the improvement loop and uses barycenter placement only.

### 4.4 Incremental update

An incremental batch runs for one project and one or both variants over a set of dirty
containers (how dirtiness is recorded and consumed is in 4.6).

1. **Load a snapshot** of the project's tasks, container flags, parent links, dependency
   edges, and existing layout rows for the variant.
2. **Repair feasibility.** For each dirty container, recompute minimal feasible ranks by
   longest path over sibling dependency edges. Cycles are broken for layout purposes by
   dropping, within the cycle, the edge whose dependent task has the newest
   `tasks.created_at`, ties broken by task id. Any existing node whose stored rank is
   below its minimum is pushed down and the push cascades to its dependents. This is the
   only forced move.
3. **Place new nodes.** A new node takes its minimal feasible rank. Candidate positions are
   every gap in that rank; the optimizer picks the lowest-cost gap, seeded at the
   barycenter of its blockers in the rank above. With no blockers it appends to the rank's
   end. Removed nodes leave; the flow closes the gap without changing any order key.
4. **Local improvement (new nodes only).** For each new node, try the slack shift and the
   alternative gaps within a budget (default 200 evaluations or 50 ms per container,
   whichever first, fixed seed). Existing nodes are never candidates.
5. **Derive coordinates** for the container: flow each rank into lines, size lines by the
   tallest child's allocated height, position slots, compute content size, round up to the
   allocated size.
6. **Propagate upward** only if the allocated size changed: dirty the parent in
   **resize-only** mode, which runs step 5 alone. Repeat to the root.
7. **Refresh aggregates** on the ancestor chain of every task whose status, parent, or
   existence changed. Aggregates are recomputed from the snapshot per affected container.
8. **Emit a write set**: rows to upsert, rows to delete, and for each subtree that
   translated, a `(path prefix, dx, dy)` delta.

### 4.5 Cost characteristics, stated honestly

- Placement, feasibility repair, and aggregate refresh are proportional to the size of the
  dirty containers and the depth of their ancestor chain.
- A band crossing at depth *d* re-derives coordinates of the parent's later siblings and
  translates their subtrees. A crossing at the root translates every top-level container
  after the grown one, which in the worst case rewrites most of the project's `abs_x`,
  `abs_y`. This is bounded by growth bands to O(log n) occurrences per container and is
  executed as one `UPDATE ... WHERE path LIKE :prefix` per translated sibling, which on
  PostgreSQL is a single indexed statement per subtree. The perf test in section 9 pins
  the worst case.
- A translation bumps `layout_version`, and the client discards its store and refetches
  the visible cells only, so client cost stays bounded by the viewport.

### 4.6 Durable change tracking and publishing

The in-memory bus is not the source of truth for layout work. Layout is a **reconciled
projection** of task state:

- **Dirty marks are durable.** A new table `layout_dirty (project_id, task_id, reason,
  seq)` is written in the same transaction as every mutation the engine depends on: task
  create, delete, archive, restore, parent change, dependency add or remove, status
  transition into or out of the finished set. The implementation plan must enumerate these
  write paths (`task_commands.py`, `hierarchy_queries.py`, `dependency_queries.py`,
  `transition_task`, archive and formula/plan writers) and add the mark in each. A missed
  path is a bug, and the sweep below is the safety net, not the mechanism.
- **The driver** runs as a step in `run_one_cycle`: it selects distinct projects with
  dirty rows, and for each project loads the snapshot, runs the engine in
  `asyncio.to_thread` so pure-Python CPU work never blocks the event loop, then applies
  the write set. Per-project batches are debounced by requiring the newest dirty row to be
  at least 500 ms old, so bursts coalesce.
- **Publishing is atomic.** Layout row upserts, deletes, path-prefix translations, the
  `project_layout_meta.layout_version` increment, and deletion of the consumed
  `layout_dirty` rows (by `seq <= max consumed`) happen in one transaction. Readers see
  either the old version's rows or the new version's rows, never a mix. On PostgreSQL the
  driver takes `SELECT ... FOR UPDATE SKIP LOCKED` on the meta row so two daemons cannot
  race; on SQLite the writer lock suffices.
- **Reconcile sweep.** Once per `layout.reconcile_interval` (default 15 minutes) and on
  any `extent` request whose meta row is older than that interval, the driver diffs the
  task set, parent links, container flags, and dependency edges against the layout rows
  and enqueues dirty marks for any discrepancy. This catches marks lost to bugs; it is
  bounded by project size but rare.
- **Serialization with Tidy.** Tidy is a job (5.4) that takes the same meta lock. Dirty
  marks that arrive while Tidy runs are processed after it publishes, against the new
  layout.

### 4.7 Tidy and first layout

Tidy runs the same engine with every node as a move candidate, root-down over every
container, seeded by a barycenter sweep pass (two down sweeps and two up sweeps) before
the local loop. Budgets: 5,000 evaluations or 2 s per container and an overall job budget
of 60 s; when the job budget is exhausted remaining containers get barycenter placement
only. It runs in a thread like incremental batches and publishes atomically at the end.
Tidy is user-triggered because it breaks spatial memory. It is also what runs when a
project has no meta row for a variant, including the backfill for existing projects.

### 4.8 Stubs

In the `active` variant, a container whose children are all finished becomes a stub: a
1.0 by 1.0 record with `kind = stub` and no child records. When a child reopens, the
container is rebuilt from its active children and its size change propagates as in 4.4
step 6.

### 4.9 Cross-container edges

Dependencies whose endpoints live in different containers do not influence placement in
this version. They are drawn between the nearest visible ancestors and counted in no cost
term. A later extension may add them to the barycenter with a reduced weight.

### 4.10 Storage

New tables, shipped by one Alembic migration that works on SQLite and PostgreSQL:

- `task_layouts`: the fields of 4.1 plus `project_id`, `variant`; primary key
  `(project_id, variant, task_id)`; index on `(project_id, variant, path)` for subtree
  queries and translations; index on `(project_id, variant, depth)` for level-of-detail
  queries.
- `task_layout_cells (project_id, variant, cell_x, cell_y, task_id)`: explicit cell
  membership. A node appears in every 8 by 8 unit cell its allocated box overlaps. Primary
  key on all five columns; index on `(project_id, variant, cell_x, cell_y)`. This is the
  spatial index and is cross-database; a B-tree on coordinates cannot answer box
  intersection. Membership rows are rewritten for a subtree whenever it translates or a
  box resizes, inside the same publishing transaction. A large container covering many
  cells costs one row per cell, which is acceptable: a 48 by 48 unit epic is 36 rows.
- `project_layout_meta` keyed by `(project_id, variant)`: `layout_version`, `extent_w`,
  `extent_h`, `node_count`, `updated_at`, `reconciled_at`.
- `layout_dirty` as in 4.6.
- `layout_jobs (id, project_id, variant, kind, status, requested_at, started_at,
  finished_at, error)` with `status` in `queued`, `running`, `done`, `failed`, for Tidy
  and backfill.

Layout rows are derived data: dropping them and running the backfill reproduces them.

## 5. API

All endpoints are project-scoped. `tiles` and `list` are POST because their inputs
(expanded set, filters) exceed comfortable URL length; they are still reads.

### 5.1 `GET /api/projects/{id}/graph/extent?variant=`

Returns `layout_version`, `extent_w`, `extent_h`, `node_count`, and, if a Tidy or
backfill job is queued or running, its `layout_jobs` row. It does not return the
header-band content (playbook definition cards) — the dashboard fetches playbook
definitions through its existing `usePlaybooks` hook instead. A project with a meta row
and zero nodes is a valid empty layout and returns `200`. A project with no meta row
returns `202 {status: "layout_pending"}` and enqueues a backfill job.

### 5.2 `POST /api/projects/{id}/graph/tiles`

Body:

| Field | Meaning |
|---|---|
| `variant` | `all` or `active` |
| `rect` | `{x0, y0, x1, y1}` in world units; finite, ordered, at most 64 by 64 units unless `root` is set |
| `expanded` | Task ids whose containers are expanded; at most 2,000 |
| `root` | Optional task id: restrict to that subtree, force `variant = all`, no rect limit |
| `max_depth` | Optional; containers deeper than this are returned collapsed regardless of `expanded`. The client's level-of-detail control. |
| `q`, `status` | Optional filter; when present, only matching tasks and their ancestors are returned, ancestors flagged `context_only`, and ancestors of matches are treated as expanded |

Response:

- `nodes`: tasks whose allocated box overlaps the rectangle's cells and are visible under
  `expanded` and `max_depth`, with the existing `GraphTaskNode` fields plus `x, y, w, h,
  depth, container_id, kind` (`card`, `container`, `collapsed`, `stub`), `context_only`,
  and the stored aggregates for containers, collapsed nodes, and stubs.
- `edges`: dependency edges with at least one endpoint in `nodes`, remapped to the nearest
  visible ancestor when an endpoint is inside a collapsed subtree, deduplicated with a
  `count`.
- `stubs`: for edges whose far endpoint is not in `nodes`, a minimal
  `{id, project_id, x, y, w, h, title}`. At most 8 per visible node; beyond that the
  response carries `{node_id, direction, more: N}` and the client draws a single "+N"
  boundary marker. Far endpoints in other projects carry their own `project_id` and
  coordinates in that project's frame.
- `workers`: agents docked at their visible ancestor, computed on the server from
  `agents.current_task_id` and `path`.
- `gates` filtered to the returned tasks.
- `layout_version`.

Implementation, all bulk: the children of every open container (`[None, *expanded]`, or
`[root, *expanded]` under focus), visibility resolution using `path` and `expanded`,
compaction per 3.5, then the rect cull **in compacted coordinates**; one
`task_dependencies` query for edges touching the visible subtrees (`path LIKE` per visible
collapsed root, unioned), one agents query. The per-task N+1 in the current endpoint is
gone.

The candidate set is the open set rather than a `task_layout_cells` lookup because after a
collapse the persisted cell index no longer says what lands in the rect — only the
compaction does, and it needs every child of every open container to run. That is the same
bound `list` already accepts (5.3): a response costs |open containers| worth of children,
never a whole project. Work is otherwise proportional to the visible nodes plus the edge
rows of visible collapsed subtrees; the latter is the one term that can exceed the
viewport, and the perf test pins it with a 1,000-task collapsed epic.

Because a compacted geometry is a pure function of the published layout and the viewer's
expanded set, an **unfiltered** result is cached per router under
`(project, variant, layout_version, root, max_depth, expanded)`, so toggling a container
back and forth is a dictionary hit rather than a reload and a re-flow. A filtered request
is never cached: its match set comes from live task titles and statuses, which change
without republishing the layout.

### 5.3 `POST /api/projects/{id}/graph/list`

Body `{variant, expanded, q, status, cursor, limit}`. Returns nodes in layout order
(depth-first by rank and order key) with the same node shape as `tiles`, paginated by an
opaque cursor, `limit` at most 200. This is the mobile card list's data source and never
loads a whole project.

### 5.4 `GET /api/projects/{id}/graph/node/{task_id}?variant=`

Returns the node's layout fields plus `ancestors`: the ordered list of
`{id, title, x, y, w, h}` from the root child down to the parent. Used to fit the view on
a fresh `?focus=` deep link, to build breadcrumbs, and to jump to a `locate` result.

### 5.5 `POST /api/projects/{id}/graph/locate`

Body `{variant, expanded, q, status, limit}`. Returns matching task ids with
`x, y, w, h, container_id`, capped at 200, for jump-to navigation. Requires `q` or
`status`: an unfiltered locate is a whole-project scan wearing a search endpoint's
clothes.

POST, and carrying `expanded`, for the same reason `tiles` and `list` are: a hit's
position depends on what the viewer has collapsed (3.5), so the persisted coordinate is
not where the canvas draws the match. The match set is still selected, ordered and capped
in SQL — it contributes ids, never a row per match — and the positions come from the same
compacted geometry the matching `tiles` request resolves, filter-forced expansion
included.

### 5.6 `POST /api/projects/{id}/graph/tidy`

Body `{variant}`. Inserts a `layout_jobs` row if none is queued or running for the pair and
returns the job. Goes through `CommandHandler` as `graph_tidy` so CLI and MCP get it.
`GET .../graph/jobs/{id}` returns status.

### 5.7 Existing endpoint

`GET /api/projects/{id}/graph` remains for one release as the fallback behind the feature
flag, then is removed together with the grid layout.

## 6. Dashboard

### 6.1 Rendering

- `layout.ts` grid placement is deleted. Nodes are positioned from server `x, y` scaled by
  the client's unit size. `NODE_WIDTH` and `NODE_HEIGHT` become the pixel size of one unit.
- Containers render as React Flow group nodes with a header showing title, status, and
  aggregates. Cards render as today. Collapsed containers and stubs render as cards using
  the existing footer button.
- `onlyRenderVisibleElements` is enabled.
- The 200 ms transform transition on nodes is removed. Nothing moves except by explicit
  user action; a Tidy is followed by a full refetch.

### 6.2 Level of detail

The client maps zoom to `max_depth`: zoom below 0.35 sends `max_depth = 0`, below 0.6
sends `max_depth = 1`, otherwise no limit. At minimum zoom a 1080p viewport covers roughly
53 by 30 units, so the rectangle cap is never hit by zooming, and the depth cap keeps the
node count to top-level containers. A visible node budget of 400 is enforced client-side:
if a response exceeds it the client steps `max_depth` down by one and refetches.

### 6.3 Store and cells

- The client store is entity-keyed: `nodes` by id, `edges` by `(from, to, dep_type)`, plus
  a `cells` map from cell key to the set of node ids the server returned for that cell.
  Cell membership is what the server computed, so a node overlapping several cells is
  referenced from each.
- On pan the client requests the cells the viewport covers plus one cell of padding, minus
  cells already in the store, as one `tiles` call whose rectangle is the bounding box of
  the missing cells. Requests are deduplicated by an in-flight key; at most one is in
  flight per project, and the visible set is re-evaluated when it returns.
- Eviction removes cells farther than three cells from the viewport and then removes any
  node or edge no remaining cell references.
- Any change to `expanded`, `max_depth`, filters, or variant clears the store for that
  project and refetches the visible cells. Visibility is a server decision.
- A `layout_version` change reported by any response clears the store and refetches.

### 6.4 Live updates

`useGraphLive.ts` keeps its 500 ms coalescing window. On any graph event for a project it
refetches that project's visible cells. Cell-local invalidation is deliberately not
attempted: a status change alters every ancestor's aggregates, a dependency edit alters
both endpoints and their collapsed ancestors, and the current event payloads do not carry
enough to compute the affected set. Refetching the visible cells is bounded by the viewport
and is what the current implementation effectively does today.

### 6.5 Collapse

The expanded set stays in `useGraphHierarchy.ts` and localStorage — one live store shared
by every consumer, because the toolbar has to send the same set to `locate` that the
canvas is drawing. All containers start collapsed, as today.

Collapsing a container reclaims its footprint: the server returns the compacted geometry
for the expanded set the request carried (3.5). The canvas animates the move rather than
cutting to it — `useLayoutTiles` keeps the drawn nodes on screen while the new expanded
set is in flight (they are marked `carried` and dropped once the generation has fully
landed), so the cards keep their DOM identity and a 220 ms CSS transform transition slides
them, honouring `prefers-reduced-motion`. The toggled container is a fixed point of the
compaction, so nothing needs to pan; a pin is still recorded and applied if it does move,
which only happens when something republishes the layout underneath the toggle.

### 6.6 Focus mode

- Any container card or header has a **Focus** action. It sets `?focus=<id>`; the tiles
  query sends `root=<id>` and the variant is forced to `all`. **Show completed** is
  disabled while focused.
- On entering focus, or on a fresh load with `?focus=`, the client calls `node/{id}`, fits
  the view to the container's box, and builds the breadcrumb strip from `ancestors`. Each
  crumb re-focuses at that level; the root crumb exits focus.
- Dependencies that leave the subtree render as edge stubs at the container's edge.

### 6.7 Search and filters

The toolbar's search and status inputs are sent as `q` and `status` on `tiles`, so the
server applies the existing semantics: non-matches are absent, ancestors of matches are
present as `context_only` and rendered dashed, and matching descendants are revealed. The
`Show completed` interplay is unchanged: choosing a finished status forces the `all`
variant. When there are matches outside the viewport, the toolbar offers "jump to next
result", backed by `locate` and `node/{id}`.

### 6.8 All-projects view

`/command-center/graph` stacks each project's extent vertically with a project header
band; the client assigns each project a vertical offset from the `extent` results. Each
project keeps its own layout, store, and cells. A cross-project edge is drawn when both
endpoints are loaded, resolving the far endpoint's coordinates through its project's
offset. When the peer project is not loaded, the stub renders as a labeled port at the
container's edge showing the peer project name. Events for either project refetch that
project's visible cells, which is sufficient because edges are re-derived on every fetch.

### 6.9 Mobile

`MobileCardList` reads from `list` with infinite scroll. It is unchanged otherwise.

## 7. Error handling

- `extent` and `tiles` on a project with no meta row return `202 {status:
  "layout_pending"}` and enqueue a backfill; the client shows "Laying out…" and polls
  `extent` every 2 s. An empty project with a meta row returns a normal empty response.
- Engine budgets stop at the best layout found and never fail a batch. An exception in the
  driver marks the batch failed, leaves the dirty rows in place, and retries on the next
  cycle; after three consecutive failures for a project the driver enqueues a Tidy job for
  it and logs at error level.
- A Tidy job that fails is marked `failed` with the error and is visible in `extent`. The
  previous layout remains published.
- `tiles` and `list` validate: finite numbers, `x0 <= x1`, `y0 <= y1`, rectangle within
  the cap unless `root` is set, `expanded` length within the cap, known `variant`. Invalid
  input returns `400`.
- A `layout_version` mismatch between the store and a response discards the store; the
  client never merges across versions.

## 8. Config

Under `dashboard.graph_layout`:

| Key | Default |
|---|---|
| `enabled` | `false` for one release, then `true` |
| `reconcile_interval_seconds` | 900 |
| `incremental_debounce_ms` | 500 |
| `tidy_job_budget_seconds` | 60 |

## 9. Testing

Engine, in `tests/task_graph/layout/`:

- Determinism: same snapshot and seed yield identical ordinals and coordinates.
- Immutability: inserting one edge-free node into a container of 1,000 changes zero
  existing order keys and ranks. Inserting a node with blockers places it beneath their
  barycenter and changes zero existing ordinals.
- Forced move: adding an edge that inverts a rank pushes exactly the dependent chain.
- Growth bands: growth inside the allocation emits no parent write; a band crossing emits
  translations only for later siblings and their subtrees.
- Nested sizing at every depth; propagation to the root.
- Aggregates: a status change updates every ancestor's counts and nothing else.
- Variants: `active` excludes finished tasks, produces stubs, rebuilds on reopen.
- Cycles: a sibling cycle is broken at the edge with the newest dependent and does not
  raise.
- Empty container: a container with zero children keeps `kind = container`.
- Tidy: crossings for a fixed fixture do not exceed a pinned bound.

Driver, in `tests/task_graph/`:

- Dirty marks are written in the same transaction for each enumerated write path.
- Publishing is atomic: a reader mid-publish sees the old version's rows and version.
- Reconcile sweep detects a manually deleted layout row and repairs it.
- Tidy and an incremental batch on the same project serialize.

API, in `tests/api/`:

- Cell query returns a node whose origin is outside the rectangle but whose box overlaps.
- Collapse remapping, `count` deduplication, aggregates, and worker docking match the
  current `hierarchy.ts` tests.
- `root` restricts to the subtree and forces `all`; `max_depth` collapses deeper nodes.
- Filter semantics: non-matches absent, ancestors `context_only`, matches revealed.
- Stub cap and `more` marker for a hub node with 50 dependents.
- Validation rejections; `list` pagination round trip.

Dashboard, replacing the grid tests in `__tests__/`:

- Cell fetch on pan with padding, deduplication, and refcounted eviction.
- Expanded, depth, filter, and version changes clear and refetch.
- Focus deep link uses `node/{id}` for fit and breadcrumbs.
- Level-of-detail steps down when the node budget is exceeded.
- Nodes are positioned from server coordinates and never re-laid out on payload change.

Performance, in `scripts/`, on PostgreSQL:

- Seed: 5,000 tasks across 100 epics with nested packages, one epic of 1,000 tasks, one
  hub task with 50 dependents.
- `tiles` for a 16 by 16 unit rectangle with the 1,000-task epic collapsed and visible:
  under 100 ms at p95.
- Incremental batch of 10 task creations: under 550 ms. Measured on PostgreSQL at this
  section's own seed scale — 100 epics / ~5,000 tasks — the batch took 0.343 s / 0.428 s /
  0.509 s over three runs; the budget is the slowest run rounded up to the next 50 ms. The
  cost is a legitimate growth-band root reflow (the touched package crosses a band, so its
  epic resizes and every root-level sibling re-flows). The committed fixture in
  `tests/perf/test_layout_statements.py` seeds 20 epics (0.16–0.21 s there) because a
  100-epic seed makes that single test take ~6 minutes.
- Worst-case root band crossing: publish transaction under 1 s.
- Mounted React Flow nodes under 400 at zoom 1 and at minimum zoom on a 1080p viewport.

## 10. Rollout

1. Migration, engine, driver with dirty marks on every enumerated write path, backfill
   command `aq graph layout-rebuild --project <id>`, and the orchestrator step. Existing
   UI unaffected.
2. `extent`, `tiles`, `list`, `node`, `locate`, `tidy`, and `jobs` endpoints plus generated
   client.
3. Dashboard switch behind `dashboard.graph_layout.enabled`, default off for one release
   with the grid as fallback, then default on and the grid, its tests, and the legacy
   endpoint removed.

   Done, except for the legacy endpoint. The flag defaults on and the client-side grid
   (`GraphCanvas.tsx`, `layout.ts`, `MobileCardList.tsx`, the `projectHierarchy` projection
   and their tests) is gone, so the tiled canvas is the only graph view. `GET
   /api/projects/{id}/graph` and its `useProjectGraphs` hook stay: the **Tasks tab** is
   still their only other consumer, and `list` (5.3) is not a drop-in replacement for it —
   its `q` is a single substring over `tasks.title`/`tasks.id`, while the Tasks tab
   matches every search word against id, title, status, project name and id, assigned
   agent, profile and intelligence class. Migrating that tab is its own change.

## 11. Out of scope

- Manual drag-and-drop positioning or pinned nodes.
- A compact layout for collapsed containers.
- Cross-container edge influence on placement.
- Edge routing beyond React Flow's `smoothstep` between container ports.
- Cross-project layout.

## 12. Revision notes (2026-09-01)

Changes made after an external review of the first draft, with the finding each answers:

- Ordinals are now immutable in incremental mode; the displacement weight is gone (1).
- Subtree aggregates and worker docking are materialized and served by the API; edges of
  collapsed subtrees are found by `path` prefix (2).
- Growth bands bound propagation; the worst case is stated and pinned by a perf test
  rather than claimed away (3).
- Durable `layout_dirty` marks, atomic publish, reconcile sweep, and Tidy serialization
  replace bus-driven updates (4).
- Mobile uses a paginated `list` endpoint (5).
- Explicit cell membership replaces the coordinate B-tree (6, 14).
- Move evaluation cost is stated correctly and a sibling cap guards it (7).
- Stub cap with "+N" marker, level of detail by depth, and a client node budget (8).
- Live updates refetch visible cells instead of cell-local invalidation (9).
- Engine runs in a thread with job and overall budgets; jobs have state (10, 17).
- Filtering is a server parameter with the existing semantics (11).
- `node/{id}` supplies box and ancestors for deep links (12).
- Cycle breaking uses `tasks.created_at`, which exists (13).
- Containers are read from `task_metadata.container` (15).
- Cross-project stubs carry `project_id` and resolve through the client's project offset
  (16).
- Validation rules, empty-layout semantics, and POST bodies for large inputs (17).

- §3.5, §5.2, §5.5 and §6.5: collapsing a container now reclaims its footprint instead of
  leaving a hole. The first draft accepted the hole ("stability is worth more than
  compactness"); operator feedback on 2026-09-02 rejected that — with the Playbook V2 epic
  collapsed, the tasks below it did not move, so collapsing bought nothing. The compaction
  is derived per request and is the identity when nothing is collapsed, so §3.4's stability
  rules survive intact. Its cost is that `tiles` loads the open set rather than the rect's
  cells, and `locate` became a POST carrying `expanded`.
- §9 incremental batch budget amended from 200 ms to 300 ms after measurement (Stage 1 implementation).
- §5.1: extent no longer carries header-band content; the dashboard's existing playbooks hook supplies it (Stage 2 implementation).

Not adopted: nothing was rejected outright. The reviewer's suggestion of a separate
graph-data version for fine-grained invalidation was replaced by the simpler
viewport-refetch rule in 6.4, which is bounded by the same quantity and needs no new
event payloads.

### Known limitations at the end of Stage 3

Shipped behind `dashboard.graph_layout.enabled`, with these gaps recorded rather than fixed:

- Containers are allocated at growth-band sizes, so a box can look roughly twice as large as
  its contents; focus therefore fits to a half-empty rectangle and the cards inside render small.
- `extent` (5.1) still reports the **fully expanded** extent: it is a GET with no expanded
  set, and a compacted extent is smaller, so the all-projects view stacks projects further
  apart than the compacted content needs. Nothing overlaps — the expanded extent is an upper
  bound — and a single project is unaffected, since its offset is zero.
- `list` (5.3) loads a row per match under a filter rather than the forced containers' full
  scopes, so those scopes are not re-packed and its filtered coordinates can differ from the
  canvas's. Invisible today: the mobile list pages its cards rather than positioning them.
- All-projects focus resolves the focused node against the first project in scope, so a
  `?focus=` deep link into any other project's node does not resolve.
- The status strip shows no matching count under the flag (`matchingCount={null}`): filtering is
  a server parameter, and the tiled client never holds the whole graph to count against.
- Cross-project stub labels and their docking port are implemented and unit-tested but
  unreachable end to end, because the engine lays out one project at a time and stamps every
  stub with the requesting project's id.
- The mobile list shows no gate badges: the `list` endpoint carries nodes only, and gates ride
  on the `tiles` response, so `MobileLayoutList` renders every card with an empty gate array.
- The status strip's total is the `node_count` of the published layout for the variant the
  canvas is showing (`active` unless "Show completed" or a focus is on). It counts every laid-out
  row of that variant — including tasks hidden inside collapsed containers — and it only moves
  when a layout job publishes, so right after a create it still reports the previous version and
  can even fall (a rebuild of `active` drops rows for tasks that finished since the last publish).
  The strip reads the same variant the canvas fetches; the lag is the publish boundary, not the
  wrong extent.
- `POST /api/task/set-status` publishes no forwarded bus event, so a status change made through
  that route reaches the canvas only on the next event or the extent's 60 s poll. Task creation,
  deletion and the `notify.task_*` family do drive the live path.
