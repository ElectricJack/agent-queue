---
tags: [implementation, work-graph, dependencies, gates, labels, state-machine, explain, migrations]
---

# Work Graph — Implementation Spec

**Status:** Draft — approved direction (2026-08-19)
**Related:** [[design/work-graph]] (authoritative semantics), [[design/workspaces-v2]], `docs/analysis/framework-overhaul-todo.md` §6, `src/database/tables.py`, `src/state_machine.py`

All file/line references verified against the tree at HEAD (2026-08-19). Python 3.12, async-first, SQLAlchemy Core + Alembic, ruff line-length 100. Commands return `{"success": bool, ...}` dicts through `CommandHandler`.

---

## 1. Scope

Phase 0 of the framework overhaul: typed edges, persisted `is_blocked`, gates + sweep, labels, outcome metadata + retry policy, state-machine enforcement, explain/ready, `after_seq` replay, payload-registry test, hierarchical child ids, computed group progress. The status collapse (design §12) is **not** implemented here; nothing below may block it.

## 2. Schema changes and migrations

Four Alembic revisions (small, reviewable, each upgradeable on SQLite **and** PostgreSQL). All schema edits land in `src/database/tables.py` first; `alembic revision --autogenerate` output is hand-reviewed per CLAUDE.md.

### 2.1 Revision 1 — `dep_type` on task_dependencies

- Add `Column("dep_type", Text, nullable=False, server_default="'blocks'")` to `task_dependencies` (tables.py line 109).
- Widen the PK from `(task_id, depends_on_task_id)` to `(task_id, depends_on_task_id, dep_type)` so one pair can carry e.g. `blocks` + `discovered-from`.
- Add `CheckConstraint("dep_type IN ('blocks','parent-child','waits-for','conditional-blocks','discovered-from','related','duplicates','supersedes')", name="ck_task_deps_dep_type")`.
- Replace the two single-column indexes with composites: drop `idx_task_deps_task_id` / `idx_task_deps_depends_on`; create `idx_task_deps_task_type (task_id, dep_type)` and `idx_task_deps_depson_type (depends_on_task_id, dep_type)`. The leading columns keep all existing lookups covered; the second column serves the recompute predicate's `dep_type` filters.

**Dialects:** SQLite cannot alter a PK — the migration uses `op.batch_alter_table("task_dependencies", recreate="always")` (full table copy; the table is small). PostgreSQL takes plain `op.drop_constraint` / `op.create_primary_key` / `op.add_column` with the server default. Existing rows read back as `'blocks'` — zero behavior change.

### 2.2 Revision 2 — `tasks.is_blocked` + backfill

- `Column("is_blocked", Integer, nullable=False, server_default="0")` on `tasks` (Integer 0/1 matches the table's existing flag style, e.g. `requires_approval`). Same column appended to `archived_tasks` so archiving stays lossless.
- New index `idx_tasks_project_status_blocked (project_id, status, is_blocked)` — serves `_check_defined_tasks`, the scheduler filter, and `aq project ready`. Also `idx_tasks_parent (parent_task_id)` (currently unindexed; group progress and tree queries need it).
- **Backfill:** the upgrade runs the full blocked-state predicate (§3.2) as one data-migration `UPDATE` over all tasks via `op.get_bind()`. The statement is ANSI SQL (`EXISTS` subqueries only) and runs unchanged on both dialects. At this revision no gates exist and all edges are `'blocks'`, so the backfill reduces to the `blocks` clause.

### 2.3 Revision 3 — gates

```python
gates = Table("gates", metadata,
    Column("id", Text, primary_key=True),                      # "gate-" + uuid4[:12]
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("gate_type", Text, nullable=False),                 # human|timer|pr-merged|ci-run|event|task
    Column("title", Text, nullable=False),
    Column("question", Text, nullable=False, server_default="''"),
    Column("await_id", Text, nullable=True),
    Column("timeout_at", Float, nullable=True),
    Column("status", Text, nullable=False, server_default="'open'"),  # open|resolved|expired
    Column("resolved_by", Text, nullable=True),
    Column("resolution", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint("gate_type IN ('human','timer','pr-merged','ci-run','event','task')", name="ck_gates_type"),
    CheckConstraint("status IN ('open','resolved','expired')", name="ck_gates_status"),
    Index("idx_gates_project_status", "project_id", "status"),
    Index("idx_gates_status_type", "status", "gate_type"),     # sweep scans open gates by type
)
task_gates = Table("task_gates", metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    Column("gate_id", Text, ForeignKey("gates.id"), primary_key=True),
    Index("idx_task_gates_gate", "gate_id"),                   # resolve → find waiters
)
```

Straight `op.create_table` on both dialects.

### 2.4 Revision 4 — labels

`task_labels(task_id Text FK→tasks.id PK, label Text PK)` + `Index("idx_task_labels_label", "label")`. Straight create on both dialects.

## 3. Blocked-state recompute

### 3.1 Module and signatures

New `src/database/queries/blocked_state.py`, mixed into the DB class like the other query mixins:

```python
BLOCKING_DEP_TYPES = frozenset({"blocks", "parent-child", "waits-for", "conditional-blocks"})

class BlockedStateMixin:
    async def recompute_blocked(self, seed_task_ids: set[str], *, conn: AsyncConnection) -> set[str]:
        """Fixpoint recompute inside the caller's transaction. Returns ids whose
        is_blocked flipped (caller emits task.blocked/unblocked after commit)."""
    async def _collect_affected(self, seeds: set[str], conn) -> set[str]: ...
    async def get_ready_frontier(self, project_id: str, *, labels: list[str] | None = None,
                                 any_label: list[str] | None = None) -> list[Task]: ...
```

`recompute_blocked` **requires** the open connection — it never opens its own transaction. Every mutating query method that can change blockedness is refactored to do read + write + recompute inside **one** `async with self._engine.begin()` block (today `transition_task` calls `get_task` and `update_task` in separate transactions — that split goes away; see §4.1).

### 3.2 The predicate (both dialects)

One set-based statement per fixpoint wave, built with SQLAlchemy Core (dialect-neutral); shown as SQL for review:

```sql
UPDATE tasks SET is_blocked = CASE WHEN
  EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'blocks'
            AND dep.status != 'COMPLETED')
  OR EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'parent-child'
            AND p.status IN ('DEFINED','AWAITING_PLAN_APPROVAL'))
  OR EXISTS (SELECT 1 FROM task_dependencies w
          WHERE w.task_id = tasks.id AND w.dep_type = 'waits-for'
            AND EXISTS (SELECT 1 FROM task_dependencies pc JOIN tasks c ON c.id = pc.task_id
                        WHERE pc.dep_type = 'parent-child'
                          AND pc.depends_on_task_id = w.depends_on_task_id
                          AND c.status != 'COMPLETED'))
  OR EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id = d.depends_on_task_id
          WHERE d.task_id = tasks.id AND d.dep_type = 'conditional-blocks'
            AND NOT (dep.status = 'BLOCKED'
                     OR (dep.status = 'FAILED' AND dep.retry_count >= dep.max_retries)))
  OR EXISTS (SELECT 1 FROM task_gates tg JOIN gates g ON g.id = tg.gate_id
          WHERE tg.task_id = tasks.id AND g.status != 'resolved')
THEN 1 ELSE 0 END
WHERE tasks.id IN (:affected_ids)
```

`_collect_affected(seeds)` = seeds ∪ `SELECT task_id FROM task_dependencies WHERE depends_on_task_id IN :seeds AND dep_type IN BLOCKING_DEP_TYPES` ∪ waiters of containers the seeds are children of (`waits-for` rows whose `depends_on_task_id` matches any `parent-child` target of a seed) ∪ waiters of changed gates. Changed rows are detected by selecting `(id, is_blocked)` for the affected set before the UPDATE and diffing after (RETURNING is avoided for dialect parity with older SQLite).

**Fixpoint:** the driver loops while the *transaction* keeps changing task statuses (bulk create, conditional auto-close); with statuses fixed, one wave suffices. Loop bound: each wave only re-seeds tasks whose status changed that wave; asserted `< 10_000` iterations as a safety valve.

**Locking:** SQLite — single writer, WAL; the whole mutation+recompute is one write transaction, inherently atomic. PostgreSQL — the single UPDATE takes row locks in one statement (no interleaving); multi-wave loops sort `affected_ids` before each UPDATE so concurrent transactions acquire locks in a canonical order, preventing deadlocks (same discipline as workspace acquisition in [[design/workspaces-v2]]).

## 4. Query-layer changes (exact functions)

### 4.1 `src/database/queries/task_queries.py`

- `transition_task(task_id, new_status, *, context="", event: TaskEvent | None = None, force: bool = False, **kwargs)` (line 159) — rewritten single-transaction: read row `FOR UPDATE`-equivalent, validate via `is_valid_status_transition`, **raise `InvalidTransition` when `config.state_machine.enforce` and not `force`** (warn-only otherwise, as today), apply, `recompute_blocked({task_id}, conn=conn)`, return the set of flipped ids so orchestrator callers can emit events. The enforce flag reaches the DB layer via a `set_enforcement(bool)` setter called from config load/reload (avoids importing config into the query layer).
- `update_task(**kwargs)` (line 148) — asserts `"status" not in kwargs` outside `transition_task` (guarded by a module-private flag); invariant test in §10.
- `delete_task` (line 199) — before deleting edges, snapshot `get_dependents(task_id)` ∪ waits-for waiters; after deletes, `recompute_blocked(snapshot, conn=conn)` in the same transaction.
- `create_task` (line 31) — inserts `is_blocked=0`; graph-creation callers pass edges and get one recompute for the whole batch.
- New: `list_ready_tasks`, `get_group_progress(container_id) -> dict` (done/ready/blocked/in_progress counts, Kahn waves over blocking edges among children, `max_parallelism`; computed, never stored).

### 4.2 `src/database/queries/dependency_queries.py`

- `add_dependency(task_id, depends_on, dep_type="blocks")` (line 14) — insert + same-transaction recompute.
- `remove_dependency(task_id, depends_on, dep_type=None)` (line 219) — `None` removes all types for the pair; recompute.
- `remove_all_dependencies_on` (line 231) — recompute former dependents.
- `get_dependencies(task_id, dep_types: frozenset[str] | None = None)` (line 21) — default `BLOCKING_DEP_TYPES` to preserve callers' semantics; `get_all_dependencies` likewise gains the filter (cycle validation passes blocking-only).
- `are_dependencies_met` (line 40) — becomes a thin shim over `is_blocked` (kept one release for plugins, then removed).
- `get_blocking_dependencies` (line 132) — returns `(dep_id, title, status, dep_type, project_id)`; consumed by explain and `_check_stuck_defined_tasks`.
- `get_stuck_defined_tasks` (line 110) — unchanged query, plus `dep_type IN BLOCKING_DEP_TYPES` filter.

### 4.3 New query mixins

- `src/database/queries/gate_queries.py`: `create_gate(project_id, gate_type, title, *, question="", await_id=None, timeout_at=None, waiter_task_ids=()) -> str` (insert gate + `task_gates` rows + recompute waiters, one tx); `resolve_gate(gate_id, *, resolved_by, resolution="") -> set[str]` (idempotent — resolving a resolved gate is a no-op; returns unblocked task ids); `expire_open_gates(now) -> list[str]`; `list_gates(project_id=None, status=None, gate_type=None)`; `get_gates_for_task(task_id)`; `list_open_gates_by_type(gate_type)`.
- `src/database/queries/label_queries.py`: `add_label`, `remove_label`, `get_labels(task_id)`, `get_labels_for_tasks(ids)`; `list_tasks` (task_queries line 79) gains `labels=None, any_label=None` join filters.
- `src/database/base.py` `DatabaseBackend` protocol: all new methods added.

### 4.4 `src/models.py` and `src/state_machine.py`

- models.py: `DepType(Enum)`, `GateType(Enum)`, `GateStatus(Enum)`, `Gate` dataclass; `Task.is_blocked: bool = False` (+ `_row_to_task` at task_queries line 357).
- state_machine.py: `BLOCKING_DEP_TYPES` re-exported; `validate_dag_with_new_edge(deps, task_id, depends_on, dep_type="blocks")` (line 157) — non-blocking types skip DFS (self-edge still rejected); new `validate_waits_for(parent_child_edges, waiter_id, container_id)` implementing the descendant-deadlock rule (design §11); `InvalidTransition` gains `from_status/to_status` fields for API error payloads.

## 5. Command surface (`src/commands/`)

- `task_commands.py::_cmd_add_dependency` (line 1192): accepts `dep_type` (validated against `DepType`), routes blocking types through `validate_dag_with_new_edge` + `validate_waits_for`; duplicate check becomes per-(pair, type).
- `_cmd_remove_dependency` (line 1240): optional `dep_type`.
- `_cmd_create_task` (line 754): new optional `parent_id` (creates the `parent-child` edge, assigns hierarchical child id §7), `labels: list[str]`, `depends_on: list[{task_id, dep_type}]` — all applied in the creation transaction with one recompute.
- New `_cmd_explain_task` — returns the design-§9 reason list; graph reasons from the DB, capacity reasons from `src/explain.py` (§6.3).
- New `_cmd_project_ready` — ready frontier + withheld section.
- New `_cmd_task_label` (add/remove/list) and label filters on `_cmd_list_tasks` (line 220).
- New `_cmd_close_task` — the completion-protocol shell: writes outcome metadata keys (`outcome`, `failure_class`, `work_outcome`, `work_commit`, `work_branch`, `verification`, `close_notes`) via `set_task_meta`, then `transition_task` to COMPLETED/FAILED. Consumed by `aq task close` per [[session-runtime]].
- `_cmd_set_task_status` (line 2672): gains `force` arg passed to `transition_task`.
- New `src/commands/gate_commands.py` mixin on `CommandHandler`: `_cmd_gate_create`, `_cmd_gate_list`, `_cmd_gate_show`, `_cmd_gate_resolve(gate_id, resolved_by, resolution="", …)`. Discord buttons ([[messaging-rework]]) and the dashboard call `gate_resolve` — no other resolution path for `human` gates.
- `event_commands.py::_cmd_get_recent_events` (line 46): new `after: int` param → ascending replay (§8).

## 6. Orchestrator integration (exact functions)

### 6.1 Cascade — `src/orchestrator/core.py::run_one_cycle` (line 1658)

Insert step **2b** between `_check_awaiting_approval()` (line 1721) and `_check_defined_tasks()` (line 1729):

```python
# 2b. Sweep gates: resolve satisfied timer/pr-merged/ci-run/event/task gates,
#     expire overdue ones. Runs before promotion so a freshly resolved gate
#     unblocks its waiters in the same cycle.
await self._sweep_gates()
```

### 6.2 `src/orchestrator/monitoring.py`

- `_check_defined_tasks` (line 47) — **rewritten** to consume the projection: promote `DEFINED ∧ is_blocked=0` → READY, and `BLOCKED ∧ is_blocked=0 ∧ has ≥1 blocking edge or gate` → READY (preserves "failure-BLOCKED stays put"). The `is_plan_subtask` special case (lines 77–102) is deleted — `parent-child` semantics subsume it; the plan parser creates `parent-child` edges instead of `blocks` edges on the parent. Shadow mode (§9) runs old and new side by side first.
- New `_sweep_gates()` (same mixin): `expire_open_gates(now)`; resolve `timer` (clock) and `task` (dep COMPLETED) gates; `pr-merged`/`ci-run` via `_poll_pr_merged` on the existing 60 s approval throttle; `event` gates re-checked against `events` rows with `id >` the gate's creation watermark. Emits `gate.resolved`/`gate.expired` and `task.unblocked` for flips.
- `_check_stuck_defined_tasks` (line 163) — adapts to the 5-tuple from `get_blocking_dependencies`; stuck detection now also covers `READY ∧ is_blocked=1`.

### 6.3 Approvals and explain

- `src/orchestrator/approval.py::_check_pr_status` (line 145): the `gh` polling body (`self.git.acheck_pr_merged`, checkout-path fallback) is extracted to `_poll_pr_merged(task_or_project_id, pr_url) -> bool | None`; `_check_pr_status` and `_sweep_gates` both call it. `_check_awaiting_approval` (line 43) itself is unchanged in phase 0 (statuses survive until the collapse).
- New `src/explain.py`: `Reason` TypedDict `{code: str, detail: str, ref: str | None}`; `build_capacity_reasons(task, state: SchedulerState, workspace_counts, idle_by_project) -> list[Reason]`. `core.py::_describe_task_blocker` (line 2030) is rewritten as a one-line wrapper (`reasons[0]` formatted), so `_log_scheduler_blockers` (line 1978), `_cmd_explain_task`, and the dashboard share one source. The orchestrator caches the last `SchedulerState` snapshot (`self._last_scheduler_state`) so explain can answer between ticks.
- `src/orchestrator/execution.py`, FAILED branch (lines 1364–1392): before the retry decision, read `failure_class` from `get_task_meta(task_id, "failure_class")`; `"hard"` → `transition_task(..., BLOCKED, context="hard_failure")` immediately (skip retry), else existing retry/backoff path. `AgentResult` mapping unchanged.
- `src/scheduler.py::Scheduler.schedule`: dispatchable filter gains `and not task.is_blocked` (defense in depth; promotion already respects it).
- EventBus wiring: orchestrator `start()` subscribes a handler that matches open `event` gates on `event_type` (+ optional payload filter) and resolves them immediately; the sweep remains the restart-safe backstop.

## 7. Hierarchical child ids

`src/task_names.py::generate_task_id(db)` (line 86) gains `parent_id: str | None = None`: children get `f"{parent_id}.{n}"` where `n` = max existing ordinal among `parent_task_id == parent_id` children + 1 (queried in the creation transaction to avoid races); depth capped at 3 (`grand.parent.1.2` rejected → falls back to a fresh root id + `discovered-from` edge, with a warning). Root ids stay adjective-noun. Dots are safe in Discord, URLs (path-encoded), and `aq/<task_id>` branch names. `_cmd_create_task` passes `parent_id` through; the supervisor's future `--graph` create reuses this helper.

## 8. Events API

- `src/database/queries/event_queries.py::get_recent_events` (line 36): new `after_id: int | None`; when set, `WHERE events.id > :after ORDER BY events.id ASC LIMIT :limit` (replay mode) instead of `DESC`.
- `src/api/websocket.py::WebSocketManager.handle`: parse `after_seq` from the query string; replay persisted rows via `get_recent_events(after_id=…)` in pages, then bridge to live mode. Live mode gains a `seq` field: `log_event` (event_queries line 15) returns the inserted id, and emitters thread it into bus payloads where both exist; pure `notify.*` UI events without a DB row carry `seq: null` (documented — replay covers the persisted stream, which is the durable one).
- `src/event_schemas.py`: register `task.blocked`, `task.unblocked`, `task.skipped_conditional`, `dependency.added`, `dependency.removed`, `label.added`, `label.removed`, `gate.created`, `gate.resolved`, `gate.expired`.

## 9. Config and rollout flags

New dataclasses in `src/config.py` (wired into `AppConfig`, line 804; hot-reloadable like `monitoring`):

```yaml
state_machine:
  enforce: false          # flip to true after a warning-free observation window
work_graph:
  blocked_state_authoritative: false   # shadow mode: recompute + compare with legacy scan, log divergence
  gate_sweep_interval_seconds: 30      # pr/ci polling stays on the 60s approval throttle
  conditional_autoclose: true          # cascade auto-close of dead conditional tasks
```

Rollout order: (1) migrations + recompute in shadow mode → (2) flip `blocked_state_authoritative` after ≥1 week of zero-divergence logs → (3) gates live (additive; nothing uses them until created) → (4) flip `state_machine.enforce` after the warning audit. Rollback for (2) is a config flip — the legacy scan path is kept until the collapse phase.

## 10. Phase checklist

**Phase WG-1 — schema + projection (shadow)**
- [ ] tables.py edits + 4 Alembic revisions (§2), reviewed for SQLite and PostgreSQL; `alembic upgrade head` green on both
- [ ] `blocked_state.py` mixin; single-transaction rewrites of `transition_task`, `add_dependency`, `remove_dependency`, `delete_task`
- [ ] Shadow comparison in `_check_defined_tasks` + divergence logging; backfill verified against legacy scan on a production DB copy
- [ ] `task.blocked`/`task.unblocked` events + schema registry entries

**Phase WG-2 — typed edges + labels**
- [ ] `DepType`, `dep_type` through `_cmd_add_dependency`/`_cmd_create_task`; cycle rules incl. `validate_waits_for`
- [ ] Plan parser emits `parent-child` (+ `discovered-from`) edges; delete the `is_plan_subtask` special case
- [ ] Conditional auto-close cascade behavior behind `conditional_autoclose`
- [ ] `task_labels` + filters + `hold:*` convention in the ready frontier

**Phase WG-3 — gates + sweep**
- [ ] gate queries + `gate_commands.py` mixin; `_sweep_gates` in the cascade at core.py step 2b; `_poll_pr_merged` extraction
- [ ] Event-gate EventBus subscription + persisted-events backstop; expiry escalation events

**Phase WG-4 — explain + ready + replay**
- [ ] `src/explain.py`; `_describe_task_blocker` refactor; `_cmd_explain_task`, `_cmd_project_ready` (cross-project deps named)
- [ ] `after`/`after_seq` replay on REST + websocket; payload-registry invariant test

**Phase WG-5 — outcomes + enforcement + ids/groups**
- [ ] `_cmd_close_task` + outcome/work-state metadata helpers (column↔key sync for `pr_url`/`branch_name`)
- [ ] `failure_class` retry policy in execution.py; `state_machine.enforce` flag + `force` plumbing + call-site audit
- [ ] Hierarchical child ids; `get_group_progress` + command
- [ ] Docs: update `docs/specs/models-and-state-machine.md`, `database.md`, `command-handler.md`

## 11. Test plan

- **Recompute unit matrix** (`tests/test_blocked_state.py`): per-dep-type satisfaction truth table; gate open/resolved/expired; dynamic `waits-for` re-block on child creation; conditional terminal-failure edge; mixed multi-edge tasks.
- **Property test:** random DAGs (≤200 nodes, mixed types) — incremental recompute after each random mutation equals brute-force full evaluation.
- **Both dialects:** the existing test harness runs SQLite by default; a `postgres` marker (env-gated, CI service container) runs migrations §2 + the recompute matrix on PostgreSQL. `pytest tests/test_database.py -v` covers migration round-trips including the SQLite batch PK rebuild with pre-seeded rows.
- **Concurrency (PG):** two parallel transactions completing sibling deps of one fan-in waiter — no deadlock, final state correct; SQLite serialized-writer equivalent.
- **Cascade integration:** `_check_defined_tasks` promotion parity with legacy on recorded fixtures (shadow-mode assertion); BLOCKED-recovery rule; gate sweep with a fake `gh` (merged/closed/open), timer, task, and event gates; same-cycle unblock (gate resolve → dependent READY in one `run_one_cycle`).
- **Enforcement:** every `(status, event)` in `VALID_TASK_TRANSITIONS` accepted; invalid pairs raise when enforced and warn when not; `force=True` bypass; invariant test that no production code calls `update_task(status=…)` outside `transition_task` (AST grep).
- **Explain goldens:** one fixture per reason code; cross-project dep names the other project.
- **Replay:** REST `after` pagination is gapless and ordered; websocket replay-then-live delivers no duplicates/losses across a simulated disconnect.
- **Registry invariant:** emit-site scan → every event type registered in `EVENT_SCHEMAS`.
- **Perf:** 10k-task synthetic graph — single-edge recompute < 50 ms (SQLite), full backfill < 5 s; frontier query uses `idx_tasks_project_status_blocked` (EXPLAIN assertion).

## 12. Risks

| Risk | Mitigation |
|---|---|
| Recompute misses a mutation path → stale `is_blocked` | Shadow mode + divergence logs before authority flip; `aq doctor` check recomputes all and reports drift; every mutation funnels through the query layer (invariant test on raw status writes) |
| SQLite PK rebuild (batch mode) on task_dependencies loses rows on odd schemas | Migration test seeds edges incl. duplicates-by-type prevention; backup note in the revision docstring; table is small |
| Enforcement flips on with an uncatalogued-but-legitimate transition in the wild | Warn-only observation window with log audit gates the flip; `force=True` escape hatch; flag is hot-reloadable off |
| Gate sweep double-resolution races (button + sweep) | `resolve_gate` is idempotent CAS (`WHERE status='open'`); second resolver gets `already_resolved` |
| `event` gates missed while daemon down | Sweep backstop re-checks persisted `events` rows past the gate's creation watermark |
| PG lock contention on hot recompute paths | Single-statement UPDATE per wave; sorted id order for multi-wave; affected sets are small (direct dependents + waiters) |
| `pr_url`/`branch_name` column vs metadata-key drift | Single write helper syncs both; doctor check compares; columns retire at the collapse migration |
| Hierarchical ids collide with legacy suffix ids (`bold-summit-42`) | Child ordinal derived under the creation transaction; dot separator is unambiguous vs the legacy hyphen suffix |
