# Task State Machine

This document describes the full task state machine including all states,
events, transitions, and the rules governing task lifecycle progression.

## States

| State | Description | Terminal? |
|-------|-------------|-----------|
| `DEFINED` | Task created, waiting for dependencies to be satisfied | No |
| `READY` | All dependencies met, eligible for scheduling | No |
| `ASSIGNED` | Scheduler has assigned an agent, awaiting execution start | No |
| `IN_PROGRESS` | Agent is actively working on the task | No |
| `WAITING_INPUT` | Agent asked a question, waiting for human reply via Discord | No |
| `PAUSED` | Temporarily paused (rate limit or token exhaustion), has `resume_after` timestamp | No |
| `AWAITING_APPROVAL` | PR created, waiting for human approval/merge | No |
| `COMPLETED` | Task finished successfully | Yes |
| `FAILED` | Task execution failed (may retry) | Semi-terminal |
| `BLOCKED` | Max retries exhausted or admin-stopped | Yes |

## State Transition Diagram

```
                    ┌──────────────────────────────────────────────────┐
                    │              ADMIN_RESTART (from any state)      │
                    └──────────────────────────────────────────────────┘

    ┌─────────┐  DEPS_MET  ┌───────┐  ASSIGNED  ┌──────────┐  AGENT_STARTED  ┌─────────────┐
    │ DEFINED ├────────────►│ READY ├────────────►│ ASSIGNED ├───────────────►│ IN_PROGRESS │
    └─────────┘             └───┬───┘             └────┬─────┘                └──┬──┬──┬──┬─┘
                                ▲                      │                         │  │  │  │
                                │               TIMEOUT│                         │  │  │  │
                                │           EXEC_ERROR │                         │  │  │  │
                                │                      ▼                         │  │  │  │
                                │                 ┌─────────┐                    │  │  │  │
                                │                 │ BLOCKED │◄──MAX_RETRIES──────┘  │  │  │
                                │                 └─────────┘◄──MERGE_FAILED─────┘  │  │  │
                                │                                                    │  │  │
                         RESUME_TIMER  ┌────────┐◄──TOKENS_EXHAUSTED────────────────┘  │  │
                                ├──────┤ PAUSED │                                       │  │
                                │      └────────┘◄──INPUT_TIMEOUT──┐                    │  │
                                │                                   │                    │  │
                                │      ┌────────────────┐          │                    │  │
                                │      │ WAITING_INPUT  ├──────────┘                    │  │
                                │      └───────┬────────┘◄──AGENT_QUESTION──────────────┘  │
                                │              │                                           │
                                │        HUMAN_REPLIED ──► back to IN_PROGRESS             │
                                │                                                          │
                           RETRY│                    PR_CREATED────────────────────────────┘
                                │      ┌───────────────────────┐
                                │      │   AWAITING_APPROVAL   │
                                │      └───────┬───────────────┘
                                │              │ PR_MERGED
                                │              ▼
                         ┌──────┤      ┌─────────────┐◄──AGENT_COMPLETED──────────────────┘
                         │      └─────►│  COMPLETED  │
                         │             └─────────────┘
                         │
                         ▼
                         ┌────────┐
                         │ FAILED │──MAX_RETRIES──► BLOCKED
                         └────────┘
```

## Transition Table

### Core Lifecycle (Happy Path)

| From | Event | To | Description |
|------|-------|----|-------------|
| DEFINED | DEPS_MET | READY | All dependency tasks are COMPLETED |
| READY | ASSIGNED | ASSIGNED | Scheduler picks task for an idle agent |
| ASSIGNED | AGENT_STARTED | IN_PROGRESS | Agent process has launched |
| IN_PROGRESS | AGENT_COMPLETED | COMPLETED | Agent reports work done, no approval needed |
| IN_PROGRESS | PR_CREATED | AWAITING_APPROVAL | PR created, needs human review |
| AWAITING_APPROVAL | PR_MERGED | COMPLETED | PR merged, task done |

### Pause & Resume

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | TOKENS_EXHAUSTED | PAUSED | Token budget or rate limit hit |
| IN_PROGRESS | AGENT_QUESTION | WAITING_INPUT | Agent needs human input |
| WAITING_INPUT | HUMAN_REPLIED | IN_PROGRESS | Human answered, resume work |
| WAITING_INPUT | INPUT_TIMEOUT | PAUSED | No reply within timeout |
| PAUSED | RESUME_TIMER | READY | `resume_after` timestamp elapsed |

### Failure & Retry

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | AGENT_FAILED | FAILED | Agent reports failure |
| IN_PROGRESS | MERGE_FAILED | BLOCKED | Post-completion merge failed |
| FAILED | RETRY | READY | retry_count < max_retries, try again |
| FAILED | MAX_RETRIES | BLOCKED | No more retries, needs intervention |
| IN_PROGRESS | MAX_RETRIES | BLOCKED | Direct shortcut (skip FAILED) |
| IN_PROGRESS | RETRY | READY | Direct shortcut (skip FAILED) |

### Administrative Operations

| From | Event | To | Description |
|------|-------|----|-------------|
| BLOCKED | ADMIN_SKIP | COMPLETED | Mark as done despite failure |
| FAILED | ADMIN_SKIP | COMPLETED | Mark as done despite failure |
| IN_PROGRESS | ADMIN_STOP | BLOCKED | Force-stop a running task |
| Any non-running | ADMIN_RESTART | READY | Re-queue for execution |

### Error Recovery

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | TIMEOUT | BLOCKED | Agent stuck timeout |
| ASSIGNED | TIMEOUT | BLOCKED | Agent never started |
| ASSIGNED | EXECUTION_ERROR | READY | Launch failed, retry |
| IN_PROGRESS | RECOVERY | READY | Daemon restarted while task running |
| ASSIGNED | RECOVERY | READY | Daemon restarted while task assigned |
| AWAITING_APPROVAL | PR_CLOSED | BLOCKED | PR closed without merge |

## Promotion Rules

1. **DEFINED → READY**: Checked every orchestrator cycle (~5s). A task
   promotes when `db.are_dependencies_met(task_id)` returns True.
   Plan subtasks get special handling: when their parent plan is IN_PROGRESS
   (approved, subtasks running), the parent dependency is treated as met.

2. **READY → ASSIGNED**: The `Scheduler` selects which project gets the
   next agent slot based on proportional credit-weight.

3. **PAUSED → READY**: Tasks in PAUSED always have a `resume_after` Unix
   timestamp. The orchestrator checks this every cycle.

4. **FAILED → READY/BLOCKED**: On failure, if `retry_count < max_retries`,
   the task goes back to READY. Otherwise it becomes BLOCKED.

## Dependency Chains

Tasks can declare dependencies on other tasks, forming a directed acyclic graph (DAG). A task in DEFINED state only promotes to READY when **all** its dependencies are in COMPLETED state. The orchestrator checks this every cycle (~5 seconds) via `db.are_dependencies_met(task_id)`.

### Cycle detection

Before adding a new dependency edge, the system validates the DAG using `validate_dag_with_new_edge()`. If the edge would create a cycle, the operation is rejected. This prevents deadlock scenarios where tasks would wait for each other indefinitely.

### Stuck chain detection

The orchestrator automatically monitors for tasks stuck in DEFINED state beyond a configurable threshold. When detected, it traces the dependency graph to identify the root cause — typically a BLOCKED or FAILED upstream task — and sends a Discord notification with the "blast radius" (how many downstream tasks are affected).

Key methods:
- `_check_stuck_defined_tasks()` — Periodic scan for stuck tasks
- `_find_stuck_downstream()` — BFS walk to find all tasks blocked by a given task
- `_notify_stuck_chain()` — Rate-limited Discord notifications

### The skip workflow

When a task in a dependency chain fails or gets blocked and the work is no longer needed, the **ADMIN_SKIP** event provides an escape hatch:

1. The blocked/failed task is marked as COMPLETED (via ADMIN_SKIP)
2. The orchestrator's next cycle checks all DEFINED tasks
3. Tasks whose only remaining unmet dependency was the skipped task now promote to READY
4. The chain resumes execution

This is a deliberate design choice: there is no separate SKIPPED state. Skipped tasks become COMPLETED so the dependency resolution logic works uniformly — downstream tasks only need to check if dependencies are COMPLETED, regardless of how they got there.

## Important Notes

- **State machine is advisory**: Transitions are NOT enforced in production.
  The orchestrator writes directly via `db.update_task()`.
- **PAUSED never stalls**: Every PAUSED task has a `resume_after` timestamp.
- **DAG validation**: Cycle detection prevents circular dependency chains.
- **No SKIPPED state**: The ADMIN_SKIP event transitions to COMPLETED, not a separate state. This keeps dependency resolution simple.
