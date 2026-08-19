---
tags: [design, work-graph, dependencies, gates, labels, state-machine, explain, events]
---

# Work Graph — Typed Edges, Blocked State, Gates, Explain

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#2 visible and editable, #7 events over coupling, #9 simple interfaces, #10 fewer moving parts)
**Related:** [[workspaces-v2]], [[agent-coordination]], [[session-runtime]], [[worktree-execution]], [[supervisor-agent]], [[messaging-rework]], `docs/analysis/framework-overhaul-todo.md` §6 (Workstream D), `docs/analysis/comparison-gascity-beads.md` §7

---

## 1. Problem

The task graph is the substrate everything else in the overhaul (sessions, worktrees, the supervisor agent) stands on, and today it is too thin:

1. **One implicit edge type.** `task_dependencies(task_id, depends_on_task_id)` means only "blocks". Plan subtasks overload `parent_task_id` *and* a dependency edge, with a special case in `_check_defined_tasks` treating an IN_PROGRESS parent as satisfied. There is no fan-in ("run when all children of X are done"), no contingency ("run only if X failed"), and no provenance (discovered-from, duplicates).
2. **Readiness is recomputed by scanning, and only positively.** Every 5 s cycle re-derives dependency satisfaction for all DEFINED/BLOCKED tasks; nothing is persisted, nothing is indexable, and a READY task that *gains* a blocking edge is never demoted — the scheduler will happily run it.
3. **"Why isn't this running?" lives in logs.** `_log_scheduler_blockers` writes heuristic reasons to the daemon log. Neither humans (`aq`), the dashboard, nor the supervisor agent can query them.
4. **Statuses encode *why* a task waits.** AWAITING_APPROVAL, AWAITING_PLAN_APPROVAL, WAITING_INPUT are each a bespoke wait with bespoke polling (`_check_awaiting_approval`) and bespoke Discord UI. Every new external condition (CI green, timer, another task) would need another status.
5. **Failures are counted, not classified.** `retry_count` increments identically for a flaky network and a fundamentally broken task; outcomes carry no structured verdict for the retry policy or (later) reflection.
6. **The state machine is advisory.** `transition_task` logs invalid transitions and applies them anyway.

## 2. Overview

The work graph adds seven pieces, all deterministic, zero-LLM, and consumed by the promotion cascade:

| Piece | What it is | Modeled on |
|---|---|---|
| Typed edges | `task_dependencies.dep_type`, blocking + non-blocking classes | Beads dependency types |
| Blocked state | persisted `tasks.is_blocked`, recomputed to fixpoint in-transaction | Beads `issueops/blocked_state` |
| Gates | first-class wait records (`gates` + `task_gates`) that block like edges | Gas City gate beads, `bd gate` |
| Labels | `task_labels` + list/ready filters; metadata-first rule | Beads labels / schema boundary |
| Outcome metadata | typed close verdicts in `task_metadata`; retry consults `failure_class` | Gas City `gc.outcome` contract |
| Enforced transitions | `transition_task` raises behind `state_machine.enforce` | Gas City session reducer |
| Explain + replay | `aq task explain`, `aq project ready`, `after_seq` event replay | `bd ready --explain`, GC `seq` |

A later phase (§12) collapses the three wait statuses into gates behind a compatibility projection. Phase 0 keeps all 11 `TaskStatus` values.

## 3. Typed dependency edges

`task_dependencies` gains `dep_type TEXT NOT NULL DEFAULT 'blocks'`. An edge `(task_id, depends_on_task_id, dep_type)` reads "*task_id* has a *dep_type* relationship to *depends_on_task_id*". One pair may carry multiple edge types (e.g. `blocks` and `discovered-from`).

### 3.1 Blocking edge types

A blocking edge contributes to `is_blocked` until its **satisfaction rule** holds:

| dep_type | Reads as | Satisfied when |
|---|---|---|
| `blocks` (default) | task runs after dep | `dep.status = COMPLETED` |
| `parent-child` | task is a child of dep (its container) | `dep.status ∉ {DEFINED, AWAITING_PLAN_APPROVAL}` — the container has been *released* |
| `waits-for` | task fans in over dep's children | every task with a `parent-child` edge to dep has `status = COMPLETED` (vacuously true with zero children; children added later **re-block** the waiter) |
| `conditional-blocks` | task runs only if dep failed | `dep.status = BLOCKED`, or `dep.status = FAILED` with `retry_count ≥ max_retries` (terminal failure only — a transiently FAILED task about to be retried does not satisfy it) |

Notes:

- **`parent-child` replaces the plan-subtask special case.** Today `_check_defined_tasks` hard-codes "parent IN_PROGRESS counts as met" for `is_plan_subtask` rows. The new rule generalizes it: a DEFINED or AWAITING_PLAN_APPROVAL parent withholds its children (a staged graph, an unapproved plan); any released parent — READY, IN_PROGRESS, even a pure container that never executes — frees them. `parent_task_id` stays as a denormalized pointer for tree rendering; the edge is authoritative.
- **`waits-for` is dynamic fan-in.** The classic pattern: a finalize/review task `waits-for` the group container; workers discovered mid-flight get `parent-child` edges to the container and automatically re-block the finalizer. A `waits-for` edge whose waiter is itself a child of the target container is rejected at write time — it can never be satisfied (§11).
- **`conditional-blocks` has a disposal rule.** When the dependency reaches COMPLETED the edge is permanently unsatisfiable. A cascade step auto-closes dependents whose *only* remaining unsatisfied blocking edges are conditional edges on COMPLETED deps: they transition to COMPLETED with `work_outcome=no-op` metadata and a `task.skipped_conditional` event. Contingency tasks never rot in the queue.

### 3.2 Non-blocking edge types

`discovered-from`, `related`, `duplicates`, `supersedes` never affect `is_blocked`. They are provenance and association: shown in `task explain`/graph views, consumed by dashboards and the supervisor, and by doctor lints (a task that `duplicates` an open task warns; a `supersedes` target still READY warns). Plan-generated subtasks gain `discovered-from` edges to the plan task instead of overloading meaning onto `parent_task_id`.

### 3.3 Cross-project edges

Cross-project dependencies are **allowed, explicitly** (todo §3b, decision 9). The single-DB model makes them ordinary rows; readiness treats them like any blocking edge. Everything that renders an edge (explain, graph, dashboard) names the other project when `dep.project_id ≠ task.project_id`. The per-project ready frontier naturally excludes tasks blocked by another project's work.

## 4. Persisted blocked state

### 4.1 Definition

`tasks.is_blocked` (0/1, default 0) is a pure projection:

> `is_blocked(t) = 1` iff **any** blocking edge from *t* is unsatisfied (§3.1) **or** any gate attached to *t* via `task_gates` has `status ∈ {open, expired}` (§5.4).

It is *graph* blockedness only. Transient capacity reasons — no idle agent, workspace locked, budget, cooldown — are **not** persisted; they change per-tick and belong to explain (§9), not the row.

### 4.2 Recompute triggers

Recomputed **in the same transaction** as the mutation, Beads-style (`issueops/blocked_state.py` semantics):

- dependency edge add / remove (including bulk delete when a task is deleted),
- task status change — every `transition_task`, close/complete, fail, reopen, admin restart/skip,
- task delete (former dependents recompute),
- task create with edges (graph create),
- gate create/attach, resolve, expire (§5).

### 4.3 Algorithm

Given seed set `S` (tasks directly touched by the mutation):

1. **Affected set** `A` = `S` ∪ direct dependents of `S` via any blocking edge ∪ waiters (`waits-for`) on any container that a member of `S` is a child of ∪ for gate changes, all `task_gates.task_id` of the gate.
2. **Evaluate** the predicate for each `t ∈ A` with one set-based SQL UPDATE (identical on SQLite and PostgreSQL — `EXISTS` subqueries per rule, see the implementation spec §3 for the exact statement). Within one transaction, other tasks' *statuses* are fixed, so the predicate is a pure function and each task needs exactly one evaluation.
3. **Fixpoint loop.** If the transaction itself changes multiple statuses (bulk graph creation, conditional auto-close, admin skip cascades), re-seed with the tasks whose status changed and repeat until no `is_blocked` value changes. The loop is bounded by the affected subgraph size because statuses only move monotonically within one transaction's intent.
4. **Events after commit:** `task.blocked` / `task.unblocked` for every flipped row (payloads registered in the schema registry, §10.2).

Blockedness is deliberately **not transitive through blockedness**: an open (non-COMPLETED) dependency blocks you regardless of whether *it* is blocked — exactly Beads' rule. Transitivity emerges from statuses: the chain unblocks link by link as tasks complete.

### 4.4 Consumers

- `_check_defined_tasks` stops scanning dependency lists: promotion becomes "DEFINED ∧ `is_blocked = 0` → READY" (one indexed query), plus the BLOCKED-recovery rule: a BLOCKED task with at least one blocking edge or gate whose `is_blocked` flips to 0 is promoted to READY; failure-BLOCKED tasks (no graph blockers) stay put, preserving today's behavior.
- The scheduler skips `READY ∧ is_blocked = 1` — closing the "edge added after READY" hole.
- `aq project ready` = `status = READY ∧ is_blocked = 0`, ordered `(priority, created_at)`, with label filters.
- During rollout a shadow mode runs both the legacy scan and the projection and logs divergence before the projection becomes authoritative (flag `work_graph.blocked_state_authoritative`).

## 5. Gates

### 5.1 Model

A **gate** is a first-class wait record — "something outside the graph must happen":

`gates(id, project_id, gate_type, title, question, await_id, timeout_at, status, resolved_by, resolution, created_at)` with `gate_type ∈ {human, timer, pr-merged, ci-run, event, task}` and `status ∈ {open, resolved, expired}`. `task_gates(task_id, gate_id)` attaches waiters; one gate may block many tasks, one task may wait on many gates.

| gate_type | `await_id` holds | Resolved when |
|---|---|---|
| `human` | optional context ref (plan id, question id) | a human runs `gate resolve` — **only** path; Discord buttons and the dashboard call the same command |
| `timer` | target epoch seconds | `now ≥ await_id` (sweep) |
| `pr-merged` | PR URL | `gh` reports merged (sweep, via the extracted PR-poll helper from the approvals mixin) |
| `ci-run` | run id / URL | `gh run` reports success (sweep) |
| `event` | event type (+ optional payload-filter JSON) | a matching EventBus event fires (subscription resolves immediately; sweep is the restart-safe backstop against events missed while the daemon was down, re-checked from the persisted `events` table) |
| `task` | task id (cross-project allowed) | that task reaches COMPLETED (sweep) |

### 5.2 Blocking semantics

An open gate blocks its waiters **exactly like a blocking dependency**: the `is_blocked` predicate includes an `EXISTS` over `task_gates ⋈ gates WHERE status != 'resolved'`. Resolving a gate triggers the same in-transaction recompute as completing a dependency. Gates are the single mechanism behind human approval, PR merge, CI, timers, and the supervisor's `ask` — "approve" becomes "resolve gate" everywhere.

### 5.3 The gate-sweep cascade step

A new deterministic step `_sweep_gates()` runs each cycle between approvals and DEFINED-promotion so a freshly resolved gate unblocks dependents in the same tick. It: resolves satisfied `timer`/`task` gates (pure SQL + clock); polls `pr-merged`/`ci-run` gates on the approvals mixin's existing throttle (60 s) reusing its `gh` logic; re-checks `event` gates against events persisted since the gate's creation; marks gates past `timeout_at` as `expired`. Zero LLM calls; each resolution logs `gate.resolved` with `resolved_by="sweep"`.

### 5.4 Expiry

`expired` **continues to block** (fail-safe: a timed-out approval must not silently self-approve) but fires `gate.expired` for escalation (Discord ping, `needs_attention`). A human clears it with `gate resolve --resolution timeout-override` or by resolving normally.

### 5.5 Consumers and producers (cross-spec)

- **[[messaging-rework]]** renders open gates as Discord buttons in task threads and the gates inbox; buttons invoke the `gate_resolve` command — no Discord-only approval path survives.
- **[[supervisor-agent]]**: `aq ask` creates a `human` gate attached to the asking task; the reply resolves it (and is delivered as a nudge).
- **[[worktree-execution]]**: "PR merged" / "CI green" completion gates replace bespoke polling states in the merge pipeline.
- The gates table is the substrate the later status collapse (§12) lands on.

## 6. Labels

`task_labels(task_id, label)` — plain strings, many per task. `list_tasks` and the ready-frontier queries accept `labels=[...]` (all-of) and `any_label=[...]` filters. Conventions, not schema: `hold:<who>` labels are filtered by the ready frontier (a held task is visible, unblocked, and unscheduled — GC's "filtered when deciding what to *do*, never what must *exist*"); routing and execution hints stay in `task_metadata`. **Metadata-first rule** (Beads' schema boundary): new per-task concepts start as metadata keys or labels; a first-class column requires a migration-reviewed justification.

## 7. Outcome and work-state metadata

No new columns — typed `task_metadata` keys (the sanctioned extension point):

| Key | Values / shape | Written by |
|---|---|---|
| `outcome` | `pass` \| `fail` | task close (session-runtime completion protocol: `aq task close --outcome …`) |
| `failure_class` | `transient` \| `hard` | task close on failure |
| `work_outcome` | `shipped` \| `no-op` \| `blocked` \| `abandoned` | task close; `no-op` also by the conditional auto-close (§3.1) |
| `work_commit`, `work_branch` | sha / branch name | task close |
| `verification` | free-form evidence string | task close |
| `close_notes` | "Done: …" summary | task close |
| `work_dir`, `branch`, `pr_url`, `rejection_reason`, `merged_at` | work-state contract | orchestrator/worktree pipeline per [[worktree-execution]] (recorded early, for crash recovery and rejection-aware resume) |

`branch` and `pr_url` currently exist as `tasks` columns; while both exist a single write helper keeps column and key in sync, with the metadata contract canonical for agents (the columns retire with the collapse migration).

**Retry policy** consults `failure_class`: `transient` (or absent — legacy default) → existing retry-with-backoff path; `hard` → BLOCKED immediately with the reason, regardless of remaining retries. This is the first structured input the (paused) reflection loop will get back.

## 8. State machine enforcement

`transition_task` currently validates, warns, and applies anyway. Behind config flag `state_machine.enforce` (default **off** at introduction, flipped **on** after one observation window with zero warning-log hits), an invalid `(from, to)` pair raises `InvalidTransition` and the command surface maps it to `{"success": false, "error": "invalid transition …"}`. `force=True` (admin override, exposed on `set_task_status`) bypasses validation but still logs and still recomputes `is_blocked`. `VALID_TASK_TRANSITIONS` stays the single source of truth; the warning-audit before flipping the flag adds any legitimately missing pairs rather than loosening enforcement. Raw `update_task(status=…)` outside `transition_task` becomes an error caught by an invariant test.

## 9. Explain and the ready frontier

### 9.1 Reason model

`explain_task(task_id)` returns a typed list — the same shape everywhere (CLI `aq task explain`, MCP, dashboard, supervisor):

```json
{"success": true, "task_id": "swift-falcon.2", "status": "DEFINED", "is_blocked": true,
 "reasons": [{"code": "dep_open", "detail": "blocks: bold-summit (IN_PROGRESS, project other-proj)", "ref": "bold-summit"}]}
```

Each reason is `{code, detail, ref}` — `code` from a closed enum, `detail` human-readable, `ref` the id of the blocking entity (task, gate, workspace kind, project):

| code | Source |
|---|---|
| `dep_open` | unsatisfied `blocks`/`parent-child` edge (cross-project deps name the other project in `detail`) |
| `waits_for_children` | open children under the fan-in container |
| `dep_conditional_pending` | conditional dep not yet terminally failed |
| `gate_open` / `gate_expired` | attached gate, with type and question |
| `status_withheld` | DEFINED and unblocked but not yet promoted / plan unapproved |
| `workspace_unavailable` | no free instance of a required kind (worktree slot, clone, merge slot) |
| `project_cap` / `project_paused` / `project_constraint` | caps, `pause_scheduling`, constraint rows |
| `budget_exhausted` | project or global token budget |
| `affinity_wait` | waiting for the preferred agent within `affinity_wait_seconds` |
| `provider_cooldown` | rate-limit cooldown on the profile |
| `retry_backoff` | PAUSED with `resume_after` in the future |
| `lease_stalled` | heartbeat/lease overdue (once [[session-runtime]] lands) |
| `hold_label` | `hold:*` label present |

Graph reasons come straight from the projection queries; scheduler reasons come from the same reason builder that replaces the string heuristics in `_describe_task_blocker`, so the log line, the CLI, and the dashboard can never disagree.

### 9.2 Ready frontier

`aq project ready` returns the frontier — tasks that would be picked next: `status = READY ∧ is_blocked = 0 ∧ no hold label`, plus a `withheld` section (DEFINED ∧ unblocked, promoted next tick) so operators see the whole runnable edge. `--json` everywhere per the CLI workstream.

## 10. Events: replay and payload registry

### 10.1 `after_seq` replay

`events.id` is already monotonic. The REST list-events command gains `after` (exclusive lower bound, ascending order, capped page size); the websocket accepts `?after_seq=N` and replays persisted events from the DB before switching to live tail. External adapters (the out-of-process Discord adapter in [[messaging-rework]], the dashboard) resume after a disconnect without loss — GC's `--after-cursor` semantics.

### 10.2 Payload registry, test-enforced

Every event type emitted anywhere (bus or `log_event`) must have a registered payload schema in `src/event_schemas.py` — including all new types from this spec (`task.blocked`, `task.unblocked`, `task.skipped_conditional`, `dependency.added`, `dependency.removed`, `gate.created`, `gate.resolved`, `gate.expired`, `label.added`, `label.removed`). A test walks emit call sites and fails on unregistered types (the AQ version of `TestEveryKnownEventTypeHasRegisteredPayload`).

## 11. Cycle rules for typed edges

- **Acyclicity is enforced over blocking edges only** — `blocks`, `parent-child`, `waits-for`, `conditional-blocks` all enter the DFS (a conditional cycle deadlocks just as hard). Non-blocking edges are exempt: `discovered-from` legitimately points backwards; `related` is symmetric.
- `validate_dag_with_new_edge` grows a `dep_type` parameter and skips validation for non-blocking types (self-edges still rejected for all types).
- **`waits-for` deadlock rule:** reject a `waits-for` edge from *X* to container *C* when *X* is a (transitive) child of *C* — it fans in over a set containing itself. Static DFS cannot see future children, so the write-time rule is complemented by a doctor/lint check that reports runtime-detected unsatisfiable fan-ins (Beads returns `Cycles[][]` from `ready --explain`; ours surfaces them as `explain` reasons and a doctor finding).
- Cross-project edges participate in the same global DFS — the graph is one DB, so no special casing.

## 12. Future: status collapse (design only — later phase)

**Not scheduled first.** Designed now so nothing in phases 0–2 paints us into a corner.

**Target enum (8):** `DEFINED, READY, ASSIGNED, IN_PROGRESS, PAUSED, BLOCKED, COMPLETED, FAILED`. The three wait statuses become gates; BLOCKED's meaning generalizes from "terminal failure" to "not runnable — `explain` says why", with failure-BLOCKED distinguished by `outcome=fail` metadata.

**Projection rules** (old → new + gate):

| Legacy status | Projects to |
|---|---|
| `AWAITING_APPROVAL` | `BLOCKED` + open `pr-merged` gate (PR exists) or open `human` gate (manual approval) |
| `AWAITING_PLAN_APPROVAL` | `BLOCKED` + open `human` gate (`await_id` = plan ref); children stay withheld via `parent-child` §3.1, whose rule swaps to "parent has an open plan gate" |
| `WAITING_INPUT` | `IN_PROGRESS` + open `human` gate while the session is alive (the reply arrives as a nudge); `BLOCKED` + the same gate when it is not |

**Migration approach:** two steps. *(a) Compatibility projection:* the DB keeps 11 values; gates become authoritative for the three wait states (dual-written); every reader (API models, Discord, dashboard, CLI) gains a `derived_status` computed from (status, open gates) and migrates onto it. *(b) Cutover migration:* rewrite in-flight rows per the table (creating the matching open gates), shrink `TaskStatus` and `VALID_TASK_TRANSITIONS`, and keep a one-release shim that accepts legacy values in filters/commands and translates them to status + gate predicates. `_check_awaiting_approval` and its reminder/escalation logic dissolve into the gate sweep + expiry escalation.

## 13. Groups and hierarchical ids

**Any task can be a container** — a task with `parent-child` in-edges is a group; there is no separate group entity. Group **progress is computed from the graph, never stored** (Beads' swarm rule): `get_group_progress(container_id)` derives done/ready/blocked/in-progress counts, Kahn-layered **waves** over the blocking edges among the children, and **max parallelism** (the widest wave) on demand. The `workflows.stages` JSON eventually becomes derivable the same way. Waves inform humans and the supervisor; the scheduler keeps ignoring them — parallelism stays emergent from the frontier plus caps.

**Hierarchical child ids:** children created under a parent (supervisor graphs, plan subtasks) get `<parent_id>.1`, `<parent_id>.1.2` — ordinal per parent, depth ≤ 3 — while root ids stay adjective-noun slugs. The id itself now carries structure a human can read in Discord, a branch name (`aq/swift-falcon.2`), or a log line, and sorting groups a family together everywhere.

## 14. Out of scope

Formulas/orders (playbook comeback, todo §8), the `messages` table (Workstream B), session/lease mechanics (Workstream A — this spec only reserves the `lease_stalled` explain code), and wave-*driven* scheduling. The `aq` CLI verbs themselves (`aq task explain`, `aq gate resolve`, `aq project ready`) are Workstream C surface over the commands this spec defines.
