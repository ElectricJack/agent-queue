# Models and State Machine Specification

## 1. Overview

This document specifies the core domain models and task state machine for Agent Queue. Together they define every entity the system tracks (tasks, agents, projects, repos, hooks) and the rules governing how a task moves through its lifecycle from initial definition to completion or permanent failure.

---

## Source Files
- `src/models.py`
- `src/state_machine.py`

---

## 2. Enums

### TaskStatus

Represents the current lifecycle position of a task. A task always holds exactly one of these values at any moment.

| Value | Meaning |
|---|---|
| `DEFINED` | Task has been created but its upstream dependencies have not yet all completed. It is not eligible to run. |
| `READY` | All dependencies are completed; the task is queued and waiting for an agent to pick it up. |
| `ASSIGNED` | The scheduler has selected an agent for this task. The agent process has not yet confirmed it started. |
| `IN_PROGRESS` | The agent has acknowledged the task and is actively working on it. |
| `WAITING_INPUT` | The agent has asked a question and is paused, waiting for a human to reply via Discord. |
| `PAUSED` | Execution is suspended temporarily, most commonly due to rate-limit or token exhaustion. The task always has a `resume_after` timestamp and will be made READY again automatically when that time passes. |
| `AWAITING_APPROVAL` | A pull request has been opened for this task. A human must review and merge (or close) the PR before the task can be marked complete. |
| `COMPLETED` | The task is done and all verification or approval requirements are satisfied. Downstream dependents may now be promoted. |
| `FAILED` | The agent run ended with an error. The task may be retried (up to `max_retries`) by transitioning back to READY. |
| `BLOCKED` | The task has exhausted its retries, was administratively stopped, timed out without recovery, or its PR was closed. No automatic recovery occurs; manual admin action is required to restart it. |

---

### TaskEvent

Events are the inputs to the state machine. Each event, combined with the current status, determines the next status. Events are never stored permanently — they are fired at runtime to drive transitions.

| Value | Meaning |
|---|---|
| `DEPS_MET` | All upstream dependencies have reached COMPLETED. Signals that a DEFINED task may now become READY. |
| `ASSIGNED` | The scheduler has chosen an idle agent for this task. |
| `AGENT_STARTED` | The agent process confirmed it has begun executing the task. |
| `AGENT_COMPLETED` | The agent finished its work without error. |
| `AGENT_FAILED` | The agent process reported an error or non-zero exit. |
| `TOKENS_EXHAUSTED` | The agent used its entire token budget before completing; execution is suspended. |
| `AGENT_QUESTION` | The agent needs human input and has posted a question to Discord. |
| `HUMAN_REPLIED` | A human responded to the agent's question via Discord. |
| `INPUT_TIMEOUT` | The human did not reply within the allowed window; execution is suspended as if paused. |
| `RESUME_TIMER` | The PAUSED task's `resume_after` timestamp has passed; it is eligible to run again. |
| `PR_CREATED` | A pull request was opened for this task's branch. Human approval is now required. |
| `PR_MERGED` | The pull request was merged by a human. The task may now be marked complete. |
| `RETRY` | The task failed but is under its retry limit and is being re-queued. |
| `MAX_RETRIES` | The task has failed and has exhausted all allowed retry attempts. |
| `ADMIN_SKIP` | An administrator has manually declared the task complete without normal verification. |
| `ADMIN_STOP` | An administrator has forcibly stopped an in-progress task and moved it to BLOCKED. |
| `ADMIN_RESTART` | An administrator has manually forced a task back to READY regardless of its current state, except IN_PROGRESS. |
| `PR_CLOSED` | The pull request was closed without merging; the task becomes BLOCKED. |
| `TIMEOUT` | The task or agent did not produce a heartbeat within the expected window. |
| `EXECUTION_ERROR` | An unexpected runtime error occurred during task execution; the assigned task is returned to READY for a new attempt. |
| `RECOVERY` | The daemon restarted and detected a task that was left in ASSIGNED or IN_PROGRESS with no running agent; it resets the task to READY. |

---

### AgentState

Represents the current operational status of an agent worker process.

| Value | Meaning |
|---|---|
| `IDLE` | The agent is registered and available to accept a new task. |
| `STARTING` | The agent has been assigned a task and its process is being initialized. |
| `BUSY` | The agent is actively executing a task. |
| `PAUSED` | The agent's current task is paused (e.g. token exhaustion); the agent is not executing but is not free for other work. |
| `ERROR` | The agent encountered an unrecoverable error and is not usable until reset. |

---

### AgentResult

The outcome value an agent reports when it finishes a task run, whether successfully or not.

| Value | Meaning |
|---|---|
| `COMPLETED` | The agent finished the task successfully. |
| `FAILED` | The agent terminated with an error. |
| `PAUSED_TOKENS` | The agent stopped because it exhausted its token budget; work may be resumed. |
| `PAUSED_RATE_LIMIT` | The agent stopped because the upstream API imposed a rate limit; work will resume after a backoff delay. |

---

### ProjectStatus

Controls whether a project is active in the scheduling loop.

| Value | Meaning |
|---|---|
| `ACTIVE` | The project participates in normal scheduling. Its tasks are eligible for assignment. |
| `PAUSED` | The project is temporarily excluded from scheduling. Existing in-progress tasks continue but no new tasks are started. |
| `ARCHIVED` | The project is permanently inactive. It is retained in the database for historical reference only. |

---

### VerificationType

Determines how a task's output is validated before it is considered complete.

| Value | Meaning |
|---|---|
| `AUTO_TEST` | The system runs a configured set of test commands and checks exit codes. |
| `QA_AGENT` | A second AI agent is spawned to review the first agent's work. |
| `HUMAN` | A human must manually inspect the work and give explicit approval. |

---

### RepoSourceType

Describes how the system acquires the repository associated with a project.

| Value | Meaning |
|---|---|
| `CLONE` | The system clones the repository from a remote URL. |
| `LINK` | The system uses an existing local directory as the repository (no cloning). |
| `INIT` | The system initializes a brand-new empty repository in a specified path. |

---

## 3. Data Models

### RepoConfig

Describes a source code repository attached to a project.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique identifier for this repo record. |
| `project_id` | `str` | The project this repo belongs to. |
| `source_type` | `RepoSourceType` | How the repo was acquired (clone, link, or init). |
| `url` | `str` | Remote URL for cloned repos. Empty string if not applicable. |
| `source_path` | `str` | Filesystem path used when source type is LINK. Empty string if not applicable. |
| `default_branch` | `str` | The branch to check out by default. Defaults to `"main"`. |
| `checkout_base_path` | `str` | The base directory where the system places agent working copies of this repo. |

All fields except `id` and `project_id` have defaults, so a minimal RepoConfig needs only its identifier and owning project.

---

### Project

Represents a software project managed by the system. Projects are the top-level organizational unit; tasks, repos, and agents are all scoped to a project.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique project identifier. |
| `name` | `str` | Human-readable project name displayed in Discord. |
| `credit_weight` | `float` | Relative scheduling weight. Higher values cause the scheduler to assign more agent time to this project. Defaults to `1.0`. |
| `max_concurrent_agents` | `int` | Maximum number of agents that may be simultaneously active on this project. Defaults to `2`. |
| `status` | `ProjectStatus` | Whether the project is ACTIVE, PAUSED, or ARCHIVED. Defaults to ACTIVE. |
| `total_tokens_used` | `int` | Cumulative count of tokens consumed by all agents across all tasks for this project. |
| `budget_limit` | `int \| None` | Optional hard cap on total tokens. When set, no new tasks are started once this limit is reached. `None` means unlimited. |
| `discord_channel_id` | `str \| None` | Optional Discord channel ID for project-specific notifications. When set, all task output for this project is routed to this channel instead of the default control channel. |

---

### Task

The central entity of the system. A task represents a unit of work to be executed by an agent.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique task identifier, typically a human-readable adjective-noun pair. |
| `project_id` | `str` | The project this task belongs to. |
| `title` | `str` | Short human-readable description of what the task does. |
| `description` | `str` | Full instructions passed to the agent. May be long and detailed. |
| `priority` | `int` | Scheduling priority. Lower numbers are higher priority. Defaults to `100`. |
| `status` | `TaskStatus` | Current lifecycle state. Defaults to DEFINED. |
| `verification_type` | `VerificationType` | How the task's output will be validated. Defaults to AUTO_TEST. |
| `retry_count` | `int` | Number of times this task has been retried after failure. Starts at `0`. |
| `max_retries` | `int` | Maximum allowed retry attempts before the task becomes BLOCKED. Defaults to `3`. |
| `parent_task_id` | `str \| None` | If this is a subtask generated from a plan, the ID of the parent task that produced the plan. `None` for top-level tasks. |
| `repo_id` | `str \| None` | The repo the agent should work in. `None` if no repo is required. |
| `assigned_agent_id` | `str \| None` | The agent currently assigned to this task. `None` when unassigned. |
| `branch_name` | `str \| None` | The git branch the agent is working on. `None` before assignment. |
| `resume_after` | `float \| None` | Unix timestamp. When the task is PAUSED, it must not be re-queued until this time has passed. `None` for non-paused tasks. |
| `requires_approval` | `bool` | If `True`, the task cannot be marked COMPLETED through automated verification alone — it must go through AWAITING_APPROVAL and have its PR merged. Defaults to `False`. |
| `pr_url` | `str \| None` | URL of the pull request created for this task. `None` until a PR exists. |
| `plan_source` | `str \| None` | Filesystem path to the archived plan file that auto-generated this task. `None` for manually created tasks. |
| `is_plan_subtask` | `bool` | `True` if this task was automatically generated from a parent task's plan output. Defaults to `False`. |

**Constraints:**
- `retry_count` must never exceed `max_retries`. When they are equal and the task fails again, it transitions to BLOCKED rather than being retried.
- A PAUSED task must always have a non-`None` `resume_after` value. There is no permanent pause without a scheduled resume time.
- An ASSIGNED or IN_PROGRESS task must have a non-`None` `assigned_agent_id`.

---

### Agent

Represents a registered agent worker that can execute tasks.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique agent identifier. |
| `name` | `str` | Human-readable agent name for display in Discord. |
| `agent_type` | `str` | The agent implementation type. Known values: `"claude"`, `"codex"`, `"cursor"`, `"aider"`. |
| `state` | `AgentState` | Current operational status. Defaults to IDLE. |
| `current_task_id` | `str \| None` | The task the agent is currently working on. `None` when IDLE. |
| `checkout_path` | `str \| None` | Filesystem path of the agent's current working directory for its task. |
| `repo_id` | `str \| None` | Repo the agent currently has checked out. |
| `pid` | `int \| None` | OS process ID of the running agent subprocess. `None` when not executing. |
| `last_heartbeat` | `float \| None` | Unix timestamp of the most recent health signal from the agent. Used to detect dead agents. |
| `total_tokens_used` | `int` | Cumulative token count across all tasks this agent has ever run. |
| `session_tokens_used` | `int` | Token count for the current session only. Reset when a new session begins. |

---

### TaskContext

The full set of information handed to an agent when a task is assigned. This is constructed at dispatch time and is not stored directly as a task field.

| Field | Type | Description |
|---|---|---|
| `description` | `str` | The task's full description (instructions for the agent). |
| `acceptance_criteria` | `list[str]` | Ordered list of conditions that define "done". The agent is expected to satisfy all of them. |
| `test_commands` | `list[str]` | Shell commands to run to verify the work. Used when `verification_type` is AUTO_TEST. |
| `checkout_path` | `str` | Absolute filesystem path where the agent should work. |
| `branch_name` | `str` | Git branch the agent should use. |
| `attached_context` | `list[str]` | Additional text blobs (documentation, examples, notes) injected into the agent's context window. |
| `mcp_servers` | `list[dict]` | MCP (Model Context Protocol) server configurations available to the agent. Each dict contains server connection parameters. |
| `tools` | `list[str]` | Names of tools the agent is permitted to use during this task. |

All list fields default to empty lists. `checkout_path` and `branch_name` default to empty strings.

---

### AgentOutput

The result an agent reports when it finishes (successfully or not).

| Field | Type | Description |
|---|---|---|
| `result` | `AgentResult` | The outcome classification (completed, failed, paused_tokens, paused_rate_limit). |
| `summary` | `str` | Human-readable summary of what the agent did. Displayed in Discord. Defaults to empty string. |
| `files_changed` | `list[str]` | List of file paths the agent modified. May be used for notifications or review. |
| `tokens_used` | `int` | Number of tokens consumed during this run. |
| `error_message` | `str \| None` | If `result` is FAILED, contains the error details. `None` on success. |

---

### Hook

A configured automation that fires on a schedule or in response to task lifecycle events and optionally calls an LLM with gathered context.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique hook identifier. |
| `project_id` | `str` | The project this hook belongs to. |
| `name` | `str` | Human-readable name for the hook. |
| `enabled` | `bool` | Whether the hook is active. Disabled hooks are never triggered. Defaults to `True`. |
| `trigger` | `str` | JSON-encoded trigger configuration. Specifies the trigger type (e.g. `"periodic"`) and parameters (e.g. `"interval_seconds"`). |
| `context_steps` | `str` | JSON-encoded array of step configurations. Each step describes a data-gathering action to run before the LLM call. |
| `prompt_template` | `str` | Template string for the LLM prompt. Supports placeholders such as `{{step_0}}` (results from context steps) and `{{event}}` (the triggering event data). |
| `llm_config` | `str \| None` | JSON-encoded LLM configuration (provider, model, etc.). `None` if the hook does not call an LLM. |
| `cooldown_seconds` | `int` | Minimum seconds that must elapse between successive runs of this hook. Defaults to `3600`. |
| `max_tokens_per_run` | `int \| None` | Optional per-run token budget cap. `None` means no limit beyond the global budget. |
| `created_at` | `float` | Unix timestamp when the hook was created. |
| `updated_at` | `float` | Unix timestamp of the most recent modification. |

---

### HookRun

A record of a single execution of a hook.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique run identifier. |
| `hook_id` | `str` | The hook that was executed. |
| `project_id` | `str` | The project the hook belongs to. |
| `trigger_reason` | `str` | What caused this run. Known values: `"periodic"`, `"cron"`, `"event:task_completed"`, `"manual"`. |
| `status` | `str` | Outcome of the run. Values: `"running"` (in progress), `"completed"` (success), `"failed"` (error), `"skipped"` (cooldown or disabled). Defaults to `"running"`. |
| `event_data` | `str \| None` | JSON-encoded data from the triggering event, if the hook was triggered by a lifecycle event. |
| `context_results` | `str \| None` | JSON-encoded output from the context-gathering steps, one result per configured step. |
| `prompt_sent` | `str \| None` | The final rendered prompt string that was sent to the LLM (after template substitution). `None` if no LLM was called. |
| `llm_response` | `str \| None` | The raw response from the LLM. `None` if no LLM was called or the run failed before reaching the LLM call. |
| `actions_taken` | `str \| None` | JSON-encoded description of any actions the hook performed as a result of the LLM response. |
| `skipped_reason` | `str \| None` | Explanation for why the run was skipped, if `status` is `"skipped"`. |
| `tokens_used` | `int` | Tokens consumed by the LLM call in this run. `0` if no LLM call was made. |
| `started_at` | `float` | Unix timestamp when the run began. |
| `completed_at` | `float \| None` | Unix timestamp when the run finished. `None` if still running. |

---

## 4. State Machine

### Overview

The state machine defines the only legal ways a task's `status` field may change. Each transition is triggered by a named event. Attempting a transition that is not listed here is an error (`InvalidTransition` exception).

The machine is defined as a lookup table keyed by `(current_status, event)` pairs. Given a current status and an event, there is at most one possible next status.

A secondary derived set, `VALID_STATUS_TRANSITIONS`, contains all `(from_status, to_status)` pairs reachable by any event. This allows callers to ask "is this status change ever legal?" without specifying a particular event.

```mermaid
stateDiagram-v2
    [*] --> DEFINED
    DEFINED --> READY : DEPS_MET / ADMIN_RESTART

    READY --> ASSIGNED : ASSIGNED

    ASSIGNED --> IN_PROGRESS : AGENT_STARTED
    ASSIGNED --> READY : EXECUTION_ERROR / RECOVERY
    ASSIGNED --> BLOCKED : TIMEOUT

    IN_PROGRESS --> COMPLETED : AGENT_COMPLETED
    IN_PROGRESS --> AWAITING_APPROVAL : PR_CREATED
    IN_PROGRESS --> AWAITING_PLAN_APPROVAL : PLAN_FOUND
    IN_PROGRESS --> BLOCKED : MERGE_FAILED / TIMEOUT / ADMIN_STOP / MAX_RETRIES
    IN_PROGRESS --> FAILED : AGENT_FAILED
    IN_PROGRESS --> PAUSED : TOKENS_EXHAUSTED
    IN_PROGRESS --> WAITING_INPUT : AGENT_QUESTION
    IN_PROGRESS --> READY : RETRY / RECOVERY

    AWAITING_APPROVAL --> COMPLETED : PR_MERGED
    AWAITING_APPROVAL --> BLOCKED : PR_CLOSED
    AWAITING_APPROVAL --> READY : ADMIN_RESTART

    FAILED --> READY : RETRY / ADMIN_RESTART
    FAILED --> BLOCKED : MAX_RETRIES
    FAILED --> COMPLETED : ADMIN_SKIP

    PAUSED --> READY : RESUME_TIMER / ADMIN_RESTART

    WAITING_INPUT --> IN_PROGRESS : HUMAN_REPLIED
    WAITING_INPUT --> PAUSED : INPUT_TIMEOUT
    WAITING_INPUT --> READY : ADMIN_RESTART

    BLOCKED --> READY : ADMIN_RESTART
    BLOCKED --> COMPLETED : ADMIN_SKIP

    COMPLETED --> READY : ADMIN_RESTART
```

---

### Core Lifecycle Transitions

These transitions represent the normal, happy-path progression of a task.

| From | Event | To | Notes |
|---|---|---|---|
| DEFINED | DEPS_MET | READY | All upstream dependencies completed. |
| READY | ASSIGNED | ASSIGNED | Scheduler selected an agent. |
| ASSIGNED | AGENT_STARTED | IN_PROGRESS | Agent process confirmed it started. |
| IN_PROGRESS | AGENT_COMPLETED | COMPLETED | Agent finished work successfully. |
| IN_PROGRESS | PR_CREATED | AWAITING_APPROVAL | A PR was opened; human review required. |
| AWAITING_APPROVAL | PR_MERGED | COMPLETED | Human merged the PR. |

---

### Failure and Retry Transitions

| From | Event | To | Notes |
|---|---|---|---|
| IN_PROGRESS | AGENT_FAILED | FAILED | Agent reported an error. |
| IN_PROGRESS | MERGE_FAILED | BLOCKED | Post-completion merge/rebase failed; task is blocked pending manual intervention. |
| FAILED | RETRY | READY | Under retry limit; re-queued for another attempt. |
| FAILED | MAX_RETRIES | BLOCKED | Retry limit exhausted; task is permanently stuck until admin action. |

---

### Pause and Resume Transitions

| From | Event | To | Notes |
|---|---|---|---|
| IN_PROGRESS | TOKENS_EXHAUSTED | PAUSED | Agent ran out of token budget; task waits with a `resume_after` timestamp. |
| IN_PROGRESS | AGENT_QUESTION | WAITING_INPUT | Agent asked a question; waiting for human reply via Discord. |
| WAITING_INPUT | HUMAN_REPLIED | IN_PROGRESS | Human answered; agent resumes. |
| WAITING_INPUT | INPUT_TIMEOUT | PAUSED | Human did not reply in time; task is paused. |
| PAUSED | RESUME_TIMER | READY | The `resume_after` time has passed; task is eligible to run again. |

---

### Direct Shortcut Transitions (from IN_PROGRESS)

These bypass the intermediate FAILED state for convenience in certain error scenarios.

| From | Event | To | Notes |
|---|---|---|---|
| IN_PROGRESS | MAX_RETRIES | BLOCKED | Task is blocked directly without going through FAILED first. |
| IN_PROGRESS | RETRY | READY | Task is re-queued directly without going through FAILED first. |

---

### Administrative Transitions

Administrative events allow a human operator to override the normal lifecycle.

| From | Event | To | Notes |
|---|---|---|---|
| BLOCKED | ADMIN_SKIP | COMPLETED | Admin declares task done, bypassing all verification. |
| FAILED | ADMIN_SKIP | COMPLETED | Admin declares task done, bypassing retry and verification. |
| IN_PROGRESS | ADMIN_STOP | BLOCKED | Admin forcibly terminates an in-progress task and blocks it. |
| BLOCKED | ADMIN_RESTART | READY | Admin manually resets a blocked task. |
| FAILED | ADMIN_RESTART | READY | Admin manually resets a failed task. |
| COMPLETED | ADMIN_RESTART | READY | Admin re-runs an already-completed task. |
| PAUSED | ADMIN_RESTART | READY | Admin clears the pause and immediately re-queues. |
| DEFINED | ADMIN_RESTART | READY | Admin bypasses the dependency check. |
| ASSIGNED | ADMIN_RESTART | READY | Admin unassigns and re-queues the task. |
| AWAITING_APPROVAL | ADMIN_RESTART | READY | Admin abandons the PR flow and restarts. |
| WAITING_INPUT | ADMIN_RESTART | READY | Admin cancels the pending question and restarts. |

Note: `IN_PROGRESS` is the only status from which `ADMIN_RESTART` is not a defined transition. An admin must use `ADMIN_STOP` first to move the task to BLOCKED, then `ADMIN_RESTART` to re-queue it.

---

### PR Lifecycle Transitions

| From | Event | To | Notes |
|---|---|---|---|
| AWAITING_APPROVAL | PR_CLOSED | BLOCKED | PR was closed without merging; task is blocked. |

---

### Error and Timeout Transitions

| From | Event | To | Notes |
|---|---|---|---|
| IN_PROGRESS | TIMEOUT | BLOCKED | Agent process stopped producing heartbeats within the expected window. |
| ASSIGNED | TIMEOUT | BLOCKED | Agent was assigned but never started and timed out. |
| ASSIGNED | EXECUTION_ERROR | READY | An error occurred before the agent even started; returned to queue for retry. |

---

### Daemon Recovery Transitions

These transitions fire automatically when the daemon process restarts and detects tasks left in an active state from a previous run.

| From | Event | To | Notes |
|---|---|---|---|
| IN_PROGRESS | RECOVERY | READY | Task was running when daemon died; reset to READY for reassignment. |
| ASSIGNED | RECOVERY | READY | Task was assigned when daemon died; reset to READY for reassignment. |

---

### Invariants

- Every transition has exactly one target status; there are no ambiguous outcomes.
- A task in COMPLETED or BLOCKED state cannot be moved anywhere except via explicit ADMIN events.
- A PAUSED task will always eventually become READY again automatically (via `RESUME_TIMER`) unless an admin intervenes.
- The state machine does not enforce side effects (e.g. clearing `assigned_agent_id`, setting `resume_after`). Those are handled by the orchestrator. The state machine's only responsibility is determining whether a transition is legal and what the resulting status is.

---

### Public API

**`task_transition(current: TaskStatus, event: TaskEvent) -> TaskStatus`**

Looks up the target status for a `(current_status, event)` pair in `VALID_TASK_TRANSITIONS`. Returns the resulting `TaskStatus` if the pair is defined. Raises `InvalidTransition` if no such transition exists.

**`is_valid_status_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool`**

Returns `True` if transitioning from `from_status` to `to_status` is covered by at least one event in the state machine (i.e. the pair exists in `VALID_STATUS_TRANSITIONS`). Does not require a specific event to be named.

**`InvalidTransition`**

Exception raised by `task_transition` when a `(status, event)` pair is not defined. Carries the originating `state` and `event` as attributes. Message format: `"Invalid transition: (STATUS_VALUE, EVENT_VALUE)"`.

---

## 5. DAG Validation

### Purpose

Tasks may declare dependencies on other tasks. Before a task can enter READY status (via `DEPS_MET`), all tasks it depends on must be in COMPLETED status. The dependency graph must never contain cycles — a task cannot (directly or indirectly) depend on itself.

### Graph Structure

The dependency graph is represented as a dictionary mapping each task ID to the set of task IDs it directly depends on: `dict[str, set[str]]`. The full set of nodes is the union of all keys and all values in this dictionary, ensuring that tasks which appear only as dependencies (not as dependents) are still included in cycle checks.

### Cycle Detection Algorithm

The system uses a depth-first search (DFS) with three-color node marking:

- **White (0):** Node has not been visited yet.
- **Gray (1):** Node is currently on the DFS call stack (being explored).
- **Black (2):** Node and all its descendants have been fully explored with no cycle found.

The algorithm visits every white node in the graph. For each node visited:

1. Mark it gray (in-progress).
2. For each dependency of this node:
   - If the dependency is gray, a cycle is detected. Raise `CyclicDependencyError` identifying the two nodes that form the back edge.
   - If the dependency is white, recurse into it.
3. Mark the node black (done).

If the traversal completes without encountering any gray-to-gray back edge, the graph is a valid DAG.

### CyclicDependencyError

When a cycle is detected, the system raises `CyclicDependencyError`. The error message includes the two node IDs that form the detected back edge in the format `"node_a -> node_b"`. If no cycle path information is available, the error message is simply `"Cyclic dependency detected"`.

### Validation Entry Points

**Full graph validation (`validate_dag`)**

Accepts the complete dependency map for a set of tasks and validates that the entire graph is a DAG. This is called when bulk-importing tasks or reconstructing the dependency graph from the database.

**Single-edge validation (`validate_dag_with_new_edge`)**

Accepts the existing dependency map plus a proposed new edge (a `task_id` that would depend on `depends_on`). It creates a copy of the existing graph, adds the new edge, and calls full graph validation on the copy. This allows the system to safely check whether adding a single new dependency would introduce a cycle before committing the change to the database.

**Contract:** Neither validation function modifies the graph it receives. `validate_dag_with_new_edge` always operates on a copy.

### Dependency Promotion Rule

A task transitions from DEFINED to READY when all task IDs in its dependency set have reached COMPLETED status. This check is performed independently of the DAG validation — the DAG validation runs at graph construction time to prevent invalid graphs from being stored, while the promotion check runs at runtime during each orchestrator tick to determine which tasks are now eligible to run.
