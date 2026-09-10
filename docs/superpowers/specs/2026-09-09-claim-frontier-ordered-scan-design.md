# The §10 work query as an index-ordered scan

**Status:** implemented (2026-09-09)
**Scope:** `src/database/queries/claim_queries.py::select_ready_for_profile`,
`src/database/tables.py` (`tasks` indexes), Alembic `a0000000000d`
**Predecessor:** `swift-beacon`, which hoisted the per-row `projects` lookup out of the two
hierarchy claim predicates (23.5 ms → 11 ms at the §15.2 scale) and deliberately stopped
short of this, because what remained needs a scheduling-semantics decision and an index
migration rather than a query tweak.

## 1. The problem

`select_ready_for_profile` answers one question — *which single task should this pool worker
take next?* — and it answered it by reading the whole frontier.

At the §15.2 reference scale (5,000 tasks, 2,499 on the frontier;
`tests/perf/test_hierarchy_statements.py::seed_scale`) one call cost **~10,100 shared
buffers and ~11 ms**, of which essentially all was the 2,500-row materialise-and-heapsort
plus the correlated `NOT EXISTS` sub-plans in `_frontier_where` evaluated once per frontier
row (container flag, workspace requirements, prepare-backoff metadata, hold label). For a
`LIMIT 1`.

The cause was one line:

```python
.order_by(
    case((tasks.c.affinity_agent_id == agent_id, 0), else_=1),   # ← the leading sort key
    tasks.c.priority.asc(),
    tasks.c.created_at.asc(),
)
```

`agent_id` is a runtime parameter, so no index can supply that order. PostgreSQL therefore
cannot stop at the first admissible row: it must produce every admissible row, sort them,
and discard 2,499.

Sorting by `priority, created_at` alone *is* index-orderable. The whole design question is
what happens to the affinity preference.

## 2. The scheduling-semantics decision

Affinity is a **soft preference**, not a filter: `_frontier_where` does not exclude a task
pinned to some other agent, so `affinity_agent_id` only ever reorders candidates. Three
shapes were considered.

### 2a. Drop or demote the preference (rejected)

Removing the affinity term entirely gives the ideal plan (10 buffers) and is a one-line
change. It is also a silent scheduling regression: `affinity_agent_id` is a real feature —
set at task creation, by `task_set`, and by workflow stage affinity, and surfaced by
`workflow_pipeline_view` as `preferred_agent` / `is_honored` — and its whole purpose is
context continuity across a multi-stage pipeline. A perf task is not the place to retire
it.

### 2b. One statement, affinity hoisted to an `InitPlan` as a *restriction* (rejected)

Keep one statement, and gate the frontier on an uncorrelated `EXISTS` over the full frontier
predicate restricted to `affinity_agent_id = :agent`:

```sql
WHERE <frontier> AND CASE WHEN (<uncorrelated EXISTS>) THEN affinity_agent_id = :agent
                          ELSE true END
ORDER BY priority, created_at LIMIT 1
```

This is semantically identical to the old sort for the top-1 row, it measures the same
(14 buffers), and it is one statement cheaper. It was rejected for its behaviour under
`SKIP LOCKED`.

The old sort *falls through*: if the best pinned row is held by another claimer it is
skipped, and if every pinned row is held the scan simply continues into unpinned work in the
same pass. The restriction form cannot — the `EXISTS` does not take locks, so it says "a
pinned task exists" while the scan below it finds every one of them locked, and the
statement returns nothing. The caller then reports `no_ready_work`, and `_cmd_task_claim`'s
wait branch parks on the `task.ready` waiter — which a *claim* by another worker does not
fire — for the rest of the `--wait` window. A worker idling up to 60 s next to claimable
work is a worse failure than the one being fixed, and it is invisible in aggregate.

### 2c. Two statements, the preference split across them (chosen)

```python
if task_id is None:
    row = await conn.execute(candidate(pinned=True))    # frontier ∧ affinity = :agent
    if row:
        return row[0]
row = await conn.execute(candidate(pinned=False))       # frontier, no affinity term
```

Both order by `priority, created_at` alone. This preserves the old ordering **exactly**:

* if any admissible task is pinned to this agent, the old leading sort key would have
  returned the best of them — and so does the first statement;
* the second statement is reached only when the first found nothing, which is exactly when
  the old leading key was constant across the whole frontier and the answer was the best row
  overall.

`SKIP LOCKED` falls through as it always did: a locked pinned row is skipped inside the
first statement, and if *every* pinned row is locked the first statement returns nothing and
the second still considers unpinned work.

A targeted claim (`task_id` given, i.e. `aq task claim --task <id>`) skips the probe: with a
single candidate row, preferring it over itself is a no-op.

**Cost:** one statement. `tests/perf/test_claim_statements.py`'s budgets move from 19/9/6 to
20/10/7, each with the reasoning above recorded in its docstring. The trade is explicit: one
statement on a held connection measured 0.46–0.54 ms on the box that file's wire floor was
measured on, against ~11 ms of query removed — and the added statement is the *cheapest* on
the path (1–2 buffers).

## 3. The indexes

Three new indexes on `tasks`, all carrying `priority, created_at` as trailing key columns so
the `ORDER BY` comes from the index:

| index | key | why |
|---|---|---|
| `idx_tasks_claim_frontier` | `project_id, status, is_blocked, priority, created_at` | the widened profile predicate |
| `idx_tasks_claim_frontier_by_profile` | `project_id, profile_id, status, is_blocked, priority, created_at` | the narrow profile predicate |
| `idx_tasks_claim_frontier_by_affinity` | `affinity_agent_id, project_id, status, is_blocked, priority, created_at` `WHERE affinity_agent_id IS NOT NULL` | the pinned half of §2c |

Two indexes are replaced rather than kept alongside, because each is a strict leading-column
prefix of one of the above and every query that used it keeps its access path:

* `idx_tasks_project_status_blocked` (`_check_defined_tasks`, the scheduler filter,
  `aq project ready`) → prefix of `idx_tasks_claim_frontier`;
* `idx_tasks_ready_by_profile` (the pool work query) → prefix of
  `idx_tasks_claim_frontier_by_profile`.

Net: `tasks` gains one index, and the one it gains is partial and holds only pinned tasks.

### 3a. Why two profile indexes rather than one

`profile_ok` widens to `profile_id = :p OR profile_id IS NULL` when the pool profile is the
project default. That `OR` makes `profile_id` unusable as an index column *and* keeps the
ordered scan out of reach:

* `FOR UPDATE ... SKIP LOCKED` cannot be combined with `UNION`, so the obvious two-branch
  rewrite is not available;
* `coalesce(profile_id, '') = ANY (ARRAY[:p, ''])` against a matching expression index does
  not help either. Measured on PostgreSQL 18, the planner will not produce sorted output
  from a `ScalarArrayOp` on a middle index column — not by choice and not under
  `enable_sort=off` + `enable_seqscan=off` + `enable_bitmapscan=off`. It is not a costing
  decision; the ordered path does not exist.

So the widened branch gets an index with **no** profile column at all and pays for the
profile as a filter on an ordered scan, which is cheap because it stops at the first row
that passes. The narrow branch keeps `profile_id` as an index column, which is what the
planner picks for it. Both were verified to be chosen by the planner with and without
statistics (`ANALYZE`), and the measurements below are with both indexes present.

## 4. Measurements

PostgreSQL 18 (Docker, `shared_buffers=512MB`), `seed_scale(n_tasks=5000)` = 2,499 frontier
rows, statements as `select_ready_for_profile` actually issues them.

| | statements | shared buffers | plan |
|---|---|---|---|
| before | 1 | ~10,100 | index scan + `Sort` over 2,500 rows |
| after, narrow profile | 2 | 15 (2 + 13) | two index-ordered scans, no `Sort` |
| after, widened profile | 2 | 15 (2 + 13) | two index-ordered scans, no `Sort` |
| after, a task pinned to this agent | 1 | 12 | first statement answers |

Wall-clock is not quoted: the box these were taken on was running the fleet at load average
4–7 on two visible cores, which moved absolute milliseconds by 5–8× between runs and buffer
counts not at all. Buffer counts and plan shape are the honest metric here, which is why
they, and not a millisecond number, are what the new test asserts.

## 5. The ratchet

`tests/perf/test_claim_statements.py::test_work_query_is_an_index_ordered_scan` runs both
statements through `EXPLAIN (ANALYZE, BUFFERS)` — via a connection proxy, so it explains the
statements production builds rather than a reconstruction — and asserts:

* **no `Sort` node in either plan.** A sort means the planner could not get
  `priority, created_at` from an index and read the whole frontier first. That is the
  regression, exactly, and it is what a statement count cannot see.
* **≤ 64 shared buffers for the pair** (measured 13–15).

It is plan-shape rather than wall-clock, so it takes the `perf` marker but not
`perf_strict`.

## 6. Follow-ups this leaves open

* The two `task_metadata` sub-plans in the frontier predicate (container flag,
  prepare-backoff deadline) are two separate `task_metadata_pkey` lookups per row examined.
  They no longer run 2,500 times, so this is now a small constant — but they could be one
  lookup with an `OR`-ed key predicate if the frontier ever has to examine many rows.
* `is_plan_subtask` and `assigned_agent_id` are still heap filters. Adding them to the two
  frontier indexes would make an index-only scan possible for the *rejecting* rows; not
  worth the write cost at the current scan depth of ~1 row.
