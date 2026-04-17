---
tags: [spec, orchestrator, core]
---

# Orchestrator Specification

This document describes the design and behaviour of the orchestrator subsystem in sufficient
detail to reimplement it from scratch.  It covers the event bus, task-name generation,
orchestrator initialisation, the scheduling cycle, every major internal operation, and the
external callback hooks that wire it to Discord.

---

## 1. Overview

The `Orchestrator` class is the central brain of the system.  Its responsibilities are:

- Maintaining the authoritative state of every task and agent via a SQLite database.
- Running a repeating scheduling cycle (typically every ~5 seconds from the outer loop)
  that drives the complete task lifecycle from DEFINED through to COMPLETED.
- Delegating actual agent execution to pluggable adapter objects, while managing
  workspace preparation, result handling, and post-completion git operations itself.
- Notifying operators through Discord via injected callbacks.

**Deterministic orchestration principle.**  The orchestrator makes zero LLM calls for
scheduling or coordination.  All promotion, assignment, and retry decisions are rule-based
and derive purely from database state.  LLM calls occur only inside agent adapters (doing
real work) and, optionally, inside the plan parser when `use_llm_parser` is enabled.

See [[design/playbooks]] and [[design/agent-coordination]] for the extended orchestration model.

**Concurrency model.**  Everything runs inside a single asyncio event loop.  Each executing
task is launched as an `asyncio.Task` background coroutine.  The orchestrator keeps a
`_running_tasks` dict mapping `task_id -> asyncio.Task` so it can detect completion and
avoid double-launching.  There are no threads and no multiprocessing.

---

## Source Files

- `src/orchestrator.py`
- `src/event_bus.py`
- `src/task_names.py`

---

## 2. Event Bus

### Purpose

`EventBus` is a lightweight in-process pub/sub mechanism used by the hook engine and any
other component that needs to react to lifecycle events without a direct dependency on the
emitting component.

### Internal structure

```
_handlers: dict[str, list[Callable]]   # event_type -> ordered list of handlers
```

The dict is a `defaultdict(list)` so subscribing to a new event type never requires
pre-registration.

### Subscribing

```python
bus.subscribe(event_type: str, handler: Callable) -> None
```

`handler` is appended to the list for `event_type`.  Multiple handlers for the same event
are called in subscription order.  A single callable may be subscribed to multiple event
types by calling `subscribe` once per type.

### Wildcard subscription

Subscribing with the string `"*"` registers a catch-all handler.  On every `emit` call,
after the specific-type handlers are collected, the list at key `"*"` is appended.  A
wildcard handler therefore receives every event regardless of type.

### Emitting

```python
await bus.emit(event_type: str, data: dict | None = None) -> None
```

1. `data` is replaced via `data = data or {}`, so `None` or any other falsy value becomes
   an empty dict `{}`.
2. The key `"_event_type"` is injected into `data` so handlers can inspect the type even
   when they are registered as wildcard subscribers.
3. The specific-type handler list is snapshotted with `list(self._handlers.get(event_type, []))`;
   the wildcard list (`self._handlers.get("*", [])`) is then appended to that snapshot.
   Iterating over this pre-built combined list means a handler modifying subscriptions
   mid-emit cannot affect the current pass.
4. Each handler is called in order.  If `inspect.iscoroutinefunction(handler)` is true the
   handler is `await`-ed; otherwise it is called synchronously.  There is no timeout or
   exception isolation — a crashing handler will propagate to the caller.

### Where it is used

The `EventBus` instance lives on `Orchestrator.bus`.  The `HookEngine` receives a
reference to it during `initialize()` and subscribes its own handlers to task lifecycle
events.

---

## 3. Task Name Generation

### Purpose

Tasks are identified by human-readable IDs of the form `adjective-noun` (e.g.
`swift-falcon`, `bold-harbor`).  These IDs are used everywhere: database primary keys,
Discord messages, CLI arguments.

### Word lists

Two fixed lists are defined at module level:

- `ADJECTIVES`: 28 words (swift, bright, calm, bold, keen, wise, fair, sharp, clear,
  eager, fresh, grand, prime, quick, smart, sound, solid, stark, steady, noble, crisp,
  fleet, nimble, brisk, vivid, agile, amber, azure).
- `NOUNS`: 32 words (falcon, horizon, cascade, ember, summit, ridge, beacon, current,
  delta, forge, glacier, harbor, impact, journey, lantern, meadow, nexus, orbit, pinnacle,
  quest, rapids, stone, torrent, vault, willow, zenith, apex, bridge, crest, dune, flare,
  grove).

This gives 28 × 32 = 896 base combinations.

### Algorithm

```python
async def generate_task_id(db) -> str
```

1. Attempt up to `_MAX_RETRIES` (10) times:
   a. Pick a random adjective and a random noun, join with a hyphen.
   b. Call `db.get_task(name)` — an async database lookup.
   c. If the result is `None` (no collision), return the name immediately.

2. If all 10 attempts collide (extremely unlikely), enter an infinite fallback loop:
   a. Construct `adjective-noun-NN` where `NN` is a random integer in [10, 99].
   b. Check for collision as above.
   c. Return on first non-collision.

The fallback loop is guaranteed to terminate because there are 896 × 90 = 80,640
suffixed combinations.

---

## 4. Initialization

### `Orchestrator.__init__`

The constructor creates all sub-objects but performs no I/O:

| Field | Type | Purpose |
|---|---|---|
| `config` | `AppConfig` | Full application config |
| `db` | `Database` | SQLite persistence layer |
| `bus` | `EventBus` | In-process pub/sub |
| `budget` | `BudgetManager` | Global daily token budget |
| `git` | `GitManager` | Git operations wrapper |
| `_adapter_factory` | optional | Factory for creating agent adapters |
| `_adapters` | `dict[str, adapter]` | `agent_id -> running adapter` |
| `_running_tasks` | `dict[str, asyncio.Task]` | `task_id -> background coroutine` |
| `_notify` | `NotifyCallback \| None` | Discord notification callback |
| `_create_thread` | `CreateThreadCallback \| None` | Discord thread creation callback |
| `_paused` | `bool` | Global scheduling pause flag |
| `_last_approval_check` | `float` | Unix timestamp of last approval poll |
| `_chat_provider` | optional | LLM provider for plan parsing |
| `_no_pr_reminded_at` | `dict[str, float]` | Rate-limit tracker for no-PR reminders |
| `_stuck_notified_at` | `dict[str, float]` | Rate-limit tracker for stuck-DEFINED alerts |
| `hooks` | `HookEngine \| None` | Hook subsystem |

If `config.auto_task.use_llm_parser` is true, the constructor attempts to instantiate a
`ChatProvider` via `create_chat_provider(config.chat_provider)`.  Failure is silently
swallowed — the system falls back to regex parsing.

### `async initialize()`

Called once before the scheduling loop starts:

1. `await self.db.initialize()` — opens the SQLite connection, runs migrations.
2. `await self._recover_stale_state()` — repairs in-flight state from a previous run
   (see section 4a below).
3. `_sync_profiles_from_config()` — syncs YAML agent profiles from config into the database.
4. If `config.hook_engine.enabled` is true:
   - Instantiate `HookEngine(db, bus, config)`.
   - Call `hooks.set_orchestrator(self)`.
   - `await hooks.initialize()`.
5. Initialize `RuleManager` with `install_defaults()` (note: `reconcile()` runs later
   in `on_ready`, after the supervisor is available).
6. Start `ConfigWatcher` for configuration hot-reloading.

### 4a. Stale state recovery (`_recover_stale_state`)

After a daemon restart, no real agents are running.  Any database records that say
otherwise must be cleaned up:

1. List all agents.  For each agent whose state is `BUSY`:
   - Log a recovery message to stdout.
   - Call `db.update_agent(id, state=IDLE, current_task_id=None)`.

2. Release all workspace locks and clean orphaned sentinel files.

3. List all tasks with status `IN_PROGRESS`.  For each:
   - Log a recovery message to stdout.
   - Call `db.transition_task(id, READY, context="recovery", assigned_agent_id=None)`.

This ensures that tasks which were interrupted mid-run are re-queued from READY rather
than left stuck in IN_PROGRESS forever.

---

## 5. Orchestration Cycle

`run_one_cycle()` is the top-level method called by the outer loop on every tick.  It
executes the following steps in strict order, wrapped in a single broad `try/except` that
logs unexpected errors with a full traceback but does not crash the loop.

```
Step 0   _check_awaiting_approval       — poll PR merge status (rate-limited to 60s)
Step 1   _resume_paused_tasks           — promote PAUSED tasks whose resume_after has elapsed
Step 2   _check_defined_tasks           — promote DEFINED tasks whose deps are all COMPLETED
Step 2b  _check_stuck_defined_tasks     — alert on DEFINED tasks stuck beyond threshold
Step 3   _schedule                      — ask Scheduler for assignment actions (skipped if paused)
Step 4   Launch background executions   — start new asyncio.Tasks for each AssignAction
Step 5   hooks.tick()                   — run hook engine tick (if enabled)
Step 6   LLM log cleanup / analytics    — clean old log files, flush analytics
Step 7   Auto-archive terminal tasks    — archive old completed/failed tasks
Step 8   Periodic memory compaction     — compact project memory indexes
```

**Pause behaviour.**  When `self._paused` is true, step 3 is skipped and `actions` is set
to an empty list.  All other steps (approval checks, promotion, etc.) continue running
because those represent state maintenance, not new work assignment.

**Background task cleanup.**  At the start of step 4, the `_running_tasks` dict is
scanned for entries whose `asyncio.Task.done()` returns true; those entries are removed.
This prevents unbounded growth of the dict.

**Double-launch guard.**  Before launching a new `asyncio.Task` for an `AssignAction`,
the orchestrator checks whether `action.task_id` is already in `_running_tasks`.  If it
is, the action is silently skipped.

---

## 6. Task Promotion (DEFINED -> READY)

### `_check_defined_tasks`

Runs every cycle.  For each task currently in status `DEFINED`:

1. Fetch the task's declared dependencies via `db.get_dependencies(task.id)`.
2. If the dependency list is empty: call `db.transition_task(id, READY, context="deps_met_no_deps")`.
3. If the dependency list is non-empty: call `db.are_dependencies_met(task.id)`.
   - This returns `True` only when every upstream task has status `COMPLETED`.
   - If met: call `db.transition_task(id, READY, context="deps_met")`.

**Plan subtask special handling:** When a plan is approved, the parent task
transitions to `IN_PROGRESS` (not `COMPLETED`).  Plan subtasks whose parent
is `IN_PROGRESS` treat the parent dependency as satisfied — only non-parent
dependencies must be `COMPLETED` for promotion.

Tasks are promoted on the same cycle they become eligible.  There is no one-cycle delay
(the re-check at the end of plan generation, step 4 of task execution, explicitly calls
`_check_defined_tasks` again for freshly created subtasks).

### `_check_plan_parent_completion`

Runs every cycle after `_check_defined_tasks`.  Scans all `IN_PROGRESS` tasks
that have subtasks (plan parents).  When all subtasks have reached `COMPLETED`,
the parent is auto-transitioned to `COMPLETED` with context `"subtasks_completed"`.
This ensures plan tasks only show as complete when all their work is truly done.

---

## 7. Stuck Task Detection

### `_check_stuck_defined_tasks`

Runs every cycle after `_check_plan_parent_completion`.

**Configuration.**  `config.monitoring.stuck_task_threshold_seconds` controls the
threshold.  A value of `<= 0` disables this feature entirely.

**Process:**

1. Call `db.get_stuck_defined_tasks(threshold)` which returns all DEFINED tasks whose age
   exceeds the threshold.
2. If the result is empty, return immediately.
3. Clean up `_stuck_notified_at` by removing entries for task IDs that are no longer in
   the stuck list (they have since been promoted or deleted).
4. For each stuck task, apply a per-task rate limit: skip if `now - last_notified < threshold`.
5. For tasks that pass the rate limit:
   a. Call `db.get_blocking_dependencies(task.id)` to get a list of `(dep_id, dep_title, dep_status)` tuples.
   b. Call `db.get_task_created_at(task.id)` to compute `stuck_hours`.
   c. Format the notification with `format_stuck_defined_task(task, blocking, stuck_hours)` and send via `_notify_channel`.
   d. Log a `"stuck_defined_task"` event in the database with `stuck_hours` and the IDs of up to 10 blocking deps.
   e. Print a summary line to stdout.
   f. Update `_stuck_notified_at[task.id] = now`.

### Downstream chain sticking (`_notify_stuck_chain` / `_find_stuck_downstream`)

Called from `stop_task`, task timeout handling, PR-closed handling, and FAILED-past-max-retries
handling.

`_find_stuck_downstream(blocked_task_id)` performs a breadth-first traversal of the
forward dependency graph:

1. Start a queue with `[blocked_task_id]`.
2. For each dequeued ID, call `db.get_dependents(id)` to get the direct downstream tasks.
3. For each downstream task whose status is `DEFINED`, append it to `stuck` and enqueue it
   for further traversal.  Tasks in any other status are ignored (they have already escaped
   the dependency gate).
4. A `visited` set prevents infinite loops in cyclic graphs.
5. Returns the full list of transitively stuck DEFINED tasks.

`_notify_stuck_chain(blocked_task)` calls `_find_stuck_downstream`, and if the result is
non-empty formats and sends a `format_chain_stuck` notification and logs a `"chain_stuck"`
event.

---

## 8. Scheduling Integration

### `_schedule` -> `list[AssignAction]`

Collects all state needed by `Scheduler.schedule` and delegates to it:

1. `db.list_projects()`, `db.list_tasks()`, `db.list_agents()` — full snapshots.
2. For each project, compute token usage in the rolling window:
   `window_start = now - (config.scheduling.rolling_window_hours * 3600)`
   `project_usage[p.id] = db.get_project_token_usage(p.id, since=window_start)`
3. Count active agents per project by iterating agents whose state is `BUSY` or `STARTING`
   and looking up their `current_task_id`.
4. Sum all per-project usage to get `total_used`.
5. Build a `SchedulerState` dataclass and call `Scheduler.schedule(state)`.

### `Scheduler.schedule` logic (summary)

- Immediately returns `[]` if the global daily budget is set and already exhausted.
- Finds idle agents.
- Groups READY tasks by project; sorts within each group by `(priority asc, id asc)`.
- Filters to active projects that have at least one READY task.
- For each idle agent, picks the project with the highest scheduling priority using a
  two-component sort key:
  1. Whether the project has received a minimum-task guarantee (projects with zero
     completions in the window are sorted first).
  2. Token-usage deficit: `(actual_token_ratio - target_token_ratio)` — lower deficit
     (more underfunded) sorts first.
- Within the chosen project, picks the first (highest priority) available READY task that
  hasn't already been assigned in this round.
- Skips projects that have hit their per-project `budget_limit` or their
  `max_concurrent_agents` limit.
- Returns a `list[AssignAction(agent_id, task_id, project_id)]`.

---

## 9. Task Execution

Task execution is driven by `_execute_task(action: AssignAction)`, wrapped by
`_execute_task_safe` which applies an overall timeout and catches unexpected exceptions.

### 9a. `_execute_task_safe`

If `config.agents_config.stuck_timeout_seconds > 0`, wraps `_execute_task` in
`asyncio.wait_for(...)`.  On `TimeoutError`:

1. Stop the adapter via `adapter.stop()` (best-effort).
2. Transition task to `BLOCKED` with `context="timeout"`.
3. Set agent to `IDLE`.
4. Remove the adapter from `_adapters`.
5. Notify the channel.
6. Call `_notify_stuck_chain` for the now-blocked task.

On any other unexpected exception:

1. Transition task back to `READY` (so it will be retried next cycle) — best-effort,
   errors ignored.
2. Set agent to `IDLE` — best-effort, errors ignored.
3. Notify the channel with the error.

In both cases, remove the task from `_running_tasks` in a `finally` block.

### 9b. `_execute_task` — step by step

**Precondition check.**  If `_adapter_factory` is `None`, notify and return immediately.

**Step 1 — Assign.**
`db.assign_task_to_agent(task_id, agent_id)` — records the assignment in the database.

**Step 2 — Mark IN_PROGRESS.**
`db.transition_task(task_id, IN_PROGRESS, context="agent_started")`
`db.update_agent(agent_id, state=BUSY)`

**Step 3 — Fetch current records.**
`task = db.get_task(task_id)`, `agent = db.get_agent(agent_id)`.

**Step 3½ — Sync workflow interception.**
If `task.task_type == TaskType.SYNC`, delegate to `_execute_sync_workflow(action, task, agent)`
and return immediately.  See §9c for the sync workflow specification.

**Step 4 — Prepare workspace.**
`project = db.get_project(project_id)`.
Call `_prepare_workspace(task, agent)` inside a try/except.  `_prepare_workspace` returns
a path or `None`.  On exception or `None` return, transition the task to PAUSED with a
60-second `resume_after` backoff (prevents infinite assign-fail-READY-assign loops),
set the agent to IDLE, send a notification telling the user to add workspaces, and return
early.  Re-fetch `task` and `agent` after workspace preparation because
`_prepare_workspace` may have updated `branch_name`.

**Step 5 — Notify start.**
Send a "Task Started" message to `_notify_channel` including the task ID, title, agent
name, and (if set) the branch name.

**Step 6 — Create Discord thread.**
If `_create_thread` callback is set, call it with `(thread_name, start_msg, project_id)`.
- `thread_name` is `"{task.id} | {task.title}"` truncated to 100 characters.
- Returns a tuple `(send_to_thread, notify_main)` or `None` on failure.
- `thread_send` — callable that streams content into the thread.
- `thread_main_notify` — callable that posts a brief reply to the thread-root in the
  notifications channel.

**Step 7 — Create adapter.**
`adapter = _adapter_factory.create("claude")`
Store in `_adapters[agent_id]`.

**Step 8 — Build system context.**
Construct a multi-line string injected ahead of the task description:

```
## System Context
- Workspace directory: {workspace}
- Global workspaces root: {config.workspace_dir}
- Project: {project.name} (id: {project.id})
- Git branch: {task.branch_name}   (if set)

## Important: Execution Rules
...

## Important: Committing Your Work
...
```

For plan subtasks (`task.is_plan_subtask = True`) the execution rules:
- Forbid plan mode (`EnterPlanMode`) and writing plan files.
- Forbid pushing (the system handles pushing and PR creation).
- Require the agent to `git add` and `git commit` its changes when done.

For root tasks, the execution rules:
- Also forbid plan mode and pushing.
- Also require committing when done.
- Additionally instruct the agent that *if* the task is to produce an implementation plan,
  it must write the plan to `.claude/plan.md` or `plan.md` in the workspace root (not any
  other path), using `## Section` headings for each step.

The full task description is appended as `## Task\n{task.description}`.

### Task Context Assembly

Task execution context is assembled using `PromptBuilder` (see [[specs/prompt-builder]]).
The orchestrator calls `_build_task_context_with_prompt_builder()` which uses PromptBuilder
to compose system metadata, execution rules, upstream dependency summaries, agent role
instructions, and the task description into a single prompt string.

**Step 9 — Start adapter.**
Build `TaskContext(description=full_description, checkout_path=workspace, branch_name=...)`.
`await adapter.start(ctx)`.

**Step 10 — Define message forwarder.**
```python
async def forward_agent_message(text: str) -> None
```
If `thread_send` is available, forward to the thread.  Otherwise prepend
`` `{task.id}` | **{agent.name}**\n `` and send to `_notify_channel`.

**Step 11 — Rate-limit retry loop.**
Enter a `while True` loop:
1. `output = await adapter.wait(on_message=forward_agent_message)` — blocks until the
   agent produces a result.
2. If `output.result != PAUSED_RATE_LIMIT`: break.
3. Increment `_rl_attempt`.  If `_rl_attempt > _rl_max_retries` (from config): break.
4. Compute exponential backoff: `min(base * 2^(attempt-1), max_backoff)`.
5. Notify "rate-limited, retrying in Ns".
6. `asyncio.sleep(backoff)`.
7. Notify "rate limit cleared, resuming".
8. Re-`await adapter.start(ctx)` to reinitialise the adapter.
9. Loop again.

Configuration values:
- `config.pause_retry.rate_limit_backoff_seconds` — base backoff (doubles each attempt)
- `config.pause_retry.rate_limit_max_backoff_seconds` — cap
- `config.pause_retry.rate_limit_max_retries` — maximum retries before giving up

**Step 12 — Record tokens.**
If `output.tokens_used > 0`: `db.record_token_usage(project_id, agent_id, task_id, tokens)`.

**Step 13 — Persist task result.**
`db.save_task_result(task_id, agent_id, output)` (best-effort, errors logged).

**Step 14 — Re-fetch task** (retry_count may have changed in the DB).

**Step 15 — Handle result.**

*`COMPLETED`:*
- Run the three-phase `_run_completion_pipeline(ctx)` which executes:
  1. `_phase_commit` — commit agent changes to git
  2. `_phase_plan_discover` — delegate to Supervisor for plan file discovery; if plan
     found, transition to `AWAITING_PLAN_APPROVAL` and return early
  3. `_phase_merge` — merge/push or create PR based on configuration; on merge
     success transitions to `COMPLETED`, on PR creation transitions to
     `AWAITING_APPROVAL`, on merge failure transitions to `BLOCKED`
- The pipeline handles PR creation, approval transitions, and completion notifications.
- Post full completion summary to thread (or `_notify_channel`); post brief to main.

> **Note:** `_complete_workspace` still exists in the code but is dead code — it is
> never called from `_execute_task`. The active code path is `_run_completion_pipeline`.

*`FAILED`:*
- Increment `retry_count`.
- If `retry_count >= max_retries`: transition to `BLOCKED` (`context="max_retries"`);
  call `_notify_stuck_chain(task)`.
- Otherwise: transition back to `READY` (`context="retry"`, incremented `retry_count`).
- Post failure details to thread (or `_notify_channel`) and a brief to main channel.

*`PAUSED_TOKENS` or `PAUSED_RATE_LIMIT`* (after rate-limit auto-retries are exhausted):
- Compute `retry_secs`:
  - `PAUSED_RATE_LIMIT` → `config.pause_retry.rate_limit_backoff_seconds`
  - `PAUSED_TOKENS` → `config.pause_retry.token_exhaustion_retry_seconds`
- Transition to `PAUSED` (`context="tokens_exhausted"`, `resume_after=now+retry_secs`).
- Post "Task Paused" notice with the reason and retry delay.

**Step 16 — Free agent.**
`db.update_agent(agent_id, state=IDLE, current_task_id=None)`.

### 9c. `_execute_sync_workflow` — Orchestrator-Managed Sync

Tasks with `task_type=SYNC` bypass normal agent execution.  Instead, `_execute_task`
delegates to `_execute_sync_workflow(action, task, agent)` which coordinates a
multi-phase workflow entirely within the orchestrator.

**Phase 1 — Pause the project.**
Set `project.status = PAUSED` via `db.update_project` to prevent the scheduler from
assigning new tasks to this project.

**Phase 2 — Wait for active tasks.**
Poll `db.list_active_tasks` (excluding `COMPLETED`, `FAILED`, `BLOCKED`) every 10 seconds,
filtering out the sync task itself.  Wait up to 3 600 seconds (1 hour).  If the timeout
expires, transition the sync task to `FAILED` with `context="sync_timeout_waiting_for_tasks"`
and return.  Progress is reported to the notification channel every 60 seconds.

**Early-out: workspaces already synced.**
After active tasks have drained, re-check whether any workspace actually needs merging.
For each workspace, inspect `git.aget_current_branch` and `git.alist_branches`.  If
every workspace is already on the default branch with no feature branches:

- Transition the sync task to `COMPLETED` (`context="sync_already_synced"`).
- Notify the channel that no merge was needed.
- Return early — the `finally` block still handles project resume.

If a workspace directory is missing, it is skipped.  If a git check raises an exception,
the workflow assumes a merge is needed (errs on the side of proceeding).

**Phase 3 — Merge feature branches.**
Acquire a workspace via `_prepare_workspace`.  If that fails, fall back to the first
workspace's path; if no workspaces exist, fail the task.  Build a detailed merge
description instructing the Claude Code agent to merge all feature branches into the
default branch one workspace at a time.  Launch an adapter, stream output to a Discord
thread, and record token usage.

**Phase 4 — Cleanup & resume.**
Executed in a `finally` block so it runs regardless of success or failure:

- Release all project workspace locks via `db.release_workspace`.
- Resume the project via `db.update_project(status=ACTIVE)`.
- If the task is still `IN_PROGRESS`, transition to `COMPLETED` — either with
  `context="sync_completed"` (merge succeeded) or `context="sync_completed_with_warnings"`
  (merge agent did not return `COMPLETED`).
- Notify the channel with a summary.
- Free the agent: set to `IDLE` (or preserve `PAUSED` if the agent was already paused).
- Remove from `_adapters`.

---

## 10. Workspace Preparation

### Design Invariants

The workspace sync workflow preserves these invariants across all code paths.
See [[specs/git]] §10 for the full design principles reference.

| Invariant | Guarantee |
|---|---|
| **Per-agent isolation** | Each `(agent, project)` pair gets its own filesystem directory; concurrent agents never share a working tree. |
| **Branch-per-task** | Every task gets a unique `<task-id>/<slug>` branch. Subtasks accumulate on the parent's branch. |
| **Fresh starting point** | `prepare_for_task` always fetches from origin before creating a task branch, so agents start from recent code. |
| **Atomic commit** | `commit_all` stages everything then checks the staging area, avoiding race conditions. Agent work is never silently lost. |
| **Hard failure on git errors** | Git errors during workspace setup cause the workspace lock to be released and `None` to be returned; the task is paused with backoff rather than proceeding without branch management. |
| **Retry resilience** | Existing branches are reused on task retry rather than causing errors. |

### Resolved Gaps

Most previously identified workspace sync gaps have been resolved. See
[[specs/git]] §11 for the full gap catalogue.

| Gap | Location in this spec | Resolution |
|-----|----------------------|------------|
| **G1** | §11 `_merge_and_push` | `sync_and_merge` fetches and hard-resets before merging. |
| **G2** | §11 `_merge_and_push` | `recover_workspace` resets local default branch after failed sync_and_merge. |
| **G3** | §11 `_merge_and_push` | `sync_and_merge` attempts rebase-before-merge on conflict. |
| **G4** | §10 `_prepare_workspace` | `prepare_for_task` rebases existing branches on retry. |
| **G5** | §11 `_create_pr_for_task` | `push_branch(force_with_lease=True)` for idempotent PR retries. |
| **G6** | §10/§11 | `mid_chain_sync` + `switch_to_branch(rebase=True)` reduce subtask chain drift. |

### Remaining Gap

| Gap | Location in this spec | Issue |
|-----|----------------------|-------|
| **G7** | §10 `_prepare_workspace` | LINK repos share a single directory across agents — no file-level isolation. |

`_prepare_workspace(task, agent) -> str`

Returns the absolute path to the workspace directory, or `None` if no workspace is available.

**Workspace resolution:**

Calls `db.acquire_workspace(project_id, agent_id, task_id)` to atomically lock an available
workspace for the project.  If no workspace is available (all locked or none exist), returns
`None`.  The caller (`_execute_task`) handles the `None` case by returning the task to READY.

**Branch name.**
- For plan subtasks that have a parent task: reuse the parent's `branch_name` (to
  accumulate all subtask commits on the same branch).  If the parent has no branch name,
  generate one from the subtask ID and title.
- For all other tasks: generate a fresh branch name with `GitManager.make_branch_name(task.id, task.title)`.

**`reuse_branch` flag.**  True when `task.is_plan_subtask and task.parent_task_id` is set.

**`rebase_on_switch` flag.**  Set to `config.auto_task.rebase_between_subtasks` (default
`False`).  When `True`, subtask branch switches include a rebase onto
`origin/<default_branch>` to reduce drift between the shared branch and main.

**By source type:**

*CLONE repos:*
- If `validate_checkout(workspace)` fails: call `git.create_checkout(repo.url, workspace)`
  (which `git clone`s the repo into `workspace`, creating parent directories as needed).
- If `reuse_branch`: call `git.switch_to_branch(workspace, branch_name, default_branch=repo.default_branch, rebase=rebase_on_switch)` — fetches from
  origin, checks out the existing branch, pulls latest, and optionally rebases onto
  `origin/<default_branch>` to reduce subtask chain drift (G6 fix).  When
  `rebase_on_switch` is True, also rebases onto `origin/<default_branch>`.
- Otherwise: call `git.prepare_for_task(workspace, branch_name, repo.default_branch)` —
  fetches from origin, checks out `default_branch`, hard-resets to `origin/<default_branch>`,
  then creates a new branch named `branch_name` (or switches to it and rebases if it
  already exists from a previous attempt — G4 fix).

*LINK repos:*
- If `workspace` does not exist as a directory: send a Discord warning notification
  via `_notify_channel` and return the path as-is.
- If the directory is a git repo (`validate_checkout` passes): apply the same branch logic
  as CLONE (`switch_to_branch` with `default_branch` and `rebase=rebase_on_switch` args, or `prepare_for_task`).
- If not a git repo: use the directory as-is (no git operations).

*INIT repos:*

> **Not yet implemented:** The code currently has no INIT source type handling branch —
> INIT workspaces silently fall through with no git operations. The CLONE and LINK paths
> above are the only active code paths.

**Database updates.**  After the git operations:
`db.update_task(task.id, branch_name=branch_name)`

**Plan file cleanup.**  Before returning, call `_cleanup_plan_files_before_task(workspace, task.id)`.
This removes ALL plan files left by previous tasks to prevent:
1. Agents failing to write a new plan because the file already exists.
2. Stale plans from being incorrectly discovered after task completion.

Cleanup covers two categories:
- **Primary plan files** (`.claude/plan.md`, `plan.md`, etc.) matching the configured
  `plan_file_patterns` — deleted unconditionally via `glob.glob` expansion.
- **Archived plan files** (in `.claude/plans/`) — filenames contain the originating task ID
  as a prefix, so files belonging to the *current* task (retry scenario) are preserved while
  all others are deleted.

If any files are removed and the workspace is a valid git checkout, the deletions are
committed with `git.acommit_all`.  `OSError` during listing/removal and any exception
during the commit are caught and logged as warnings — they never prevent the workspace
from being returned.

**Error handling.**  All git operations in `_prepare_workspace` are wrapped in a
try/except.  If any git operation fails, the workspace lock is released, the sentinel
file is removed, and the method returns `None` — the caller transitions the task to
PAUSED with a backoff rather than allowing the agent to proceed without branch management.

---

## 11. Workspace Completion

`_complete_workspace(task, agent) -> str | None`

Called after the adapter signals `COMPLETED`.  Returns a PR URL if one was created,
otherwise `None`.

**Preconditions.**  Look up the workspace via `db.get_agent_workspace(agent.id, task.project_id)`.
If no workspace is found or it is not a valid git checkout, or if `task.branch_name` is not set,
return `None` immediately.

**Commit.**  Call `git.commit_all(workspace, "agent: {title}\n\nTask-Id: {id}")`.  If
nothing was committed, log a message (not an error).

**Repo config.**  Resolve `repo_id` from task then agent; fetch `RepoConfig`.

**Plan subtask path.**  If `task.is_plan_subtask`:
- Call `_is_last_subtask(task)`.
  - `_is_last_subtask` fetches all sibling subtasks (same `parent_task_id`) via
    `db.get_subtasks(parent_task_id)` and returns `True` only when every sibling other
    than this task has status `COMPLETED`.
- If last subtask and repo exists: fetch the parent task record.
  - If parent task exists and has `requires_approval`: return `await _create_pr_for_task(...)`,
    which may return a PR URL or `None`.
  - Otherwise: call `_merge_and_push`.
- If not the last subtask and repo exists and branch_name is set:
  call `_mid_chain_rebase(task, repo, workspace)` to optionally rebase the shared branch
  onto latest main between subtask completions.  This internally calls
  `git.mid_chain_sync(workspace, branch_name, repo.default_branch)` which pushes
  intermediate work to the remote and rebases the chain branch onto
  `origin/<default_branch>`, reducing drift for the next subtask.  This catches conflicts
  early and keeps the branch close to main.  Log success/failure but continue regardless
  (non-fatal).
- Return `None`.

**Root task path.**
- If repo exists and `requires_approval`: call `_create_pr_for_task`, return the URL.
- If repo exists and no approval needed: call `_merge_and_push`, return `None`.
- If no repo: changes remain committed on the branch but nothing is pushed, return `None`.

### `_is_last_subtask(task) -> bool`

Checks if all sibling subtasks (same `parent_task_id`) are COMPLETED except this one.
Returns `True` if the task has no `parent_task_id` or if every sibling's status is
`COMPLETED`.

### `_mid_chain_rebase(task, repo, workspace) -> bool`

Optionally rebases the shared subtask branch onto latest main between subtask completions.
Called after an intermediate subtask commits its work (not the final subtask).

**Preconditions (skip if not met):**
- `config.auto_task.rebase_between_subtasks` must be `True`.
- `config.auto_task.chain_dependencies` must be `True` — without chained dependencies
  the subtasks may run in parallel on different branches, so mid-chain rebase is not
  applicable.

> **Note:** The `mid_chain_rebase` config field exists in `src/config.py` but is NOT
> referenced in `orchestrator.py`. The actual gate is `rebase_between_subtasks`.

**Execution:**
- Calls `git.mid_chain_rebase(workspace, branch_name, default_branch, push=config.auto_task.mid_chain_rebase_push)`.
- Logs the outcome (success or conflict skip).
- Returns `True` if the rebase succeeded, `False` otherwise.

**Error handling:**  All exceptions are caught silently — mid-chain rebase is best-effort
and never blocks the subtask chain.

**Benefits:**
- **Early conflict detection:** Conflicts are surfaced after each subtask rather than as a
  giant conflict at the end of the chain.
- **Smaller diffs at merge time:** The final merge stays close to a fast-forward, reducing
  the risk of push rejections.
- **Backed up progress:** With `mid_chain_rebase_push` enabled, intermediate progress is
  pushed to the remote.

### `_merge_and_push(task, repo, workspace, *, _max_retries=3)`

Merges the task branch into the default branch and pushes.  The workflow differs by repo
type:

**CLONE repos** — delegates to `git.sync_and_merge()`:

`sync_and_merge(workspace, branch_name, repo.default_branch)` encapsulates the full
sync-merge-push flow:

1. Fetch latest remote state.
2. Checkout default branch and hard-reset to `origin/<default_branch>` (**G1 fix**).
3. Attempt merge; on conflict, rebase task branch onto `origin/<default_branch>` and
   retry (**G3 fix**).
4. Push with retry (pull --rebase on push failure).

The `_max_retries` parameter represents total push attempts; internally this maps to
`max_retries = _max_retries - 1` (retries after the initial attempt).

Handles the `(success, error_msg)` return value:
- **Success:** Clean up the task branch locally and on the remote via `delete_branch(delete_remote=True)`.
- **`"merge_conflict"`:** Send a "Merge Conflict" notification suggesting manual
  resolution.  Reset the workspace to a clean state via `git.recover_workspace(workspace, repo.default_branch)`
  which checks out the default branch and runs `git reset --hard origin/<default_branch>`
  to discard any un-pushed merge commits (**G2 fix**).
- **`"push_failed: ..."`:** Send a "Push Failed" notification with attempt count and
  divergence warning.  Same workspace recovery as merge conflict.

Workspace recovery after failure is best-effort — errors are silently ignored.

> **Gaps G1--G3 are resolved.** `sync_and_merge` handles stale-main pulls (G1),
> `recover_workspace` resets after failures (G2), and rebase-before-merge resolves
> conflicts caused by branch staleness (G3).

**LINK / INIT repos** — no remote push:

1. Calls `git.merge_branch(workspace, branch_name, default_branch)`.
2. If merge fails with conflict: attempt `rebase_onto(branch_name, default_branch)` as a
   fallback, then retry the merge.  If still failing, send a "Merge Conflict" notification
   and recover by checking out the default branch (no hard reset — LINK repos have no
   remote).
3. On success: clean up the task branch locally via `delete_branch(delete_remote=False)`.

Branch cleanup and workspace recovery are always best-effort — failures are silently ignored.

### `_create_pr_for_task(task, repo, workspace) -> str | None`

Pushes the task branch and creates a PR. Returns the PR URL or `None`.

**LINK repos:**
- Notify "Approval Required" with manual-review instructions (LINK repos typically have
  no remote). Return `None`.

**CLONE repos:**
1. Push the branch with `git.push_branch(workspace, branch_name, force_with_lease=True)`.
   Uses `--force-with-lease` so retries don't fail if the branch was previously
   pushed (G5 fix). Task branches are agent-owned and safe to force-push.
   On push failure: notify and return `None`.
2. Create the PR via `git.create_pr(workspace, branch, title, body, base=default_branch)`.
   - PR body: `"Automated PR for task \`{id}\`.\n\n{description[:500]}"`.
3. On PR creation failure: notify and return `None` (branch was already pushed).
4. On success: return the PR URL.

---

## 12. Plan-Generated Tasks (Two-Step Approval Workflow)

Plan generation follows a two-step workflow: **discover → approval → create subtasks**.
After a task completes, the orchestrator delegates plan discovery to the Supervisor
via `_phase_plan_discover`. The Supervisor calls `process_task_completion` to find,
parse, and store plan files, then returns whether a plan was found. If found, the
task transitions to `AWAITING_PLAN_APPROVAL`. Subtasks are only created once a
human approves the plan via the `approve_plan` command (see [[specs/command-handler]]).

### 12a. Plan Discovery via Supervisor (`_phase_plan_discover`)

Called as part of the completion pipeline, BEFORE merge (so the workspace is still
available). Delegates to `Supervisor.on_task_completed()` which calls the
`process_task_completion` command in CommandHandler.

If no Supervisor is set (legacy mode), falls back to `_phase_plan_generate()`.

The `process_task_completion` command wraps the same logic as the former
`_discover_and_store_plan`:

### 12a-legacy. `_discover_and_store_plan(task, workspace) -> bool` (legacy fallback)

Called immediately after any successful COMPLETED path in `_execute_task`.  Returns `True`
if a plan was found, parsed, and stored for approval; `False` otherwise.

**Guards:**
- If `config.auto_task.enabled` is false: return `False`.
- If `task.is_plan_subtask` is true: return `False` (prevent recursive explosion).
- **Skip-if-implemented heuristic:** if `config.auto_task.skip_if_implemented` is true,
  call `git.has_non_plan_changes(workspace, default_branch)`.  If the task already made
  substantial code changes beyond the plan file itself, the plan was likely already
  executed during this task — log a message and return `False`.

**Plan file discovery.**
Call `find_plan_file(workspace, config.auto_task.plan_file_patterns)`.
If no file found, log to stdout and return `False`.

**Plan reading.**
`raw = read_plan_file(plan_path)`.  On I/O error, log and return `False`.

**Parsing.**
If `config.auto_task.use_llm_parser` and `_chat_provider` is set:
- Call `parse_plan_with_llm(raw, provider, source_file, max_steps)`.
- On failure, log and fall back to `parse_plan(raw, source_file, max_steps)`.

Otherwise: call `parse_plan(raw, source_file, max_steps)`.

If `plan.steps` is empty, log and return `False`.

**Plan archiving.**
Move the plan file to `.claude/plans/{task.id}-plan.md` inside the workspace.  This
prevents the file from being re-processed if the workspace is reused.  Any `OSError` is
silently ignored.  Store the archived path as a `plan_archived_path` task context entry.

**Store plan data.**  Store the parsed plan steps, preamble/context, and configuration
as structured `task_context` entries so the plan can be retrieved later during approval
without re-reading the file.

**Transition.**  Move the task to `AWAITING_PLAN_APPROVAL` status and emit a
`PLAN_FOUND` event.  Notify the Discord channel with a plan summary so the user can
review and approve/reject/delete the plan.

Return `True`.

### 12b. `_create_subtasks_from_stored_plan(task) -> list[Task]`

Called by `CommandHandler._cmd_approve_plan` after the user approves the plan.  Retrieves
the stored plan data from `task_context` entries and creates subtasks.

**Preamble extraction.**
Extract text from the stored plan raw content before the first step title as
`plan_context`.  Strip a leading `# Title` heading if present.  This context is
prepended to every subtask description.

**Subtask creation loop.**  Iterate the stored plan steps with an index:

For each step:
1. `new_id = await generate_task_id(db)`.
2. `description = build_task_description(step, parent_task=task, plan_context=plan_context)`.
3. Determine `requires_approval`:
   - If `inherit_approval` and `chain_dependencies`: only the final step inherits the parent's
     `requires_approval`; intermediate steps get `False`.
   - If `inherit_approval` and not `chain_dependencies`: every step inherits.
   - Otherwise: `False`.
4. Construct a `Task` dataclass:
   - `status = DEFINED`
   - `parent_task_id = task.id`
   - `repo_id = task.repo_id if inherit_repo else None`
   - `priority = config.base_priority + step.priority_hint`
   - `plan_source = archived_path`
   - `is_plan_subtask = True`
5. `db.create_task(new_task)`.
6. If `chain_dependencies` and `prev_task_id` is set: `db.add_dependency(new_id, depends_on=prev_task_id)`.
7. Record `prev_task_id = new_id` for the next iteration.

**After creation.**  Return the list of created tasks.  The caller (`_cmd_approve_plan`)
transitions the task to COMPLETED, then the next scheduling cycle promotes subtasks with
no unmet dependencies to READY.

### 12c. Plan File Cleanup

Plan files are cleaned up at two points to prevent stale plans from being re-presented
for approval by subsequent tasks reusing the same workspace:

**Post-approval cleanup** (`CommandHandler._cleanup_plan_files_after_approval`):
Runs after both `approve_plan` and `delete_plan` commands.
1. Deletes the archived plan file from `.claude/plans/<task_id>-plan.md` (path retrieved
   from the `plan_archived_path` task context entry).
2. Deletes any original plan files (`.claude/plan.md`, `plan.md`) that may still exist
   if archival failed.
3. Commits the deletions to git via `git.acommit_all` so the plan file is removed from
   the branch.
Uses `ws.workspace_path` (not `ws.path`) from the workspace record.

**Pre-task cleanup** (`Orchestrator._cleanup_plan_files_before_task`):
Runs during `_prepare_workspace` before every task launch.  Removes both primary plan files
(matching configured `plan_file_patterns`) and archived plan files from `.claude/plans/`
that belong to *other* tasks (identified by task ID prefix in the filename).  Files belonging
to the current task are preserved (retry scenario).  Removals are committed to git.  See §10
for details.

---

## 13. Approval Checking

### `_check_awaiting_approval`

Rate-limited to once per 60 seconds using `_last_approval_check`.

1. List all tasks with status `AWAITING_APPROVAL`.
2. Clean up `_no_pr_reminded_at` for task IDs no longer in the list.
3. For each task:
   - If `task.pr_url` is absent: call `_handle_awaiting_no_pr(task, now)`.
   - Otherwise: call `_check_pr_status(task)`.

### `_handle_awaiting_no_pr(task, now)`

Compute `updated_at = db.get_task_updated_at(task.id)` and
`age = (now - updated_at) if updated_at else 0`.

**Auto-complete path** (when `task.requires_approval` is false):
- If `age >= _NO_PR_AUTO_COMPLETE_GRACE` (120 seconds default): transition to COMPLETED
  (`context="auto_complete_no_pr"`), log a `"task_completed"` event, notify, and clear the
  reminder tracker.  Return immediately after (the manual-approval path below is skipped).

**Manual-approval path** (when `task.requires_approval` is true):
- Check the reminder interval: skip if `now - _no_pr_reminded_at[task.id] < _NO_PR_REMINDER_INTERVAL` (3600s).
- Update `_no_pr_reminded_at[task.id] = now`.
- If `age >= _NO_PR_ESCALATION_THRESHOLD` (86400s = 24h): send a high-visibility escalation
  warning with the age in hours and log `"approval_stuck"`.
- Otherwise: send a standard "awaiting manual approval" reminder.

### `_check_pr_status(task)`

Resolves a checkout path by checking `db.get_agent_workspace(agent_id, project_id)`,
then falling back to any workspace for the task's project via `db.list_workspaces(project_id=...)`.
If no path is found, return.

Call `git.check_pr_merged(checkout_path, task.pr_url)`:
- Returns `True` if merged.
- Returns `None` if closed without merge.
- Returns `False` if still open.

**Merged (`True`):**
Transition to COMPLETED, log `"task_completed"`, notify.
Best-effort: delete the task branch locally and remotely.

**Closed without merge (`None`):**
Transition to BLOCKED, context `"pr_closed"`, notify.
Call `_notify_stuck_chain(task)`.

**Still open (`False`):** no action.

---

## 14. Pause and Resume

### PAUSED task resume (`_resume_paused_tasks`)

Runs every cycle.  Lists all PAUSED tasks.  For each task where
`task.resume_after <= time.time()`:
`db.transition_task(id, READY, context="resume_paused", assigned_agent_id=None, resume_after=None)`.

### How tasks become PAUSED

Inside `_execute_task`, when `output.result` is `PAUSED_TOKENS` or `PAUSED_RATE_LIMIT`
(and rate-limit auto-retries have been exhausted):

```
resume_after = now + retry_secs
db.transition_task(task_id, PAUSED, context="tokens_exhausted", resume_after=...)
```

`retry_secs` comes from:
- `PAUSED_RATE_LIMIT`: `config.pause_retry.rate_limit_backoff_seconds`
- `PAUSED_TOKENS`: `config.pause_retry.token_exhaustion_retry_seconds`

A brief notification is sent to the task thread or notifications channel.

### Global pause (`pause()` / `resume()`)

`orchestrator.pause()` sets `_paused = True`.  The scheduling step (step 3) in
`run_one_cycle` is skipped, so no new tasks are assigned.  All other cycle steps
continue running.  `orchestrator.resume()` sets `_paused = False`.

---

## 15. Admin Operations

### `skip_task(task_id) -> (error | None, list[Task])`

Allowed states: BLOCKED or FAILED only.  Any other state returns an error string.

1. `db.transition_task(task_id, COMPLETED, context="skip_task")`.
2. `db.log_event("task_skipped", ...)`.
3. Fetch `db.get_dependents(task_id)`.  For each dependent in status DEFINED whose
   dependencies are all now met: add to `unblocked` list.
4. Notify the channel with a summary, including the unblock count.
5. Return `(None, unblocked)`.

The actual promotion of unblocked tasks from DEFINED to READY happens in the next
`_check_defined_tasks` cycle, not immediately in this method.

### `stop_task(task_id) -> error | None`

Allowed state: IN_PROGRESS only.  Any other state returns an error string.

1. Fetch `agent_id` from the task record.
2. If `agent_id` is set and an adapter exists for it: call `adapter.stop()` (best-effort;
   exceptions are logged and swallowed).
3. `db.transition_task(task_id, BLOCKED, context="stop_task", assigned_agent_id=None)`.
4. If `agent_id` is set: `db.update_agent(agent_id, state=IDLE, current_task_id=None)` and
   remove the adapter from `_adapters`.
5. Notify the channel.
6. Call `_notify_stuck_chain(task)`.
7. Return `None`.

---

## 16. Shutdown

`async shutdown()`

1. `await wait_for_running_tasks(timeout=10)` — waits up to 10 seconds for all background
   task-execution coroutines to finish.  Tasks still running after the timeout are
   abandoned (the process is exiting).
2. Stop `ConfigWatcher` if running.
3. If `hooks` is set: `await hooks.shutdown()`.
4. Close `memory_manager` if initialized.
5. `await db.close()`.

`wait_for_running_tasks(timeout)` collects the values of `_running_tasks` into a list and
calls either `asyncio.wait(tasks, timeout=timeout)` (if a timeout is provided) or
`asyncio.gather(*tasks, return_exceptions=True)` (if no timeout).  Returns immediately
if `_running_tasks` is empty.

---

## 17. Callbacks

The orchestrator is wired to Discord by injecting callbacks after construction but
before the scheduling loop starts.  No callback is required — the orchestrator runs
without them (notifications are silently dropped). Five callback setters exist:

### `set_notify_callback(callback: NotifyCallback)`

```python
NotifyCallback = Callable[[str, str | None], Awaitable[None]]
```

Arguments: `(message: str, project_id: str | None)`.

`_notify_channel(message, project_id)` is the internal wrapper.  It calls the callback
inside a try/except, logging errors to stdout.  When `project_id` is provided, the Discord
bot uses it to route the message to the project's dedicated channel, falling back to the
global notifications channel if none is configured.

### `set_create_thread_callback(callback: CreateThreadCallback)`

```python
ThreadSendCallback = Callable[[str], Awaitable[None]]
CreateThreadCallback = Callable[
    [str, str, str | None],
    Awaitable[tuple[ThreadSendCallback, ThreadSendCallback] | None],
]
```

Arguments to the callback: `(thread_name: str, initial_message: str, project_id: str | None)`.

Returns `(send_to_thread, notify_main)` or `None` if thread creation fails.

- `send_to_thread(text)` — appends content to the Discord thread for this task.  Used to
  stream all agent output and post completion/failure summaries.
- `notify_main(text)` — posts a brief message to the thread-root reply in the main
  notifications channel.  Used for completion/failure one-liners so operators see a summary
  without having to open the thread.

When `_create_thread` is not set, all output falls back to `_notify_channel`.

### `set_get_thread_url_callback(callback)`

Returns the URL for a task's Discord thread. Used for linking from notifications.

### `set_edit_thread_root_callback(callback)`

Edits the root message of a task's Discord thread. Used to update status after completion.

### `set_command_handler(handler)`

Sets the CommandHandler reference for interactive Discord views (e.g., plan approval buttons).

---

## Appendix A: Key Constants

| Constant | Default | Location | Purpose |
|---|---|---|---|
| `_MAX_RETRIES` | 10 | `task_names.py` | Max random attempts before using suffixed fallback |
| `_NO_PR_REMINDER_INTERVAL` | 3600s | `Orchestrator` | Min gap between no-PR approval reminders |
| `_NO_PR_ESCALATION_THRESHOLD` | 86400s | `Orchestrator` | Age at which no-PR reminder escalates |
| `_NO_PR_AUTO_COMPLETE_GRACE` | 120s | `Orchestrator` | Grace period before auto-completing non-approval tasks with no PR |
| Approval poll interval | 60s | `_check_awaiting_approval` | Rate limit on PR status checks |
| Shutdown timeout | 10s | `shutdown` | Max wait for running tasks before close |

---

## Appendix B: Git Sync Configuration

The following `auto_task` configuration fields control the workspace sync behavior:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `rebase_between_subtasks` | `bool` | `False` | Pass `rebase=True` to `switch_to_branch()` when switching to a shared subtask branch.  Rebases the branch onto `origin/<default_branch>` before the next subtask begins. |
| `mid_chain_rebase` | `bool` | `True` | After an intermediate subtask completes, rebase the shared branch onto latest `origin/<default_branch>`.  Catches conflicts early and reduces drift. |
| `mid_chain_rebase_push` | `bool` | `False` | When mid-chain rebase succeeds, push the rebased branch with `--force-with-lease` to back up intermediate progress. |
| `chain_dependencies` | `bool` | `True` | When `True`, subtasks depend on the previous step.  Required for mid-chain rebase (without it, subtasks may run in parallel). |

### Two drift-reduction mechanisms

The system provides two complementary mechanisms for keeping subtask chains close to main:

1. **Pre-start rebase** (`rebase_between_subtasks`): Controlled by `switch_to_branch(rebase=True)`.
   When the orchestrator prepares a workspace for the next subtask in a chain, it rebases the
   shared branch onto `origin/<default_branch>`.  This brings in upstream changes *before* the
   agent starts working.

2. **Post-completion rebase** (`mid_chain_rebase`): Controlled by `_mid_chain_rebase()`.
   After an intermediate subtask commits and before the next subtask is scheduled, the shared
   branch is rebased onto `origin/<default_branch>`.  This is best-effort and never blocks the
   chain.

Both mechanisms abort silently on conflict.  Conflicts are deferred to final merge time,
where `sync_and_merge()` applies its rebase-before-merge fallback.

---

## 18. Rule Manager Initialization

During `initialize()`, after hook engine setup:
1. Create `RuleManager` with the data directory, database, and hook engine
2. Run `install_defaults()` to create default global rules if not present

> **Note:** `reconcile()` is NOT called during `initialize()`. It runs later in `on_ready`,
> after the Supervisor is available (the supervisor is needed for rule prompt expansion).

The RuleManager is stored as `self.rule_manager` and is accessible by CommandHandler
for rule CRUD operations. Rules have been replaced by [[design/playbooks|playbooks]].
