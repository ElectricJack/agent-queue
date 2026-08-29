---
tags: [design, swarm, hierarchy, claim, pools, formulas, performance]
status: approved design, revised after adversarial review (2026-08-28) — awaiting implementation plan
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
| D5 | **Bounded worker loop, configured per profile.** `max_claims_per_session` = 1 gives one-per-claim; `NULL` (unset) gives run-until-age/idle; `0` is rejected. | Both long-context and short-context workers are needed. The cap turns the context-bleed vs cold-start trade into a dial. |
| D6 | **Claim is one transaction that records the holder.** Session row locked, task taken with `FOR UPDATE SKIP LOCKED` (CAS-and-retry on SQLite), session/agent/workspace updated together; a per-claim `claim_epoch` fences every later mutation. | Only shape that meets the latency budget *and* leaves a consistent holder after any crash; beads' `row_lock` + Gas City's session fencing, combined. |
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
- Changing the merge pipeline. Every pullable task has its own branch and goes through
  `_phase_integrate` under the merge slot; plan subtasks, which share their parent's
  branch, are excluded from pull (§10) rather than given branch-per-child integration.
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

**Two depths, defined separately** (they diverge after a reparent because ids are
immutable, D3):

- **Structural depth** — the length of the live `parent-child` chain from a task to its
  root (root = 1). This is the invariant: ≤ `MAX_STRUCTURAL_DEPTH` (3). `reparent_task`
  validates `new_parent.structural_depth + subtree_height(task) ≤ 3`.
- **Naming depth** — the number of dot-segments in the id. It governs only whether a
  *dotted* child id can be minted: a parent whose id already has 3 segments mints root ids
  for its children (with a `discovered-from` edge, existing fallback), even if its
  structural depth is 1 after a move to root. Naming depth never blocks a structural
  operation.

**Invariants** (all tested; `aq doctor` checks in §16):

1. `parent_task_id = p` ⇔ an edge `(task_id, p, 'parent-child')` exists. Both null/absent
   together.
2. At most one `parent-child` out-edge per task (index-enforced).
3. `parent.project_id = child.project_id`.
4. Structural depth ≤ 3.
5. No `parent-child` cycles (existing `validate_dag_with_new_edge` covers blocking types).
6. A `COMPLETED` container has no non-terminal child (§7, container-close semantics).

### 5. The single writer: `set_parent`

```python
async def set_parent(self, task_id: str, parent_id: str | None, *, conn) -> set[str]:
    """Move task_id under parent_id (or to root). Returns blocked-state flips.

    Same transaction: delete any existing parent-child edge, insert the new one,
    write tasks.parent_task_id, recompute is_blocked over the affected set
    (old container's waits-for waiters ∪ new container's ∪ the task itself),
    set task_metadata.container='1' on new_parent if not already set, then
    settle_containers({old_parent, new_parent}) (§7) so a container that just
    lost its last open child completes, and one that gained an open child is a
    valid target. Raises HierarchyError(code) for: not_found, cross_project,
    cycle, depth, self_parent, container_closed.
    """
```

`conn` is mandatory: `set_parent` never opens its own transaction, so every caller's
membership change, blocked recompute and container settlement commit together.

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

**Transition machinery refactor (prerequisite).** `transition_task` today opens its own
transaction and has no `conn` parameter. It is split into `_apply_transition(conn, task_id,
new_status, *, context, event, force, **cols) -> TransitionResult` (read, validate, apply,
recompute `is_blocked`, retire satisfied gates — everything it does now, on a caller-owned
connection) and the public `transition_task`, which opens the transaction, calls it, and
emits after commit. Nothing in this spec writes `tasks.status` with raw SQL.

**Containers are marked, not inferred.** `task_metadata.container = '1'` is written in the
same transaction that gives a task its first child — by `set_parent` (any path),
`create_task_graph`, `formula_cook`, `approve_plan`. It is never cleared: a container
whose children were all moved away is still a container, which is what lets settlement
handle the empty case without guessing whether an `IN_PROGRESS` leaf is mid-launch.

**Container settlement — one set-based fixpoint.** `settle_containers(seeds: set[str],
conn) -> TransitionResult` evaluates, for every id in `seeds` (deduplicated, bounded by
depth 3 as it walks up), the predicate

```
container flag set
∧ status = IN_PROGRESS
∧ no live session holds it        (sessions.task_id = id ∧ state ∈ starting|running|draining)
∧ no non-COMPLETED child          (vacuously true when empty)
```

An emptied container (last child deleted or reparented away) therefore settles
immediately; the "has ≥ 1 child" clause of the previous draft is gone.

and applies `_apply_transition(conn, id, COMPLETED, context="subtasks_completed")` to each
hit, adding its parent to the next round. It is called in the same transaction by: the
child's own `_apply_transition` when the new status is `COMPLETED` (seed = parent);
`set_parent` (seeds = old and new parent); `delete_task` / `archive_task` (seed = parent).
The **no-live-session guard** is what lets a worker that spawned subtasks keep ownership
of its own task until it closes it explicitly. The container's `task.completed` is emitted
after commit like any other transition.

`_check_plan_parent_completion` is **deleted**. A backstop `_sweep_container_completion`
runs every `work_graph.container_sweep_interval_seconds` (default 60): one aggregate
statement selects containers matching the predicate, then `settle_containers` on that set
in one transaction, logging every hit as a divergence (the event path should leave it
nothing to find).

**Container-close semantics** (closes the lifecycle under every mutation):

- A child ending `FAILED` or `BLOCKED` does not complete or fail the container; it stays
  `IN_PROGRESS`, `waits-for` waiters stay blocked, `explain` on the container lists the
  open children.
- Explicit close of a container (`task_close`, `skip_task`, `set_task_status → COMPLETED`)
  with non-terminal children is refused with `hierarchy.open_children` (beads'
  `ErrCloseOpenChildren`). There is no force that leaves open children under a completed
  container — that would break invariant 6. The operator's option is
  `--abandon-children`: in the same transaction every non-terminal descendant is closed
  `COMPLETED` with `work_outcome = abandoned` (held ones are released first via
  `release_claim`), then the container closes normally. Invariant 6 and its doctor check
  stand unweakened.
- A `COMPLETED` container cannot receive children: `create_task --parent`,
  `reparent_task`, `add_dependency(parent-child)` and `formula_cook --parent` fail with
  `hierarchy.container_closed`. The operator path is `reopen_with_feedback` on the
  container (→ `IN_PROGRESS`), then add the work. This resolves the conflict between
  explicit and child-derived completion in favour of explicit: settlement never reopens.
- Reparenting or deleting the last open child settles the *old* container immediately
  (same transaction), not at the next sweep.

**Delete.** `delete_task` refuses a container with children (`hierarchy.has_children`);
`cascade: true` deletes the subtree depth-first in one transaction, snapshotting affected
waiters before edges disappear (existing pattern).

**Archive.** Subtree-atomic. A container is archivable only when every descendant is
terminal (`COMPLETED`/`FAILED`); the subtree archives together in one transaction, root
last. `_auto_archive_tasks` selects only roots-of-terminal-subtrees. The current
`UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = :id` in `archive_task` is
removed.

**Reparent.** `reparent_task(task_id, parent_id | None)` → `set_parent`. Rejections:
`cross_project`, `cycle` (new parent is a descendant), `depth` (structural: new parent's
depth + the task's subtree height > 3), `container_closed`, `not_found`. The id does not
change (D3). Both the old and the new container are settled in the same transaction.

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
| `agent_profiles` | `lifecycle` accepts `pool`; new `min_active INTEGER NULL`, `max_active INTEGER NULL`, `max_claims_per_session INTEGER NULL` (pool-only; parser rejects them on other lifecycles like it does `mode`/`wake_mode`). **`NULL` = unlimited; `0` is a parse error** — everywhere (parser, storage, sizing, API, tests). |
| `sessions` | `task_id` is mutable for `lifecycle = 'pool'`; new `claims INTEGER NOT NULL DEFAULT 0`; new `agent_id TEXT NULL` (soft ref to the agent row a pool session owns, §11.1); new `claim_phase TEXT NULL` (`claiming \| preparing \| active`, §10) and `claim_phase_at FLOAT NULL` (when the phase was entered — the timeout clock) |
| `tasks` | new `claim_epoch INTEGER NOT NULL DEFAULT 0` (the per-claim fence, §10); new index `idx_tasks_ready_by_profile (project_id, profile_id, status, is_blocked)` |
| `events` (schema registry) | `task.ready` `{task_id, project_id, title, reason}` — emitted post-commit on **every** entry into the frontier (§10 long-poll); `task.claimed` `{task_id, project_id, title, session_id, profile_id, claim_epoch}`; `task.claim_conflict` `{task_id, project_id, session_id}` (debug); `pool.scaled` `{project_id, profile_id, desired, active, reason}` |

The holder of a claim is `(assigned_agent_id, claim_epoch)`. Each pool session owns an
agent row for its lifetime (§11.1), so `assigned_agent_id` stays the identity every
existing reader (scheduler snapshot, dashboard, Discord) already understands; the epoch
is what makes a claim distinguishable from an earlier claim of the same task by the same
session.

**`task.ready`.** Today promotion emits only `task.unblocked`, and only when `is_blocked`
flips; a `DEFINED → READY` promotion of an unblocked task is silent
(`monitoring.py:100-107`). "Entered the frontier" is defined as: post-state
`READY ∧ is_blocked = 0 ∧ no hold label` and pre-state not. It is detected by every
mutation that can produce it — `_apply_transition` (status change), `recompute_blocked`
(dependency/gate change flipping `is_blocked`), `remove_task_label` when the label is
`hold:*`, and `task_route` when it resolves a routing gate — through one helper,
`_note_frontier_entry(conn, ids, reason)`. That helper **writes the `events` audit row
inside the same transaction** (`log_event` on the caller's `conn`), so a crash between
commit and bus emission cannot lose it; the bus emission of `task.ready` happens after
commit, and the long-poll's fallback reads the audit table (§10). Reasons: `promoted`,
`unblocked`, `gate_resolved`, `hold_removed`, `routed`, `restarted`, `released`,
`resumed`. `task.unblocked` keeps its current meaning for playbook triggers.

### 10. Claim — `task_claim`

**Arguments.** `task_id` *or* `next: true`; `wait: int` seconds (0 default; max
`swarm.claim_wait_max`, default 60). `session_id`, `project_id`, `profile_id` come from the
token scope — a caller-supplied value that disagrees is `out_of_scope`.

**Admission preconditions** (cheap, from the per-tick cached scheduler snapshot; no
recompute): project `ACTIVE`; no `pause_scheduling` constraint; global and project budget
not exhausted. Checked before the transaction; they are policy, not correctness, so a
stale snapshot costs at most one extra task within a tick.

**Work query** — what a pool session may take:

```
project_id = :project
AND status = 'READY' AND is_blocked = 0
AND NOT EXISTS (SELECT 1 FROM task_labels l WHERE l.task_id = tasks.id AND l.label LIKE 'hold:%')
AND (profile_id = :profile OR (profile_id IS NULL AND :profile = :project_default_profile))
AND NOT EXISTS (SELECT 1 FROM task_workspace_requirements r
                WHERE r.task_id = tasks.id AND r.kind_id <> 'project-repo')
AND is_plan_subtask = 0
```

Two exclusions keep pull inside what a single `project-repo` slot can honour:
tasks with other workspace kinds, and **plan subtasks**, which share their parent's branch
and therefore serialise (`worktree-execution.md` §4.4; `workspace.py:547` resumes
`parent.branch_name` for `is_plan_subtask`). Git refuses a branch checked out in two
worktrees, so two pool sessions claiming sibling plan subtasks would each mark one
`IN_PROGRESS` and one would fail its slot reset. Both classes stay on the push path, whose
branch-busy handling already exists. Graph-creator and formula nodes are
`is_plan_subtask = 0` with their own branches and are pullable. Branch-per-child
integration for plans remains the follow-up worktree-execution §4.4 named.

Ordering: `CASE WHEN affinity_agent_id = :agent THEN 0 ELSE 1 END ASC, priority ASC,
created_at ASC` (a bare boolean `DESC` sorts `NULL` affinity first on Postgres).
Affinity is a preference under pull, not a hold; the 120 s affinity wait remains push-only.

**The claim transaction — one transaction, holder included.** Everything that records
*who holds what* commits together; the only thing outside is the git reset.

```sql
-- 1. take the session's claim slot with a conditional write.  A write is the first
--    statement on purpose: on Postgres it is the row lock; on SQLite it is what
--    acquires the RESERVED (writer) lock — a SELECT in a deferred transaction would
--    not, and two sessions could both read "free" and both proceed.
UPDATE sessions SET claim_phase = 'claiming', claim_phase_at = :now
WHERE id = :session AND task_id IS NULL AND claim_phase IS NULL
  AND desired_state = 'running'
  AND (:cap IS NULL OR claims < :cap);
--    rowcount = 0 → re-read the row (still inside the transaction, we now hold the
--    writer lock) and classify: task_id IS NOT NULL → claimed (idempotent, same
--    epoch); desired_state <> running → drain_requested; claims >= cap →
--    session_exhausted; claim_phase = 'claiming' → another request from this session
--    is mid-claim → return its result once it commits (wait on the row lock on
--    Postgres; on SQLite the writer lock already serialised us, so this case cannot
--    be observed).

-- 2. pick + take the task
WITH cand AS (
  SELECT id FROM tasks WHERE <work query> [AND id = :task_id]
  ORDER BY <ordering> LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE tasks t SET status = 'IN_PROGRESS', assigned_agent_id = :agent,
                   claim_epoch = claim_epoch + 1, updated_at = :now
FROM cand WHERE t.id = cand.id
RETURNING t.*;
--    0 rows → roll back step 1 (claim_phase back to NULL) and return
--    no_ready_work (--next) or claim_conflict (explicit id)

-- 3. record the holder
UPDATE sessions SET task_id = :task, claim_phase = 'preparing', claim_phase_at = :now
WHERE id = :session;
UPDATE agents   SET state = 'BUSY', current_task_id = :task WHERE id = :agent;
UPDATE workspaces SET locked_by_task_id = :task WHERE locked_by_agent_id = :agent;
INSERT INTO task_metadata (claimed_by_session, work_dir) …;   -- upsert
```

Step 2 goes through `_apply_transition` (so validation, `is_blocked` recompute and the
`task.ready` bookkeeping run) with the candidate-selection statement supplying the id.
`sessions.claims` is **not** incremented here — it is incremented when the claim reaches
`active` (step 4), so a failed preparation does not consume a claim.

**SQLite.** The transaction is opened with `BEGIN IMMEDIATE` (the engine gains an
`immediate()` context manager used by every claim/release/terminate path; the default
deferred `begin()` stays for reads). Step 1 is the same conditional `UPDATE`. Step 2 is
`SELECT id … LIMIT 1` then
`UPDATE tasks SET … WHERE id = :id AND status = 'READY' AND is_blocked = 0 AND assigned_agent_id IS NULL`.
Because the transaction holds the writer lock, no other writer can commit between the
select and the CAS, so `rowcount = 0` can only mean the candidate changed under a
*previous* commit that this transaction's snapshot predates — it re-selects at most once
and, if still 0, returns `no_ready_work` / `claim_conflict`. No retry loop runs against a
stale snapshot.

Consequences: two concurrent requests from one session serialise on the session row and
the second sees `task_id IS NOT NULL` → idempotent `claimed` with the same epoch; a crash
after commit leaves a consistent holder (session, agent, workspace lock and task all
agree); the cap is checked under the lock; the agent row is `BUSY` from the same instant
the task is `IN_PROGRESS`.

**Step 4 — outside the transaction: the git reset, with a recoverable phase.**
`reset_slot_for_task(slot, task)` runs after commit while `sessions.claim_phase =
'preparing'` (`claim_phase_at` is the clock the timeout reads — the phase alone cannot
say how long it has been in that phase). On success, one transaction:
`claim_phase = 'active', claim_phase_at = :now, claims = claims + 1`, and the claim file
(below) is written; then `task.claimed` and `task.started` are emitted (the latter keeps
every existing subscriber — dashboard, Discord, playbooks — working unchanged). On
failure, or if the reconciler finds `claim_phase = 'preparing'` with
`now − claim_phase_at > swarm.prepare_timeout` (default 120 s, e.g. daemon crash
mid-reset): `release_claim` (§11.3) runs with context `slot_reset_failed`,
`task_metadata.needs_attention = slot_reset_failed`, and the response is `prepare_failed`.
`claims` is untouched, so a capped worker is not exhausted by failures it did not cause;
three consecutive `prepare_failed` on one session quarantine it via the existing ladder,
which is the bound on a slot that cannot be reset.

**Per-claim fence — the epoch travels in a session-local file, not the environment.** A
running process cannot receive a new environment variable, so `AQ_CLAIM_EPOCH` is only
usable for `lifecycle: task` sessions (fixed at launch). For every lifecycle the source of
truth the CLI reads is `<work_dir>/.aq/claim.json` —
`{"task_id", "claim_epoch", "session_id", "claimed_at"}` — written atomically (temp file +
rename) by the **daemon** at step 4 before it returns `claimed`, and deleted by
`release_claim`/`task_close`. `work_dir` is the session's own worktree slot, so the file
is session-local by construction. `aq task close|heartbeat|set|handoff` read it (env var
first for task sessions, then the file) and send `claim_epoch`; the agent never types it.
`aq prime` prints the epoch for humans reading the transcript. Every such mutation applies
its writes with `… WHERE id = :task AND assigned_agent_id = :agent AND claim_epoch = :epoch`;
`rowcount = 0` → `stale_claim`. A delayed close from an earlier attempt therefore cannot
land on a later re-claim of the same task by the same session, and a command run from a
stale shell in a reset slot finds no file and gets `stale_claim` too. Ownership is
`sessions.task_id = task ∧ claim_epoch match`; the token still pins project and session.

**Result** — `{"result": <code>, "task": <task_show shape> | null, "claim_epoch": int | null, "session": {claims, cap, desired_state, claim_phase}}` with codes:

| Code | Meaning | Agent's move |
|---|---|---|
| `claimed` | task is yours (`claim_epoch` returned; idempotent repeat returns the same) | `aq prime --task <id>`, work |
| `no_ready_work` | frontier empty (after `wait`) | pool: claim again or drain-ack |
| `claim_conflict` | explicit `task_id` was taken | try `--next` |
| `prepare_failed` | claimed but the slot reset failed; task released | claim again; after 3 the ladder quarantines |
| `session_exhausted` | `max_claims_per_session` reached | `aq session drain-ack` |
| `drain_requested` | pool scaled down | `aq session drain-ack` |
| `stale_claim` | (on mutations) epoch mismatch | stop; the task is no longer yours |
| `out_of_scope` | token mismatch / not a claimable lifecycle | stop |

**Long-poll — subscribe first, then check.** With `wait > 0`: (1) subscribe to `task.ready`
filtered on `project_id` (plus `gate.resolved`, `task.restarted` for belt-and-braces);
(2) read `events.max(id)` as `seq0`; (3) run the claim; (4) if `no_ready_work`, wait on the
subscription **or** until `select count(*) from events where id > seq0 and type='task.ready'
and project_id = :p` is non-zero (checked once immediately after subscribing, which closes
the window between (1) and (3)); on wake, go to (3). Return `no_ready_work` on timeout.
`task.ready` exists precisely because `DEFINED → READY` promotion of an unblocked task
emits nothing today (§9). Cost: one waiting HTTP request per idle session; no polling.

**`close --claim-next`.** `task_close` gains `claim_next: bool`. It runs the existing close
(fenced by `claim_epoch`; outcome metadata, completion pipeline; token revoke is
**skipped** for pool sessions because the token is session-scoped) and, only if the close
succeeded, `task_claim(next=True)` in a **separate** transaction, returning
`{…close result…, "next": <claim result>}`. A failed close never claims.

### 11. Pool reconciler — `_reconcile_pools`

New cascade step, after `_reconcile_sessions`, gated by `swarm.enabled`. Per tick:

1. **Demand** (1 statement): `ready` = count of tasks matching the work query minus the
   profile term, grouped by `(project_id, COALESCE(profile_id, project.default_profile_id))`,
   **restricted to admissible projects** — `ACTIVE`, no `pause_scheduling` constraint,
   project and global budget not exhausted, read from the same per-tick snapshot the
   claim's admission check uses. A paused or over-budget project has zero demand and
   therefore never gets sessions launched for work every claim would refuse.
2. **Supply** (1 statement): pool sessions grouped by `(project_id, profile_id)` into
   `busy` (holding a task, `state ∈ starting|running|draining`), `idle` (live, no task),
   `starting` (row exists, not yet observed live), `draining_requested`
   (`desired_state = 'stopped'`). Plus, per project, the count of **every** agent row that
   occupies a slot (`state ∈ BUSY|IDLE`, any lifecycle) so caps count what actually
   consumes capacity.
3. **Desired** — pure function `size_pools(demand, supply, profiles, projects, caps,
   deficits) -> list[PoolAction]` in `src/scheduler.py` beside `Scheduler.schedule`
   (table-tested, no I/O). Per `(project, profile)`:
   - `want = busy + ready` — sessions that are working plus tasks waiting; the previous
     draft compared ready tasks against total sessions and under-provisioned by exactly
     the busy count;
   - `desired = clamp(want, min_active, max_active)`, then `desired = max(desired,
     busy + starting)` — a session holding a task or mid-launch is never a scale-down
     candidate, so the floor is the non-drainable supply;
   - project bound: `Σ desired over pool profiles ≤ project.max_concurrent_agents −
     (agent rows not owned by pool sessions)`; when it binds, marginal sessions above each
     profile's floor go to the profile with the largest `want − desired`;
   - global bound: `Σ ≤ global cap − others`, plus the usage-aware headroom hook
     (`headroom_fn: (project) -> int | None`, default None); when it binds, marginal
     sessions go to the project with the largest `BudgetManager` deficit — fair-share at
     the capacity layer;
   - hysteresis: scale-up at most `swarm.max_starts_per_tick` (default 2) per tick;
     scale-down only for surplus (`idle − (desired − busy − starting)`) that has persisted
     `swarm.scale_down_grace` seconds (default 120), tracked in memory per key.
   Worked example from the review: one busy worker, two ready tasks, `max_active = 3` →
   `want = 3`, `desired = 3`, start two.
4. **Converge**: for each `start` action → `_launch_pool_session` (§11.1); for each
   `drain` action → `update_session(desired_state='stopped')` on an **idle** pool session
   (never one holding a task; the next `claim` returns `drain_requested`). A session in
   `claim_phase = 'preparing'` counts as busy.
5. Emit `pool.scaled` when desired or active changed for a key.

`AgentReconciler` is taught `lifecycle='pool'`: it neither creates nor reaps agent rows
for pool profiles — pool sessions own their rows (§11.1) — and counts them toward
`max_concurrent_agents` like any BUSY/IDLE row.

#### 11.1 `_launch_pool_session(project, profile)`

Mirrors `_launch_session_for_task` step for step, differences only:

- creates an `agents` row (`profile_id`, `state=IDLE`) first; the session references it
  via `sessions.agent_id` (§9) so the claim can set `assigned_agent_id` without a lookup.
  The claim transaction flips the row to `BUSY` with `current_task_id`; release/close
  flips it back to `IDLE` — so `project_active_agent_counts`, `_idle_by_project` and the
  dashboard's agent tiles see pool workers exactly as they see push workers;
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

#### 11.2 Ownership lifecycle: two operations, both single transactions

A pool session owns three things at different times: its **agent row and worktree slot**
for its whole life, and a **task** between claim and close. Two helpers, and nothing else,
change that ownership; both open with `BEGIN IMMEDIATE` on SQLite.

**`release_claim(session, *, context, task_status)`** — between claims. Task:
`_apply_transition(conn, task, task_status)` with `assigned_agent_id = NULL`
(`READY` on normal release/prepare failure; `BLOCKED` on `PRODUCTIVE_DEATH` or
`needs_attention`); `claim_epoch` unchanged (the next claim increments it). Session:
`task_id = NULL, claim_phase = NULL, claim_phase_at = NULL`. Agent: `state = IDLE,
current_task_id = NULL`. Workspace: `locked_by_task_id = NULL` — **`locked_by_agent_id`
is retained**; the slot stays the session's. Claim file deleted. Callers: `task_close`
(after the completion pipeline), `prepare_failed`, `_step_prepare_timeout`,
`_step_orphans` (b) — the session lives on in each case; `_step_exits` verdicts go
through `terminate_pool_session` instead. The existing
`release_workspaces_for_task` is **not** used on this path — it clears the agent lock too
(`workspace_queries.py:536-543`), which would hand the slot to the scheduler while the
session is still in it.

**`terminate_pool_session(session, *, reason)`** — session end. Runs when the reconciler
records a terminal state for a pool row (`_stop_session` with `state='stopped'` or
`'quarantined'`, drain-ack completion, `_step_exits` verdicts). In one transaction: if a
task is still held, `release_claim` semantics inline (task `READY`, or `BLOCKED` per the
verdict); `release_workspaces_for_agent(agent_id)` (clears both lock columns — the
existing helper is right here); agent row **retired** (`state = 'RETIRED'`,
`current_task_id = NULL`; `AgentReconciler` deletes retired rows at startup like it does
over-cap idle rows today, so ledger rows keep their soft reference); session `task_id =
NULL, agent_id` retained for forensics, `claim_phase = NULL`, state set; API token revoked
(`SessionTokenStore.revoke_session`) — revoke is inside the transaction only if the store
shares the engine; otherwise it runs immediately after commit and the 60 s expiry sweep is
the backstop, as today. Claim file deleted with the slot's reset on next use.

`_stop_session` for a pool row therefore calls `terminate_pool_session` before writing
the state; for `task` and `named` rows it is unchanged.

#### 11.3 Reconciler carve-outs for `lifecycle='pool'`

| Step | `task` behaviour | `pool` behaviour |
|---|---|---|
| `_step_orphans` (a) live session, task closed | drain | **normal between claims**; drain only if `desired_state='stopped'`, or idle (`task_id IS NULL`) longer than `profile.idle_timeout` |
| `_step_orphans` (b) open task, no live row | BLOCKED + release | same (task released to `READY` — pool tasks are retried, not blocked, unless the verdict was `PRODUCTIVE_DEATH`) |
| new: `_step_prepare_timeout` | — | a pool session with `claim_phase='preparing'` for longer than `swarm.prepare_timeout` has its claim released (§10 step 4) |
| `_step_exits` | verdicts as today | same verdicts; the row is terminal, so `terminate_pool_session` (§11.2) runs — held task released per the verdict, both lock columns cleared, agent retired, token revoked, one transaction |
| stall ladder | applies | applies **only while `task_id IS NOT NULL`** |
| `_step_drain_ack` premature-drain guard | ack with open task → nudge | same |
| `_step_backstop` | `stuck_timeout_seconds` | same, per held task |

`_cmd_task_close`, `task_set`, `task_heartbeat`, `task_handoff` verify ownership through
`sessions.task_id == task_id ∧ tasks.claim_epoch == :epoch` (`_assert_session_owns`,
§10 per-claim fence), not the token's `task_id`, which is null for pools. Token revoke on
close is skipped for pools; the token is revoked at drain.

### 12. Worker-filed work

**Scope.** `AGENT_COMMAND_SET` += `create_task`, `task_claim`, `task_children`,
`task_progress`, `project_ready`, `formula_list`, `formula_show`.

**Server-enforced constraints on `create_task` for non-elevated sessions** (in
`enforce_scope` + `_cmd_create_task`), all in the creation transaction:

- `project_id` := token project (mismatch → `out_of_scope`);
- `created_by_kind='session'`, `created_by_id=<session_id>`;
- **holding a task `T`:** `parent_id`, if given, must be `T` or a descendant of `T`
  (else `hierarchy.parent_out_of_scope`); if absent, a `discovered-from` edge to `T` is
  added automatically. **Idle (no held task):** `parent_id` must be absent
  (`hierarchy.parent_out_of_scope`) — an idle worker may only file root-level work;
- initial status **`DEFINED`** regardless of edges;
- **root-level worker-filed tasks get a `routing` gate in the same transaction**
  (`create_gate(gate_type='routing', await_id=<task_id>)` + `task_gates` row, via the
  existing routing-gate code path that `task-created-routing` uses today). The task is
  therefore blocked by a durable record from the instant it exists; nothing about its
  safety depends on a playbook running. Subtasks of the held task get no gate: they
  inherit the container's profile (or `profile_id` = the session's own profile) and
  proceed once the cascade promotes them;
- `profile_id` may be omitted (routing decides) or set only to the session's own profile.

**Policy lives in the default pipeline — it resolves the hold, it does not create it.**
`task.created` gains optional payload fields `created_by_kind`, `created_by_id`,
`parent_task_id`, `discovered_from`, `routing_gate_id` (schema registry updated; required
triple unchanged). The shipped `default-pipeline.md` rule:

```yaml
- id: worker-filed-triage
  on: task.created
  when: { created_by_kind: session, parent_task_id: null }
  steps:
    - action: ensure_task
      args: { dedup_key: triage-open, profile_id: triage, title: "Triage open work" }
    # triage resolves the routing gate with `aq task route`; a project that wants
    # auto-routing replaces this step with `task_route` directly.
```

If the pipeline never runs (dispatch failure, concurrency cap, daemon crash — dispatch is
`asyncio.create_task` fire-and-forget, `core.py:1026`), the task stays gated; `explain`
reports `blocked_gate routing`, `project_ready` lists it under withheld, and the
`pools.stuck` / existing `tasks.stuck` doctor checks surface it. Failure mode is "work
waits for a human", never "unrouted work runs".

**Ordering.** No timing argument is needed: the gate is created in the same transaction
as the task. The former claim that a fire-and-forget pipeline "always lands" before the
next tick is withdrawn.

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
| `formula_cook` | `name`, `project_id`, `vars`, `parent_id?`, `dry_run?` | `create_task_graph` under a new container or `--parent`, **in one transaction with** the provenance writes below |

**Provenance (reproducibility).** Formulas are mutable files, project-shadowed, with an
inheritance chain, so name + vars is not enough to say what was cooked. The container
carries, written in the creation transaction: `task_metadata` `formula=<name>`,
`formula_scope=<system|project:<pid>>`, `formula_path=<vault-relative path>`,
`formula_vars=<json>`, `formula_chain_sha=<sha256 over the resolved chain's file
contents, root→leaf>`; a `task_context` row `type='formula_snapshot'` holding the fully
resolved graph document (post-`extends`, post-vars, pre-id) as JSON; and label
`formula:<name>`. `formula_show --as-cooked <container_id>` re-renders from the snapshot,
not from the current file.

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
| `aq task close <id> … [--abandon-children]` (containers) | `task_close` | yes (own container only) |
| `aq db preflight hierarchy`* | `db_preflight_hierarchy`* — dry-run canonicalisation, committed report | no |
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

Three budgets are kept apart, because they measure different things and cannot all be
"identical across dialects":

- **Logical statements** — the statements issued on the happy path, with no retries and no
  extra actions. Asserted exactly **per dialect** (Postgres's CTE claim is one statement
  where SQLite's select-plus-CAS is two; the counts are not expected to match).
- **Transactions** — how many commits a path performs. Asserted exactly.
- **Worst case** — retries (SQLite CAS), per-action convergence (`_reconcile_pools` issues
  one statement per start/drain action), ancestor settlement (up to depth 3). Asserted as
  an upper bound.

| Path | Logical statements | Transactions | Worst case / timing (Postgres, p99) |
|---|---|---|---|
| `task_claim --next`, DB portion | ≤ 6 (lock session, select+take, session, agent, workspace, metadata) | 1 | SQLite: + ≤ 5 CAS retries; ≤ 50 ms |
| `task_claim` end-to-end incl. slot reset | — | 1 + phase update | ≤ 3 s (git-bound; reported separately) |
| 20 concurrent `claim --next`, 10-task frontier | — | — | exactly 10 `claimed` + 10 `no_ready_work`; 0 double-claims; both dialects |
| `_reconcile_pools` per tick | 2 | 0 (reads) | + 1 statement per action, ≤ `max_starts_per_tick` starts; ≤ 20 ms DB excluding launches |
| Cascade tick DB time (promotion + gates + sessions + pools + messages) | — | — | ≤ 250 ms |
| `get_task_tree` / `task_children --recursive` / `task_progress` | ≤ 3, size-independent | 0 | — |
| `set_parent`, `create_task --parent` | ≤ 5 (edge delete/insert, pointer, recompute, settle) | 1 | + settlement of ≤ 2 ancestors |
| `create_task_graph`, 200 nodes | batched inserts | 1 | ≤ 1 s |
| container settlement after last child closes | + 2 per settled container (predicate, transition) | 0 extra — same transaction as the child's close | ≤ depth-3 ancestors |
| worker-filed root `create_task` | + 2 (gate, task_gates) | 1 | — |
| `aq prime` per claim | — | — | ≤ 2,000 tokens, logged |
| push-vs-pull A/B (same 50-task set, `lifecycle: task` vs `pool` cap 5) | — | — | report wall-clock, tokens, cold starts; no threshold — the measurement D4 asks for |

`tests/perf/` seeds this scale (fixture reused by all perf tests), runs on a Postgres
service in CI, and asserts the table. SQLite runs the same suite with timing relaxed 4×,
transaction counts identical, and its own exact logical-statement and worst-case bounds.

### 16. Testing

**Concurrency and durability (the review's scenarios, each a named test).**
Two concurrent `claim --next` from one session → one task, one epoch, second call
idempotent. Crash injected after the claim transaction commits but before the slot reset →
holder consistent (session/agent/workspace/task agree), `claim_phase='preparing'`,
`_step_prepare_timeout` releases it after `prepare_timeout` measured from
`claim_phase_at`, `claims` unchanged, task claimable again. Concurrent duplicates: 10
concurrent `claim --next` from one session → one task, all ten responses `claimed` with
the same epoch (idempotent contract). Cap: `max_claims_per_session=1`, claim → close
`--claim-next` → `session_exhausted`; a `prepare_failed` in between does not count.
SQLite serialisation: two sessions racing on a one-task frontier under `BEGIN IMMEDIATE`
→ one `claimed`, one `no_ready_work`, never two `IN_PROGRESS`. Terminal cleanup: pool
session stopped with a held task → task released, both workspace lock columns cleared,
agent `RETIRED`, token revoked — one transaction; stopped idle → same minus the task.
Emptied container: delete the last child → container `COMPLETED` in the delete
transaction. Delayed close from epoch *n* after re-claim at epoch *n+1* →
`stale_claim`, epoch *n+1* work untouched. Worker-filed root with the pipeline runner
disabled → task exists, gated, never promoted; `explain` says `blocked_gate routing`.
Long-poll: a `task.ready` emitted between the subscribe and the first check is not lost
(the seq check catches it); a `DEFINED→READY` promotion of an unblocked task wakes a
waiter. Sizing: `busy=1, ready=2, max_active=3` → two starts. Two pool sessions racing for
sibling plan subtasks → neither claims (excluded from the work query); both remain on push.
Migration: two parent edges where one matches the column → column's edge kept; two edges,
no column → oldest kept; column-only parent in another project → rejected, task becomes
root, migration fails without `AQ_MIGRATION_ALLOW_REJECTS=1`.

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
from edges), `hierarchy.single_parent`, `hierarchy.depth` (structural),
`hierarchy.closed_container_children` (invariant 6), `hierarchy.migration_rejects`,
`formulas.parse`, `pools.stuck` (desired > active for > 10 ticks), `pools.orphan_agents`
(agent rows with no live pool session), `pools.preparing_stuck` (sessions past
`prepare_timeout` — should be empty if the reconciler step runs), `claims.holder_consistency`
(for every `IN_PROGRESS` task held by a pool session: `sessions.task_id`,
`agents.current_task_id`, `workspaces.locked_by_task_id` all agree).

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
  prepare_timeout: 120      # claim_phase='preparing' longer than this → claim released
work_graph:
  container_sweep_interval_seconds: 60
```

Hierarchy changes (Part I) ship **ungated** — they fix drift and remove a per-tick scan.
`task_claim` is callable by `lifecycle: task` sessions even with `swarm.enabled: false`
(idempotent re-claim only), so the command surface is stable before pools turn on.

**Migration** — two revisions plus a preflight, so the report can never be rolled back
with the step that produced it:

*Revision A (DDL only, commits on its own):* `tasks`: `next_child_ordinal`,
`created_by_kind`, `created_by_id`, `claim_epoch`; index `idx_tasks_ready_by_profile`;
`archived_tasks` mirrors the provenance columns. `sessions`: `claims`, `agent_id`,
`claim_phase`, `claim_phase_at`. `agent_profiles`: `min_active`, `max_active`,
`max_claims_per_session`. Remediation table
`hierarchy_migration_rejects (task_id, parent_id, source, reason, detail, run_id, created_at)`.

*Preflight (`aq db preflight hierarchy`, also run automatically at the start of revision
B):* executes steps 1–3 below **read-only**, writes the rejects to
`hierarchy_migration_rejects` under a fresh `run_id` **in its own committed transaction**
(separate connection, autocommit on Postgres), and writes the same report to
`~/.agent-queue/logs/hierarchy-preflight-<run_id>.json`. Exit status non-zero if rejects
exist. Operators run it before upgrading; the revision runs it again so the report reflects
the data actually migrated.

*Revision B (data step + constraint):* aborts before touching any row if the preflight it
just ran found rejects and `AQ_MIGRATION_ALLOW_REJECTS=1` is not set — the report is
already committed, so the abort loses nothing. With the flag, it proceeds and `aq doctor`
reports `hierarchy.migration_rejects` until the rows are cleared.

Data step — canonicalise from an **immutable snapshot** taken before any write:

1. Snapshot `S_col = {(task, parent_task_id)}` and
   `S_edge = {(task, depends_on) | dep_type = 'parent-child'}` into temp tables.
2. For each task, choose its canonical parent: the single edge if there is one; if there
   are several, the one equal to `S_col` (the column is the evidence, read from the
   snapshot, so update order cannot destroy it), else the oldest edge by `created_at`,
   else none. Every non-chosen edge → `hierarchy_migration_rejects(source='duplicate_edge')`.
   Tasks with a column value and no edge → candidate edge from the column
   (`source='column_only'`).
3. **Validate the candidate graph as a whole** before writing it: cross-project parent,
   cycle, structural depth > 3, parent not found. Each failure → rejects table with the
   reason; the candidate is dropped (task becomes a root).
4. Apply: delete every `parent-child` edge, insert the canonical set, write
   `parent_task_id` from it, set `task_metadata.container='1'` for every task with a
   child. Backfill `next_child_ordinal` **by id prefix, not by current parent**: for every
   id of the form `<prefix>.<n>[.…]` across `tasks ∪ archived_tasks`, `next_child_ordinal
   (prefix) = 1 + max(n)`. Grouping by `parent_task_id` would let a reparented child's
   ordinal be re-minted under its birth parent.
5. Create `uq_task_deps_single_parent`.
6. Rejected edges are re-attachable by hand (`aq task reparent`) or kept as
   `discovered-from` provenance by the operator (`aq task deps add … --type discovered-from`).

**Shipped profile defaults** (inert until `swarm.enabled`): `worker-fast`
(`lifecycle: pool, max_claims_per_session: 2, max_active: 3`), `worker-standard`
(`pool, 5, 3`), `worker-deep` (`pool, max_claims_per_session` unset = unlimited,
`max_session_age: 14400, max_active: 1`). `min_active: 0` everywhere.

**Implementation plan split** (each independently mergeable):
1. Part I — hierarchy, graph creator, migration, doctor checks, `children/progress/reparent`.
2. Part II — claim, pools, worker loop, scope/filing, pipeline rule. Depends on 1's
   migration.
3. Part III — formulas.

### 18. Property crosswalk

| Beads property (parity doc) | Delivered by |
|---|---|
| P1 graph decides | `blocked_state_authoritative` flip + claim over `get_ready_frontier` |
| P2 fenced claim + leases | §10 (holder recorded in the claim transaction; `claim_epoch` fences every later mutation; token pins session/project); existing leases |
| P4 typed close | `--claim-next` on the existing typed close |
| P5 agents file work | §12 |
| P6 ordering primitives | Part I (single parent, subtree archive/delete, event-driven completion) |
| P7 workflows as data | Part III |
| P11 provenance | `created_by_*`, `GIT_AUTHOR_NAME` per session |
| P9 context frugality | `aq schema` completion, prime size per claim |
