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

> **Landed (partially, 2026-08-31):** the two approval statuses are gone.
> `AWAITING_APPROVAL` and `AWAITING_PLAN_APPROVAL` were deleted from
> `TaskStatus` together with their events and transitions (see §12 note);
> human approval is a `human` gate and PR-merge waiting is a `pr-merged`
> gate resolved by the gate sweep. `WAITING_INPUT` remains a status for now.

## 3. Typed dependency edges

`task_dependencies` gains `dep_type TEXT NOT NULL DEFAULT 'blocks'`. An edge `(task_id, depends_on_task_id, dep_type)` reads "*task_id* has a *dep_type* relationship to *depends_on_task_id*". One pair may carry multiple edge types (e.g. `blocks` and `discovered-from`).

### 3.1 Blocking edge types

A blocking edge contributes to `is_blocked` until its **satisfaction rule** holds:

| dep_type | Reads as | Satisfied when |
|---|---|---|
| `blocks` (default) | task runs after dep | `dep.status = COMPLETED` |
| `parent-child` | task is a child of dep (its container) | `dep.status ≠ DEFINED` — the container has been *released* (the rule originally also excluded the since-removed `AWAITING_PLAN_APPROVAL` status) |
| `waits-for` | task fans in over dep's children | every task with a `parent-child` edge to dep has `status = COMPLETED` (vacuously true with zero children; children added later **re-block** the waiter) |
| `conditional-blocks` | task runs only if dep failed | `dep.status = BLOCKED`, or `dep.status = FAILED` with `retry_count ≥ max_retries` (terminal failure only — a transiently FAILED task about to be retried does not satisfy it) |

Notes:

- **`parent-child` replaces the plan-subtask special case.** Today `_check_defined_tasks` hard-codes "parent IN_PROGRESS counts as met" for `is_plan_subtask` rows. The new rule generalizes it: a DEFINED or AWAITING_PLAN_APPROVAL (status since removed) parent withholds its children (a staged graph, an unapproved plan); any released parent — READY, IN_PROGRESS, even a pure container that never executes — frees them. `parent_task_id` stays as a denormalized pointer for tree rendering; the edge is authoritative.
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
  - "Has a blocking edge" alone cannot tell a graph-BLOCKED task from a terminal close: every child of a container carries a `parent-child` edge. A transition into BLOCKED with a **terminal context** (`session_close_hard_failure`, `max_retries`, `session_close_pipeline_stop`, `timeout`, `stop_task`) therefore writes a `blocked_terminal = <context>` row in `task_metadata` inside the same transaction, and any transition out of BLOCKED (restart, reopen, supervisor recovery, admin skip) deletes it. The cascade drops marked rows from the BLOCKED candidate set before either decider runs, so a hard failure is terminal for hierarchical tasks too; only an explicit restart/reopen brings one back. `aq task explain` reports the mark as `blocked_terminal`.
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
| `pr-merged` | PR URL | `gh` reports merged **and** the PR's work has reached the default branch (sweep, via the extracted PR-poll helper from the approvals mixin) — see §5.1a |
| `ci-run` | run id / URL | `gh run` reports success (sweep) |
| `event` | event type (+ optional payload-filter JSON) | a matching EventBus event fires (subscription resolves immediately; sweep is the restart-safe backstop against events missed while the daemon was down, re-checked from the persisted `events` table) |
| `task` | task id (cross-project allowed) | that task reaches COMPLETED (sweep) |

#### 5.1a Merged is not the same as "on the default branch"

A `pr-merged` gate exists to tell a dependent "your prerequisite has shipped".
A PR whose base is a *feature* branch merges without putting a line on the
default branch, so resolving on `merged` alone is wrong. That is the Pkg 4
outage: PRs #284/#288/#289 all merged into `feature/playbook-v2-pkg4-core`,
their tasks closed COMPLETED, every downstream gate resolved — and nothing
ever merged `pkg4-core` into `main`, so `main` lacked
`src/playbooks/executors/agent_task.py` while every dependent believed it was
there.

So `_sweep_resolve_pr_ci_gates` asks a second question of a merged PR
(`_pr_reached_default_branch`): if `baseRefName` is not the project default
branch, the gate stays open until `origin/<base>` is an ancestor of
`origin/<default>`. An **unanswerable** question — no `gh`, no auth, no
network, no checkout — resolves the gate as before: unknowable must not wedge
every dependent shut forever.

Before it merges anything, `pr_merge` asks what CI said. `integration.merge_ci_policy`
(`off` / `warn` / `required`, shipped as `warn`) decides what a non-green
status-check rollup does — nothing, a logged verdict in the result's `ci`
block, or a refusal. The gate exists because GitHub's does not: `main`
carries no required status check, so `gh pr merge` merged 29 of the last 30
PRs red, #341 among them. It asks a second question too: is the head *up to
date* with its base? A green rollup only proves the head passed against the
base as it was when the run started, and two PRs each green on a stale base
put a red `main` together (#390 + #391). `integration.merge_require_up_to_date`
(shipped `true`) folds that into the same verdict as a `base` block. See
[the merge-gating guide](../../guides/merge-gating.md).

The merge itself records what it did. `pr_merge` writes `pr_base` and
`pr_merged_to_default` on the task carrying that PR and returns
`merged to <base> (not <default>)`, so the state is visible without asking
GitHub again. `aq doctor --check pools.stranded_feature_branches` is the
standing alarm for the other half: a branch that has had PRs merged *into* it
and has no open PR taking it to the default branch.

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
| `work_outcome` | `shipped` \| `no-op` \| `blocked` \| `abandoned` | task close; `no-op` also by the conditional auto-close (§3.1). `no-op` tells git verification the task produced no code: the require-a-PR / merge checks and the integrate phase are skipped (the `reviewer` / `final-reviewer` stage profiles get the same treatment by profile id) |
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

**Swarm work model additions** (`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part II §9, all registered in `src/event_schemas.py`): `task.ready` `{task_id, project_id, title, reason}` — emitted on every entry into the ready frontier (promotion, unblock, hold-label removal, gate resolution, release), with an in-transaction `events` audit row so a crash between commit and bus emission cannot lose it; `task.claimed` `{task_id, project_id, title, session_id?, profile_id?, claim_epoch?}`; `task.claim_conflict` `{task_id, project_id, title, session_id?}` (debug); `pool.scaled` `{project_id, profile_id, kind, count}`; `session.claim_timeout` `{session_id, task_id?}`; admission-wait wake signals `snapshot.refreshed` `{tick}`, `project.resumed` `{project_id}`, `constraint.released` `{project_id}`.

## 11. Cycle rules for typed edges

- **Acyclicity is enforced over blocking edges only** — `blocks`, `parent-child`, `waits-for`, `conditional-blocks` all enter the DFS (a conditional cycle deadlocks just as hard). Non-blocking edges are exempt: `discovered-from` legitimately points backwards; `related` is symmetric.
- `validate_dag_with_new_edge` grows a `dep_type` parameter and skips validation for non-blocking types (self-edges still rejected for all types).
- **`waits-for` deadlock rule:** reject a `waits-for` edge from *X* to container *C* when *X* is a (transitive) child of *C* — it fans in over a set containing itself. Static DFS cannot see future children, so the write-time rule is complemented by a doctor/lint check that reports runtime-detected unsatisfiable fan-ins (Beads returns `Cycles[][]` from `ready --explain`; ours surfaces them as `explain` reasons and a doctor finding).
- Cross-project edges participate in the same global DFS — the graph is one DB, so no special casing.

## 12. Future: status collapse (design only — later phase)

> **Landed for the approval statuses (2026-08-31).** `AWAITING_APPROVAL` and
> `AWAITING_PLAN_APPROVAL` were deleted from `TaskStatus` (with the
> `PR_CREATED`/`PR_MERGED`/`PR_CLOSED`/`PLAN_*` events and their
> transitions). PR-merge waiting is a `pr-merged` gate resolved by the gate
> sweep (`_sweep_resolve_pr_ci_gates` → `_poll_pr_merged`,
> `src/orchestrator/pr_polling.py`); human approval is a `human` gate; the
> old 60-second `_check_awaiting_approval` poller is deleted. Integration
> policy is the explicit `integration_mode` column (task/project) + config
> `integration.default_mode`, replacing `requires_approval` (Alembic
> `c4d5e6f7a8b9`, with a preflight for stranded rows — see
> `docs/guides/upgrade-integration-mode.md`). The `WAITING_INPUT` collapse
> below remains future work. The mapping table is kept as the original
> design record.

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

**Superseded by the full hierarchy model.** This section sketched the shape; the implemented design — invariants, the `set_parent` single-writer contract, container settlement, close/delete/archive semantics, the migration, and doctor checks — lives in `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part I §4–§8, §17. Summary of what landed:

**Any task can be a container** — a task with a `parent-child` in-edge is a group; there is no separate group entity. The `parent-child` edge is the source of truth; `tasks.parent_task_id` is a denormalized cache, written only by `HierarchyQueryMixin.set_parent` (`src/database/queries/hierarchy_queries.py`). Exactly one parent per task is enforced by the partial unique index `uq_task_deps_single_parent` on `task_dependencies` (`dep_type = 'parent-child'`). Ids are immutable and dotted (`<parent_id>.<n>`), assigned from `tasks.next_child_ordinal` (`src/task_names.py: reserve_child_ordinal` / `child_task_id`). Two depths are tracked separately: structural depth (parent-child chain, root = 1) is capped at `MAX_STRUCTURAL_DEPTH` (3); naming depth (dot segments in the id) is a display artifact of the same cap. `--parent` on task creation is refused at either cap with `hierarchy.depth`.

Containers are marked explicitly: `task_metadata.container = true`, set on a task's first child and never cleared. Group **progress is computed from the graph, never stored** (Beads' swarm rule): `get_group_progress(container_id)` derives done/ready/blocked/in-progress counts, Kahn-layered **waves** over the blocking edges among the children, and **max parallelism** (the widest wave) on demand. Container **settlement** (auto-completing a container once every child is terminal) is event-driven — it runs inside `_apply_transition` → `settle_containers`, in the same transaction as the child transition that completes it, walking up to `MAX_STRUCTURAL_DEPTH` ancestor levels. A settled container emits `task.completed` on the bus like any other transition, so the default pipeline's branch-guarded review rules (and reflection, once unpaused) see it. A backstop sweep, `_sweep_container_completion`, runs every `work_graph.container_sweep_interval_seconds` (default 60; 0 disables) and logs a WARNING on any hit — it should normally find nothing. Waves inform humans and the supervisor; the scheduler keeps ignoring them — parallelism stays emergent from the frontier plus caps.

**Events and error codes.** A successful reparent emits `task.reparented` on the bus after commit, with `task_id`, `project_id`, `title`, `old_parent` and `new_parent` (`null` at the root); playbooks can trigger on it. Rejected hierarchy mutations raise `HierarchyError` and surface as `hierarchy.<code>`, the full set being: `not_found` (task or parent missing), `self_parent`, `cross_project`, `container_closed` (parent is COMPLETED), `cycle`, `depth` (structural or naming cap), `open_children` (any unforced transition to COMPLETED with a non-terminal direct child — enforced in `_apply_transition`, not only at the close surfaces), `open_descendants` (archive of a subtree with a non-terminal *descendant*), `has_children` (delete without `--cascade`), `live_descendants` (abandon or cascade while a *descendant* — never the closing task itself — has a live session), `manually_paused_descendants` (abandon while a descendant is hand-paused), and `cycle_check_skipped` (internal: the bulk graph-creation writer `set_parent_bulk` was handed a task that is not a freshly inserted leaf). `aq schema` exposes the same list as the `hierarchy_error` enum.

**Hierarchical child ids:** children created under a parent (supervisor graphs, plan subtasks, `--parent`) get `<parent_id>.1`, `<parent_id>.1.2` — ordinal per parent via `next_child_ordinal`, depth ≤ 3 — while root ids stay adjective-noun slugs. Ids never change after assignment. The id itself now carries structure a human can read in Discord, a branch name (`aq/swift-falcon.2`), or a log line, and sorting groups a family together everywhere.

## 13a. Claims and pools

**Implemented by the swarm work model.** Pull-based work assignment for `lifecycle: pool`
profiles — the claim transaction, worker pools, and worker-filed work — is specified in
`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part II (§9–§12); it does
not change anything above. `lifecycle: task` sessions keep the existing push assignment
(§9 D4 — hybrid dispatch). Summary: a pool session calls `aq task claim [--next] [--wait
S]` against the same ready frontier this spec defines (§9 "Reads" / the work query in
design §10 layers on top of it); the frontier-entry event this section's `task.ready`
depends on is emitted on every `DEFINED → READY` promotion, `is_blocked` flip, hold-label
removal, and gate resolution (design §9), not only on the legacy `task.unblocked` path.
Off by default (`swarm.enabled: false`, `docs/specs/config.md` §4.11).

## 14. Out of scope

Formulas/orders (playbook comeback, todo §8), the `messages` table (Workstream B), session/lease mechanics (Workstream A — this spec only reserves the `lease_stalled` explain code), and wave-*driven* scheduling. The `aq` CLI verbs themselves (`aq task explain`, `aq gate resolve`, `aq project ready`) are Workstream C surface over the commands this spec defines.
