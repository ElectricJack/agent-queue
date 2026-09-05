# Dashboard performance investigation — task system, PostgreSQL, task graph

**Date:** 2026-09-04
**Goal:** make the dashboard feel snappy with thousands of tasks.
**Method:** code reading plus measurement against a throwaway PostgreSQL 18 database
(`aq_perfprobe` on the local `:5533` compose instance) seeded with two projects:

| project | shape | tasks | edges |
|---|---|---|---|
| `perf` | spec §9 shape: 100 epics × packages, one 1,000-task epic, hub with 50 dependents | 5,607 | 9,970 |
| `flat` | 5,000 root-level tasks, chain deps, 60 % COMPLETED, 800-char descriptions | 5,000 | 4,000 |

Statement counts come from a `before_cursor_execute` listener on the engine; timings are
medians of 3–5 runs after one warm-up. Scripts live in the session scratchpad
(`bench.py`, `bench2.py`, `bench3.py`); the seeded database was left in place for follow-up work.

## Headline numbers

| path | what the dashboard does with it | measured | statements |
|---|---|---|---|
| `GET /api/projects/{pid}/graph` (perf) | Command Center **task list** and every live graph refresh | **11.5 s**, 2.1 MB | 5,611 |
| `GET /api/projects/{pid}/graph` (flat) | same | **10.2 s**, 1.3 MB | 5,004 |
| same data, batched (prototype) | — | 53–68 ms | 2 |
| `_check_defined_tasks` legacy scan, 4,627 DEFINED | every 5 s orchestrator cycle | **9.1 s** | 4,627 |
| same, batched (prototype) | — | 41 ms | 1 |
| `db.list_tasks()` all projects → dicts | `task_list` command, `useTasks` | 256 ms | 1 |
| `db.list_tasks(status=DEFINED)` | monitoring, per cycle | 75 ms (9 ms in SQL) | 1 |
| `POST /graph/tiles` root rect, geometry cached | pan / live refetch | 42–82 ms (60–70 % Python) | 8–12 |
| `POST /graph/tiles` cold geometry | first paint, after every layout version bump | 77–96 ms | 9–13 |
| `POST /graph/tiles` root=epic0, 100 pkgs expanded | focus mode | 60 ms, 573 KB, 1,101 nodes | 8 |
| `process_dirty` one task, `perf` | every status change, in the cycle | 540–630 ms | 80–94 |
| `process_dirty` one task, `flat` | same | **1.4 s** | 28 |
| `set_parent` (single) | task creation with a parent | 92–95 ms | 26 |
| `add_dependency` | edge creation | 17 ms | 9 |
| `create_task` (no parent) | — | 4.2 ms | 3 |
| `transition_task` | status change | 13 ms | 9 |

## After (2026-09-04, branch worktree-perf-dashboard)

Measured on a fresh PostgreSQL 18 database `aq_perfprobe_wt` seeded from this branch with the
same two projects as the "Headline numbers" table (the original `aq_perfprobe` was stamped by
another session with a migration this branch lacks). Note that the original `bench.py`
incremental rows marked dirty with reason `status`, which does not exercise the `status.*` fast
path; the After `process_dirty` rows below come from real `transition_task` calls.

| path | before | after |
|---|---|---|
| `GET /api/projects/{pid}/graph` (perf, 5,607 tasks) | 11,544 ms, 5,611 statements | 134 ms, 6 statements |
| `GET /api/projects/{pid}/graph` (flat, 5,000 tasks) | 10,211 ms, 5,004 statements | 67 ms, 6 statements |
| promotion scan dependency reads, 4,647 DEFINED tasks | 9,091 ms, 4,647 statements (one per task) | 42 ms, 1 statement (batched) |
| `process_dirty`, one real `status.finished` leaf, flat project, both variants | 1,342 ms | 214–262 ms |
| `process_dirty`, one real `status.finished` leaf in the 1,000-task epic | 649 ms | 351 ms |
| `set_parent` (single) | 92–95 ms, 26 statements | 32.5 ms, 26 statements |
| `add_dependency` | 17 ms | 16 ms (unchanged) |
| `create_task` | 4.2 ms | 5 ms (unchanged) |
| `POST /graph/tiles` root rect, geometry cached | 42–82 ms | 41–88 ms (unchanged, not targeted) |

Seeding 5,100 hierarchical tasks one-by-one took 344 s (≈67 ms/task), dominated by `set_parent`.

## Findings, in priority order

### 1. `/api/projects/{pid}/graph` is N+1 and is refetched on every event — the single biggest problem

`src/api/graph.py` calls `get_typed_dependencies_detailed(t.id)` once **per task** and
`get_gate_waiters(g.id)` once **per gate**, each opening its own transaction. At 5,600 tasks that is
5,600 round trips and 11 s per request, and the payload is 2 MB.

Two consumers make it hurt:

- `dashboard/src/pages/command-center/Tasks.tsx` uses this endpoint (via `useProjectGraphs`) as the
  **task list**, deliberately, because `task_list` truncates completed history.
- `useGraphLive.ts` invalidates the project graph query after **any** `task.*`, `agent.*`, `gate.*`,
  `session.*` or task-bearing `notify.*` event, coalesced at 500 ms. With a working fleet emitting a
  handful of events per cycle, the daemon runs this 11 s request continuously and the Tasks tab is
  perpetually 10 s stale. Requests overlap (`cancelQueries` only cancels client-side).

The same data batched into two statements (one narrow task select, one `task_dependencies ... WHERE
task_id IN (...)`) took 53–68 ms in the prototype — a ~170× improvement before any caching.
`get_dependency_map_for_tasks` already exists in `dependency_queries.py` as a batched helper, and
gate waiters can be one `task_gates JOIN gates WHERE project_id = ...`.

Also: `list_tasks` selects `*` (description, deliverables, attachments JSON) and hydrates a full
`Task` for each row just to project ~12 fields. The narrow projection ran in 9 ms against 75 ms.

**Recommendation.** Rewrite the endpoint as three set-based queries (tasks projected to the node
fields, edges by `task_id IN`, gates joined to waiters) and add an `ETag`/`layout_version`-style
version so the client can short-circuit unchanged snapshots. Then give the Tasks tab a real
server-side list: paginated, filtered, sorted in SQL, and virtualized on the client (see §5).

### 2. The promotion cascade is N+1 per DEFINED/BLOCKED task, every 5 s

`blocked_state_authoritative` defaults to `False`, so `_legacy_promotion_decisions`
(`src/orchestrator/monitoring.py:384`) still runs as the shadow oracle and calls
`get_typed_dependencies(task.id)` for **every** DEFINED and BLOCKED task each cycle. At 4,627
DEFINED tasks that is 9.1 s per 5 s cycle: the orchestrator cannot keep its cadence, and everything
downstream of the cycle (scheduling, layout, gate sweeps, message delivery) inherits the lag.
`_check_defined_tasks` also does `get_task_meta(task.id, "needs_attention")` per BLOCKED task
(200 tasks → 385 ms) even though `task_ids_with_meta` (batched) is called three lines later.

The batched equivalent (one `IN` query for all 4,627 ids) took 41 ms.

**Recommendation.** Either flip `blocked_state_authoritative` on (the projection path is already
set-based: `tasks_with_graph_blockers` is one query) or batch the legacy scan with one
`task_dependencies WHERE task_id IN (...)` query grouped in Python. Replace the `get_task_meta`
loop with `task_ids_with_meta`. Also narrow `list_tasks(status=...)` here: the cycle only needs id,
status, is_blocked, parent_task_id, is_plan_subtask.

### 3. Every task status change re-lays-out its whole container, inside the cycle

`process_dirty` runs in `run_one_cycle` (`layout_step.py`) and re-runs `layout_container` for the
dirty task's container. In a flat project the container is the root with 5,000 children, so **one**
status change costs 1.4 s of engine CPU (28 statements — the time is Python, in `asyncio.to_thread`,
but the cycle still awaits it). In the hierarchical project a single change costs 540–630 ms and
80–94 statements (`_db_row` is called one id at a time in `_seed_queue`/`_current_size`).

Every republish also bumps `layout_version`, which invalidates the tiles geometry cache for every
viewer, so each change turns the next tile request into a cold one (~80–100 ms instead of ~40 ms).

**Recommendations.**
- Skip re-layout when the dirty mark cannot change geometry: a status flip on a leaf keeps its box.
  Only `add/remove child`, `set_parent`, reparenting or `active`-variant membership changes need
  re-flow; a pure status change should publish only the row's aggregate/status columns (or nothing —
  tiles read status live from `tasks`).
- Give a container with more than N children (say 500) a cheaper incremental placement (append to
  the last band) rather than a full re-flow, and defer full re-flow to the tidy job.
- Batch `_db_row` lookups in the incremental driver (`load_layout_rows` already takes a list).
- Run `process_dirty` as a background task rather than inline in the cycle, bounded by a budget.

### 4. `set_parent` loads the whole dependency graph per call

`hierarchy_queries.set_parent` calls `_blocking_edges(conn)` — **every** blocking edge in the
database — for the cycle check, plus recursive CTEs for depth/height, plus `recompute_blocked`,
26 statements and ~93 ms per call at 14k edges. That cost grows linearly with the whole database,
not the subtree. Formula cook / graph creation of 1,000 tasks pays ~90 s here alone unless it uses
`set_parent_bulk` (which exists and skips the check for fresh leaves — verify every creation path
uses it).

**Recommendation.** Replace the global load with a targeted reachability check (recursive CTE from
`parent_id` following blocking edges, bounded), and make sure `creator.write_plan` / formula cook /
`task_create` with `parent_task_id` go through `set_parent_bulk`.

### 5. The Tasks tab renders every row and filters on the client

`Tasks.tsx` maps `filtered` straight into `<tr>`s with `InlineStatus`, `InlinePriority` and
`RowActions` per row: 5,600 tasks → 5,600 rows and ~17k interactive components per render, re-rendered
on every filter keystroke and every graph refetch. There is no virtualization and no server paging.

**Recommendation.** Virtualize (e.g. `@tanstack/react-virtual`, already in the React Query family) and
move filter/sort/paging into a server-side list endpoint that returns a narrow projection plus a total
count. Keep the full graph snapshot only for the canvas.

### 6. Websocket fan-out overhead

`WebSocketManager` logs at **INFO** three times per event per client (`WS forwarding event`,
`WS sending event to client`, `WS sent successfully`) and calls `send_json` (a fresh
`json.dumps`) per client per event. Fine at 1 client and 1 event/s; at a busy fleet with several
dashboard tabs open it is a steady stream of log lines and repeated serialization.

**Recommendation.** Drop those to DEBUG, serialize once per event and `send_text` the shared string,
and consider a per-connection micro-batch (flush every ~50 ms) so bursts become one frame.

### 7. Client-side invalidation is too broad

`useEventStream.ts` invalidates `["tasks"]` and `["agents"]` on nearly every event type, and
`useGraphLive` refetches graph + tiles + extent for every `session.*`/`agent.*` event even when the
event carries no task change. Combined with §1 this multiplies the expensive requests.

**Recommendation.** Patch caches in place from the event payload (the `patchTask` path already
exists for `task.blocked/unblocked` and `notify.task_*`) and only refetch on structural events
(`task.created`, `task.deleted`, `task.archived`, dependency/parent changes). Gate the graph refetch
on `project_id` matching and on a server-side graph version so unchanged snapshots cost a 304.

### 8. Schema and PostgreSQL observations (secondary)

- `tasks` rows are wide (`width=1080` in plans): `description`, `deliverables`, `attachments` ride
  along on every `SELECT *`. `list_tasks(project_id=flat)` spills its sort to disk
  (`external merge Disk: 4552kB`) with the container default `work_mem = 4 MB`. A narrow projection
  fixes the spill; bumping `work_mem` to 16–32 MB and `shared_buffers` above the 128 MB default in
  the compose file would help the daemon generally.
- Status-only lists (`list_tasks(status=X)`, used by monitoring every cycle) seq-scan `tasks`
  because the only status index leads with `project_id`. Cheap now (2.4 ms at 10k rows), but a
  `(status, project_id)` or partial index on the non-terminal statuses keeps it flat as completed
  history grows.
- `idx_task_layouts_path` and `idx_tasks_project_dedup` recorded zero scans in the whole run;
  `load_paths_by_prefixes` filters `path LIKE` after a bitmap scan on `(project_id, variant)`. A
  `text_pattern_ops` index on `(project_id, variant, path)` makes the prefix scans index-driven.
- `load_matching_ids` (`q=` search) is a `lower(title) LIKE '%q%'` seq scan on `tasks`; a
  `pg_trgm` GIN index on `lower(title)` makes search O(matches) instead of O(tasks).
- Every query opens `engine.begin()` — a transaction per statement. With asyncpg that is
  `BEGIN`/`COMMIT` round trips around each read; the N+1 paths above pay it thousands of times.
  Read-only helpers can use `engine.connect()` with autocommit, or better, be batched.
- `pg_stat_statements` is not enabled on the compose instance; enabling it
  (`shared_preload_libraries`) would make the next investigation a query instead of a probe.

### 9. Things that are already in good shape

- The tiles endpoint is viewport-bounded, cached by `layout_version`, and stays under ~100 ms even
  with the 1,000-task epic fully expanded; statement counts are 8–15 regardless of project size.
- `full_layout` is 13 statements and ~1 s for 5,600 tasks.
- Websocket replay is indexed (`events_pkey`, 0.3 ms for a 500-row page).
- `create_task`, `add_dependency`, `transition_task`, `update_task` are all single-digit to low
  double-digit milliseconds and bounded by the affected set, not the database size.

## Suggested order of work

1. Batch `/api/projects/{pid}/graph` (three statements) — one afternoon, removes the 11 s request.
2. Batch or retire the legacy promotion scan; batch the `needs_attention` meta lookup — restores the
   5 s cycle.
3. Narrow `list_tasks` projections for list/graph/cycle consumers; add the `(status, …)` and
   `text_pattern_ops` indexes; raise `work_mem`.
4. Make `process_dirty` skip pure status flips and run off the cycle.
5. Server-side paged task list + virtualized Tasks tab; in-place cache patching instead of blanket
   invalidation; graph snapshot versioning.
6. Websocket logging to DEBUG, serialize once, micro-batch.
7. Targeted cycle check in `set_parent`; audit creation paths for `set_parent_bulk`.

Each of 1–4 is measurable with the scratchpad benchmark against the retained `aq_perfprobe`
database; a `tests/perf` statement-count test for the graph endpoint (budget: ≤ 5 statements,
independent of task count) would lock the win in.
