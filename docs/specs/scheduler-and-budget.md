---
tags: [spec, scheduler, budget, tokens]
---

# Scheduler and Budget Specification

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../README.md) for what AQ does today, and
> see [historical material](../history/README.md) for how this material is
> organised.

## 1. Overview

The scheduler and budget subsystem controls which tasks get assigned to which agents, in what order, and under what resource constraints. It is entirely deterministic and stateless: given the same snapshot of system state, it always produces the same assignment decisions. No LLM calls, no randomness, no side effects. All scheduling logic runs in-process inside the asyncio event loop.

See [design/agent-coordination](design/agent-coordination.md) for how coordination playbooks interact with the scheduler.

The subsystem is split across two files:

- `src/scheduler.py` — the core scheduling algorithm
- `src/tokens/budget.py` — the configured fleet-wide budget plus budget math (target ratios, deficits, exhaustion checks)

> **Removed.** `src/tokens/tracker.py` (`RateLimitWindow`) held a sliding-window token counter per (agent type, limit type). It had no production importer and no test, and it counted against a hand-configured cap rather than any provider's real quota, so it was deleted rather than carried. Provider headroom is an unstarted design (`docs/superpowers/specs/2026-08-24-usage-aware-concurrency.md`); whatever implements it will be sampler-driven and does not build on that dataclass.

The scheduler is called once per orchestrator tick (roughly every 5 seconds). It produces a list of zero or more `AssignAction` objects, each pairing one idle agent with one ready task. The orchestrator then executes those assignments by updating the database and launching agent processes.

---

## Source Files
- `src/scheduler.py`
- `src/tokens/budget.py`

---

## 2. Scheduling Algorithm

### 2.1 Entry Point

The scheduler exposes a single static method:

```
Scheduler.schedule(state: SchedulerState) -> list[AssignAction]
```

It takes a complete snapshot of the current system state and returns a list of assignment actions to execute. It never mutates any state.

> **Pre-tick reconciliation.** Before `Scheduler.schedule()` runs each tick, the orchestrator invokes `AgentReconciler.reconcile()` (see `src/orchestrator/agent_reconciler.py` and the design doc at `docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md`). The reconciler lazily creates idle agent rows so the scheduler always has slots to assign work to, sized by `project.max_concurrent_agents`. The scheduler itself stays a pure function — all DB writes for agent supply happen in the reconciler.

### 2.2 SchedulerState Snapshot

`SchedulerState` is a plain dataclass that packages everything the scheduler needs in one object. The orchestrator constructs it by querying the database immediately before calling `schedule()`.

Fields:

| Field | Type | Description |
|---|---|---|
| `projects` | `list[Project]` | All projects known to the system |
| `tasks` | `list[Task]` | All tasks known to the system (any status) |
| `agents` | `list[Agent]` | All agents known to the system (any state) |
| `project_token_usage` | `dict[str, int]` | Maps project ID to token count consumed in the current rolling window |
| `project_active_agent_counts` | `dict[str, int]` | Maps project ID to the number of agents currently running tasks for that project |
| `tasks_completed_in_window` | `dict[str, int]` | Maps project ID to the number of tasks completed in the current rolling window |
| `global_budget` | `int or None` | Optional hard cap on total tokens across all projects; `None` means unlimited |
| `global_tokens_used` | `int` | Total tokens consumed across all projects in all time (or the relevant window) |

All token counts are integers representing individual tokens.

### 2.3 AssignAction Output

Each assignment the scheduler decides to make is represented as an `AssignAction` dataclass:

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | The ID of the idle agent to assign |
| `task_id` | `str` | The ID of the READY task to assign to that agent |
| `project_id` | `str` | The project the task belongs to (denormalized for convenience) |

The scheduler returns a list of these. One agent and one task appear in at most one `AssignAction` per call. The list may be empty if no valid assignments can be made.

### 2.4 Step-by-Step Algorithm

**Step 1: Global budget check.**
If `global_budget` is not `None` and `global_tokens_used >= global_budget`, return an empty list immediately. No assignments are made when the global token budget is exhausted.

**Step 2: Idle agent check.**
Collect all agents whose state is `IDLE`. If there are none, return an empty list immediately.

**Step 3: Group READY tasks by project.**
Iterate over all tasks. For each task with status `READY`, add it to a dictionary keyed by `project_id`. Tasks with any other status are ignored.

**Step 4: Sort tasks within each project.**
For each project's list of READY tasks, sort by `(priority, id)` ascending. Lower priority value means higher scheduling priority. The task `id` is used as a stable tiebreaker, so earlier-created tasks win.

**Step 5: Filter to eligible projects.**
Keep only projects where both conditions hold:
- The project's `status` is `ACTIVE`.
- The project has at least one READY task (i.e., it appears in the grouped task dict).

If no eligible projects remain, return an empty list.

**Step 6: Compute total weight and total token usage.**
Sum the `credit_weight` of all eligible projects to get `total_weight`. Sum all values in `project_token_usage` to get `total_tokens`. If `total_tokens` is zero, set it to 1 to avoid division by zero in ratio calculations.

**Step 7: Iterate over idle agents.**
Maintain three local tracking structures that accumulate across iterations:
- `assigned_agents`: set of agent IDs already assigned this round.
- `assigned_tasks`: set of task IDs already assigned this round.
- `round_agent_counts`: a copy of `project_active_agent_counts` that is incremented as assignments are made, so later iterations see the up-to-date concurrency count.

For each idle agent (in whatever order they appear in `state.agents`):

- Skip the agent if it is already in `assigned_agents`.
- Sort the eligible projects using the project sort key described in section 2.5.
- Iterate over the sorted projects and attempt to assign a task. Stop at the first project that accepts an assignment.
- If a valid assignment is found, record it and move to the next agent.

**Step 8: Return the collected actions.**

### 2.5 Project Sort Key (Deficit-Based Ordering)

For each agent assignment attempt, eligible projects are sorted by a two-element tuple `(has_guarantee, deficit)` in ascending order.

**`has_guarantee`** is an integer:
- `0` if the project has completed zero tasks in the current window (i.e., `tasks_completed_in_window.get(project_id, 0) == 0`). These projects sort first because they have not yet received their minimum-task guarantee.
- `1` if the project has completed at least one task in the window. These projects sort after projects that need their guarantee.

**`deficit`** is a float:
- Computed as `actual_ratio - target_ratio`.
- `target_ratio = project.credit_weight / total_weight`
- `actual_ratio = project_token_usage.get(project_id, 0) / total_tokens`
- A negative deficit means the project has consumed fewer tokens than its fair share. Negative values sort before positive ones, so projects that are behind get priority.

Together, the sort key ensures: (1) any project that has not yet received a single task in the window is always considered before projects that have, regardless of deficit; (2) among projects in the same guarantee tier, the one furthest below its token target goes first.

### 2.6 Per-Project Eligibility Checks (Inner Loop)

When evaluating a project as a candidate for the current agent, two checks must both pass:

**Budget check:** If `project.budget_limit` is not `None` and `project_token_usage.get(project_id, 0) >= project.budget_limit`, skip this project. It has exhausted its token budget for the window.

**Concurrency check:** If `round_agent_counts.get(project_id, 0) >= project.max_concurrent_agents`, skip this project. It already has as many agents running as it is allowed.

If either check fails, the project is skipped and the next project in sorted order is tried.

### 2.7 Task Selection Within a Project

Once a project passes both checks, the scheduler picks the first task from the project's sorted READY list that is not already in `assigned_tasks`. Because tasks were sorted in step 4 by `(priority, id)`, this is always the highest-priority, earliest-created unassigned task.

If all tasks for this project are already assigned in the current round, the project is skipped and the next project is tried.

### 2.8 Recording an Assignment

When a valid `(agent, project, task)` triple is found:
1. Append an `AssignAction(agent_id, task_id, project_id)` to `actions`.
2. Add `agent_id` to `assigned_agents`.
3. Add `task_id` to `assigned_tasks`.
4. Increment `round_agent_counts[project_id]` by 1.
5. Break out of the project loop and proceed to the next idle agent.

---

## 3. Budget Manager

`BudgetManager` in `src/tokens/budget.py` is a lightweight class that encapsulates the arithmetic for fair-share budget allocation. It holds one piece of persistent state: the configured `global_budget` (an integer or `None`). All methods are pure functions of their arguments.

The orchestrator constructs exactly one (`Orchestrator.budget`) and treats it as the **single source of truth for the global budget**. `Orchestrator._on_config_reloaded` writes `budget.global_budget` on every `config.reloaded` event, and `Orchestrator._schedule` reads it back into `SchedulerState.global_budget`, so a hot edit of `global_token_budget_daily` takes effect on the next tick without a restart. The assignment is unconditional: dropping the key from the config means "no global cap", and must clear the manager rather than leave the previous cap enforced.

`BudgetManager` declares `__slots__`. The reload hook previously assigned `budget._global_budget` — a name nothing reads — which silently discarded every reloaded budget; `__slots__` turns that class of typo into an immediate `AttributeError`.

### 3.1 Construction

```
BudgetManager(global_budget: int | None = None)
```

`global_budget` is the maximum total number of tokens that can be spent across all projects. `None` means there is no global limit.

### 3.2 Target Ratio Calculation

```
calculate_target_ratios(weights: dict[str, float]) -> dict[str, float]
```

Given a mapping of project ID to credit weight, returns a mapping of project ID to its target fraction of the total token budget.

- Sum all weight values. If the sum is zero, return an empty dict.
- For each project, `target_ratio = weight / total_weight`.
- All ratios are in `[0.0, 1.0]` and sum to 1.0.

This is used to express the intended proportional spending for each project. For example, if project A has weight 2 and project B has weight 1, A's target ratio is 0.667 and B's is 0.333.

### 3.3 Deficit Calculation

```
calculate_deficits(weights: dict[str, float], usage: dict[str, int]) -> dict[str, float]
```

Given credit weights and current token usage per project, returns a deficit score for each project. A positive deficit means the project has received less than its fair share; a negative deficit means it has received more.

Algorithm:
1. Compute target ratios via `calculate_target_ratios(weights)`.
2. Sum all values in `usage` to get `total_usage`.
3. If `total_usage` is zero, return the target ratios directly as deficits (every project is equally behind because nothing has been spent yet).
4. Otherwise, for each project in the target ratios:
   - `actual = usage.get(project_id, 0) / total_usage`
   - `deficit = target - actual`

Note: the sign convention here (target minus actual) is the inverse of what the scheduler's inline sort key computes (actual minus target). The `BudgetManager.calculate_deficits` method returns positive-means-behind, while the scheduler's sort key uses negative-means-behind. Both arrive at the same ordering when sorted ascending: the scheduler sorts by `actual - target` ascending, which is equivalent to sorting by `target - actual` descending. The `BudgetManager` class is available as a utility but the scheduler does its own inline calculation rather than calling `BudgetManager` directly — `Scheduler.schedule` is a pure function over its snapshot and holds no reference to the manager.

### 3.4 Global Budget Exhaustion Check

```
is_global_budget_exhausted(total_used: int) -> bool
```

Returns `True` if `global_budget` is not `None` and `total_used >= global_budget`. Returns `False` if there is no global budget configured.

### 3.5 Per-Project Budget Exhaustion Check

```
is_project_budget_exhausted(project_used: int, budget_limit: int | None) -> bool
```

Returns `True` if `budget_limit` is not `None` and `project_used >= budget_limit`. Returns `False` if the project has no budget limit configured.

---

## 4. Interactions and Invariants

- The `Scheduler` does not call `BudgetManager` methods directly. It replicates the target ratio and deficit arithmetic inline within the sort key closure, because `schedule()` is a pure function over a snapshot and holds no object references. The `BudgetManager` instance still reaches it *by value*: `Orchestrator._schedule` copies `budget.global_budget` into `SchedulerState.global_budget` on every tick.

- Nothing in this subsystem tracks the provider's own rate limits. The scheduler only sees the aggregate `project_token_usage` and `global_tokens_used` values from the token ledger, which measure our recorded spend against hand-configured caps, not remaining quota.

- `SchedulerState` is constructed fresh on every orchestrator tick. The scheduler never holds a reference to it between calls, so there is no risk of stale state.

- The `round_agent_counts` dict is a local copy made at the start of each `schedule()` call. Mutations to it do not propagate back to the database or to `state.project_active_agent_counts`. The orchestrator applies the returned `AssignAction` list to the database separately.

- If two projects have identical sort keys, their relative order is determined by Python's stable sort behavior applied to whatever order they appear in `active_projects`, which itself comes from `state.projects` filtered by status. No secondary tiebreaker is applied beyond what the sort key provides.
