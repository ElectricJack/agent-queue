---
tags: [system-spec, task-graph, tasks, dependencies, hierarchy, gates, readiness, layout]
---

# System spec: task-graph

**System:** `task-graph`, one of the 18 systems in the Agent Queue system inventory
(vault: `projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, in review on 2026-10-20).
**Written:** 2026-10-20, against `main` at `22cffd1b7`, for task `eager-vault-55.2`.
**What this page is:** a map of the system as the code stands, written for an engineer
new to Agent Queue. It describes; it does not decide. The "Planned work" section is a
snapshot of the open tasks and in-flight specs on the date above and goes stale as they
land.

Where this page summarises something another document explains in depth, it links
there instead of repeating it. The everyday reader's page is
[docs/concepts/tasks.md](../../concepts/tasks.md); read it first if you have not.

---

## 1. Purpose: the problem it solves

Agent Queue runs many coding agents at once on one project. Each agent needs to know
exactly one thing to start: *this piece of work, and everything it depends on, is
ready*. The operator needs the opposite view: *what is the whole plan, how far along is
it, and why is this piece not moving?*

The task graph is the durable record that answers both. It stores:

- **tasks** — what should happen, with a status for where each piece of work is;
- **typed edges** between tasks — what must be true before a task may start, and
  how tasks relate (part of, discovered from, duplicate of);
- **gates, labels and holds** — non-task conditions that keep a task waiting
  (a human approval, a timer, a CI run, a deliberate hold);
- **the hierarchy** — epics (containers), phases and their children, so a large plan
  is navigable and settles itself when its children finish;
- **the evidence of completion** — deliverables, subtasks and outcome metadata, so a
  close says what actually happened.

From those facts it derives, in the same database transaction as every write, one
persisted bit per task: `tasks.is_blocked`. That bit is the single authority on
whether the graph allows a task to run. Everything downstream — the scheduler, worker
claims, `aq task explain`, the dashboard — reads it instead of re-deriving dependency
semantics.

The design principle behind this split is the repository's "mechanism in code, policy in
playbooks": the graph is deterministic mechanism. It never calls an LLM, never guesses
intent from text, and never decides *who* runs a task or *when* it is delivered. Those
are other systems (section 4).

## 2. Where the code lives

| Concern | Code |
|---|---|
| Statuses, task types, dependency kinds | `src/models.py` (`TaskStatus`, `TaskType`, `DepType`, `BLOCKING_DEP_TYPES`, `HOLD_LABEL_PREFIX`) |
| Transition table and DAG validation | `src/state_machine.py` |
| Schema | `src/database/tables.py` (`tasks`, `task_dependencies`, `task_metadata`, `task_labels`, `gates`, `task_gates`, `task_subtasks`, `task_criteria`, `task_proposals`, `ingest_batches*`, `task_layouts*`, `archived_tasks`, `epic_dependencies`) |
| Task rows and transitions | `src/database/queries/task_queries.py` (`transition_task`) |
| Edges | `src/database/queries/dependency_queries.py` |
| The `is_blocked` projection | `src/database/queries/blocked_state.py` |
| Gates | `src/database/queries/gate_queries.py`; sweep in `src/orchestrator/core.py` (`_sweep_gates`) |
| Hierarchy (parent/child, containers, settlement) | `src/database/queries/hierarchy_queries.py` |
| Ingestion hold | `src/database/queries/ingest_hold.py`, `ingest_batch_queries.py` |
| Graph documents, formulas, validation | `src/task_graph/parser.py`, `validator.py`, `creator.py`, `formulas.py` |
| Change sets and proposals | `src/commands/task_changes.py`, `src/commands/proposal_commands.py`, `src/commands/fidelity_review_commands.py` |
| Batch checks and digest (held ingestion) | `src/task_graph/batch_checks.py`, `batch_digest.py` |
| Readiness and container activity (read side) | `src/task_graph/readiness.py`, `src/task_graph/activity.py` |
| Explain | `src/explain.py`, `_cmd_explain_task` in `src/commands/task_commands.py` |
| Phases and subtasks | `src/commands/phase_commands.py`, `src/commands/task_subtask_commands.py` |
| Deliverables and the close gate | `src/deliverables.py`; `_cmd_task_close` in `src/commands/session_commands.py` |
| One-shot operator closes | `src/sessions/obsolete.py` |
| Deletion | `_cmd_delete_task` in `task_commands.py`, `src/integration/task_deletion.py` |
| Ids | `src/task_names.py` |
| Spatial layout | `src/task_graph/layout/`, `src/database/queries/layout_queries.py`, `src/orchestrator/layout_step.py`, `src/api/graph_layout.py`, `src/commands/graph_commands.py` |
| Cascade promotion and container backstop | `src/orchestrator/monitoring.py` (`_check_defined_tasks`, `_sweep_container_completion`), `src/orchestrator/lifecycle.py` |
| Epic-level integration ordering | `src/task_graph/integration_dependencies.py` |

The command handlers in `src/commands/task_commands.py` (about 6,700 lines) are the
entry point for almost every graph write; see section 5 for why that matters.

---

## 3. How it works today

### 3.1 Key concepts

**Task.** A row in `tasks`. Its id is human-friendly: a root task gets an
adjective-noun slug (`bold-summit`, or `bold-summit-42` once the 896 plain pairs run
short), and a child gets its parent's id plus a dotted ordinal (`eager-vault-55.2`).
Ordinals come from an atomic counter on the parent (`next_child_ordinal`) and are never
reused. A new root name is refused if any identity table — live or archived tasks,
integration checkpoints, branch origins, branch owners — still knows it, so a new task
never inherits a deleted task's git state (`_task_identity_exists` in
`src/task_names.py`). A task carries a `task_type` (feature, bugfix, refactor, test,
docs, chore, operator, research, plan, sync, design, art, plus the daemon-only
promotion and backmerge), a priority, routing fields, and a JSON list of
`deliverables`.

**Status.** One of nine stored values (`TaskStatus`, `src/models.py`): DEFINED, READY,
ASSIGNED, IN_PROGRESS, WAITING_INPUT, PAUSED, COMPLETED, FAILED, BLOCKED. Status is
the task's *lifecycle*; it is not its blockedness. A READY task can still be blocked by
a gate; a BLOCKED task is one that was stopped *by a decision* (a hard failure, a spent
retry budget, an operator stop) and needs attention. The approval statuses that once
existed (AWAITING_APPROVAL, AWAITING_PLAN_APPROVAL) were removed on 2026-08-31 and
became gates. The [concepts page](../../concepts/tasks.md#statuses-and-phases) explains
each status for operators.

**Typed edge.** A row in `task_dependencies`: `(task_id, depends_on_task_id,
dep_type)`, all three in the primary key so one pair can carry several kinds. Five kinds
block (`BLOCKING_DEP_TYPES`):

| Kind | Satisfied when |
|---|---|
| `blocks` | the prerequisite is COMPLETED |
| `parent-child` | the parent (container) has been released — is no longer DEFINED |
| `waits-for` | every child of the named container is COMPLETED (dynamic fan-in; vacuously true with no children) |
| `conditional-blocks` | the prerequisite has *terminally* failed (a contingency path); a retryable failure does not count |
| `delivery-of` | source completion in SQL, plus current default-target Git containment in `AdmissionView` |

Four are informational and never block: `discovered-from`, `related`, `duplicates`,
`supersedes`. Self-edges are refused by a check constraint; cycles over blocking edges
are refused at write time (`validate_dag_with_new_edge` in `src/state_machine.py`).

**Gate.** A row in `gates`, attached to one or more tasks through `task_gates`. Types:
`human`, `timer`, `pr-merged`, `ci-run`, `event`, `task`, `routing`, `review`. Status
is `open`, `resolved`, `expired` or `cancelled`. Only `resolved` releases the task:
an *expired* gate keeps blocking on purpose, so a timed-out approval never silently
approves itself. The cycle's gate sweep resolves gates whose condition has become true
(a timer passed, a PR merged, a CI run finished) and expires overdue ones.

**Label and hold.** Free-form labels live in `task_labels`. Any label starting with
`hold:` withholds the task from the claim frontier without touching its status — a
cheap, reversible "not yet".

**Ingestion hold.** A separate, stronger hold: `tasks.held_batch_id` is set on every
member of a held ingestion batch (section 3.3.4). A held task may be READY and
unblocked, but no claim, frontier, `task.ready` event or train will admit it until the
batch is released. It is independent of status, routing and gates
(`src/database/queries/ingest_hold.py`).

**Container (epic).** Any task can be a parent. The `parent-child` edge is the truth;
`tasks.parent_task_id` is a cache that only `HierarchyQueryMixin.set_parent` writes, in
the same transaction as the edge. A unique partial index (`uq_task_deps_single_parent`)
enforces one parent per task. A task with children is a *container*: it is never leased
to a worker, it is withheld from the frontier, and it **settles** — completes itself —
when its children are done. Structural depth is capped at three levels below a root
(`MAX_STRUCTURAL_DEPTH`). A container can also be *declared* (`task_metadata.container`)
before it has children, so an empty epic stays open rather than being mistaken for a leaf.

**Phase.** A container (born DEFINED, `task_type` plan) with `phase` metadata
(`{order, label}`). Each phase gets `blocks` edges onto the earlier sibling phases, so it
stays DEFINED until they complete; a DEFINED parent withholds every descendant through
the `parent-child` rule, so phases run in order while the tasks *inside* a phase run in
parallel. No edges are written between the work items of two phases. Work joins a phase
with `aq task create --parent <phase-id>`; a settled phase refuses new children
(`hierarchy.container_closed`). Empty phases, keyed standing parents and declared
containers stay open while childless (`childless_held_open_container()`). There is no
separate "barrier" primitive: phases and `blocks` edges are the ordering tools
(`src/commands/phase_commands.py`; [work-graph design §13b](../design/work-graph.md)).

**Subtask.** A checklist line on one task, in `task_subtasks` (pending, in_progress,
done, skipped). Subtasks are *not* tasks: no edges, no claims, no ids. They exist so a
worker's plan is visible on the graph, and a close is refused with `subtasks.open` while
any is unfinished unless the closer explicitly skips them
([work-graph design §13c](../design/work-graph.md)).

**Keyed standing parent.** A long-lived container identified by a `parent_key`
(deduplicated through `dedup_key = "parent:<key>"` under an advisory lock), used by
playbooks that file work into a stable bucket over time (work-graph design §13d).

**Deliverable.** An item in `tasks.deliverables` that a passing close must satisfy. The
kinds are `file`, `test`, `command`, `flag`, `registration` and `review`: a file that must
exist (repo-relative), a test or command the close must record (`--test`, `--command`;
close never runs it), a flag or registration that must appear in the repository, or a
submitted document review. Research and design tasks carry an implicit `review`
deliverable (dispatched adversarial reviewers are exempt). A closer who cannot meet one says so with
`--deliverable-unmet '<item>: <reason>'` (`src/deliverables.py`).

**Outcome metadata.** `task_metadata` is a key/value side table. A close writes the
outcome there: `outcome`, `failure_class`, `work_outcome` (`shipped`, `no-op`,
`blocked`, `abandoned`), `work_commit` and similar. Many other keys live there too:
`needs_attention`, `blocked_terminal`, `container`, `phase`, `obsolete`. The repository
rule is metadata first: new per-task facts go in `task_metadata` before anyone adds a
column.

**Graph document.** A versioned (`version: 1`) description of a set of tasks — nodes
with keys, `needs` (edges, `blocks` by default), phases, subtasks, labels, acceptance and
an optional intelligence-class hint, never a profile or agent — created in one call with
`aq task create --graph FILE`, or `--from-spec PATH` to take the first fenced `aq-graph`
block of a spec (with `--dry-run` to see the validation report only). **Formulas** are reusable graph documents stored in the
vault (`vault/formulas/` and `vault/projects/<p>/formulas/`) with declared variables;
`aq formula cook` expands one into a graph document
([formulas design](../design/formulas.md), [concepts page](../../concepts/tasks.md#formulas)).

### 3.2 Data model

The tables the system owns, and what each holds:

| Table | Holds | Notes |
|---|---|---|
| `tasks` | one row per live task | `status`, `is_blocked` (projection, never authored), `parent_task_id` (cache), `held_batch_id` (ingestion hold), `next_child_ordinal`, `deliverables`, `dedup_key`, routing and integration fields |
| `task_dependencies` | typed edges | PK `(task_id, depends_on_task_id, dep_type)`; indexes `(task_id, dep_type)` and `(depends_on_task_id, dep_type)`; one-parent unique index |
| `task_metadata` | key/value facts per task | outcome, attention codes, container/phase flags, obsolete records |
| `task_labels` | labels | `hold:*` withholds |
| `gates`, `task_gates` | non-task conditions and their waiters | one gate can hold many tasks |
| `task_subtasks` | per-task checklist | no foreign key to `tasks`; ordinal-ordered |
| `task_criteria`, `task_context` | acceptance criteria and context rows written with a graph | |
| `task_proposals` | staged change sets (draft → committed / held) | payload is `{tasks, edges}` with temporary ids |
| `ingest_batches`, `ingest_batch_members`, `ingest_batch_receipts` | held ingestion batches, keyed by proposal id | historical task ids are kept as plain text, not foreign keys |
| `archived_tasks` | archived task rows | same shape as `tasks`, so archiving is lossless and restorable |
| `epic_dependencies` | declared epic-to-epic ordering for integration batches | written insert-or-ignore |
| `task_layouts`, `task_layout_cells`, `project_layout_meta`, `layout_dirty`, `layout_reflow_requests`, `layout_jobs`, `layout_tidy_requests*` | persisted graph layout and its work queues | section 3.3.8 |

Schema details and migration history: [docs/specs/database.md](../database.md) and
[implementation/work-graph.md §2](../implementation/work-graph.md). Field-by-field model
documentation: [models-and-state-machine.md](../models-and-state-machine.md).

### 3.3 Main flows

#### 3.3.1 Creating work

There are five ways in, all through `CommandHandler` (section 5, principle 1):

1. **One task** — `aq task create` (`_cmd_create_task`). It can name a parent, edges,
   labels, deliverables and subtasks. A worker filing emergent work gets it as a child
   of the task it holds, and that child blocks the parent's passing close
   ([AGENTS.md](../../../AGENTS.md#working-as-an-aq-worker)); workers have a filing
   quota per held task.
2. **A graph document** — `aq task create --graph` (`_cmd_create_task_graph`). The
   parser (`src/task_graph/parser.py`) decides only what the document *says*, and
   refuses routing choices (`routing_choice_forbidden`, `unsupported_routing`); the
   validator (`validator.py`) runs the deterministic rule table — variable substitution
   (`unknown_var`), `duplicate_key`, `bad_dep_type`, `self_edge`, `cycle` over blocking
   kinds, `unresolved_need`, `dependency_on_ancestor`, the phase rules
   (`duplicate_phase_key`, `unknown_phase`, `inverted_phase_edge`, and the warnings
   `redundant_phase_edge`, `phase_without_nodes`), and spec references
   (`missing_spec_ref`, `missing_spec_section`: errors for `--from-spec`, warnings
   otherwise) — and reports every finding at once. Rule names are a contract: golden
   tests and `--dry-run` match on them. The creator (`creator.py`) then writes every row
   in **one transaction**: the graph exists whole or not at all. Leaves are born DEFINED
   and promoted by the cascade; new containers are born IN_PROGRESS and settle. Children
   of a new container get ids `<container>.1..N`; a graph placed under an existing
   container gets provisional ids that are reserved and rewritten inside the same
   transaction. A scoped worker session can only file under the task it holds.
3. **A formula** — `aq formula cook` renders a vault template into a graph document and
   creates it the same way ([formulas design](../design/formulas.md)). Formulas can
   `extend` one parent (phases merge by key); the registry keeps the last good parse of
   each file; provenance (`formula`, `formula_chain_sha`, a `formula:<name>` label and a
   snapshot context row) is written onto the created tasks. Cook is refused for
   non-elevated sessions.
4. **A change set / proposal** — `src/commands/task_changes.py` applies a connection-
   scoped set of task and edge operations inside the caller's transaction, with
   PostgreSQL table locks fencing the scheduler, claims and routing. Validation runs
   the same code inside a rolled-back savepoint, so a dry run and a real run cannot
   disagree. Operations are `tasks`, `edges`, `edits` (pause, resume, block, archive),
   `remove_edges` and `comments`. Proposals (`task_proposals`, statuses `draft`,
   `ready`, `held`, `committed`, `discarded`) stage a change set: `aq task
   batch-propose` writes a `ready` proposal and emits `proposal.ready`, and `aq task
   batch-commit` materialises it ([task change sets guide](../../guides/task-change-sets.md)).
   The automatic approval gate on `proposal.ready` is the default-pipeline playbook's,
   so on an install with playbooks paused a proposal waits for a manual commit.
5. **Held ingestion** — when a `spec-ingest` session proposes the implementation graph
   for an approved spec, the batch is created **held**: every member exists, is routed
   and visible, but carries `held_batch_id`. Deterministic batch checks
   (`batch_checks.py`) and a fidelity reviewer judge it before release (section 3.3.4).

Every creation path marks the layout dirty and recomputes `is_blocked` for the new rows
in the same transaction.

#### 3.3.2 Blockedness: the persisted projection

`blocked_state.py` defines one SQL predicate:

> `is_blocked(t) = 1` iff any blocking edge out of `t` is unsatisfied **or** any gate
> attached to `t` is not resolved.

It is built from one correlated `EXISTS` per rule, in the order of the
[design §3.1](../design/work-graph.md), so the same expression can be used inside an
`UPDATE tasks` and in a `SELECT … WHERE`. Every query method that can change an input —
an edge, a gate, a status, `retry_count`/`max_retries` (they distinguish a transient
failure from a terminal one for `conditional-blocks`), `branch_name`, `repo_id` — calls
`recompute_blocked(seeds, conn=conn)` **inside its own transaction**. The recompute
walks outward from the seeds to everything they can affect and repeats until nothing
flips, then emits `task.blocked` / `task.unblocked`. A reader therefore never sees a
graph write without its projection.

Two deliberate properties:

- **Graph only.** Capacity reasons (no idle agent, workspace locked, budget, cooldown)
  are never persisted; they change every tick and belong to explain.
- **Not transitive through blockedness.** A task waits on its prerequisite's
  *completion*, not on the prerequisite being unblocked.

The development-delivery side of a `blocks` edge — "is the prerequisite's code on the
branch I will start from?" — is *not* in this SQL. It is observed asynchronously by the
integration system's `DeliveryObserver` and applied at claim time (section 3.3.3).
`delivery-of` edges require source completion in SQL. `AdmissionView` separately
requires current default-target containment; a receipt cannot authorize admission
after a target rewind.

#### 3.3.3 From DEFINED to claimable

Each orchestrator cycle (the ordering is owned by `daemon-core`) runs, among much else:

1. **Gate sweep** (`_sweep_gates`, cadence `work_graph.gate_sweep_interval_seconds`) —
   resolve satisfied gates, expire overdue ones. It runs first so a newly resolved gate
   unblocks its waiters in the same cycle.
2. **Cascade promotion** (`_check_defined_tasks` in `src/orchestrator/monitoring.py`) —
   first dispose of contingency tasks that can never fire (`conditional_autoclose`), then
   move DEFINED and graph-blocked BLOCKED tasks to READY when `is_blocked` is 0. It
   leaves alone any BLOCKED task carrying a `needs_attention` code (except the advisory
   `stale_open`) or a `blocked_terminal` marker: those were blocked by a decision, and
   the graph clearing says nothing about the failure.
3. **Container backstop** (`_sweep_container_completion`, section 3.3.6).
4. **Lifecycle sweep** (`src/orchestrator/lifecycle.py`) — flags READY work that has sat
   rejected by the frontier for `work_graph.frontier_rejected_after_seconds` (600 s) with
   `needs_attention = stale_open`, so a long wait is reported rather than silent.

A READY, unblocked task is *graph*-ready. Whether a particular worker may take it is the
**claim frontier**, owned by `scheduling-and-claims` but built from this system's
predicates. `claim_frontier_predicates()` in `src/database/queries/claim_queries.py`
combines, among others: `is_blocked` is false; no `hold:*` label; not ingestion-held;
not a container; not a plan subtask; and, for hierarchical (train) projects, the
delivery fences — same-parent prerequisites delivered to the shared parent,
cross-epic prerequisites contained in the default branch, and no pending epic refresh.
Each predicate has a human sentence in `FRONTIER_PREDICATE_DETAILS`, which is what
explain prints.

`src/task_graph/readiness.py` is the read-only projection the dashboard and lists use:
it labels each task `claimable`, `withheld`, `waiting_on_delivery` or `unknown`, from the
same exclusions (with `cached_only=True`, so a list never waits on git).
`aq project ready` lists the READY, unblocked, unheld frontier plus a withheld section.

#### 3.3.4 Held ingestion (partly built)

The spec-ingestion design ([section 7.2](#72-in-flight-specs)) turns an approved implementation spec into
its task graph in one transaction, but holds it:

1. A `spec-ingest` session proposes the graph (`_cmd_task_batch_propose`). The source
   must be the approved decision (`spec:decision:<id>`), whose packet is immutable.
   An implementation batch is stored `held`; a design spec's single follow-up task is
   released immediately.
2. `batch_checks.run`/`check` compare the batch with the live graph around it — cycles,
   endpoints, ancestors, missing metadata (`section_anchor`, `acceptance`, `size`,
   `owned_modules`, …), claims and integration ownership. Hard findings refuse; some
   (`stale_revision`, `active_claim`, `integration_owned`) are hard but retryable;
   advisory findings go to the reviewer. No LLM, no reading meaning from words.
3. A fidelity reviewer (`_cmd_ensure_fidelity_review`, `_cmd_task_batch_review_record`)
   checks the tasks against the spec section by section and may edit the held batch
   in place (`_cmd_task_batch_update`, live assigned reviewer only: rewrite, reparent,
   rewire, add a leaf, split, combine, move to root, retire). `batch-commit` refuses a
   held batch (`fidelity_review_required`).
   `batch_digest.py` hashes the batch's members and edges (titles excluded) so a review
   receipt stays valid across a retitle.
4. **Release** clears `held_batch_id` atomically. The query
   (`ingest_batch_queries.release`) exists, but no command calls it yet — see
   sections 6 and 7.

#### 3.3.5 Running and closing

A claim moves the task to ASSIGNED/IN_PROGRESS (claims are `scheduling-and-claims`;
sessions are `sessions-and-harnesses`). Every status change goes through
`transition_task` (`task_queries.py`), which checks the pair against
`VALID_STATUS_TRANSITIONS` in `src/state_machine.py`. By default an invalid transition
is **logged, not refused**: `state_machine.enforce` defaults to false. One rule is
always enforced: a task cannot move to COMPLETED while it has open children
(`hierarchy.open_children`).

A worker's `aq task close` (`_cmd_task_close` in `session_commands.py`) is where the
graph's completion rules bite. After checking that the status is closeable and that the
caller owns the claim fence, an ordinary close refuses, in order, when:

- the session has unread messages (`messages.pending_before_close`) or an unpublished
  review revision (`reviews.revision_unpublished`);
- on a pass, a required deliverable has no evidence and was not declared unmet
  (`deliverables.unmet`, `deliverables.invalid_waiver`; `src/deliverables.py`: a path
  target is checked in the worktree, a command target against the `--test`/`--command`
  values on the close, a `review` item against the task's submitted document reviews);
- a subtask is still pending or in progress (`subtasks.open`, unless
  `--skip-open-subtasks`);
- a child is still open (`open_children`, `hierarchy.open_children`,
  `hierarchy.live_descendants`, `hierarchy.manually_paused_descendants`);
- required completion CI has not passed (`completion.required_ci`; see
  [task completion CI](../design/task-completion-ci.md)).

The close records outcome metadata and, through the integration system, the delivery
evidence. Completion recomputes the dependents' `is_blocked` in the same transaction, and
the next cascade promotes them.

For work no worker will close, the operator and supervisor have one-shot dispositions
(`src/sessions/obsolete.py`): `aq task close --obsolete` (`work_outcome=abandoned`),
`--no-change` (passing, `no-op`) and `--done` (passing, `shipped`). Each completes the
task from any non-terminal status in one transaction and leaves nothing to publish.
See [superseded completion](../../guides/superseded-completion.md).

#### 3.3.6 Containers settle themselves

A container is never run. When its children are done it completes through
`settle_containers` (`hierarchy_queries.py`), called in the same transaction as the
child's close. A low-cadence backstop (`_sweep_container_completion`, every
`work_graph.container_sweep_interval_seconds`) catches anything the event path missed
and logs a warning when it does; it also releases claims stopped workers left on
containers and completes stale BLOCKED/PAUSED containers whose children are all
delivered. A held container never auto-settles.

A container's *stored* status is bookkeeping (an epic reads IN_PROGRESS while any child
is unfinished). What the dashboard shows is derived on read by
`src/task_graph/activity.py` from the leaves under it — `active`, `blocked`, `waiting`
(with a reason) or `completed` — and never stored. Delivery status of an epic is shown
separately from its implementation progress.

Hierarchy edits — reparent, container declaration, abandoning a subtree — go through
`HierarchyQueryMixin` and refuse with a structured `hierarchy.<code>` error: `not_found`,
`self_parent`, `cross_project`, `container_closed`, `dependency_on_container`, `cycle`,
`depth`, `open_children`, `open_descendants`, `has_children`, `live_descendants`,
`live_parent`, `manually_paused_descendants`. In hierarchical (train) projects bulk
writes, phases and keyed standing parents are refused
(`hierarchy.phases_unsupported_mode`), because the train owns the branch layout.

#### 3.3.7 Explaining

`aq task explain <id>` (`_cmd_explain_task`) answers "why is this not running?" as a
list of `{code, detail, ref}` reasons, persistent ones first: operator-decision holds,
recovery incidents, `needs_attention`, terminal blocks, pauses and `hold:*` labels,
delivery and admission fences, then `blocked_dependency` / `blocked_gate` from the
graph, then provider holds, route and pool waits, and last the transient capacity
reasons read from the scheduler's cached state (`src/explain.py`). Integration blockers
are appended, not substituted. The reasons it prints for graph blockers are computed by
the same per-edge predicate the projection uses (`unmet_dependency_predicate`), so explain
and `is_blocked` cannot disagree about an edge.

#### 3.3.8 Showing the graph: persisted spatial layout

The dashboard's graph is laid out on the server and stored, so every viewer sees the same
picture and a large project loads by viewport tile.

- **Model.** Each container lays out only its own children as a layered DAG over the
  ranking edges (`blocks`, `waits-for`, `conditional-blocks`); `discovered-from` edges
  are drawn but do not rank. Positions persist in `task_layouts` per
  `(project, variant, task)`, with fractional order keys so an insert rarely moves its
  neighbours. Two variants: `all` and `active` (finished subtrees drop out or become
  stubs).
- **When it runs.** Every graph mutation writes a `layout_dirty` row in its own
  transaction. The scheduler's layout step (`src/orchestrator/layout_step.py`) drains
  queued jobs, then dirty projects (debounced, deepest containers first), then tidy
  requests; every 15 minutes it reflows, reconciles presence and converges
  `ENGINE_RULES_VERSION`. `publish_layout` applies a batch atomically and bumps
  `layout_version`.
- **Determinism.** Non-root containers order children by dependency depth, then
  `(created_at, id)`, with no optimiser. The project root places new nodes incrementally
  and runs a bounded optimiser on tidy (`aq graph tidy` / `graph_tidy`). Budgets are
  evaluation counts, so the same input yields the same layout; wall-clock limits are only
  safety valves.
- **Reading it.** `src/api/graph_layout.py` serves extent, tiles, list, node, locate,
  tidy and job endpoints. View resolution (collapse, focus, filters, stubs, docked workers)
  and compaction of collapsed containers happen per request and are never stored.

The [spatial layout design](../../superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md)
is the background; the code has moved past it in its constants, budgets, the
rules-version ledger and the on-by-default switch (`graph_layout.enabled`).

#### 3.3.9 Removing work

- **Archive** (`aq task archive`) moves terminal tasks into `archived_tasks` and writes a
  markdown note; archived tasks can be listed, restored or purged. The archive rule is
  delivery-aware: work whose integration history still matters is kept
  ([archive design](../../superpowers/specs/2026-09-20-archive-tasks-with-integration-history-design.md)).
- **Delete** (`aq task delete`, `_cmd_delete_task`; `remove` is a deprecated alias) is
  operator- or supervisor-only. Without `confirmed` it returns a read-only plan; with it,
  it removes the task (or the subtree with `--recursive`) and keeps or deletes its
  branches (`--branches keep|delete`, default `keep`) synchronously, emitting
  `task.deleted` ([unified delete](../../superpowers/specs/2026-10-11-unified-task-delete.md)).
  This supersedes the 2026-09-08 design's refuse-unless-told branch policy and its
  asynchronous discard drain, which now serves only legacy `pending` rows
  (`src/integration/branch_discard.py`).

---

## 4. Boundaries and interactions

The inventory's other 17 systems, and where the line falls with each. "Owns" means the
other system decides; "reads/writes" means it consumes or produces graph facts.

| System | Interaction |
|---|---|
| **daemon-core** | Owns the schema, migrations and `run_one_cycle` ordering. The graph contributes steps to that cycle (gate sweep, cascade, container backstop, lifecycle sweep, layout step) but does not choose their order. |
| **scheduling-and-claims** | Owns the frontier *query*, pools and claims (`claim_queries.py`, `src/orchestrator/pools.py`). It reads `is_blocked`, hold labels, `held_batch_id` and the hierarchy predicates the graph defines. The planned single admission view (`stark-willow-95.13`) moves more of this into one shared object. |
| **routing-and-profiles** | Owns *who* runs a task (profile, intelligence class, route). The graph stores the routing fields and the `routing` gate type but never chooses. |
| **sessions-and-harnesses** | Owns the agent session. `aq prime` renders the task's description, deliverables and subtasks; the session's `aq task close` lands in this system's close gate. |
| **projects-workspaces-git** | Owns projects, repositories and worktrees. A task's `repo_id`, `branch_name` and workspace requirements are graph inputs (`repo_id` and `branch_name` feed the projection). |
| **integration-and-delivery** | Owns delivery truth: branch origins, checkpoints, trains, landing receipts, the `DeliveryObserver`. The graph reads it for `delivery-of` edges and the claim-time delivery fences, and calls into it for deletion. Hierarchical train projects restrict graph edits the train depends on. |
| **playbooks** | Policy that *drives* graph commands (spec ingestion, the object loop, triage). Playbooks create tasks, gates and labels through commands; formulas are the graph's own templates. |
| **messaging-and-human-asks** | A `human` gate is how a question to a person holds a task; resolving the ask resolves the gate. |
| **supervisors-and-policy** | Supervisors act on graph state — stale-open flags, `needs_attention`, obsolete/no-change/done closes, deletion — through the same commands. |
| **reviews-and-knowledge** | Document reviews satisfy `review` deliverables and `review` gates; `--after-review` gates implementation on a spec approval; the approved spec packet is the source of held ingestion. |
| **providers** | Provider outages surface as `provider_hold` in explain; they never change graph state. |
| **configuration-and-vault** | Supplies `work_graph.*`, `state_machine.enforce`, `graph_layout.*`; the vault holds formulas and task summary notes. |
| **api-and-clients** | Exposes graph reads and writes (`src/api/`, graph layout router) and generates the typed clients. |
| **dashboard** | Renders the persisted layout, readiness and activity; it never computes blockedness. |
| **test-service-and-jobs** | `test`/`command` deliverables are evidenced by the commands a close records; managed test jobs can back `ci-run`-style waits. |
| **operations-and-recovery** | Doctor checks and recovery commands repair stale containers, orphan gates and claims through graph commands. |
| **plugins-mcp-and-tools** | MCP tools are another front door to the same commands. |

## 5. Design principles and invariants

1. **Every write goes through `CommandHandler`.** CLI, MCP, API and Discord all call the
   same command, so validation, the projection recompute, events and layout dirtiness
   happen once.
2. **`is_blocked` is the sole readiness authority, and it is never authored.** It is
   recomputed in the writer's transaction to a fixpoint; nothing else re-derives edge
   semantics. Explain uses the same per-edge predicate.
3. **Graph blockedness is not capacity.** Only durable graph facts are persisted;
   per-tick reasons stay in explain.
4. **Status is lifecycle, holds are separate.** Holds (`hold:*` labels, ingestion hold,
   gates) withhold without rewriting status, so releasing a hold never has to guess
   what the status used to be.
5. **An expired gate still blocks.** Nothing self-approves on a timeout.
6. **A graph is created whole or not at all.** Graph documents, change sets and
   ingestion batches each commit in one transaction; dry runs execute the same path in a
   rolled-back savepoint.
7. **The `parent-child` edge is the truth; `parent_task_id` is a cache** with one
   writer. One parent per task, structural depth at most three, ids never reused.
8. **Containers are never leased; they settle.** Settlement is event-driven in the
   child's transaction, with a logged backstop.
9. **Blocking edges form a DAG.** Cycles over blocking kinds are refused at write time;
   informational kinds may form any shape.
10. **Deterministic, explainable judgement.** Validation rules, batch checks, cascade,
    settlement and layout use declared data and fixed budgets. No LLM call, no scoring of
    prose. Rule and reason codes are a public contract.
11. **A decision outranks the graph.** A task BLOCKED by a failure or operator decision is
    not promoted just because its edges are satisfied.

## 6. Known gaps

- **The state machine is warn-only by default.** `state_machine.enforce` is false
  (`src/config.py`, `StateMachineConfig`), so an invalid transition is logged and
  allowed. The `src/state_machine.py` module docstring still says it is not enforced at
  all, which is out of date in the other direction: `transition_task` does check, and
  raises when enforcement is on.
- **WAITING_INPUT has not collapsed.** The status collapse proposed in
  [work-graph design §12](../design/work-graph.md) is still future work; nine statuses
  remain.
- **Held ingestion has no release path.** Batches proposed by a `spec-ingest` session
  are stored `held`, but no command calls `ingest_batch_queries.release()`. Until the
  release epic lands (`sharp-cascade-53`), a held batch can be checked and reviewed but
  not released through a supported command. The batch editor can
  reshape a batch only while it is held; editing released work goes through the ordinary
  commands.
- **Two delivery truths for `blocks` edges.** The SQL projection treats a `blocks` edge
  as satisfied on COMPLETED, while development delivery is checked separately at claim
  time by the observer. A task can therefore be READY and unblocked yet not claimable
  (`waiting_on_delivery`). Explain and readiness report this, but it is a second place
  to look; `stark-willow-95.13` plans one admission view.
- **Historical design docs disagree with the code.** The work-graph design and
  implementation specs, the spatial layout design and the swarm work model are marked
  historical. Examples: the layout constants and budgets, the layout's default (the
  design says off, the code says on), and the gate types (`routing` and `review` were
  added later).
- **Layout dead weight.** `task_layout_cells` is written on every publish but tiles read
  the open set instead (`load_rows_in_cells` has no callers outside the query module);
  `flow.clamp_row_target` is tested but never called by the engine.
- **Two ways to write phase order.** A phase created by the `phase_create` command gets `blocks`
  edges only onto earlier phases that are not yet COMPLETED; phases created by a graph
  document get edges onto every earlier phase (`creator.py`). The result is the same
  readiness, but the stored edges differ.
- **Weak `flag` and `registration` deliverables.** They are satisfied by finding the
  target text anywhere in the repository (`_repo_text_contains` in `src/deliverables.py`),
  not by a structural check.
- **Stale secondary docs.** `docs/specs/design/formulas.md` omits phase merging and the
  object-loop guard; `docs/specs/plan-parser.md` describes a deleted module and says no
  planner profile ships (one does); the `task_proposals.payload` column comment lists
  only tasks and edges; the feature-pauses design still reads "Draft" though it is built.
  The 2026-09-08 deletion design's doctor check and dashboard prompt were never built
  (the unified delete replaced them).
- **Two depth constants.** `MAX_STRUCTURAL_DEPTH` is defined in both `src/task_names.py`
  and `src/database/hierarchy_migration.py`; they agree (3) but are not one symbol.
- **`task_commands.py` is very large** (about 6,700 lines) and mixes graph, archive,
  explain and listing concerns, which makes the graph's write surface hard to audit.

## 7. Planned work

Snapshot taken 2026-10-20 by a read-only query of the open `agent-queue` tasks and the
vault specs. Vault specs are not in this repository; their review id and state are
quoted as they stood that day.

### 7.1 Open tasks that change the task graph

| Task | State | What it changes |
|---|---|---|
| `stark-willow-95.13` — one admission view for frontier, pools, explain and delivery guards | READY | Replaces the separate structural SQL, routing/resource checks and delivery observation with one immutable `AdmissionView` shared by the scheduler, claims, pool demand, explain and the cleanup/archive guards. Missing observations fail closed for the affected candidates only; the SQL receipt fallback is retired. Touches `hierarchy_queries.py`, `claim_queries.py`, dependency readiness and task explain. |
| `stark-willow-95.14` — retained prerequisite stacks as the general default | DEFINED | Makes `prerequisite_branches: stacked` the default for every project (today only `agent-queue` gets it implicitly, `src/integration/stacked_branches.py`). A dependent may start over a *completed* prerequisite before its parent epic assembles; several prerequisites merge in topological order; reopening a prerequisite gives running consumers a stale-input blocker. Changes what "a `blocks` prerequisite is ready" means for claims. Depends on `.13`. |
| `stark-willow-95.10` — epic-level checks advisory by default | DEFINED | A red, missing or unavailable epic-level check stops delaying parent readiness and close unless the project requires it. The task text describes a `parent.required_checks.mode` field; the spec's later decision notes move the switch into project playbook data instead. |
| `stark-willow-95.15` — author-assisted fenced parent merge on close | DEFINED | Extends the passing `task_close` into a merge of the task's branch into its parent with the author still attached, with budgets as verified waits. Changes the close flow rather than the graph tables. |
| `sharp-cascade-53` — held ingestion P3: release and recovery | IN_PROGRESS (`.2` PAUSED) | The missing release command, recovery, doctor checks and a downgrade guard for held batches. |
| `nimble-lantern-37` — held ingestion P5: policy, profiles, pipeline, docs | IN_PROGRESS | The `spec-ingest` profile text and the `default-pipeline` rules that drive held ingestion, plus its documentation. |
| `calm-orbit-69` — held ingestion P6: API, dashboard, CLI | IN_PROGRESS | `aq batch …`, doctor checks, MCP tools and dashboard surfaces for held batches. |
| `bold-zenith-11` — event-driven hibernation and verified waits | IN_PROGRESS | Agent waits become verified conditions an agent can sleep on; overlaps with gates as the "what am I waiting for" vocabulary. Final cutover leaf `bold-zenith-11.22` is READY. |
| `eager-vault-55` — system map part 2 | IN_PROGRESS | This page and its 17 siblings; `eager-vault-55.19` then rewrites the vision spec from them. |

### 7.2 In-flight specs

| Spec | State on 2026-10-20 | What it changes |
|---|---|---|
| Spec ingestion with task-structure review before release (vault, `2026-10-13-spec-ingestion-with-task-structure-review-before-release.md`) | approved, `rev-bright-meadow` revision 2 | The design behind section 3.3.4. Also: held containers never settle; post-landing operational work becomes separate root tasks gated by `delivery-of`, never children of the delivery epic; post-release edits (root relocation, split, retire with replacement). |
| Its implementation spec (vault, `2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`) | approved, `rev-agile-beacon` revision 1 | Phases P0–P6. P0 (schema and hold predicate), P1 (batch creation and checks) and P2 (reviewer) are built; P3–P6 are the open epics above. |
| Fast smooth integration (vault, `2026-10-12-fast-smooth-integration.md`) | in review, `rev-fair-lantern` revision 3, with decision notes for revisions 2–4 | Source of `stark-willow-95`: one delivery contract, one admission view, retained stacks, advisory epic checks, author-assisted merge. |
| Event-driven hibernation and verified waits (vault, design `rev-bold-pinnacle` rev 2; implementation `rev-fleet-orbit` rev 1) | approved | Source of `bold-zenith-11`. |
| Task graph UI redesign 2 (vault, `2026-10-06-task-graph-ui-redesign-2.md`) | approved, `rev-bold-current` revision 1 | Card, edge and frame restyle; semantic zoom, hover card, focus path, phase rail, right rail and minimap; `LayoutNode` gains `needs_attention`; layout constants retuned with an `ENGINE_RULES_VERSION` bump. Partly built: design tokens, light theme, card stripes, edge dash-per-type and `started_at`/`finished_at` are in; zoom bands, rails, minimap, `needs_attention` and the constant retune are not. |
| Agent Queue system inventory and vision map (vault, `2026-10-20-…-system-inventory.md`, `…-the-vision-and-the-system-map.md`) | in review, `rev-bright-rapids` / `rev-smart-nexus` | The frame for this page; no graph behaviour change. |
| Roadmap small work (vault, `2026-09-21-roadmap-small-work-design.md`) | draft | Checklist and archive-runbook leftovers. |

Repository specs from 2026-09-15 onward that touch the graph — graph visibility and
subtasks, phases and subtasks in planning, layout reorganisation (with its 2026-10-11
correction), archive with integration history, lifecycle cleanup, epic delivery status,
unified delete — are implemented. They are listed in section 8 as background.

## 8. Further reading

Current behaviour:

- [Tasks, epics, dependencies and task graphs](../../concepts/tasks.md) — the concepts page; start here.
- [Scheduling](../../concepts/scheduling.md) and [worker pools](../../guides/worker-pools.md) — the frontier and claims.
- [Integration](../../concepts/integration.md) and [hierarchical integration trains](../../guides/hierarchical-integration-trains.md) — delivery fences.
- [Task change sets](../../guides/task-change-sets.md), [generated task templates](../../guides/generated-task-templates.md), [superseded completion](../../guides/superseded-completion.md).
- [Models and state machine](../models-and-state-machine.md), [database](../database.md), [plan parser](../plan-parser.md), [npm workspace test deliverables](../npm-workspace-test-deliverables.md).
- Module reference: [docs/reference/modules/tasks.md](../../reference/modules/tasks.md).

Design records (historical; the code wins where they disagree):

- [Work graph design](../design/work-graph.md) and [implementation](../implementation/work-graph.md) — edges, gates, labels, the projection, explain, hierarchy (§13), phases (§13b), subtasks (§13c), standing parents (§13d), approval ingestion (§15).
- [Swarm work model](../../superpowers/specs/2026-08-28-swarm-work-model-design.md) — hierarchy, containers, settlement, formulas.
- [Formulas](../design/formulas.md), [generated task templates](../design/generated-task-templates.md), [feature pauses](../design/feature-pauses.md), [task completion CI](../design/task-completion-ci.md), [review revision completion](../design/review-revision-completion.md).
- [Spatial layout](../../superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md), [layout reorganisation](../../superpowers/specs/2026-09-20-graph-layout-reorganisation-design.md).
- [Graph visibility and subtasks](../../superpowers/specs/2026-09-19-graph-visibility-markers-subtasks-implementation.md), [planning emits phases and subtasks](../../superpowers/specs/2026-09-20-planning-emits-phases-and-subtasks-design.md).
- [Archive with integration history](../../superpowers/specs/2026-09-20-archive-tasks-with-integration-history-design.md), [lifecycle cleanup](../../superpowers/specs/2026-09-26-lifecycle-cleanup-design.md), [epic delivery status](../../superpowers/specs/2026-10-02-epic-delivery-status-design.md), [unified delete](../../superpowers/specs/2026-10-11-unified-task-delete.md).
