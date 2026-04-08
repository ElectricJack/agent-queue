---
tags: [spec, database]
---

# Database Specification

## 1. Overview

The `Database` class in `src/database.py` is the sole persistence layer for the Agent Queue system. It wraps an `aiosqlite` connection to a SQLite file on disk, exposed through async methods organized by domain (projects, repos, tasks, dependencies, agents, token ledger, task results, events, hooks, hook runs, system config, rate limits).

All database interaction is async. The `Database` object is constructed with a file path, then explicitly initialized with `initialize()` before use. A `row_factory` of `aiosqlite.Row` is applied so columns can be accessed by name. Every mutating method issues an explicit `await self._db.commit()` before returning. There is no connection pooling; one `aiosqlite.Connection` is held for the lifetime of the process.

The class uses a convention of thin `_row_to_<model>` private methods to map raw `aiosqlite.Row` objects into typed dataclass instances from `src/models.py` (see [[models-and-state-machine]]). Update methods accept arbitrary `**kwargs` and build parameterized `SET` clauses dynamically, converting enum values to their `.value` string automatically.

---

## Source Files
- `src/database.py`

---

## 2. Connection Management

### Construction

```python
db = Database(path="/path/to/agent_queue.db")
```

The constructor stores the file path and sets `self._db = None`. No connection is opened yet.

### Initialization

```python
await db.initialize()
```

Performs the following steps in order:

1. Opens a connection with `aiosqlite.connect(path)`.
2. Sets `row_factory = aiosqlite.Row` so all rows support column-name access.
3. Executes the full `SCHEMA` string via `executescript`, which creates all tables with `CREATE TABLE IF NOT EXISTS` (idempotent on existing databases).
4. Enables WAL journal mode: `PRAGMA journal_mode=WAL`.
5. Enables foreign key enforcement: `PRAGMA foreign_keys=ON`.
6. Runs a series of additive `ALTER TABLE` migrations (see Section 14). Each migration is wrapped in a bare `try/except` that silently swallows any exception, so a migration that fails because the column already exists is harmless.
7. Commits.

### Close

```python
await db.close()
```

Closes the connection if one is open. Safe to call even if `initialize()` was never called (checks `if self._db`).

---

## 3. Schema

All 18 tables are declared in the module-level `SCHEMA` string (the source code comment says "14 tables" — this is stale). Foreign key relationships are declared inline with `REFERENCES`. A `CHECK` constraint exists on `task_dependencies`. Integer booleans (SQLite has no native boolean) are used for `enabled` (hooks), `requires_approval` and `is_plan_subtask` (tasks). Timestamps are stored as `REAL` (Unix epoch, floating-point seconds).

### Table: `projects`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `name` | TEXT | NOT NULL | Human-readable project name |
| `credit_weight` | REAL | NOT NULL DEFAULT 1.0 | Scheduler weight |
| `max_concurrent_agents` | INTEGER | NOT NULL DEFAULT 2 | Cap on parallel agents |
| `status` | TEXT | NOT NULL DEFAULT 'ACTIVE' | One of: ACTIVE, PAUSED, ARCHIVED |
| `total_tokens_used` | INTEGER | NOT NULL DEFAULT 0 | Cumulative token counter |
| `budget_limit` | INTEGER | nullable | Max tokens allowed (NULL = unlimited) |
| `workspace_path` | TEXT | nullable | **Deprecated/unused.** Legacy column kept for backward compatibility; workspace paths are now managed via the `workspaces` table. |
| `discord_channel_id` | TEXT | nullable | Per-project Discord channel |
| `discord_control_channel_id` | TEXT | nullable | Legacy column (superseded by `discord_channel_id`); kept for backward compatibility |
| `repo_url` | TEXT | DEFAULT '' | Repository URL for the project (added via migration) |
| `repo_default_branch` | TEXT | DEFAULT 'main' | Default branch name (added via migration) |
| `default_profile_id` | TEXT | nullable REFERENCES agent_profiles(id) | Default agent profile (added via migration) |
| `created_at` | REAL | NOT NULL | Unix timestamp, set on insert |

No `updated_at` on projects. The `discord_control_channel_id` column exists for backward compatibility — `_row_to_project` falls back to it when `discord_channel_id` is NULL.

### Table: `repos`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Parent project |
| `url` | TEXT | NOT NULL | Git remote URL or empty string |
| `default_branch` | TEXT | NOT NULL DEFAULT 'main' | Branch used for cloning |
| `checkout_base_path` | TEXT | NOT NULL | Base directory for worktrees |
| `source_type` | TEXT | NOT NULL DEFAULT 'clone' | Added by migration; one of: clone, link, init |
| `source_path` | TEXT | NOT NULL DEFAULT '' | Added by migration; local filesystem path for `link`/`init` sources |

### Table: `tasks`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Human-readable adjective-noun ID |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | |
| `parent_task_id` | TEXT | nullable REFERENCES tasks(id) | Self-referential; for subtasks |
| `repo_id` | TEXT | nullable REFERENCES repos(id) | |
| `title` | TEXT | NOT NULL | Short display name |
| `description` | TEXT | NOT NULL | Full prompt/instructions for the agent |
| `priority` | INTEGER | NOT NULL DEFAULT 100 | Lower number = higher priority |
| `status` | TEXT | NOT NULL DEFAULT 'DEFINED' | See task state machine |
| `verification_type` | TEXT | NOT NULL DEFAULT 'auto_test' | One of: auto_test, qa_agent, human |
| `retry_count` | INTEGER | NOT NULL DEFAULT 0 | How many times this task has been retried |
| `max_retries` | INTEGER | NOT NULL DEFAULT 3 | |
| `assigned_agent_id` | TEXT | nullable REFERENCES agents(id) | Set when status = ASSIGNED or IN_PROGRESS |
| `branch_name` | TEXT | nullable | Git branch for this task's work |
| `resume_after` | REAL | nullable | Unix timestamp; PAUSED tasks resume after this |
| `requires_approval` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1); whether task requires manual approval before merge |
| `pr_url` | TEXT | nullable | GitHub/GitLab PR link |
| `plan_source` | TEXT | nullable | Path to the plan file that generated this task |
| `is_plan_subtask` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1); flags auto-generated plan subtasks |
| `task_type` | TEXT | nullable | Task type classification (added via migration) |
| `profile_id` | TEXT | nullable REFERENCES agent_profiles(id) | Agent profile for execution (added via migration) |
| `preferred_workspace_id` | TEXT | nullable REFERENCES workspaces(id) | Preferred workspace (added via migration) |
| `attachments` | TEXT | DEFAULT '[]' | JSON-encoded list of attachment paths/URLs (added via migration) |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

### Table: `task_criteria`

Acceptance criteria items for a task, stored as individual rows.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | |
| `type` | TEXT | NOT NULL | Category of criterion |
| `content` | TEXT | NOT NULL | Human-readable criterion text |
| `sort_order` | INTEGER | NOT NULL DEFAULT 0 | Display ordering |

No CRUD methods are implemented on `Database` for this table directly; it is populated and deleted as part of task creation/deletion.

### Table: `task_dependencies`

Directed edge: "`task_id` depends on `depends_on_task_id`" (i.e., `depends_on_task_id` must complete before `task_id` can become READY).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | The waiting task |
| `depends_on_task_id` | TEXT | NOT NULL REFERENCES tasks(id) | Must complete first |
| (composite PK) | | PRIMARY KEY (task_id, depends_on_task_id) | No duplicate edges |
| (check) | | CHECK (task_id != depends_on_task_id) | No self-dependencies |

### Table: `task_context`

Arbitrary context blobs attached to a task (e.g., file contents, URLs, notes).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | |
| `type` | TEXT | NOT NULL | Category string |
| `label` | TEXT | nullable | Human-readable label |
| `content` | TEXT | NOT NULL | The context data |

No CRUD methods on `Database` for this table directly.

### Table: `task_tools`

Tool configurations allowed for a task.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | |
| `type` | TEXT | NOT NULL | Tool type identifier |
| `config` | TEXT | NOT NULL | JSON configuration blob |

No CRUD methods on `Database` for this table directly.

### Table: `agents`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `name` | TEXT | NOT NULL | Display name |
| `agent_type` | TEXT | NOT NULL | e.g. "claude", "codex" |
| `state` | TEXT | NOT NULL DEFAULT 'IDLE' | One of: IDLE, BUSY, PAUSED, ERROR |
| `current_task_id` | TEXT | nullable REFERENCES tasks(id) | |
| `checkout_path` | TEXT | nullable | Filesystem path to the agent's worktree |
| `repo_id` | TEXT | nullable REFERENCES repos(id) | |
| `pid` | INTEGER | nullable | OS process ID of the agent subprocess |
| `last_heartbeat` | REAL | nullable | Unix timestamp of last liveness ping |
| `total_tokens_used` | INTEGER | NOT NULL DEFAULT 0 | Lifetime total |
| `session_tokens_used` | INTEGER | NOT NULL DEFAULT 0 | Current session total |
| `created_at` | REAL | NOT NULL | Set on insert |

### Table: `token_ledger`

Immutable append-only log of token usage events.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID (generated on insert) |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | |
| `agent_id` | TEXT | NOT NULL REFERENCES agents(id) | |
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | |
| `tokens_used` | INTEGER | NOT NULL | Tokens consumed in this event |
| `timestamp` | REAL | NOT NULL | Unix timestamp, set on insert |

No deletes on this table during normal operation. Deleted only as part of cascading `delete_project` or `delete_task`.

### Table: `events`

Audit log of system events (immutable append-only).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Auto-assigned integer |
| `event_type` | TEXT | NOT NULL | Arbitrary string, e.g. "task_assigned" |
| `project_id` | TEXT | nullable | May be NULL for system-level events |
| `task_id` | TEXT | nullable | |
| `agent_id` | TEXT | nullable | |
| `payload` | TEXT | nullable | Arbitrary string (JSON or plain text) |
| `timestamp` | REAL | NOT NULL | Unix timestamp |

No foreign key declarations despite the ID columns — these are soft references. Events are deleted only by cascading `delete_project`.

### Table: `rate_limits`

Tracks rolling-window token consumption for rate-limit enforcement.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `agent_type` | TEXT | NOT NULL | e.g. "claude" |
| `limit_type` | TEXT | NOT NULL | Category of limit |
| `max_tokens` | INTEGER | NOT NULL | Ceiling for this window |
| `current_tokens` | INTEGER | NOT NULL DEFAULT 0 | Consumed so far in this window |
| `window_start` | REAL | NOT NULL | Unix timestamp when window began |

No CRUD methods are defined on `Database` for this table; it is managed externally.

### Table: `task_results`

One row per agent execution attempt. A task that is retried accumulates multiple rows.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID (generated on insert) |
| `task_id` | TEXT | NOT NULL REFERENCES tasks(id) | |
| `agent_id` | TEXT | NOT NULL REFERENCES agents(id) | |
| `result` | TEXT | NOT NULL | AgentResult enum value: completed, failed, paused_tokens, paused_rate_limit |
| `summary` | TEXT | NOT NULL DEFAULT '' | Human-readable summary produced by agent |
| `files_changed` | TEXT | NOT NULL DEFAULT '[]' | JSON-encoded list of file paths |
| `error_message` | TEXT | nullable | Error detail if failed |
| `tokens_used` | INTEGER | NOT NULL DEFAULT 0 | Tokens consumed by this run |
| `created_at` | REAL | NOT NULL | Unix timestamp, set on insert |

### Table: `system_config`

Simple key-value store for system-wide configuration.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | Unique configuration key |
| `value` | TEXT | NOT NULL | Value as string |

No CRUD methods are defined on `Database` for this table in the current implementation.

### Table: `hooks`

Hook definitions — automated responses to events or time triggers.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | |
| `name` | TEXT | NOT NULL | Display name |
| `enabled` | INTEGER | NOT NULL DEFAULT 1 | Boolean (0/1) |
| `trigger` | TEXT | NOT NULL | JSON string, e.g. `{"type": "periodic", "interval_seconds": 7200}` |
| `context_steps` | TEXT | NOT NULL DEFAULT '[]' | JSON array of context-gathering step configs |
| `prompt_template` | TEXT | NOT NULL | Template string with `{{step_0}}`, `{{event}}` placeholders |
| `llm_config` | TEXT | nullable | JSON: `{"provider": "anthropic", "model": "..."}` |
| `cooldown_seconds` | INTEGER | NOT NULL DEFAULT 3600 | Minimum interval between runs |
| `max_tokens_per_run` | INTEGER | nullable | Per-run token cap (NULL = unlimited) |
| `last_triggered_at` | REAL | nullable | Unix timestamp of last trigger (added via migration) |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

### Table: `workspaces`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Parent project |
| `workspace_path` | TEXT | NOT NULL | Absolute filesystem path |
| `source_type` | TEXT | NOT NULL DEFAULT 'clone' | One of: clone, link, init |
| `name` | TEXT | NOT NULL DEFAULT '' | Human-readable workspace name |
| `locked_by_agent_id` | TEXT | nullable | Agent currently using this workspace |
| `locked_by_task_id` | TEXT | nullable | Task the workspace is locked for |
| `locked_at` | REAL | nullable | Unix timestamp of lock acquisition |
| `created_at` | REAL | NOT NULL | Set on insert |

UNIQUE constraint on `(project_id, workspace_path)`. Has extensive CRUD methods: `create_workspace`, `get_workspace`, `list_workspaces`, `delete_workspace`, `acquire_workspace`, `release_workspace`, `release_workspaces_for_agent`, `release_workspaces_for_task`, `get_workspace_for_task`, `get_project_workspace_path`, `count_available_workspaces`.

### Table: `agent_profiles`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `name` | TEXT | NOT NULL UNIQUE | Human-readable profile name |
| `description` | TEXT | NOT NULL DEFAULT '' | Profile description |
| `model` | TEXT | NOT NULL DEFAULT '' | LLM model identifier |
| `permission_mode` | TEXT | NOT NULL DEFAULT '' | Permission level |
| `allowed_tools` | TEXT | NOT NULL DEFAULT '[]' | JSON-encoded list of tool names |
| `mcp_servers` | TEXT | NOT NULL DEFAULT '{}' | JSON-encoded server configurations |
| `system_prompt_suffix` | TEXT | NOT NULL DEFAULT '' | Additional system prompt text |
| `install` | TEXT | NOT NULL DEFAULT '{}' | JSON-encoded install manifest |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

Full CRUD: `create_profile`, `get_profile`, `list_profiles`, `update_profile`, `delete_profile`.

### Table: `chat_analyzer_suggestions`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Auto-assigned |
| `project_id` | TEXT | NOT NULL | Project scope |
| `channel_id` | TEXT | NOT NULL | Discord channel |
| `suggestion_type` | TEXT | NOT NULL | Type of suggestion |
| `suggestion_text` | TEXT | NOT NULL | Suggestion content |
| `suggestion_hash` | TEXT | NOT NULL | Deduplication hash |
| `status` | TEXT | NOT NULL DEFAULT 'pending' | pending, resolved, dismissed |
| `created_at` | REAL | NOT NULL | Set on insert |
| `resolved_at` | REAL | nullable | When resolved/dismissed |
| `context_snapshot` | TEXT | nullable | JSON context at suggestion time |

Two indexes: on `(project_id, status)` and on `suggestion_hash`. Reused by the ChatObserver system.

### Table: `archived_tasks`

Mirrors the `tasks` table schema plus an `archived_at` REAL column. Stores tasks that have been archived (completed/failed tasks moved out of the active tasks table).

Methods: `archive_task`, `archive_completed_tasks`, `archive_old_terminal_tasks`, `list_archived_tasks`, `get_archived_task`, `restore_archived_task`, `delete_archived_task`, `count_archived_tasks`.

### Table: `hook_runs`

Execution log for each hook invocation.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `hook_id` | TEXT | NOT NULL REFERENCES hooks(id) | |
| `project_id` | TEXT | NOT NULL | Denormalized from hook for easier queries |
| `trigger_reason` | TEXT | NOT NULL | e.g. "periodic", "manual", "event:task_completed" |
| `event_data` | TEXT | nullable | JSON blob of the event that triggered the run |
| `context_results` | TEXT | nullable | JSON blob of gathered context |
| `prompt_sent` | TEXT | nullable | Resolved prompt string sent to LLM |
| `llm_response` | TEXT | nullable | Raw response from LLM |
| `actions_taken` | TEXT | nullable | JSON or text record of actions performed |
| `skipped_reason` | TEXT | nullable | Reason string if run was skipped (cooldown, etc.) |
| `tokens_used` | INTEGER | NOT NULL DEFAULT 0 | |
| `status` | TEXT | NOT NULL DEFAULT 'running' | One of: running, completed, failed, skipped |
| `started_at` | REAL | NOT NULL | |
| `completed_at` | REAL | nullable | NULL while running |

---

## 4. Projects

### `create_project(project: Project) -> None`

Inserts a new row into `projects`. The `created_at` value is always `time.time()` — the value on the `Project` dataclass is ignored. The `status` field is serialized from `ProjectStatus.value`. The `discord_control_channel_id` column is **not** written by this method (only `discord_channel_id` is). Commits after insert.

### `get_project(project_id: str) -> Project | None`

Selects by primary key. Returns `None` if not found. Delegates to `_row_to_project`.

### `list_projects(status: ProjectStatus | None = None) -> list[Project]`

Returns all projects, optionally filtered to a single status value. No ordering is applied.

### `update_project(project_id: str, **kwargs) -> None`

Dynamic `UPDATE` using keyword arguments as column-value pairs. `ProjectStatus` enum values are automatically converted to their `.value` string. There is no `updated_at` column on projects, so none is appended. Commits after update.

### `delete_project(project_id: str) -> None`

Performs a cascading delete of all data owned by the project, in this order:

1. Collects all `task_id` values for the project.
2. For each task: deletes rows from `task_results`, `task_dependencies` (both directions), `task_criteria`, `task_context`, `task_tools`.
3. Deletes all `hook_runs` for the project.
4. Deletes all `hooks` for the project.
5. Deletes all `token_ledger` entries for the project.
6. Deletes all `tasks` for the project.
7. Deletes all `chat_analyzer_suggestions` for the project.
8. Deletes all `workspaces` for the project.
9. Deletes all `repos` for the project.
10. Deletes all `events` for the project.
11. Deletes the `projects` row itself.
12. Commits.

### `_row_to_project(row) -> Project`

Private helper. Reads `discord_channel_id`; if that column is absent or NULL, falls back to `discord_control_channel_id`. Returns a `Project` dataclass instance. The `workspace_path` DB column is ignored (deprecated).

---

## 5. Repos

### `create_repo(repo: RepoConfig) -> None`

Inserts into `repos`, including the migration-added `source_type` and `source_path` columns. `source_type` is serialized from `RepoSourceType.value`. Commits.

### `get_repo(repo_id: str) -> RepoConfig | None`

Selects by primary key. Returns `None` if not found.

### `list_repos(project_id: str | None = None) -> list[RepoConfig]`

Returns all repos, optionally filtered by `project_id`. No ordering.

### `delete_repo(repo_id: str) -> None`

Deletes a single repo row. Does not cascade to tasks. Commits.

### `_row_to_repo(row) -> RepoConfig`

Reads `source_type` as a `RepoSourceType` enum (defaults to `RepoSourceType.CLONE` if NULL). Reads `source_path` with a `key in row.keys()` guard for backward compatibility.

---

## 6. Tasks

### `create_task(task: Task) -> None`

Inserts all task columns. Both `created_at` and `updated_at` are set to `time.time()` at insert time; the dataclass values are ignored. `status` and `verification_type` are serialized to their enum `.value`. `requires_approval` and `is_plan_subtask` are stored as integers (0/1) via `int()`. Commits.

### `get_task(task_id: str) -> Task | None`

Selects by primary key. Returns `None` if not found.

### `list_tasks(project_id: str | None = None, status: TaskStatus | None = None) -> list[Task]`

Returns tasks filtered by zero, one, or both of `project_id` and `status`. Always ordered by `priority ASC, created_at ASC` — lower priority numbers first, older tasks first within the same priority.

### `update_task(task_id: str, **kwargs) -> None`

Dynamic `UPDATE`. `TaskStatus` and `VerificationType` enum instances in kwargs are automatically serialized to `.value`. Always appends `updated_at = time.time()` to the SET clause. Commits.

### `transition_task(task_id: str, new_status: TaskStatus, *, context: str = "", **kwargs) -> None`

A validated wrapper around `update_task`. Behavior:

1. Fetches the current task. If the task does not exist, logs a warning and still calls `update_task` (optimistic behavior for race conditions).
2. If `current_status == new_status`, skips the state-machine check. If there are extra kwargs, applies them without a status change; otherwise does nothing.
3. Calls `is_valid_status_transition(current_status, new_status)`. If invalid, logs a warning with the optional `context` string. **The update is always applied regardless** — the state machine is advisory (logging-only), not enforced.
4. Calls `update_task(task_id, status=new_status, **kwargs)`.

### `delete_task(task_id: str) -> None`

Deletes a task and all its owned data in this order:

1. `task_results` where `task_id` matches.
2. `token_ledger` where `task_id` matches.
3. `task_dependencies` where the task appears on either side (`task_id = ?` OR `depends_on_task_id = ?`).
4. `task_criteria` where `task_id` matches.
5. `task_context` where `task_id` matches.
6. `task_tools` where `task_id` matches.
7. The `tasks` row itself.
8. Commits.

### `get_task_updated_at(task_id: str) -> float | None`

Returns only the `updated_at` REAL value for a task. Returns `None` if task not found. Avoids fetching the full row.

### `get_task_created_at(task_id: str) -> float | None`

Returns only the `created_at` REAL value for a task. Returns `None` if task not found.

### `get_subtasks(parent_task_id: str) -> list[Task]`

Returns all tasks whose `parent_task_id` matches the given value. No ordering guaranteed.

### `assign_task_to_agent(task_id: str, agent_id: str) -> None`

Atomic multi-table update (no explicit transaction — relies on SQLite's default serialized writes):

1. Validates the READY → ASSIGNED transition using `is_valid_status_transition`. If invalid, logs a warning (does not abort).
2. Updates the task: `status = ASSIGNED`, `assigned_agent_id = agent_id`, `updated_at = now`.
3. Updates the agent: `state = BUSY`, `current_task_id = task_id`.
4. Inserts an event row with `event_type = "task_assigned"`. The `project_id` is fetched inline via a subquery (`SELECT project_id FROM tasks WHERE id = ?`).
5. Commits.

### `_row_to_task(row) -> Task`

Private helper. Uses `key in row.keys()` guards for migration-added columns (`requires_approval`, `pr_url`, `plan_source`, `is_plan_subtask`) to handle databases that predate those migrations. `requires_approval` and `is_plan_subtask` are cast to `bool`.

---

## 7. Dependencies

### `add_dependency(task_id: str, depends_on: str) -> None`

Inserts a single directed edge `(task_id, depends_on_task_id)`. The composite primary key and `CHECK` constraint enforce no duplicates and no self-dependencies at the database level. Commits.

### `get_dependencies(task_id: str) -> set[str]`

Returns the set of all `depends_on_task_id` values for a given `task_id` (i.e., what this task is waiting on). Returns an empty set if there are no dependencies.

### `get_all_dependencies() -> dict[str, set[str]]`

Returns the entire dependency graph as a dictionary mapping each `task_id` to the set of all its `depends_on_task_id` values. Used by the orchestrator and DAG cycle detection.

### `are_dependencies_met(task_id: str) -> bool`

Determines whether a task is eligible for promotion from DEFINED to READY.

Logic: Performs a JOIN between `task_dependencies` and `tasks` to get the status of every upstream dependency for the given `task_id`. Returns `True` if and only if **all** upstream tasks have `status = 'COMPLETED'`. If the task has no dependencies (no rows in `task_dependencies`), the result is trivially `True` (vacuously all satisfied).

### `get_stuck_defined_tasks(threshold_seconds: int) -> list[Task]`

Returns DEFINED tasks that cannot make progress because at least one of their direct dependencies is in a terminal failure state (BLOCKED or FAILED).

Note: The `threshold_seconds` parameter is accepted but **not used** in the query. The method does not filter by age. The query uses a three-way JOIN: tasks (`status = DEFINED`) → `task_dependencies` → upstream tasks (`status IN (BLOCKED, FAILED)`). DISTINCT is applied to avoid duplicates when a task has multiple failed dependencies. Ordered by `created_at ASC`.

### `get_blocking_dependencies(task_id: str) -> list[tuple[str, str, str]]`

Returns a list of `(dep_task_id, dep_title, dep_status)` tuples for all unmet dependencies of a given task — i.e., dependencies whose status is NOT COMPLETED.

### `get_dependents(task_id: str) -> set[str]`

Reverse lookup: returns the set of `task_id` values that directly depend on the given `task_id`. Used to find tasks that may become promotable after a task completes.

### `remove_dependency(task_id: str, depends_on: str) -> None`

Removes a single edge from `task_dependencies` matching both `task_id` and `depends_on_task_id`. Commits.

### `remove_all_dependencies_on(depends_on_task_id: str) -> None`

Removes all edges in `task_dependencies` where `depends_on_task_id = ?`. Used when a task is being skipped/bypassed and its dependents should no longer wait for it. Commits.

---

## 8. Agents

### `create_agent(agent: Agent) -> None`

Inserts all agent columns. `created_at` is always `time.time()`. `state` is serialized from `AgentState.value`. Commits.

### `get_agent(agent_id: str) -> Agent | None`

Selects by primary key. Returns `None` if not found.

### `list_agents(state: AgentState | None = None) -> list[Agent]`

Returns all agents, optionally filtered to a single state. No ordering.

### `update_agent(agent_id: str, **kwargs) -> None`

Dynamic UPDATE. `AgentState` enum instances are automatically serialized to `.value`. Note: unlike `update_task`, this method does **not** automatically append an `updated_at` (there is no `updated_at` column on agents). Commits.

### `_row_to_agent(row) -> Agent`

Uses a `key in row.keys()` guard for `repo_id` for backward compatibility.

---

## 9. Token Ledger

### `record_token_usage(project_id: str, agent_id: str, task_id: str, tokens: int) -> None`

Appends one row to `token_ledger`. The `id` is a fresh UUID4. The `timestamp` is `time.time()`. Commits.

### `get_project_token_usage(project_id: str, since: float | None = None) -> int`

Returns the sum of `tokens_used` for a project, optionally restricted to entries with `timestamp >= since`. Uses `COALESCE(SUM(...), 0)` so it always returns an integer, never NULL.

---

## 10. Task Results

### `save_task_result(task_id: str, agent_id: str, output: AgentOutput) -> None`

Inserts one row into `task_results`. Fields come from the `AgentOutput` dataclass:

- `result` = `output.result.value` (AgentResult enum serialized to string)
- `summary` = `output.summary`
- `files_changed` = `json.dumps(output.files_changed)` (list serialized to JSON string)
- `error_message` = `output.error_message`
- `tokens_used` = `output.tokens_used`
- `id` = fresh UUID4; `created_at` = `time.time()`

Commits.

### `get_task_result(task_id: str) -> dict | None`

Returns the **most recent** result for a task, ordered by `created_at DESC LIMIT 1`. Returns `None` if no results. Returns a plain dict (not a dataclass).

### `get_task_results(task_id: str) -> list[dict]`

Returns **all** results for a task ordered by `created_at ASC` (oldest first). Useful for inspecting retry history. Each element is a plain dict.

### `_row_to_task_result(row) -> dict`

Returns a dict with keys: `id`, `task_id`, `agent_id`, `result`, `summary`, `files_changed` (parsed from JSON back to Python list), `error_message`, `tokens_used`, `created_at`.

---

## 11. Events

### `log_event(event_type, project_id=None, task_id=None, agent_id=None, payload=None) -> None`

Appends one row to `events`. All parameters except `event_type` are optional and nullable. `timestamp` is `time.time()`. The `id` column is `AUTOINCREMENT` and not supplied. Commits.

### `get_recent_events(limit: int = 50) -> list[dict]`

Returns the most recent events ordered by `id DESC` (most recent first), limited to `limit` rows. Returns plain dicts via `dict(row)` for all columns.

---

## 12. Hooks and Hook Runs

### Hooks

#### `create_hook(hook: Hook) -> None`

Inserts all hook columns. Both `created_at` and `updated_at` are set to `time.time()` at insert, ignoring the values on the `Hook` dataclass. `enabled` is stored as `int(hook.enabled)`. Commits.

#### `get_hook(hook_id: str) -> Hook | None`

Selects by primary key. Returns `None` if not found.

#### `list_hooks(project_id: str | None = None, enabled: bool | None = None) -> list[Hook]`

Returns hooks filtered by zero, one, or both of `project_id` and `enabled`. `enabled` is converted to `int` for comparison. No ordering.

#### `update_hook(hook_id: str, **kwargs) -> None`

Dynamic UPDATE. The `enabled` key is automatically converted to `int`. Always appends `updated_at = time.time()`. Commits.

#### `delete_hook(hook_id: str) -> None`

Deletes all `hook_runs` for the hook first, then deletes the hook row. Commits. (Manual cascade, since foreign keys are enabled.)

#### `_row_to_hook(row) -> Hook`

Maps row directly to `Hook` dataclass fields. `enabled` is cast to `bool`.

### Hook Runs

#### `create_hook_run(run: HookRun) -> None`

Inserts all columns from the `HookRun` dataclass verbatim (no timestamp overrides — caller sets `started_at` and `completed_at`). Commits.

#### `update_hook_run(run_id: str, **kwargs) -> None`

Dynamic UPDATE. No automatic `updated_at` or `completed_at` — caller must supply `completed_at` explicitly when finishing a run. Commits.

#### `get_last_hook_run(hook_id: str) -> HookRun | None`

Returns the most recent run for a hook ordered by `started_at DESC LIMIT 1`. Used by the hook engine to check cooldown. Returns `None` if no runs exist.

#### `list_hook_runs(hook_id: str, limit: int = 20) -> list[HookRun]`

Returns up to `limit` runs for a hook, ordered by `started_at DESC` (most recent first).

#### `_row_to_hook_run(row) -> HookRun`

Maps row directly to `HookRun` dataclass fields.

---

## 13. System Config

The `system_config` table (key TEXT PRIMARY KEY, value TEXT NOT NULL) is present in the schema but **no CRUD methods are implemented on the `Database` class**. The table is available for direct SQL access or future implementation.

---

## 14. Migration / Schema Evolution

The `initialize()` method applies a fixed list of additive `ALTER TABLE ... ADD COLUMN` statements after the initial schema creation. Each migration is attempted individually inside a bare `try/except Exception: pass` block — if the column already exists (or any other error occurs), the exception is silently swallowed and the next migration proceeds. This means migrations are always retried on every startup but are idempotent.

The full list of migrations applied in order:

| Statement | Effect |
|---|---|
| `ALTER TABLE projects ADD COLUMN workspace_path TEXT` | Legacy migration — column is now deprecated/unused (workspace paths managed via `workspaces` table) |
| `ALTER TABLE repos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'clone'` | Adds repo source type enum |
| `ALTER TABLE repos ADD COLUMN source_path TEXT NOT NULL DEFAULT ''` | Adds local path for linked/initialized repos |
| `ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0` | Adds approval requirement flag |
| `ALTER TABLE tasks ADD COLUMN pr_url TEXT` | Adds pull request URL field |
| `ALTER TABLE projects ADD COLUMN discord_channel_id TEXT` | Adds per-project Discord channel |
| `ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT` | Adds legacy control channel column |
| `ALTER TABLE tasks ADD COLUMN plan_source TEXT` | Adds path to originating plan file |
| `ALTER TABLE tasks ADD COLUMN is_plan_subtask INTEGER NOT NULL DEFAULT 0` | Flags auto-generated plan subtasks |
| `ALTER TABLE tasks ADD COLUMN task_type TEXT` | Adds task type classification |
| `ALTER TABLE projects ADD COLUMN repo_url TEXT DEFAULT ''` | Adds project-level repo URL |
| `ALTER TABLE projects ADD COLUMN repo_default_branch TEXT DEFAULT 'main'` | Adds project-level default branch |
| `ALTER TABLE tasks ADD COLUMN profile_id TEXT REFERENCES agent_profiles(id)` | Adds agent profile reference to tasks |
| `ALTER TABLE projects ADD COLUMN default_profile_id TEXT REFERENCES agent_profiles(id)` | Adds default profile to projects |
| `ALTER TABLE archived_tasks ADD COLUMN profile_id TEXT` | Mirrors profile_id on archived tasks |
| `ALTER TABLE tasks ADD COLUMN preferred_workspace_id TEXT REFERENCES workspaces(id)` | Adds preferred workspace to tasks |
| `ALTER TABLE archived_tasks ADD COLUMN preferred_workspace_id TEXT` | Mirrors preferred_workspace_id on archived tasks |
| `ALTER TABLE tasks ADD COLUMN attachments TEXT DEFAULT '[]'` | Adds attachments list to tasks |
| `ALTER TABLE archived_tasks ADD COLUMN attachments TEXT DEFAULT '[]'` | Mirrors attachments on archived tasks |
| `ALTER TABLE hooks ADD COLUMN last_triggered_at REAL` | Adds last trigger timestamp to hooks |

**Post-migration steps:**
- Two `CREATE INDEX IF NOT EXISTS` statements for `task_dependencies` (on `depends_on_task_id` and `task_id`).
- `_migrate_repos_to_projects()` — copies repo URL/branch into project columns.
- `_normalize_workspace_paths()` — resolves relative paths, removes cross-project duplicates.
- `_drop_legacy_agent_workspaces()` — drops the legacy `agent_workspaces` table.

The `SCHEMA` constant includes migrated columns for `projects` and `tasks`, so those tables have all columns from the start on fresh databases. However, the `repos` table in `SCHEMA` does **not** include `source_type` or `source_path` — those two columns are only added via the migration statements, meaning fresh databases also require the migrations to be run for `repos` to have those columns. Migrations always matter for `repos` regardless of whether the database is new or existing.

There is no version table, no migration registry, and no rollback capability. Destructive schema changes (DROP COLUMN, column renames, type changes) are not handled by this mechanism.

---

## 15. Undocumented Methods

> The following method groups exist in the implementation but are not yet fully
> documented in this spec.

### Tasks (additional)
- `list_active_tasks()` — tasks in non-terminal status
- `list_active_tasks_all_projects()` — cross-project active task listing
- `count_tasks_by_status()` — aggregate task counts
- `add_task_context()` / `get_task_contexts()` — CRUD for task_context table
- `get_task_tree()` — hierarchical subtask tree
- `get_parent_tasks()` — ancestor chain for a task

### Dependencies (additional)
- `get_dependency_map_for_tasks()` — batch dependency fetcher

### Agents (additional)
- `delete_agent()` — cascading delete with workspace lock release

### Workspaces (11 methods)
- `create_workspace`, `get_workspace`, `list_workspaces`, `delete_workspace`
- `acquire_workspace`, `release_workspace`, `release_workspaces_for_agent`, `release_workspaces_for_task`
- `get_workspace_for_task`, `get_project_workspace_path`, `count_available_workspaces`

### Agent Profiles (5 methods)
- `create_profile`, `get_profile`, `list_profiles`, `update_profile`, `delete_profile`

### Archived Tasks (8 methods)
- `archive_task`, `archive_completed_tasks`, `archive_old_terminal_tasks`
- `list_archived_tasks`, `get_archived_task`, `restore_archived_task`
- `delete_archived_task`, `count_archived_tasks`

### Chat Analyzer Suggestions (~10 methods)
- Suggestion CRUD, status updates, deduplication queries

### Hooks (additional)
- `list_hooks_by_id_prefix()`, `delete_hooks_by_id_prefix()`

### Repos (additional)
- `update_repo()` — update repo fields
