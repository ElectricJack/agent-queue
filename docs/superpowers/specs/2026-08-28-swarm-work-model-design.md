---
tags: [design, swarm, hierarchy, claim, pools, formulas, performance]
status: approved design — awaiting implementation plan
date: 2026-08-28
related:
  - ../../specs/design/work-graph.md
  - ../../specs/design/session-runtime.md
  - ../../specs/design/worktree-execution.md
  - ../../analysis/2026-08-28-beads-swarm-migration-evaluation.md
  - ../../analysis/2026-08-28-beads-properties-and-parity.md
---

# Swarm work model — hierarchy, claims, pools, formulas

## 1. Purpose

Turn Agent Queue's push-assigned task queue into a work model that supports **agent
swarms**: many concurrent sessions that pull their own work from a graph-decided ready
frontier, file the work they discover, and run multi-step methods encoded as data — while
keeping the existing store, projects, dashboard, and `aq` CLI.

The companion analysis ([[2026-08-28-beads-properties-and-parity]]) established that the
codebase already has eleven of the fifteen properties that make beads work as a swarm
ledger. This spec closes the load-bearing gaps: a **claim** primitive, **worker pools**
with a bounded worker loop, **worker-filed work** with playbook-owned policy, a
**formula** registry, and — because it is a prerequisite for correct claims over
containers — a single-authority **hierarchy** model with the two known defects fixed
(column/edge drift, flat ids in the graph creator).

## 2. Decisions taken (with the reasoning that matters)

| # | Decision | Why |
|---|---|---|
| D1 | **Exactly one parent per task.** | Dotted ids, tree, progress, auto-completion and `waits-for` are all unambiguous only under single parenthood. Cross-cutting grouping uses labels or `related`/`tracks` edges. Matches beads. |
| D2 | **The `parent-child` edge is the truth; `tasks.parent_task_id` is a derived cache written only by the query layer, in the same transaction.** | Readiness already depends on the edge. One writer makes drift impossible by construction; the column keeps O(1) parent lookup and the indexed children scan. Dropping the column would touch ~19 source files, the dashboard/Discord field contract and `archived_tasks` for no behavioural gain. |
| D3 | **Ids are immutable.** `<parent>.<n>` records where a task was born; the edge records where it lives. | Branch names (`aq/<id>`), sessions, logs and threads key on the id. |
| D4 | **Hybrid dispatch.** The daemon decides how many sessions exist per `(project, profile)` and what is admissible; pool sessions decide which ready item they take. `lifecycle: task` keeps push. | The consumer is the only party that knows precisely when it is free (work-stealing result, Blumofe & Leiserson 1999; Celery's `prefetch_multiplier=1` guidance for long tasks). Pushing into a *live* session rides the nudge path, which the repo documents as fragile; pushing into a *fresh* session is reliable but costs a cold start. Fair-share stays at the capacity layer, which is what preserves it under pull. Gas City's model. |
| D5 | **Bounded worker loop, configured per profile.** `max_claims_per_session` = 1 gives one-per-claim; 0 gives run-until-age/idle. | Both long-context and short-context workers are needed. The cap turns the context-bleed vs cold-start trade into a dial. |
| D6 | **Claim is one atomic statement.** `FOR UPDATE SKIP LOCKED` on Postgres; CAS-and-retry on SQLite. | Only shape that meets the latency budget; beads/Gas City's shape. |
| D7 | **Pool sizing is one cascade step** (`_reconcile_pools`) reusing `sessions.desired_state` and the session lens. | The representational prerequisite landed 2026-08-27; the session-runtime comparison named this as the missing half. |
| D8 | **Workers may file work; policy is a playbook.** Mechanism in code (project pinned, provenance edge automatic, starts `DEFINED`, `created_by` stamped); what happens next is a `task.created` pipeline rule. | Keeps D2 of the framework overhaul ("the supervisor decides what runs") while letting the graph grow from work. |
| D9 | **Formulas are markdown + `aq-graph`**, in the vault, project-shadowed by name. | Reuses the existing parser/validator/creator and the vault watcher; no second format. |
| D10 | **Postgres semantics first; SQLite parity second.** Nothing SQLite-only on a hot path. | Production target is Postgres. |

Prior decisions this spec relies on and does not reopen: work-graph typed edges, persisted
`is_blocked`, gates, labels, metadata-first; session-runtime explicit completion
(`task close` → `drain-ack`, exit ≠ success); worktree slots and the merge slot; framework
overhaul D9 (one daemon, N projects, single DB).

## 3. Non-goals

- Replacing the store (see the migration evaluation — revisit with data).
- Control steps in formulas (retry-with-class, check loops, fan-out over a runtime set) —
  comparison §9.2, a later spec.
- Multiple parents, wave-driven scheduling, ephemeral/wisp tier, cross-machine sync.
- Changing the merge pipeline. Each claim still gets its own branch and goes through
  `_phase_integrate` under the merge slot.
- Agentic rework on merge conflict (`REWORK_REQUEST` analogue) — separate spec.

---

## Part I — Hierarchy

### 4. Data model

**Schema changes** (one Alembic revision, §14):

| Table | Change |
|---|---|
| `task_dependencies` | partial unique index `uq_task_deps_single_parent (task_id) WHERE dep_type = 'parent-child'` |
| `tasks` | `next_child_ordinal INTEGER NOT NULL DEFAULT 1` |
| `tasks` | `created_by_kind TEXT NULL` (`user \| session \| system \| playbook`), `created_by_id TEXT NULL` |

`parent_task_id` stays, with its FK and `idx_tasks_parent`, and gains the rule: **only
`set_parent` writes it.** `archived_tasks` gains `created_by_kind/created_by_id` (lossless
copy rule).

**Invariants** (all tested; `aq doctor` checks in §12):

1. `parent_task_id = p` ⇔ an edge `(task_id, p, 'parent-child')` exists. Both null/absent
   together.
2. At most one `parent-child` out-edge per task (index-enforced).
3. `parent.project_id = child.project_id`.
4. Depth (dot-segments of the id) ≤ `_MAX_HIERARCHY_DEPTH` (3). A child of a depth-3 parent
   gets a root id and a `discovered-from` edge instead (existing fallback).
5. No `parent-child` cycles (existing `validate_dag_with_new_edge` covers blocking types).

### 5. The single writer: `set_parent`

```python
async def set_parent(self, task_id: str, parent_id: str | None, *, conn) -> set[str]:
    """Move task_id under parent_id (or to root). Returns blocked-state flips.

    Same transaction: delete any existing parent-child edge, insert the new one,
    write tasks.parent_task_id, recompute is_blocked over the affected set
    (old container's waits-for waiters ∪ new container's ∪ the task itself).
    Raises HierarchyError(code) for: not_found, cross_project, cycle, depth,
    self_parent.
    """
```

Callers, and only these: `create_task` (when `parent_id` is given), `add_dependency` and
`remove_dependency` when `dep_type == 'parent-child'` (they delegate instead of inserting
the edge themselves), `reparent_task`, `delete_task`, `archive_task`,
`create_task_graph`'s `write_plan`. `add_dependency` with `parent-child` on a task that
already has a different parent is a **reparent**, not a second edge — it goes through the
same function and the same validation.

`_collect_affected` (blocked_state.py) already expands seeds to `waits-for` waiters over
containers; `set_parent` seeds it with `{task_id, old_parent, new_parent}`.

### 6. Ids

`child_task_id(db, parent_id, *, conn)` becomes:

```sql
UPDATE tasks SET next_child_ordinal = next_child_ordinal + 1
WHERE id = :parent RETURNING next_child_ordinal - 1
```

(SQLite: `UPDATE … RETURNING` is supported ≥ 3.35; the fallback for older SQLite is
`SELECT` + `UPDATE` inside the same transaction, which is serialised by SQLite's writer
lock.) The sibling scan in `_next_child_ordinal` is deleted. Ordinals are never reused;
deletes leave gaps, which is correct — an id must never be re-minted.

**Graph creator.** `assign_child_ids(db, parent_id, keys, *, conn)` is implemented, not
stubbed:

- New container (today's path): the container row is inserted with
  `next_child_ordinal = len(keys) + 1` and nodes get `<parent>.1 … .N` in document order.
  Zero extra round trips; ids are known at `build_plan` time, so `--dry-run` shows the
  real ids.
- `--parent <existing>` (new): ordinals are reserved from the existing row's counter
  **inside the write transaction**. `--dry-run` shows `<parent>.?` placeholders labelled
  `provisional: true`, because reserving in a dry run would burn ordinals.
- Depth is validated before anything is written: a `--parent` at depth 3 is rejected with
  `hierarchy.depth` (a graph cannot fall back to root ids node-by-node without lying about
  its structure).

### 7. Container semantics

**Release.** Children are withheld while the container is `DEFINED` or
`AWAITING_PLAN_APPROVAL` (`_parent_child_unsat`, unchanged).

**Auto-completion — event-driven.** In `transition_task`, when a task reaches
`COMPLETED` and has a parent:

```sql
UPDATE tasks p SET status = 'COMPLETED', updated_at = :now
WHERE p.id = :parent
  AND p.status = 'IN_PROGRESS'
  AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.task_id = p.id
                  AND s.state IN ('starting','running','draining'))
  AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_task_id = p.id
                  AND c.status <> 'COMPLETED')
```

executed in the same transaction, then (if it flipped) the parent's own parent is checked
the same way, bounded by depth 3. The completion logs `context="subtasks_completed"` and
emits `task.completed` for the container after commit. The **no-live-session guard** is
what lets a worker that spawned subtasks keep ownership of its own task until it closes
it explicitly.

`_check_plan_parent_completion` is **deleted**. A backstop `_sweep_container_completion`
runs every `work_graph.container_sweep_interval_seconds` (default 60) as one aggregate
statement — the same predicate over all `IN_PROGRESS` containers — and logs any hit as a
divergence (it should find nothing).

A child ending `FAILED` or `BLOCKED` does not complete or fail the container; it stays
`IN_PROGRESS`, `waits-for` waiters stay blocked, and `explain` on the container lists the
open children.

**Delete.** `delete_task` refuses a container with children (`hierarchy.has_children`);
`cascade: true` deletes the subtree depth-first in one transaction, snapshotting affected
waiters before edges disappear (existing pattern).

**Archive.** Subtree-atomic. A container is archivable only when every descendant is
terminal (`COMPLETED`/`FAILED`); the subtree archives together in one transaction, root
last. `_auto_archive_tasks` selects only roots-of-terminal-subtrees. The current
`UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = :id` in `archive_task` is
removed.

**Reparent.** `reparent_task(task_id, parent_id | None)` → `set_parent`. Rejections:
`cross_project`, `cycle` (new parent is a descendant), `depth` (task's subtree height +
new depth > 3), `not_found`. The id does not change (D3).

### 8. Reads

| Query | Today | Spec |
|---|---|---|
| `get_subtasks(parent)` | indexed column scan | unchanged |
| `get_task_tree(root, max_depth)` | recursive N+1 | one `WITH RECURSIVE` CTE over `parent_task_id` bounded by `max_depth`, plus one edge query when annotations are requested — **2 statements** |
| `get_children(parent, recursive, status, limit, offset)` — new | — | column scan or the same CTE; paginated |
| `get_group_progress(parent)` | 2 statements | unchanged; add `max_parallelism = max(len(w) for w in waves)` and `depth` to the payload |
| `task_show` | — | adds `parent: {id, title, status}` and `children: {total, done, ready, blocked, in_progress}` (one aggregate statement, only when the task has children) |

---

## Part II — Claim, pools, worker loop, worker-filed work

### 9. Data model

| Table | Change |
|---|---|
| `agent_profiles` | `lifecycle` accepts `pool`; new `min_active INTEGER NULL`, `max_active INTEGER NULL`, `max_claims_per_session INTEGER NULL` (pool-only; parser rejects them on other lifecycles like it does `mode`/`wake_mode`) |
| `sessions` | `task_id` is mutable for `lifecycle = 'pool'`; new `claims INTEGER NOT NULL DEFAULT 0`; new `agent_id TEXT NULL` (soft ref to the agent row a pool session owns, §11.1) |
| `tasks` | new index `idx_tasks_ready_by_profile (project_id, profile_id, status, is_blocked)` |
| `events` (schema registry) | `task.claimed` `{task_id, project_id, title, session_id, profile_id}`; `task.claim_conflict` `{task_id, project_id, session_id}` (debug); `pool.scaled` `{project_id, profile_id, desired, active, reason}` |

No claim columns on `tasks`: the holder is `assigned_agent_id` (each pool session owns an
agent row for its lifetime, §11), held-since is the `task.claimed` event.

### 10. Claim — `task_claim`

**Arguments.** `task_id` *or* `next: true`; `wait: int` seconds (0 default; max
`swarm.claim_wait_max`, default 60). `session_id`, `project_id`, `profile_id` come from the
token scope — a caller-supplied value that disagrees is `out_of_scope`.

**Preconditions** (cheap, from the per-tick cached scheduler snapshot; no recompute):
project `ACTIVE`; no `pause_scheduling` constraint; global and project budget not
exhausted. Session preconditions: `lifecycle in ('pool','task')`; for pool, `claims <
max_claims_per_session` when the cap is set, and `desired_state = 'running'`. A pool
session that already holds a task may only re-claim that same task (idempotent).

**Work query** — what a pool session may take:

```
project_id = :project
AND status = 'READY' AND is_blocked = 0
AND NOT EXISTS (SELECT 1 FROM task_labels l WHERE l.task_id = tasks.id AND l.label LIKE 'hold:%')
AND (profile_id = :profile OR (profile_id IS NULL AND :profile = :project_default_profile))
AND NOT EXISTS (SELECT 1 FROM task_workspace_requirements r
                WHERE r.task_id = tasks.id AND r.kind_id <> 'project-repo')
```

Tasks with non-default workspace requirements stay on the push path (a pool session holds
exactly one `project-repo` slot). Ordering: `(affinity_agent_id = :agent) DESC,
priority ASC, created_at ASC`. Affinity is a preference under pull, not a hold; the 120 s
affinity wait remains push-only.

**Postgres** (one transaction, ≤ 3 statements):

```sql
WITH cand AS (
  SELECT id FROM tasks WHERE <work query> [AND id = :task_id]
  ORDER BY <ordering> LIMIT 1
  FOR UPDATE SKIP LOCKED
)
UPDATE tasks t SET status = 'IN_PROGRESS', assigned_agent_id = :agent, updated_at = :now
FROM cand WHERE t.id = cand.id
RETURNING t.*;
```

**SQLite:** `SELECT id … LIMIT 1` then
`UPDATE tasks SET … WHERE id = :id AND status = 'READY' AND is_blocked = 0 AND assigned_agent_id IS NULL`;
`rowcount = 0` → re-select, up to 5 attempts, then `claim_conflict`.

**After the DB transaction** (in order, each idempotent):
1. `sessions.task_id = :task, claims = claims + 1` (pool only; `task` lifecycle already
   has it).
2. `task_metadata`: `claimed_by_session`, `work_dir`.
3. `reset_slot_for_task(slot, task)` on the session's worktree slot. Failure → task is
   released back to `READY` with `needs_attention = slot_reset_failed`, the session gets
   `slot_reset_failed`, and the reconciler's ladder takes over.
4. `task.claimed` then `task.started` on the bus (the latter keeps every existing
   subscriber — dashboard, Discord, playbooks — working unchanged).

**Result** — `{"result": <code>, "task": <task_show shape> | null, "session": {claims, cap, desired_state}}` with codes:

| Code | Meaning | Agent's move |
|---|---|---|
| `claimed` | task is yours | `aq prime --task <id>`, work |
| `no_ready_work` | frontier empty (after `wait`) | pool: drain-ack unless you want to wait again |
| `claim_conflict` | explicit `task_id` was taken | try `--next` |
| `session_exhausted` | `max_claims_per_session` reached | `aq session drain-ack` |
| `drain_requested` | pool scaled down | `aq session drain-ack` |
| `out_of_scope` | token mismatch / not a claimable lifecycle | stop |

**Long-poll.** With `wait > 0` and an empty frontier, the handler subscribes to
`task.unblocked`, `task.created`, `task.restarted`, `gate.resolved` filtered on
`project_id`, retries the claim on each event, and returns `no_ready_work` on timeout.
Cost: one waiting HTTP request per idle session; no polling.

**`close --claim-next`.** `task_close` gains `claim_next: bool`. It runs the existing close
(outcome metadata, completion pipeline, token revoke is **skipped** for pool sessions
because the token is session-scoped, not task-scoped) and then `task_claim(next=True)`,
returning `{…close result…, "next": <claim result>}`. A failed close never claims.

### 11. Pool reconciler — `_reconcile_pools`

New cascade step, after `_reconcile_sessions`, gated by `swarm.enabled`. Per tick:

1. **Demand** (1 statement): ready count grouped by `(project_id, COALESCE(profile_id,
   project.default_profile_id))` over the work query minus the profile term.
2. **Supply** (1 statement): live pool sessions grouped by `(project_id, profile_id)` with
   `state`, `desired_state`, `task_id IS NULL` (idle) counts.
3. **Desired** — pure function `size_pools(demand, supply, profiles, projects, caps,
   deficits) -> list[PoolAction]` in `src/scheduler.py` beside `Scheduler.schedule`
   (table-tested, no I/O):
   - `desired = clamp(demand, min_active, max_active)` per `(project, profile)`;
   - project bound: `Σ desired over profiles ≤ project.max_concurrent_agents −
     non-pool BUSY agents`; when it binds, marginal sessions go to the profile with the
     largest unmet demand;
   - global bound: `Σ ≤ global cap − others`, plus the usage-aware headroom hook
     (`headroom_fn: (project) -> int | None`, default None); when it binds, marginal
     sessions go to the project with the largest `BudgetManager` deficit — fair-share at
     the capacity layer;
   - hysteresis: scale-up at most `swarm.max_starts_per_tick` (default 2) sessions per
     tick; scale-down only for surplus that has persisted `swarm.scale_down_grace`
     seconds (default 120), tracked in memory per key.
4. **Converge**: for each `start` action → `_launch_pool_session` (§11.1); for each
   `drain` action → `update_session(desired_state='stopped')` on an **idle** pool session
   (never one holding a task; the next `claim` returns `drain_requested`).
5. Emit `pool.scaled` when desired or active changed for a key.

`AgentReconciler` is taught `lifecycle='pool'`: it neither creates nor reaps agent rows
for pool profiles — pool sessions own their rows (§11.1) — and counts them toward
`max_concurrent_agents` like any BUSY/IDLE row.

#### 11.1 `_launch_pool_session(project, profile)`

Mirrors `_launch_session_for_task` step for step, differences only:

- creates an `agents` row (`profile_id`, `state=IDLE`) first; the session references it
  via `sessions.agent_id` — **new column** `sessions.agent_id TEXT NULL` (soft ref) so the
  claim can set `assigned_agent_id` without a lookup;
- acquires one `project-repo` worktree slot for the **session** via
  `acquire_for_holder(db, project_id, holder_agent_id, kinds=['project-repo'])` — a thin
  generalisation of `acquire_for_task` whose lock columns already use `agent_id`; the lock
  is held for the session's life; `locked_by_task_id` is updated on each claim;
- mints the token with `task_id=None`, `project_id` pinned, `elevated=False`;
- `build_pool_spec` (new, beside `build_task_spec`/`build_named_spec`): session name
  `p-<profile>--<project>--<n>`, `lifecycle="pool"`, `POOL_BOOTSTRAP_PROMPT`, env adds
  `GIT_AUTHOR_NAME=aq/<profile>/<session-short>`, `GIT_AUTHOR_EMAIL=<profile>@agent-queue`;
  `AQ_TASK_ID` omitted;
- adoption: `adopt_on_start` scans the `p-` prefix like `s-`/`n-`.

`POOL_BOOTSTRAP_PROMPT`:

```
You are a {profile} pool worker for project {project} in {work_dir}.
Loop:
  1. `aq task claim --next --wait 60`
     - claimed        → `aq prime --task <id>`, then do the work
     - no_ready_work  → run the claim again (the wait is cheap); after three empty
                        waits run `aq session drain-ack`
     - session_exhausted / drain_requested → `aq session drain-ack`
  2. Before anything quiet for more than a few minutes: `aq task heartbeat <id>`
  3. When done: `aq task close <id> --outcome pass --work-outcome shipped --summary "..." --claim-next`
     and continue with the task it returns.
Exiting without closing your task is treated as a failure, not a success.
```

#### 11.2 Reconciler carve-outs for `lifecycle='pool'`

| Step | `task` behaviour | `pool` behaviour |
|---|---|---|
| `_step_orphans` (a) live session, task closed | drain | **normal between claims**; drain only if `desired_state='stopped'`, or idle (`task_id IS NULL`) longer than `profile.idle_timeout` |
| `_step_orphans` (b) open task, no live row | BLOCKED + release | same (task released to `READY` — pool tasks are retried, not blocked, unless the verdict was `PRODUCTIVE_DEATH`) |
| `_step_exits` | verdicts as today | same; on any verdict with a held task, the task is released via `_release_task` and `sessions.task_id` cleared |
| stall ladder | applies | applies **only while `task_id IS NOT NULL`** |
| `_step_drain_ack` premature-drain guard | ack with open task → nudge | same |
| `_step_backstop` | `stuck_timeout_seconds` | same, per held task |

`_cmd_task_close`, `task_set`, `task_heartbeat` verify ownership through
`sessions.task_id == task_id` (`_assert_session_owns`), not the token's `task_id`, which is
null for pools. Token revoke on close is skipped for pools; the token is revoked at drain.

### 12. Worker-filed work

**Scope.** `AGENT_COMMAND_SET` += `create_task`, `task_claim`, `task_children`,
`task_progress`, `project_ready`, `formula_list`, `formula_show`.

**Server-enforced constraints on `create_task` for non-elevated sessions** (in
`enforce_scope` + `_cmd_create_task`):

- `project_id` := token project (mismatch → `out_of_scope`);
- `created_by_kind='session'`, `created_by_id=<session_id>`;
- if the session holds a task `T`: when `parent_id` is given it must be `T` or a
  descendant of `T` (else `hierarchy.parent_out_of_scope`); when absent, a
  `discovered-from` edge to `T` is added automatically;
- initial status **`DEFINED`** regardless of edges (the cascade promotes it);
- `profile_id` may be omitted (routing decides) or set only to the session's own profile.

**Policy lives in the default pipeline.** `task.created` gains optional payload fields
`created_by_kind`, `created_by_id`, `parent_task_id`, `discovered_from` (schema registry
updated; required triple unchanged). The shipped `default-pipeline.md` rule:

```yaml
- id: worker-filed-routing
  on: task.created
  when: { created_by_kind: session }
  steps:
    - when: { parent_task_id: null }          # root-level discovered work
      action: gate_create
      args: { gate_type: routing, waiter_task_ids: ["{{event.task_id}}"] }
    - when: { parent_task_id: null }
      action: ensure_task
      args: { dedup_key: triage-open, profile_id: triage, title: "Triage open work" }
    # subtasks of the creator's own task: no step — they inherit and proceed
```

**Ordering guarantee.** `task.created` is emitted inside `_cmd_create_task` before it
returns; the pipeline runner dispatches on the same event loop; promotion `DEFINED →
READY` happens no earlier than the next cascade tick (≥ 5 s later, and `_check_defined_tasks`
is one statement under `blocked_state_authoritative`). A gate or `hold:` label attached
by the rule therefore always lands before the task can enter the frontier. Tested with a
fake clock: create → assert not claimable until the tick after the gate resolves.

---

## Part III — Formulas

### 13. Format, registry, commands

**File:** `vault/formulas/<name>.md` (system) or `vault/projects/<pid>/formulas/<name>.md`
(project shadows system by `name`). Frontmatter:

```yaml
name: review-and-fix
description: Review a branch, fix findings, re-review
vars:
  branch: {required: true}
  reviewer: {default: reviewer, enum: [reviewer, final-reviewer]}
extends: base-review          # optional, single inheritance, same scope resolution
```

Body: exactly one fenced `aq-graph` block (existing schema: `parent`, `nodes[]` with
`key/title/description/acceptance/context/needs/labels/priority/profile/task_type`;
`{var}` references). `vars` in frontmatter **declare**; values arrive at cook time; the
block's own `vars:` key is disallowed in a formula (`formula.vars_in_body`).

**Resolution order at cook:** load `extends` chain (root first; cycle → error) → merge
(`parent` field-wise; nodes by `key`, child wins, new keys appended in child order) →
validate declared vars against supplied values (`required` missing, value ∉ `enum`) →
apply defaults → `substitute_vars` → `validate_graph` → `create_graph`.

**Registry.** `FormulaRegistry` in `src/task_graph/formulas.py`: in-memory, loaded and
reloaded by the vault watcher (`formulas/*.md`, `projects/*/formulas/*.md`), same pattern
as `HarnessRegistry`. Parse errors are logged, surfaced by `aq doctor` (`formulas.parse`),
and the file is skipped.

**Commands.**

| Command | Args | Effect |
|---|---|---|
| `formula_list` | `project_id?` | names, descriptions, scope (`system`/`project`), var declarations |
| `formula_show` | `name`, `project_id?`, `vars?` | resolved graph + validation findings; **no writes** |
| `formula_cook` | `name`, `project_id`, `vars`, `parent_id?`, `dry_run?` | `create_task_graph` under a new container or `--parent`; container gets `task_metadata` `formula=<name>`, `formula_vars=<json>`, label `formula:<name>` |

CLI: `aq formula list [-p]`, `aq formula show <name> [--var k=v]…`,
`aq formula cook <name> -p <pid> [--var k=v]… [--parent <id>] [--dry-run]`.

---

## Part IV — Surface, performance, testing, rollout

### 14. Consolidated command / CLI surface

Every row is a `_cmd_*`; REST routes, OpenAPI, the Python/TS clients and MCP tools
regenerate from it. `*` = new. Response models: add `src/api/models/task.py` entries for
`TaskClaimResult`, `TaskChildren`, `TaskProgress`, `PoolStatus`, `FormulaShow`.

| CLI | Command | Agent scope |
|---|---|---|
| `aq task create … [--parent <id>] [--discovered-from <id>]` | `create_task` | yes* (constrained, §12) |
| `aq task claim [<id> \| --next] [--wait S]`* | `task_claim`* | yes |
| `aq task close <id> --outcome … [--claim-next]` | `task_close` | yes |
| `aq task children <id> [--recursive] [--status S] [--brief]`* | `task_children`* | yes |
| `aq task progress <id>`* | `task_progress`* | yes |
| `aq task reparent <id> (--parent <id> \| --root)`* | `reparent_task`* | no |
| `aq task tree <id>` | `get_task_tree` | yes |
| `aq task delete <id> [--cascade]` | `delete_task` | no |
| `aq task show <id>` (+ `parent`, `children`, `claimed_by`) | `task_show` | yes |
| `aq task ready [-p] [--profile] [--brief]`* | `project_ready` (+ `profile_id` filter) | yes |
| `aq formula list \| show \| cook`* | `formula_*`* | list/show yes; cook no |
| `aq pool status [-p] [--profile]`* | `pool_status`* — desired/active/idle/claims per key, last `pool.scaled` reason | no |
| `aq pool scale <profile> [-p] --min N --max N`* | `pool_scale`* — edits the profile's `## Config` in the vault (source of truth), sync follows | no |
| `aq session drain-ack` | `session_drain_ack` | yes |
| `aq schema` | `get_schema` (+ `outcome`, `work_outcome`, `failure_class`, `session_state`, `claim_result`, `lifecycle`) | yes |

`--brief` projections: `task_children` → `id,title,status,priority,is_blocked`;
`task_claim` → `result,task.id,task.title`.

### 15. Performance

#### 15.1 Principles (the path to fleet scale)

1. **Every cascade step is O(statements), not O(tasks).** No query inside a loop over
   tasks or sessions. Aggregates, `IN (:ids)` sets, recursive CTEs. Enforced by a
   statement-counting engine fixture asserting a fixed count per step.
2. **Hot paths are one transaction, index-backed.** Claim, `set_parent`, container
   auto-complete, `is_blocked` recompute. Every predicate has a covering index
   (`idx_tasks_project_status_blocked`, `idx_tasks_parent`, `uq_task_deps_single_parent`,
   `idx_tasks_ready_by_profile`, `idx_task_deps_task_type`, `idx_task_deps_depson_type`).
3. **Postgres semantics first, SQLite parity second.** `SKIP LOCKED`, `RETURNING`,
   partial indexes; SQLite gets the CAS/retry equivalent and identical statement counts.
4. **Event-driven with a low-cadence backstop.** Container completion, blocked flips, and
   claims happen on the mutation; sweeps run at ≥ 60 s and are single statements that
   should find nothing.
5. **Long-poll, never spin.** Idle pool sessions wait on the bus inside one request.
6. **Admission reads a snapshot.** Budget/constraint checks inside claim read the per-tick
   scheduler snapshot; the tick refreshes it once.
7. **Bounded fan-out per tick.** `max_starts_per_tick`, scale-down grace; a tick's work is
   bounded regardless of demand.
8. **Measure before tuning.** `prompt_analytics` logs prime size per claim; `command.invoked`
   carries `duration_ms`; `pool.scaled` records every sizing decision.

Fleet step-ups this design leaves open, in order: partition pool sizing by project across
worker processes (the sizing function is pure); move claim long-poll to `LISTEN/NOTIFY`;
materialise the per-`(project, profile)` ready count as a trigger-maintained table when
the demand aggregate exceeds the tick budget; replace the per-tick session enumeration
with provider-side change feeds.

#### 15.2 Budgets (acceptance tests — Postgres; solo/small-team scale)

Scale: 5,000 active tasks, 10 projects, 25 concurrent sessions, containers ≤ 200 children,
depth ≤ 3, 5,000 archived tasks, 50,000 events.

| Path | Budget |
|---|---|
| `task_claim --next` DB portion, p99 | ≤ 50 ms; ≤ 3 statements |
| `task_claim` end-to-end incl. slot reset, p99 | ≤ 3 s (git-bound; reported separately) |
| 20 concurrent `claim --next` on a 10-task frontier | exactly 10 `claimed` + 10 `no_ready_work`; 0 double-claims; both dialects |
| `_reconcile_pools` per tick | ≤ 2 statements + starts; ≤ 20 ms DB |
| Cascade tick DB time (promotion + gates + sessions + pools + messages) | ≤ 250 ms |
| `get_task_tree` / `task_children --recursive` / `task_progress` | ≤ 3 statements, size-independent |
| `set_parent`, `create_task --parent` | ≤ 4 statements, one transaction |
| `create_task_graph`, 200 nodes | one transaction, ≤ 1 s |
| container auto-complete after last child | same transaction as the child's close; 0 extra round trips |
| `aq prime` per claim | ≤ 2,000 tokens, logged |
| push-vs-pull A/B (same 50-task set, `lifecycle: task` vs `pool` cap 5) | report wall-clock, tokens, cold starts; no threshold — it is the measurement D4 asks for |

`tests/perf/` seeds this scale (fixture reused by all perf tests), runs on a Postgres
service in CI, and asserts the table. SQLite runs the same suite with timing relaxed 4×
and statement counts identical.

### 16. Testing

**Unit.** `set_parent` invariants (edge ⇔ pointer, single parent index, cycle/depth/
cross-project rejection, affected-set includes both containers' `waits-for` waiters);
ordinal counter under concurrent inserts (both dialects); dotted ids in `build_plan` with
and without `--parent`, dry-run provisional ids; claim CAS (explicit and `--next`), work
query filtering (profile, default profile, holds, multi-kind exclusion), affinity
ordering, every result code; `size_pools` table-driven (min/max, project cap binding,
global cap with deficit, hysteresis, `max_starts_per_tick`); formula `extends` merge,
var validation, shadowing, cycle detection; scope enforcement for worker `create_task`.

**Integration on `FakeProvider`.** Pool lifecycle start → claim → close `--claim-next` →
`session_exhausted` → drain-ack → row `stopped`; crash with a held task → task `READY`
within `lease_ttl + 1 tick`, salvage patch on the task, next claimant sees it in `prime`;
`drain_requested` never interrupts a held task; worker-filed subtask claimable next tick,
worker-filed root gated before promotion; container auto-completes in the child's close
transaction and not while the container's own session is live; archive subtree atomic;
delete refuses/cascades; reparent re-evaluates waiters; adoption of `p-` sessions after
daemon restart; push-vs-pull A/B.

**Doctor checks.** `hierarchy.parent_pointer` (edge ⇔ column, `--fix` rewrites the column
from edges), `hierarchy.single_parent`, `hierarchy.depth`, `formulas.parse`, `pools.stuck`
(desired > active for > 10 ticks), `pools.orphan_agents` (agent rows with no live pool
session).

**Invariant tests.** Every new event type registered with a payload; every new `_cmd_*`
has a tool definition or is in the exclusion list; `AGENT_COMMAND_SET` entries all exist;
statement counts per cascade step.

### 17. Rollout, flags, migrations

**Prerequisite.** `work_graph.blocked_state_authoritative: true`. Read the divergence log
first; the flip removes the legacy per-task scan in `_check_defined_tasks`, which this
spec's tick budget assumes.

**Flags.** New `SwarmConfig`:

```yaml
swarm:
  enabled: false            # gates _reconcile_pools and lifecycle: pool launches
  claim_wait_max: 60
  max_starts_per_tick: 2
  scale_down_grace: 120
work_graph:
  container_sweep_interval_seconds: 60
```

Hierarchy changes (Part I) ship **ungated** — they fix drift and remove a per-tick scan.
`task_claim` is callable by `lifecycle: task` sessions even with `swarm.enabled: false`
(idempotent re-claim only), so the command surface is stable before pools turn on.

**Migration** (one revision, DDL + one data step):

- `tasks`: `next_child_ordinal`, `created_by_kind`, `created_by_id`; index
  `idx_tasks_ready_by_profile`; `archived_tasks` mirrors the two provenance columns.
- `task_dependencies`: `uq_task_deps_single_parent` partial unique index (SQLite and
  Postgres both support partial indexes; written by hand in the revision).
- `sessions`: `claims`, `agent_id`.
- `agent_profiles`: `min_active`, `max_active`, `max_claims_per_session`.
- Data step: for every `parent-child` edge set `parent_task_id`; for every non-null
  `parent_task_id` with no edge, insert the edge; where a task has **two** parent edges,
  keep the one matching the column (or the oldest) and log the rest to a report — the
  unique index is created *after* this step; backfill `next_child_ordinal = 1 + max
  existing ordinal` per parent.

**Shipped profile defaults** (inert until `swarm.enabled`): `worker-fast`
(`lifecycle: pool, max_claims_per_session: 2, max_active: 3`), `worker-standard`
(`pool, 5, 3`), `worker-deep` (`pool, 0, max_session_age: 14400, max_active: 1`).
`min_active: 0` everywhere.

**Implementation plan split** (each independently mergeable):
1. Part I — hierarchy, graph creator, migration, doctor checks, `children/progress/reparent`.
2. Part II — claim, pools, worker loop, scope/filing, pipeline rule. Depends on 1's
   migration.
3. Part III — formulas.

### 18. Property crosswalk

| Beads property (parity doc) | Delivered by |
|---|---|
| P1 graph decides | `blocked_state_authoritative` flip + claim over `get_ready_frontier` |
| P2 fenced claim + leases | §10 (token-fenced, CAS/SKIP LOCKED); existing leases |
| P4 typed close | `--claim-next` on the existing typed close |
| P5 agents file work | §12 |
| P6 ordering primitives | Part I (single parent, subtree archive/delete, event-driven completion) |
| P7 workflows as data | Part III |
| P11 provenance | `created_by_*`, `GIT_AUTHOR_NAME` per session |
| P9 context frugality | `aq schema` completion, prime size per claim |
