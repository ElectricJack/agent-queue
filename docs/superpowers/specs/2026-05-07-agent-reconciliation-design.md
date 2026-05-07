---
tags: [design, scheduler, agents, workspaces]
date: 2026-05-07
status: Draft
related:
  - specs/scheduler-and-budget.md
  - specs/models-and-state-machine.md
  - specs/orchestrator.md
  - specs/design/agent-coordination.md
---

# Agent Reconciliation Design

## 1. Problem Statement

The recent workspace-as-agent rewrite (the merge that introduced
`WorkspaceAgent` in `models.py:347`) deprecated `aq agent create` with the
message *"agents are now derived from project workspaces"* — but never
replaced it with an automatic creation path. The result is a half-finished
migration:

- The `agents` table is the source of truth for the scheduler:
  `Scheduler.schedule()` filters `state.agents` for `AgentState.IDLE`
  rows (`scheduler.py:281-287`) and returns `[]` if none exist.
- No production code path calls `db.create_agent()`. The deprecation stub
  (`agent_commands.py:67-78`) returns an error; tests are the only
  remaining caller.
- `WorkspaceAgent`, the dataclass introduced as the new "view," is used
  only by `notifications/builder.py` for API-shape conversion — it has no
  effect on scheduling.

Consequence: any project gets stuck. Concrete trigger encountered while
debugging: project `atom-claude` had 4 enabled workspaces, a project
`default_profile_id`, and a READY task `quick-ember`. The scheduler logged
`scheduler blocked task=quick-ember reason=no idle agent on project
'atom-claude'` every tick because the `agents` table was empty and nothing
in the system would ever populate it.

## 2. Goal & Non-Goals

**Goal.** Restore automatic agent supply so READY tasks dispatch without
operator intervention, in a way that fits the architectural intent the
recent rewrite described but did not finish.

**In scope:**
- A reconciler that lazily creates agent rows when work needs them.
- A coherent meaning for "agent" that handles 0-workspace tasks
  (supervisor-style), 1-workspace tasks (the default), and the existing
  multi-workspace SYNC special case.
- Renaming `agents.agent_type` → `agents.profile_id` (the field has
  always *been* the profile id by string match in actual usage —
  see `runtimes/supervisor.py:516,527` and `vault_manager`'s profile
  directory layout). `_task_agent_type_matches()` is updated to
  compare on `agent.profile_id`.
- **Dropping the unused `tasks.agent_type` and `archived_tasks.agent_type`
  columns** along with `_task_agent_type_matches()`, the `agent_type`
  parameter on Discord/MCP task commands, and the related Task
  dataclass field. Empirical check (live DB + `git grep`) showed the
  coordination-category-filter feature these supported was specced in
  `docs/specs/design/agent-coordination.md` but never implemented; one
  task in the live DB has `agent_type=NULL` and zero archived tasks have
  it set. Tasks already have a separate, working `profile_id` column
  (FK to `agent_profiles.id`) — the reconciler reads that.
- Removal of the dead `aq agent create / edit / delete / pause / resume`
  CLI subcommands.

**Out of scope (follow-on work):**
- Shipping `claude-opus.md` and `claude-sonnet.md` profile templates as
  guaranteed defaults in the source `vault/agent-types/` directory.
- Auto-picking a project default profile in `create_project` (prefer
  `claude-opus`, fall back to `claude-sonnet`, then any non-supervisor).
- `/health` flipping `tasks.ok=false` when a task has been READY > 60s
  with no progress, and surfacing the blocker reason inline.
- Generalizing multi-workspace task requirements beyond the SYNC special
  case (e.g. an explicit `task.workspace_count` field). Out of scope here
  because nothing today needs it; SYNC handles its own case in
  `sync_workflow.py`.
- Fully eliminating the `agents` table (the more aggressive variant of
  finishing the workspace-as-agent migration). This design preserves the
  table; future work can revisit.

## 3. Architecture

The mental model after this change:

- An **agent** is a project execution slot. Sized by
  `project.max_concurrent_agents`. Has a current `profile_id`
  (mutable per task). Created lazily by the reconciler.
- A **workspace** is a lockable directory resource that tasks acquire.
  How many a task needs (0 / 1 / N) is determined by
  `task.profile_id → profile.runtime → runtime.requires_workspace`,
  with `TaskType.SYNC` continuing to be the only multi-workspace case
  (handled as today).
- The **scheduler** stays a pure function operating on a `SchedulerState`
  snapshot — no DB writes inside it. Its algorithm (deficit-based fair
  share, concurrency caps, workspace gate, SYNC exclusivity) is unchanged.
- The new **`AgentReconciler`** runs at the top of each orchestrator tick,
  before scheduling. It examines READY tasks per project and ensures the
  `agents` table contains enough idle rows (with the right `profile_id`s)
  for the scheduler to dispatch them, subject to
  `project.max_concurrent_agents`.
- `WorkspaceAgent` is kept and clarified as the API-shape for "an agent
  currently bound to a workspace" — a derived view used by the notification
  builder, not a persisted entity.

What's **not** changing: `Workspace` model and lock semantics
(`locked_by_agent_id` FK stays); `AgentState` enum and existing transition
sites in `execution.py`; `Scheduler.schedule()` algorithm; `TaskType.SYNC`
exclusivity handling; the supervisor singleton (lives outside the agents
table; not affected).

## 4. Components

### 4.1 New: `src/orchestrator/agent_reconciler.py`

```python
class AgentReconciler:
    """Lazy-creates agent rows so the scheduler always has idle slots
    when there's dispatchable work.

    Called once per orchestrator tick, before Scheduler.schedule().
    Does not assign tasks — only ensures supply matches demand subject
    to project.max_concurrent_agents.
    """
    def __init__(self, db: Database): ...

    async def reconcile(self) -> ReconcileReport:
        # For each project with READY tasks:
        #   1. Compute resolved profile_id per ready task
        #      (task.profile_id or project.default_profile_id).
        #   2. Compute current agent inventory (idle/busy by profile_id).
        #   3. Decide creates / reassignments per the rules below.
        #   4. Persist via db.create_agent / db.update_agent.
        ...
```

**Decision rules (in order, per project):**

1. Skip if no `default_profile_id` AND no task has an explicit
   `profile_id`. Log once per (project, reason) at WARN: *"atom-claude
   has READY tasks but no resolvable profile_id"*. Reuses the
   `_scheduler_blocker_reasons` dedup dict so it doesn't spam.
2. For each unique resolved `profile_id` needed by READY tasks in the
   project:
   - If an idle agent of that `profile_id` exists → nothing to do.
   - Else if `current_agent_count < max_concurrent_agents` → **create
     new agent** with that `profile_id`, `state=IDLE`,
     `current_task_id=NULL`.
   - Else if there is an idle agent with a different `profile_id` →
     **reassign** in place (`UPDATE agents SET profile_id=?`). This
     preserves the "workspace identity, dynamic profile" intent.
   - Else → leave it; scheduler will report blocked, next tick retries.
3. Workspace-requiring runtimes only count toward dispatchable work
   if the project has at least one available workspace (unlocked **and**
   `enabled=True` — same predicate the existing
   `db.count_available_workspaces` uses); otherwise the reconciler
   skips the create (we'd be making agents that can't dispatch). Use
   `runtime.requires_workspace` via the resolved profile.
4. Reassignment is capped at **1 per agent per tick** to prevent
   thrashing under high churn (three different-profile tasks arriving
   simultaneously with capacity=1 → first wins, others wait for next tick).

### 4.2 Modified files

- `src/orchestrator/core.py` — at the top of `_schedule()`, call
  `self._agent_reconciler.reconcile()` before building `SchedulerState`.
  Wire `AgentReconciler` instance in `__init__`. Extend the existing
  startup cleanup pass at `core.py:1349` to also handle:
  (a) idle agents over `max_concurrent_agents`, (b) idle agents
  referencing a deleted profile, (c) `state=BUSY AND current_task_id IS
  NULL` orphans (reset to IDLE).
- `src/scheduler.py` — no behavior change; consumes renamed
  `Agent.profile_id` / `Task.profile_id` fields.
- `src/models.py` — rename `Agent.agent_type` → `Agent.profile_id`.
  Delete `Task.agent_type` field. Remove the `Agent` deprecation
  comment (the table is no longer deprecated under this design).
  Update `WorkspaceAgent` docstring to say *"API view of an agent that
  currently holds a workspace lock — not a persisted entity."*
- `src/database/tables.py` — rename `agents.agent_type` →
  `agents.profile_id` (with a comment noting it's a soft reference to
  `agent_profiles.id`). Drop `tasks.agent_type` and
  `archived_tasks.agent_type` columns. Drop the `agent_type` field
  from any Discord/MCP tool schemas in `tools/definitions.py` that
  reference task `agent_type`.
- `src/database/queries/agent_queries.py`, `task_queries.py`,
  `archive_queries.py` — column references for the agents rename;
  remove `agent_type` from task/archived_task SELECT/INSERT and from
  the Task/ArchivedTask reconstruction code.
- `src/scheduler.py` — rename `agent.agent_type` → `agent.profile_id`
  in the matcher signatures; **delete `_task_agent_type_matches()`
  entirely** along with the call site at `scheduler.py:446`. Update
  `provider_cooldowns` keying to use `agent.profile_id` consistently.
- `src/orchestrator/execution.py` — `agent.agent_type` →
  `agent.profile_id` (~3 sites).
- `src/commands/task_commands.py` — remove the now-stale comment at
  `:867-868`; remove the `agent_type` parameter handling from create
  and edit task commands.
- `src/discord/commands.py:3308,3311` — remove the `agent_type`
  parameter from the `/edit` slash-command's signature and choices.
- `src/runtimes/supervisor.py:516,527` — `agent_type="supervisor"` →
  `profile_id="supervisor"`.
- All other call sites (`mcp_interfaces.py`, `workflow_pipeline_view.py`,
  `cli/formatters.py`, etc.) — targeted rename of agent.agent_type
  references only. Do **not** blanket-rename: `vault_manager.py`'s
  scope name `"agent_type"`, `override_handler.py`'s vault path
  segment, `Project.default_agent_type`, `rate_limits.agent_type`, and
  `ProjectConstraint.max_agents_by_type` are unrelated identifiers
  that share the substring and must be left alone.

### 4.3 New Alembic migration

`migrations/versions/2026_05_07_rename_agent_type_and_drop_task_agent_type.py`,
modeled on the existing
`2026_04_28_rename_platform_column_to_runtime.py` from the recent
runtime-rename merge — same shape (batch ALTER on SQLite, native
`ALTER TABLE RENAME COLUMN` on PostgreSQL). Three operations in one
revision: (1) rename `agents.agent_type` → `profile_id`,
(2) drop `tasks.agent_type` column, (3) drop
`archived_tasks.agent_type` column. All three idempotent (inspect
schema first; no-op if already in target shape). Note: `alembic heads`
must return a single revision before authoring `down_revision`; if
multiple heads exist (the repo has historical merge revisions like
`b5cc4799efad_merge_migration_heads`), depend on the latest single
head per `alembic heads --resolve-dependencies`.

### 4.4 Removed code

- `_cmd_create_agent`, `_cmd_edit_agent` and `_cmd_delete_agent` /
  `_cmd_pause_agent` / `_cmd_resume_agent` deprecation stubs in
  `src/commands/agent_commands.py`.
- The corresponding CLI subcommands (`aq agent create / edit / delete /
  pause / resume`) and their tests.
- The `Agent` deprecation comment block in `models.py`.

## 5. Data Flow

End-to-end trace of `quick-ember` (READY task in atom-claude with
4 workspaces, `default_profile_id='claude-opus'`, `max_concurrent_agents=2`,
0 agent rows) once this lands.

**Tick N (every ~5s):**

1. **Reconciler phase.** Loads projects, tasks, agents from DB. Resolves
   `quick-ember`'s profile to `claude-opus` (project default). Loads
   profile → runtime → `requires_workspace=True`. Project has available
   workspaces, so the agent is creatable. Current agent count (0) < cap
   (2), no idle agent of `claude-opus` → **creates agent row**:
   `id='agent-{uuid7}'`, `profile_id='claude-opus'`, `state=IDLE`,
   `current_task_id=NULL`. Returns
   `ReconcileReport(created=[(atom-claude, claude-opus)],
   reassigned=[], skipped=[])`.
2. **Scheduling phase.** `Scheduler.schedule()` builds `SchedulerState`
   from a fresh DB read (now sees the new agent). `idle_agents =
   [agent-{uuid7}]`. Emits `AssignAction(agent_id='agent-{uuid7}',
   task_id='quick-ember', workspace_id='ws-east-dome')`.
3. **Execution phase.** `execution._execute_task` (unchanged) acquires
   the workspace lock, sets agent BUSY, sets task IN_PROGRESS, spawns
   the runtime, runs to completion, releases the workspace lock,
   transitions agent back to IDLE.

**Tick N+1.** 1 idle agent exists, no new READY tasks → reconciler is
a no-op, scheduler is a no-op.

**Tick N+M (a new task arrives needing `claude-sonnet`):** Reconciler
sees a READY task with `profile_id='claude-sonnet'`. Inventory: 1 idle
opus agent, capacity 1/2. Under cap → **creates a second agent**
`profile_id='claude-sonnet'`. Scheduler dispatches the sonnet task.

**Tick N+M' (same scenario but capacity already at max):** 2 idle opus
agents, sonnet task arrives, cap=2. Create blocked. There IS an idle
agent of a different profile → **reassign** one
(`UPDATE agents SET profile_id='claude-sonnet'`). Scheduler dispatches
the sonnet task to the now-reassigned agent.

The misleading `"no idle agent"` log goes away naturally on the happy
path. Where a true blocker exists, the reconciler emits a more useful
message: `"reconciler: project=atom-claude needs profile=claude-opus
but cannot create — capacity full and no reassignable idle agent"`.

## 6. Error Handling and Edge Cases

### Profile lifecycle

- *Profile deleted while an idle agent references it.* `agents.profile_id`
  is a soft reference. The reconciler treats the orphan idle agent as
  **eligible for reassignment first** (preferred over an idle agent with
  a valid-but-mismatching profile). If no work is pending, the
  reconciler does **not** reap the orphan mid-run (reaping idle rows
  while there's no demand would just churn). It stays idle until the
  next daemon startup cleanup pass at `core.py:1349`, which is
  extended (per Section 4.2) to drop orphan-profile idle agents.
- *Profile deleted while an agent is BUSY.* Don't interfere with the
  in-flight task — the runtime is already loaded. The agent will go IDLE
  on completion, then fall under the rule above.
- *`task.profile_id` references a non-existent profile.* Resolved at
  reconcile time: log WARN, fall back to `project.default_profile_id`.
  If that's also missing or invalid, leave the task READY and emit the
  "no resolvable profile" log.

### Workspace lifecycle

- *Workspace deleted while its agent is BUSY.* The
  `workspaces.locked_by_agent_id` FK behavior (default `NO ACTION`)
  means the DB refuses the delete. Surface this in the
  `delete_workspace` command: *"workspace is locked by agent X running
  task Y; cancel the task first."*
- *Workspace becomes available/unavailable between reconcile and
  schedule.* Self-correcting — the scheduler always reads fresh state.
  Worst case: reconciler created an agent that can't dispatch this tick;
  next tick sees idle agent, doesn't create more, dispatch happens when
  workspace frees.

### Capacity changes

- *`max_concurrent_agents` lowered while existing agents exceed the new
  cap.* Reconciler stops creating new ones immediately. Excess idle
  agents get reaped by the extended startup-cleanup pass at
  `core.py:1349`. BUSY agents are left alone (let them complete the
  current task).
- *Reassignment thrashing.* Cap is 1 reassignment per agent per tick
  (Section 4.1, rule 4).

### Reconciler robustness

- *Partial failure mid-reconcile.* Each `db.create_agent` is its own
  transaction. If one fails, log it, continue with the rest, return a
  partial `ReconcileReport`. Next tick retries the failed creates.
  Idempotent because the reconciler always reads fresh inventory.
- *No profiles exist in the system at all.* Reconciler logs CRITICAL
  once per daemon startup, then no-ops every tick. The follow-on (ship
  opus+sonnet defaults) is what actually fixes this; in this design we
  fail loudly rather than silently.
- *Orphan BUSY agents (`state=BUSY AND current_task_id IS NULL`, or
  current task missing).* Reconciler resets to IDLE before counting
  capacity. This subsumes part of what `core.py:1349` does today and is
  also required mid-run, not just at startup.

### Concurrency

Reconciler runs in the same async task as `_schedule()` — no cross-tick
race. The hand-off between reconciler and scheduler within a single tick
is fine: they share a session, the orchestrator tick is single-threaded,
and the scheduler always re-reads agent state.

## 7. Testing Strategy

### New: `tests/test_agent_reconciler.py`

Unit tests for `AgentReconciler.reconcile()` in isolation, with a fake DB.

- *No-op*: empty projects, no tasks.
- *Happy path*: 1 project, 1 default profile, 1 ready task, capacity
  available → 1 agent created.
- *Profile-resolution failure*: ready task with no `task.profile_id` and
  project has no `default_profile_id` → no creation, "no resolvable
  profile" log emitted exactly once per project.
- *Multiple profiles, under cap*: ready tasks needing opus + sonnet,
  capacity 2/2 free → creates one per profile.
- *At-cap reassignment*: 2 idle opus agents, ready task needing sonnet,
  cap=2 → reassigns one in place.
- *Reassignment cap*: 3 different-profile tasks, capacity=1 →
  1 reassignment, other 2 stay blocked.
- *Workspace-required-but-none-available*: ready task,
  `runtime.requires_workspace=True`, 0 available workspaces → no creation.
- *No-workspace runtime*: ready task with supervisor-style profile
  (`requires_workspace=False`), 0 workspaces → still creates.
- *Orphan BUSY*: agent `state=BUSY` but `current_task_id` missing →
  reset to IDLE before counting capacity.
- *Orphan profile_id*: idle agent referencing a deleted profile →
  flagged as preferred-reassignment-target.

### Integration: `tests/test_orchestrator.py`

Add a regression test that would have caught the original bug:

```
test_ready_task_dispatches_with_only_workspace_and_default_profile
  given: project with 1 workspace + default_profile_id, task READY
  when:  orchestrator.run_one_cycle()
  then:  task.status == IN_PROGRESS, agents table has 1 BUSY row
```

Plus an end-to-end multi-tick test for profile reassignment.

### Migration: `tests/test_database.py`

Boot the daemon at the prior alembic revision with sample data:
an `agents` row with `agent_type='claude-opus'` and a `tasks` row with
`agent_type='coding'`. Run `alembic upgrade head`. Assert: (a) the
agent's value lives at `profile_id` afterwards, (b) the
`tasks.agent_type` column no longer exists, (c) the task row still
exists (column drop only loses the column's data, not the row).

### Updates to existing tests

- `tests/test_database.py:365,373,380,381` and
  `tests/test_database_postgresql.py:80` — `Agent(agent_type=...)` →
  `Agent(profile_id=...)`.
- `tests/test_notifications.py` — `WorkspaceAgent` keeps its fields
  (it's the API shape, not the persisted shape), no rename.
- Any test that constructs `Task(agent_type=...)` — drop the kwarg
  (the field is gone). Any test that asserts on `_task_agent_type_matches`
  behavior — delete (the function is gone).
- Any test that exercises `aq agent create` directly — **delete** (the
  CLI command is being removed).

### Test scaffold

Add `make_reconciled_state(db, ...)` helper that runs the reconciler
then builds `SchedulerState`, so existing scheduler tests don't have to
manually populate the `agents` table — they declare "this project has 4
workspaces and 2 ready tasks" and get a populated state for free. This
quietly catches reconciler regressions whenever scheduler tests run.

## 8. Migration and Rollout

**Single PR, ordered commits** so each commit is independently reviewable
and the test suite passes at each step:

1. **Schema changes** — rename `agents.agent_type` → `profile_id`;
   drop `tasks.agent_type` and `archived_tasks.agent_type` columns;
   delete `_task_agent_type_matches()` and the related Task field /
   Discord-MCP `agent_type` parameters. New Alembic revision;
   targeted (not blanket) updates to `tables.py`, models, queries,
   scheduler, and all read/write sites. Behavior change is limited to:
   (a) tasks no longer have a category-filter field, and (b) the
   `_task_agent_type_matches` no-op disappears. The reconciler is not
   yet wired in, so dispatch behavior remains broken until step 3 —
   that's intentional, keeping the schema/code-shape change isolated.
2. **Add `AgentReconciler`** + unit tests. Not yet wired in. Test suite
   passes.
3. **Wire the reconciler** into the orchestrator tick. Add the
   regression integration test. Extend the startup cleanup pass at
   `core.py:1349`.
4. **Remove dead code**: deprecation stubs in `agent_commands.py`,
   the `aq agent create / edit / delete / pause / resume` CLI groups,
   their tests. Update CLI help.
5. **Update `Agent` deprecation comment** to reflect the new role,
   clarify `WorkspaceAgent` as the API view shape.

### Backward compatibility

- *Existing data.* The migration preserves all `agents` and `projects`
  data; the `tasks.agent_type` and `archived_tasks.agent_type` columns
  are dropped (live DB has 1 task with NULL and 0 archived rows with a
  value, so no data is lost in practice).
- *External tools.* Anything querying the DB directly for
  `agents.agent_type` or `tasks.agent_type` will break. Surface in PR
  description and `CHANGELOG.md`. The typed `aq-client` SDK regenerates
  from the OpenAPI spec, so dashboard and CLI users get the field
  changes for free.
- *External MCP / Discord callers.* Any caller that passed
  `agent_type=` when creating or editing a task will get a "no such
  parameter" rejection. Treat this as a breaking change in the PR
  description; the parameter wasn't doing anything functional anyway.
- *In-flight tasks at upgrade time.* Migration is atomic; agents go
  IDLE on completion and read the renamed column with no interruption.

### Rollout flag

None. The reconciler is purely additive (creates rows that never
previously existed; doesn't disable any existing behavior). The rename
is a one-shot Alembic migration; reverting the PR plus running
`alembic downgrade -1` is the rollback path. No need for a feature flag.

### Operator-visible changes

- New INFO log per reconciler create/reassign:
  `reconciler: project=atom-claude created agent agent-{uuid7}
  profile_id=claude-opus`.
- New WARN log on profile-resolution failure:
  `reconciler: project=atom-claude has READY tasks but no resolvable
  profile_id (task X has no profile_id and project has no
  default_profile_id)`. Deduped per (project, reason).
- `aq agent list` continues to work and shows the auto-created agents.
  The deprecated subcommands disappear from `--help`.

### Doc updates

- `docs/specs/scheduler-and-budget.md` — add a paragraph describing the
  reconciler step.
- `docs/specs/models-and-state-machine.md` — reflect the rename and
  clarify the workspace-vs-agent relationship.
- `docs/specs/design/agent-coordination.md` — note the model is now
  *workspace-as-resource, agent-as-project-slot*; the reconciler
  ensures supply.

## 9. Out of Scope / Follow-ons

The original investigation surfaced two adjacent problems beyond agent
reconciliation. They are intentionally **not** addressed here so this PR
stays focused; they should each be their own design + plan after this
ships.

1. **Ship default profiles.** Bake `claude-opus.md` and
   `claude-sonnet.md` into the source `vault/agent-types/` and have the
   setup wizard copy them into `~/.agent-queue/vault/` if absent. Have
   `create_project` auto-pick `claude-opus` if present, else
   `claude-sonnet`, else first non-supervisor profile, else error.
2. **Surface stuck-ready tasks in `/health`.** Today
   `/health` returned `tasks: {ok: true, ready: 1}` while the queue
   was permanently stalled. Flip `tasks.ok=false` when a task has been
   READY > 60s with no progress; surface the blocker reason inline.
   This converts the silent-failure mode this design is fixing into a
   loud failure if some new variant slips through.
