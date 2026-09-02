---
tags: [spec, database]
---

# Database Specification

## 1. Overview

The `Database` class in `src/database.py` is the sole persistence layer for the Agent Queue system. It wraps an `aiosqlite` connection to a SQLite file on disk, exposed through async methods organized by domain (projects, repos, tasks, dependencies, agents, token ledger, task results, events, hooks, hook runs, system config, rate limits).

All database interaction is async. The `Database` object is constructed with a file path, then explicitly initialized with `initialize()` before use. A `row_factory` of `aiosqlite.Row` is applied so columns can be accessed by name. Every mutating method issues an explicit `await self._db.commit()` before returning. There is no connection pooling; one `aiosqlite.Connection` is held for the lifetime of the process.

The class uses a convention of thin `_row_to_<model>` private methods to map raw `aiosqlite.Row` objects into typed dataclass instances from `src/models.py` (see [[specs/models-and-state-machine]]). Update methods accept arbitrary `**kwargs` and build parameterized `SET` clauses dynamically, converting enum values to their `.value` string automatically.

---

## Source Files
- `src/database/tables.py` — SQLAlchemy Core `Table` definitions (the schema)
- `src/database/engine.py` — engine factory, PRAGMAs, Alembic startup upgrade
- `src/database/adapters/sqlite.py`, `adapters/postgresql.py` — backends
- `src/database/queries/` — domain query mixins
- `migrations/` — Alembic revision history

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

Every table is declared as a SQLAlchemy Core `Table` in `src/database/tables.py`, which is the single source of truth; DDL is applied by Alembic (`migrations/`). Foreign keys are declared with `ForeignKey(...)`. A `CHECK` constraint exists on `task_dependencies`. Integer booleans (SQLite has no native boolean) are used for flags such as `is_plan_subtask` and `is_blocked` (tasks). Timestamps are stored as `REAL` (Unix epoch, floating-point seconds).

> **This catalog is enforced.** `tests/test_docs_sync.py` compares the `### Table:` headings below against `src/database/tables.py` and fails when they drift, so a schema change lands with its doc row in the same commit (see `docs/specs/design/trust-and-ops.md` §6). `alembic_version` is the one deliberate exclusion.

### Table: `agent_questions`

Durable questions raised by worker turns. Session identity, instance token and claim epoch fence answers to the originating task attempt; human-only questions remain human-only. Notification retry and delivery-lease fields prevent repeated or stale delivery.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `session_id` | TEXT | NOT NULL |
| `session_name` | TEXT | NOT NULL |
| `instance_token` | TEXT | NOT NULL |
| `task_id` | TEXT | NOT NULL |
| `project_id` | TEXT | NOT NULL |
| `agent_id` | TEXT | NOT NULL |
| `turn_id` | TEXT | NOT NULL |
| `claim_epoch` | INTEGER | NOT NULL |
| `question` | TEXT | NOT NULL |
| `requires_human` | BOOLEAN | NOT NULL |
| `state` | TEXT | NOT NULL |
| `answer` | TEXT | nullable |
| `answered_by` | TEXT | nullable |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL |
| `source_ts` | FLOAT | NOT NULL |
| `discord_channel_id` | TEXT | nullable |
| `discord_message_id` | TEXT | nullable |
| `supervisor_routed_at` | FLOAT | nullable |
| `notification_next_at` | FLOAT | NOT NULL DEFAULT 0 |
| `notification_attempts` | INTEGER | NOT NULL DEFAULT 0 |
| `delivery_token` | TEXT | nullable |
| `delivery_lease_until` | FLOAT | nullable |
| `delivered_at` | FLOAT | nullable |
| `reason` | TEXT | nullable |

Indexes: `idx_agent_questions_pending` (`state`, `created_at`), `idx_agent_questions_session` (`session_id`, `instance_token`).

### Table: `message_discord_receipts`

Records successful Discord delivery per AQ message. The message ID is the primary key so repeated event processing does not repost an acknowledged reply.

| Column | Type | Constraints |
|---|---|---|
| `message_id` | TEXT | PRIMARY KEY |
| `discord_channel_id` | TEXT | nullable |
| `discord_message_id` | TEXT | nullable |

### Table: `task_comments`

Append-only authored task feedback. The task ID plus project ID is a logical reference so comments survive archiving and restoration; permanent task or project deletion removes only that project's comments. Nullable project IDs preserve legacy comments with ambiguous ownership; these rows remain hidden instead of being attributed to either project. Author identity comes from the authenticated request scope. Bodies must contain 1–16000 characters, and author_kind is user, agent or supervisor.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `task_id` | TEXT | NOT NULL |
| `project_id` | TEXT | nullable for unresolved legacy ownership; internal only |
| `body` | TEXT | NOT NULL |
| `author_kind` | TEXT | NOT NULL |
| `author_id` | TEXT | NOT NULL |
| `created_at` | FLOAT | NOT NULL |

Indexes: `idx_task_comments_task_created` (`task_id`, `created_at`, `id`), `idx_task_comments_project_created` (`task_id`, `project_id`, `created_at`, `id`).

Authorized project moves transfer known active-task comment ownership in the same transaction. Moves that would merge a source or destination archive identity, and archival over a different-project ID, are refused without modifying either history.

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
| `assignment_playbook_id` | TEXT | nullable | Assignment-routing playbook selected for the project; NULL uses the bundled system default. Added by Alembic `a7c91e4d2b63` |
| `integration_mode` | TEXT | nullable | Project-level integration policy: `'direct'`, `'pull_request'`, or NULL (fall through to config `integration.default_mode`). Added by Alembic `c4d5e6f7a8b9` |
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
| `integration_mode` | TEXT | nullable | Integration policy override: `'direct'`, `'pull_request'`, or NULL (inherit from parent/project/config `integration.default_mode`). Replaces the dropped `requires_approval` flag (Alembic `c4d5e6f7a8b9` backfilled `1`→`'pull_request'`, `0`→`'direct'`) |
| `pr_url` | TEXT | nullable | GitHub/GitLab PR link |
| `plan_source` | TEXT | nullable | Path to the plan file that generated this task |
| `is_plan_subtask` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1); flags auto-generated plan subtasks |
| `task_type` | TEXT | nullable | Task type classification (added via migration) |
| `profile_id` | TEXT | nullable REFERENCES agent_profiles(id) | Agent profile for execution (added via migration) |
| `preferred_workspace_id` | TEXT | nullable REFERENCES workspaces(id) | Preferred workspace (added via migration) |
| `attachments` | TEXT | DEFAULT '[]' | JSON-encoded list of attachment paths/URLs (added via migration) |
| `next_child_ordinal` | INTEGER | NOT NULL DEFAULT 1 | Per-parent counter for dotted child ids (swarm-work-model §4, §6); incremented atomically by `task_names.reserve_child_ordinal`; never read for anything else |
| `created_by_kind` | TEXT | nullable | Provenance (swarm-work-model §9): who created the row; stamped by `CommandHandler.execute` from the request scope (Plan 2); nullable so rows from legacy paths stay valid |
| `created_by_id` | TEXT | nullable | Provenance (swarm-work-model §9), paired with `created_by_kind` |
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
| `dep_type` | TEXT | NOT NULL DEFAULT `'blocks'` | Edge type: `blocks`, `parent-child`, `waits-for`, `conditional-blocks`, `discovered-from`, `related`, `duplicates`, `supersedes`. Part of the PK, so one pair of tasks may carry several differently-typed edges. |
| (composite PK) | | PRIMARY KEY (task_id, depends_on_task_id, dep_type) | No duplicate edges *of the same type* |
| (check) | | CHECK (task_id != depends_on_task_id) | No self-dependencies |
| (check) | | CHECK `ck_task_deps_dep_type` on `dep_type` | Only the eight known types |

Partial unique index `uq_task_deps_single_parent` on `task_id` where
`dep_type = 'parent-child'` (swarm-work-model §4): enforces exactly one
parent per task. Created by migration revision `b2c3d4e5f6a7` after the
existing data is canonicalised to satisfy it.

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
| `profile_id` | TEXT | NOT NULL | Soft reference to `agent_profiles.id`; selects model, tools and runtime |
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
| `tokens_used` | INTEGER | NOT NULL | Tokens consumed in this event (authoritative total) |
| `model` | TEXT | nullable | Model that consumed the tokens; NULL for writers that don't report it |
| `input_tokens` | INTEGER | nullable | Input half of the split; NULL when unknown |
| `output_tokens` | INTEGER | nullable | Output half of the split; NULL when unknown |
| `timestamp` | REAL | NOT NULL | Unix timestamp, set on insert |

The three pricing columns are nullable by design: rows written before they existed cannot be priced accurately, so `get_cost_rollup` / `aq costs` report them as `unpriced_tokens` rather than pricing them at a guessed rate (`docs/specs/design/trust-and-ops.md` §7).

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

### Table: `task_metadata`

Free-form per-task key/value store. Used for values that don't warrant a column
(workflow bookkeeping, adapter hints). Notably `container = "true"`, set on a
task the first time it gains a child and never cleared — marks the task as a
hierarchy container (swarm-work-model §4, §7).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY REFERENCES tasks(id) | Composite PK part 1 |
| `key` | TEXT | PRIMARY KEY | Composite PK part 2 |
| `value` | TEXT | NOT NULL | Stored as text; JSON when structured |

### Table: `task_labels`

Many-to-many tags on tasks.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY REFERENCES tasks(id) | Composite PK part 1 |
| `label` | TEXT | PRIMARY KEY | Composite PK part 2 |

### Table: `hierarchy_migration_rejects`

Preflight/reject log for the swarm-work-model hierarchy migration (revision B):
one row per task the canonicalisation step could not cleanly assign a single
parent-child edge to. Never written outside the migration.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Row id |
| `run_id` | TEXT | NOT NULL | Groups rows from one migration run |
| `task_id` | TEXT | NOT NULL | The task the rejection is about |
| `parent_id` | TEXT | NULL | Candidate parent, if any |
| `source` | TEXT | NOT NULL | `duplicate_edge` \| `column_only` \| `edge` |
| `reason` | TEXT | NOT NULL | `cross_project` \| `cycle` \| `depth` \| `not_found` \| `duplicate` |
| `detail` | TEXT | NULL | Free-text explanation |
| `created_at` | FLOAT | NOT NULL | Unix timestamp |

### Table: `gates`

Human-in-the-loop decision points. A gate is opened by a playbook or workflow and
blocks progress until it is resolved (principle #5 — human judgment stays human).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `gate_type` | TEXT | NOT NULL | Kind of decision being requested |
| `title` | TEXT | NOT NULL | Short display name |
| `question` | TEXT | NOT NULL DEFAULT '' | Prompt shown to the human |
| `await_id` | TEXT | nullable | Correlates the gate with the waiter that opened it |
| `timeout_at` | REAL | nullable | Unix timestamp; NULL = waits indefinitely |
| `status` | TEXT | NOT NULL DEFAULT 'open' | One of: open, resolved, expired, cancelled |
| `resolved_by` | TEXT | nullable | Identity that resolved the gate |
| `resolution` | TEXT | nullable | The decision recorded |
| `created_at` | REAL | NOT NULL | Set on insert |

### Table: `task_gates`

Join table binding tasks to the gates that block them.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY REFERENCES tasks(id) | Composite PK part 1 |
| `gate_id` | TEXT | PRIMARY KEY REFERENCES gates(id) | Composite PK part 2 |

### Table: `workspace_kinds`

Typed workspace definitions (Workspaces v2). Rows are projected from markdown in
`vault/[projects/<pid>/]workspace-kinds/<id>.md`; the vault file is the source of
truth and this table is the queryable copy. `project_id` holds the system scope
sentinel for system-wide kinds.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY | Composite PK part 1; system scope sentinel for global kinds |
| `id` | TEXT | PRIMARY KEY | Composite PK part 2; kind id, e.g. `project-repo`, `vault` |
| `description` | TEXT | NOT NULL DEFAULT '' | Prose from the markdown body |
| `writable` | INTEGER | NOT NULL DEFAULT true | Boolean (0/1) |
| `lockable` | INTEGER | NOT NULL DEFAULT true | Boolean (0/1); unlockable kinds need no lease |
| `is_git_repo` | INTEGER | NOT NULL DEFAULT true | Boolean (0/1) |
| `repo_url` | TEXT | nullable | Clone source when the kind is a repo |
| `default_lock_mode` | TEXT | nullable | Lock granularity when lockable |
| `auto_attach` | INTEGER | NOT NULL DEFAULT false | Boolean (0/1); attached without being declared |
| `mode` | TEXT | NOT NULL DEFAULT 'worktree' | Acquisition mode, e.g. worktree, clone, readonly |
| `worktree_setup` | TEXT | NOT NULL DEFAULT '[]' | JSON array of setup commands — **operator-authored, trusted** |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

### Table: `task_workspace_requirements`

Per-task declaration of which workspace kinds a task needs. The orchestrator
acquires one workspace per declared kind, all-or-nothing, in canonical lock order.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY REFERENCES tasks(id) | Composite PK part 1 |
| `kind_id` | TEXT | PRIMARY KEY | Composite PK part 2; references `workspace_kinds.id` |
| `position` | INTEGER | PRIMARY KEY DEFAULT 0 | Composite PK part 3; allows two of the same kind |
| `alias` | TEXT | nullable | Name the task uses to refer to this attachment |

### Table: `task_proposals`

A batch of tasks and dependency edges proposed as one reviewable graph, before
anything exists in the live work graph. Written by spec ingest and by agents via
`task_batch_propose`; `task_batch_commit` materialises the whole batch atomically
and flips `status` to `committed` in a single conditional update, so two
concurrent commits cannot both win.

`payload` is a JSON blob rather than normalised rows on purpose: a proposal is
reviewed and committed or discarded as a unit, never queried edge-by-edge, and
its tasks do not have real ids until the commit creates them.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `"prop-"` + uuid4[:12] |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `source` | TEXT | NOT NULL | Provenance, e.g. `spec:projects/foo/specs/2026-08-21-thing.md`; stamped onto every task the commit creates |
| `payload` | TEXT | NOT NULL | JSON: `{"tasks":[{tempId,title,description,priority?},...], "edges":[{from,to,dep_type},...]}` |
| `status` | TEXT | NOT NULL DEFAULT 'draft' | CHECK `ck_task_proposals_status`: draft, ready, committed, discarded |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

Index: `idx_task_proposals_project_status` on (`project_id`, `status`) — the
review surface lists pending proposals per project.

### Table: `merge_slots`

One row per project — the mutex that serialises merges into the default branch.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY REFERENCES projects(id) | One slot per project |
| `holder_task_id` | TEXT | nullable | Task currently holding the slot; NULL = free |
| `acquired_at` | REAL | nullable | Unix timestamp of acquisition |
| `expires_at` | REAL | nullable | Lease expiry, so a crashed holder can't hold forever |
| `updated_at` | REAL | NOT NULL | Set on every state change |

### Table: `sessions`

Agent session rows (session-runtime). One row per launched harness session.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Session id |
| `task_id` | TEXT | nullable REFERENCES tasks(id) | NULL for non-task sessions |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `profile_id` | TEXT | NOT NULL | Profile the session runs under |
| `harness` | TEXT | NOT NULL | Harness id from the vault |
| `provider` | TEXT | NOT NULL | Underlying provider/runtime |
| `name` | TEXT | NOT NULL | Human-readable session name |
| `lifecycle` | TEXT | NOT NULL | Lifecycle class (e.g. per-task, long-lived) |
| `state` | TEXT | NOT NULL DEFAULT 'starting' | Observed state — the runtime projection |
| `desired_state` | TEXT | NOT NULL DEFAULT 'running' | Intent the reconciler converges toward: `running`/`sleeping`/`stopped` |
| `session_key` | TEXT | nullable | Exact harness conversation ID used to find or resume its transcript |
| `work_dir` | TEXT | NOT NULL | Working directory (an isolated worktree for task sessions) |
| `epoch` | TEXT | NOT NULL | Restart generation marker |
| `instance_token` | TEXT | NOT NULL | Identifies this process instance |
| `started_at` | REAL | NOT NULL | Set on insert |
| `last_activity` | REAL | nullable | Updated from transcript/pane activity |
| `restarts` | INTEGER | NOT NULL DEFAULT 0 | Restart counter |
| `quarantined_at` | REAL | nullable | Set when the session is quarantined after repeated failure |
| `sleep_reason` | TEXT | nullable | Why the session is idle/asleep |
| `ended_at` | REAL | nullable | Observed end time; unknown for legacy sessions |
| `end_reason` | TEXT | nullable | Specific exit, stop, quarantine or sleep reason |
| `hooks_provisioned` | BOOLEAN | NOT NULL DEFAULT 0 | Whether this launch wired the harness's subagent hooks; written once from the SessionSpec, never re-derived |

### Table: `task_session_attempts`

Durable task/session associations. Created atomically with task-session insertion
or a pool claim; finished on claim release or an observed session exit. References
are logical IDs without foreign keys so archival and session or agent deletion do
not erase execution history. Agent name and launch settings are snapshots.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Attempt ID, distinct across retries and pool claims |
| `session_id`, `task_id` | TEXT | NOT NULL | Stable logical references |
| `project_id`, `agent_id`, `agent_name` | TEXT | nullable | Attribution snapshots |
| `profile_id`, `name`, `lifecycle` | TEXT | NOT NULL | Session launch metadata |
| `model`, `intelligence_class`, `llm_provider` | TEXT | nullable | Effective launch settings |
| `harness`, `provider` | TEXT | NOT NULL | Harness and runtime |
| `state` | TEXT | NOT NULL | Attempt state; released claims are stopped |
| `work_dir` | TEXT | NOT NULL | Workspace at assignment |
| `started_at` | REAL | NOT NULL | Launch time for task sessions, assignment time for pool claims |
| `session_started_at` | REAL | NOT NULL | Original process launch time, used for transcript discovery |
| `ended_at`, `end_reason` | REAL, TEXT | nullable | Known release/exit time and reason |
| `outcome` | TEXT | nullable | Accepted task-close outcome |
| `session_key` | TEXT | nullable | Exact harness conversation ID |

Indexes cover (`task_id`, `started_at`) and (`session_id`, `started_at`). The legacy
migration imports only associations still present in `sessions` whose project and
assignment time match the current task incarnation (or archived task if no current
row exists). Known terminal `sleep_reason` values are retained as `end_reason`; it does not
invent missing prior pool claims, exit times or task outcomes. Reading a legacy
ended attempt may compute `transcript_end_at` from the next known launch sharing
its conversation or workspace. This is a read boundary, not a stored exit time.
The task history API filters by the resolved task's project and creation time; older
audit associations stay stored and remain addressable by attempt ID.
The SQLite-to-PostgreSQL copy inventory includes this audit table.

### Table: `subagent_events`

Append-only native sub-agent telemetry. A harness `SubagentStart` / `SubagentStop`
hook reports a fact about a moment; "how many children is this session running"
is a fold over those facts (`src/database/queries/subagent_queries.py`), so a
re-delivered hook or a lost `stop` cannot corrupt a counter permanently. The
primary key is a digest of (`session_id`, `event`, `subagent_id`), which makes a
duplicate delivery a no-op. References are logical IDs without foreign keys so the
audit trail survives session, task and project deletion.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | SHA-256 of (session_id, event, subagent_id) — idempotent insert |
| `session_id` | TEXT | NOT NULL | Parent session that spawned the child; indexed with `event` |
| `harness` | TEXT | NOT NULL | Harness that fired the hook (e.g. `claude`, `codex`) |
| `project_id` | TEXT | nullable | Owning project, when the hook reports one |
| `task_id` | TEXT | nullable | Task the parent session was running, when known |
| `subagent_id` | TEXT | NOT NULL | Harness's own id for the child; sent on both halves, which is what makes the pairing exact |
| `agent_type` | TEXT | nullable | Sub-agent type reported by the harness |
| `turn_id` | TEXT | nullable | Parent turn the child was spawned from |
| `event` | TEXT | NOT NULL | CHECK IN ('start', 'stop') — the two halves of one child's lifetime |
| `occurred_at` | REAL | NOT NULL | Daemon clock at hook receipt; indexed |

Indexes cover (`session_id`, `event`) for the per-session fold and (`occurred_at`)
for time-ordered listing. The fold clamps at zero: a `stop` whose `start` never
arrived is still stored, because losing a Start must not make a session look like
it is running a child forever.
The SQLite-to-PostgreSQL copy inventory includes this table.

### Table: `metrics_samples`

Fleet Metrics tab time-series buckets. Each row stores one JSON metric sample
for a resolution and bucket timestamp; keeping the payload dict-shaped permits
new per-harness, profile, and model series without a schema migration. The
unique bucket constraint makes sampling and roll-up writes idempotent after a
duplicate tick or restart.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY, autoincrement | Sample row identifier |
| `resolution` | TEXT | NOT NULL | Retention tier and bucket step: `1s`, `1m`, or `1h` |
| `bucket_ts` | FLOAT | NOT NULL | Unix timestamp floored to the resolution |
| `payload` | TEXT | NOT NULL | JSON-encoded metric sample body |

Unique constraint `uq_metrics_samples_bucket` covers (`resolution`,
`bucket_ts`). Index `idx_metrics_samples_res_ts` covers (`resolution`,
`bucket_ts`) for ordered range reads.

### Table: `messages`

Inter-agent message queue (supervisor/agent messaging).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `from_kind` | TEXT | NOT NULL | Sender class (agent, supervisor, human, system) |
| `from_id` | TEXT | NOT NULL | Sender identifier |
| `to_kind` | TEXT | NOT NULL | Recipient class |
| `to_id` | TEXT | NOT NULL | Recipient identifier |
| `thread_id` | TEXT | nullable | Groups a conversation |
| `subject` | TEXT | nullable | Short header |
| `body` | TEXT | NOT NULL | Message text — **untrusted** (agent/human authored) |
| `priority` | INTEGER | NOT NULL DEFAULT 100 | Lower number = higher priority |
| `created_at` | REAL | NOT NULL | Set on insert |
| `delivered_at` | REAL | nullable | Set when injected into a recipient's context |
| `read_at` | REAL | nullable | Set when the recipient acknowledges |
| `archive_after_inject` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1) |
| `archived_at` | REAL | nullable | Set when archived |
| `reply_to_id` | TEXT | nullable REFERENCES messages(id) | Self-referential reply chain |
| `via` | TEXT | nullable | Delivery channel used |

### Table: `api_session_tokens`

Task-scoped API tokens minted for agent sessions. Only the hash is stored — the
plaintext token exists once, at mint time, and is injected into the session
environment (`AQ_API_TOKEN`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `token_hash` | TEXT | PRIMARY KEY | Hash of the token; the plaintext is never persisted |
| `session_id` | TEXT | NOT NULL | Session the token was minted for |
| `task_id` | TEXT | nullable | Task scope, when the token is task-scoped |
| `project_id` | TEXT | nullable | Project scope |
| `created_at` | REAL | NOT NULL | Set on insert |
| `expires_at` | REAL | NOT NULL | Hard expiry |
| `revoked_at` | REAL | nullable | Set when explicitly revoked before expiry |

### Table: `project_constraints`

Per-project scheduling constraints, one row per project.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY REFERENCES projects(id) | One row per project |
| `exclusive` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1); project runs alone when set |
| `max_agents_by_type` | TEXT | NOT NULL DEFAULT '{}' | JSON map of agent type → cap |
| `pause_scheduling` | INTEGER | NOT NULL DEFAULT 0 | Boolean (0/1) |
| `created_by` | TEXT | nullable | Who set the constraint |
| `created_at` | REAL | NOT NULL | Set on insert |

### Table: `plugins`

Installed plugin registry.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Plugin name |
| `version` | TEXT | NOT NULL DEFAULT '0.0.0' | Installed version |
| `source_url` | TEXT | NOT NULL DEFAULT '' | Git remote it was installed from |
| `source_rev` | TEXT | NOT NULL DEFAULT '' | Pinned revision |
| `source_branch` | TEXT | NOT NULL DEFAULT '' | Tracked branch |
| `install_path` | TEXT | NOT NULL DEFAULT '' | Filesystem location of the clone |
| `status` | TEXT | NOT NULL DEFAULT 'installed' | One of: installed, enabled, disabled, error |
| `config` | TEXT | NOT NULL DEFAULT '{}' | JSON plugin config |
| `permissions` | TEXT | NOT NULL DEFAULT '[]' | JSON array of granted permissions |
| `error_message` | TEXT | nullable | Last load/install error |
| `installed_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

### Table: `plugin_data`

Per-plugin key/value persistence. Scoped by `plugin_id` so plugins cannot read
each other's data.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `plugin_id` | TEXT | PRIMARY KEY REFERENCES plugins(id) | Composite PK part 1 |
| `key` | TEXT | PRIMARY KEY | Composite PK part 2 |
| `value` | TEXT | NOT NULL DEFAULT '{}' | JSON value |
| `updated_at` | REAL | NOT NULL | Set on every write |

### Table: `playbook_runs`

One row per playbook execution. Playbooks replaced the removed `hooks` /
`hook_runs` tables.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `run_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook that was run |
| `playbook_version` | INTEGER | NOT NULL | Compiled version, so a run is reproducible |
| `trigger_event` | TEXT | NOT NULL DEFAULT '{}' | JSON of the event that started the run |
| `status` | TEXT | NOT NULL DEFAULT 'running' | One of: running, paused, completed, failed |
| `current_node` | TEXT | nullable | Node the run is sitting on |
| `conversation_history` | TEXT | NOT NULL DEFAULT '[]' | JSON transcript |
| `node_trace` | TEXT | NOT NULL DEFAULT '[]' | JSON list of visited nodes |
| `tokens_used` | INTEGER | NOT NULL DEFAULT 0 | Run token total |
| `started_at` | REAL | NOT NULL | Set on insert |
| `completed_at` | REAL | nullable | NULL while running |
| `error` | TEXT | nullable | Failure detail |
| `pinned_graph` | TEXT | nullable | JSON of the compiled graph used by this run |
| `paused_at` | REAL | nullable | Set when the run pauses on a human/event wait |
| `waiting_for_event` | TEXT | nullable | Event type the run is waiting for |

### Table: `playbook_artifacts`

One row per immutable compiled Playbook V2 artifact, addressed by the SHA-256 of
its canonical bytes.  The artifact body itself lives on disk at
`{compiled_root}/artifacts/<sha256>.json`; this table is the index, never the
payload.  See `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`
§ "Storage and activation".

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `artifact_sha256` | TEXT | PRIMARY KEY | `sha256:<64 lowercase hex>` over the canonical bytes |
| `playbook_id` | TEXT | NOT NULL | Playbook this artifact compiles |
| `scope` | TEXT | NOT NULL DEFAULT 'system' | One of: system, project, agent_type, supervisor |
| `scope_identifier` | TEXT | NOT NULL DEFAULT '' | Project or agent-type id; `''` for system scope |
| `schema_generation` | INTEGER | NOT NULL DEFAULT 2 | Artifact schema generation this build stores |
| `version` | INTEGER | NOT NULL DEFAULT 0 | Authored playbook version |
| `source_digest` | TEXT | NOT NULL | Digest of the authored Markdown the artifact came from |
| `contract_fingerprint` | TEXT | NOT NULL | Fingerprint of the command contracts compiled against |
| `profile_fingerprint` | TEXT | NOT NULL DEFAULT '' | Capability-policy fingerprint, compared as an opaque string |
| `compiler_build` | TEXT | NOT NULL | Compiler build that produced the bytes |
| `path` | TEXT | NOT NULL | On-disk location of the artifact file |
| `size_bytes` | INTEGER | NOT NULL DEFAULT 0 | Byte length of the artifact file |
| `validation` | TEXT | NOT NULL DEFAULT '{}' | JSON validation summary (counts and questions, not diagnostics) |
| `compiled_at` | TEXT | nullable | Compiler-reported timestamp string |
| `created_at` | REAL | NOT NULL | Set on insert |

### Table: `playbook_activations`

Operational activation metadata for a playbook in one scope: which artifact hash
is live, whether an operator has enabled it, and its readiness health.  Kept
outside the artifact so pausing a playbook never rewrites immutable content.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `activation_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook being activated |
| `scope` | TEXT | NOT NULL DEFAULT 'system' | One of: system, project, agent_type, supervisor |
| `scope_identifier` | TEXT | NOT NULL DEFAULT '' | `''` rather than NULL so the scope UNIQUE constraint is total |
| `active_artifact_sha256` | TEXT | nullable, FK → playbook_artifacts (RESTRICT) | NULL until first activated |
| `enabled` | BOOLEAN | NOT NULL DEFAULT false | Operator intent, independent of health |
| `health` | TEXT | NOT NULL DEFAULT 'disabled' | One of: ready, question_required, invalid, disabled, stale_contract, unavailable |
| `reasons` | TEXT | NOT NULL DEFAULT '[]' | JSON list of health reason objects |
| `activated_at` | REAL | nullable | When the current artifact was activated |
| `activated_by` | TEXT | nullable | Server-derived principal that activated it |
| `updated_at` | REAL | NOT NULL | Set on every write |

### Table: `playbook_v2_runs`

One row per Playbook V2 run.  Named `playbook_v2_runs` rather than reusing
`playbook_runs` because V1 runs must stay readable after V1 execution is
removed.  `snapshot` holds the whole durable run state as canonical JSON;
the columns beside it are the indexed projection of that same state, so an
operator query is an index scan rather than a JSON parse of every row.
`snapshot_version` is the optimistic-concurrency token every durable advance
compares and increments.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `run_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook this run executes |
| `artifact_sha256` | TEXT | NOT NULL, FK → playbook_artifacts (RESTRICT) | The pinned artifact — a run reads no mutable playbook content |
| `rule_id` | TEXT | NOT NULL | The one rule this run executes |
| `lifecycle` | TEXT | NOT NULL DEFAULT 'running' | One of: running, paused, cancelling, completed, failed, timed_out, cancelled |
| `mode` | TEXT | NOT NULL DEFAULT 'live' | One of: live, dry_run, shadow |
| `current_step_id` | TEXT | nullable | Step the run is sitting on |
| `snapshot_version` | INTEGER | NOT NULL DEFAULT 0 | Compare-and-set token; advanced once per boundary |
| `snapshot` | TEXT | NOT NULL DEFAULT '{}' | Canonical JSON of the whole run snapshot |
| `snapshot_bytes` | INTEGER | NOT NULL DEFAULT 0 | Byte length of `snapshot`, capped by `playbooks.v2_max_snapshot_bytes` |
| `event_type` | TEXT | NOT NULL DEFAULT '' | Trigger event type |
| `event_id` | TEXT | nullable | Trigger event id |
| `dispatch_id` | TEXT | nullable | One dispatch creates at most one run per rule |
| `parent_run_id` | TEXT | nullable | Parent run for a nested execution |
| `parent_step_id` | TEXT | nullable | Step of the parent run that spawned this one |
| `deadline_at` | REAL | nullable | Whole-run deadline |
| `cancel_requested_at` | REAL | nullable | When cancellation was requested |
| `cancel_requested_by` | TEXT | nullable | Server-derived principal that requested it |
| `cancel_reason` | TEXT | nullable | Operator-supplied reason |
| `summary` | TEXT | NOT NULL DEFAULT '' | Human-readable outcome summary |
| `error` | TEXT | nullable | Failure detail |
| `error_code` | TEXT | nullable | Machine-readable failure code |
| `started_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on every boundary |
| `completed_at` | REAL | nullable | NULL until terminal |

A partial unique index `uq_playbook_v2_runs_dispatch_rule` on
`(dispatch_id, rule_id)` where `dispatch_id IS NOT NULL` makes "one matching
event may create multiple rule runs, but each run executes exactly one rule"
unforgeable — a retried dispatch cannot duplicate them.

### Table: `playbook_step_receipts`

One immutable row per step *attempt*.  Attempt identity is four-part —
`(run_id, step_id, iteration, attempt)` — and is enforced by
`uq_playbook_step_receipts_attempt`, so a replayed attempt after an ambiguous
interruption is rejected by the database rather than by an in-memory guard a
restart would have forgotten.  `principal`, `inputs` and `result` hold the
default-deny receipt projection, never raw values.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `receipt_id` | TEXT | PRIMARY KEY | UUID string |
| `run_id` | TEXT | NOT NULL, FK → playbook_v2_runs (CASCADE) | Run this attempt belongs to |
| `artifact_sha256` | TEXT | NOT NULL | Artifact the attempt executed against |
| `rule_id` | TEXT | NOT NULL | Rule being executed |
| `step_id` | TEXT | NOT NULL | Step being attempted |
| `step_kind` | TEXT | NOT NULL | Step kind (command, llm, decision, wait, terminal, …) |
| `iteration` | INTEGER | NOT NULL DEFAULT -1 | `-1` outside a loop, `0..n` inside one |
| `attempt` | INTEGER | NOT NULL DEFAULT 1 | 1-based attempt number |
| `idempotency_key` | TEXT | NOT NULL | `<run>:<step>:<iteration|->:<attempt>` |
| `snapshot_version` | INTEGER | NOT NULL DEFAULT 0 | Snapshot version this attempt ran against |
| `contract_fingerprint` | TEXT | NOT NULL DEFAULT '' | Command contract fingerprint, compared as an opaque string |
| `principal` | TEXT | NOT NULL DEFAULT '{}' | JSON, redacted |
| `inputs` | TEXT | NOT NULL DEFAULT '{}' | JSON, default-deny projection |
| `result` | TEXT | NOT NULL DEFAULT '{}' | JSON, default-deny projection |
| `outcome` | TEXT | NOT NULL | One of: success, failure, skipped, timeout, cancelled, operator_decision_required |
| `selected_transition` | TEXT | nullable | `<rule>::<step>::<outcome>`; the graph overlay joins on it |
| `error` | TEXT | nullable | Failure detail |
| `error_code` | TEXT | nullable | Machine-readable failure code |
| `tokens_in` | INTEGER | NOT NULL DEFAULT 0 | LLM prompt tokens |
| `tokens_out` | INTEGER | NOT NULL DEFAULT 0 | LLM completion tokens |
| `cost_usd` | REAL | nullable | Attempt cost when known |
| `wait_id` | TEXT | nullable | The wait this attempt suspended on |
| `timed_out` | BOOLEAN | NOT NULL DEFAULT false | Whether the attempt hit its deadline |
| `cancelled_at` | REAL | nullable | When cancellation was acknowledged |
| `started_at` | REAL | NOT NULL | Set on insert |
| `completed_at` | REAL | nullable | NULL while the attempt is open |
| `duration_ms` | INTEGER | NOT NULL DEFAULT 0 | Attempt duration |

### Table: `task_completion_records`

Append-only audit records for accepted task-close operations.  This deliberately
uses a logical `task_id` reference rather than a foreign key, so completion
history survives archival of the active task row.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Completion record identifier |
| `task_id` | TEXT | NOT NULL | Logical task reference |
| `outcome` | TEXT | NOT NULL | Close outcome |
| `work_outcome` | TEXT | nullable | Work result classification |
| `failure_class` | TEXT | nullable | Failure classification when applicable |
| `changes` | TEXT | NOT NULL DEFAULT '' | Reported changes |
| `verification` | TEXT | NOT NULL DEFAULT '' | Verification summary |
| `tests` | TEXT | NOT NULL DEFAULT '[]' | JSON test list |
| `commands` | TEXT | NOT NULL DEFAULT '[]' | JSON command list |
| `branch` | TEXT | nullable | Source branch |
| `commits` | TEXT | NOT NULL DEFAULT '[]' | JSON commit list |
| `pr_url` | TEXT | nullable | Pull request URL |
| `summary` | TEXT | NOT NULL DEFAULT '' | Human-readable close summary |
| `notes` | TEXT | NOT NULL DEFAULT '' | Supplemental close notes |
| `completed_at` | REAL | NOT NULL | Unix timestamp |

### Table: `task_assignment_routes`

Persisted routing decisions for task assignment playbooks. Each task has at
most one current decision; the project index supports routing and audit views.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY, REFERENCES tasks(id) ON DELETE CASCADE | Routed task |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE CASCADE | Owning project; indexed |
| `input_hash` | TEXT | NOT NULL | Hash of routing inputs |
| `task_updated_at` | REAL | NOT NULL | Task update timestamp used for the decision |
| `options_hash` | TEXT | NOT NULL | Hash of candidate route options |
| `intelligence_class` | TEXT | NOT NULL | Selected intelligence class |
| `provider` | TEXT | nullable | Selected provider, when specified |
| `playbook_id` | TEXT | NOT NULL | Assignment playbook identifier |
| `playbook_version` | INTEGER | NOT NULL | Compiled playbook version |
| `playbook_run_id` | TEXT | NOT NULL REFERENCES playbook_runs(run_id) ON DELETE CASCADE | Source run |
| `reason` | TEXT | NOT NULL | Decision rationale |
| `decided_at` | REAL | NOT NULL | Unix timestamp of the decision |

### Table: `workflows`

Multi-agent pipelines with stage gates and agent affinity.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `workflow_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook that defines the pipeline |
| `playbook_run_id` | TEXT | NOT NULL REFERENCES playbook_runs(run_id) | Owning run |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `status` | TEXT | NOT NULL DEFAULT 'running' | One of: running, completed, failed |
| `current_stage` | TEXT | nullable | Stage the workflow is on |
| `task_ids` | TEXT | NOT NULL DEFAULT '[]' | JSON array of member task ids |
| `agent_affinity` | TEXT | NOT NULL DEFAULT '{}' | JSON map pinning stages to agents |
| `stages` | TEXT | NOT NULL DEFAULT '[]' | JSON stage definitions |
| `created_at` | REAL | NOT NULL | Set on insert |
| `completed_at` | REAL | nullable | NULL until the pipeline finishes |

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

Inserts all task columns. Both `created_at` and `updated_at` are set to `time.time()` at insert time; the dataclass values are ignored. `status` and `verification_type` are serialized to their enum `.value`. `is_plan_subtask` is stored as an integer (0/1) via `int()`. Commits.

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

Private helper. Uses `key in row.keys()` guards for migration-added columns (`pr_url`, `plan_source`, `is_plan_subtask`) to handle databases that predate those migrations. `is_plan_subtask` is cast to `bool`. `integration_mode` is read as-is (nullable TEXT).

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

### `record_token_usage(project_id, agent_id, task_id, tokens, *, model=None, input_tokens=None, output_tokens=None) -> None`

Appends one row to `token_ledger`. The `id` is a fresh UUID4 and `timestamp` is `time.time()`. `tokens` is the authoritative total; `model` and the input/output split are optional because most writers only know the total.

### `get_cost_rollup(*, project_id=None, since_ts=None, group_by='project') -> list[dict]`

Rolls the ledger up per `(group key, model)` for `aq costs`. Grouping happens in Python so no dialect-specific date functions are needed. Rows without a model or without a split are returned with zeroed split columns; the caller reports them as `unpriced_tokens` and never prices them.

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

## 12. Playbook Runs (formerly Hooks)

The `hooks` and `hook_runs` tables and their `Database` methods were **removed**. Event- and time-triggered automation is now expressed as playbooks — markdown DAGs compiled to JSON — and each execution is a row in `playbook_runs` (see `docs/specs/design/playbooks.md`). Queries live in `src/database/queries/playbook_queries.py`; workflow pipelines built on top of runs live in `src/database/queries/workflow_queries.py`.

---

## 13. System Config

The `system_config` table (key TEXT PRIMARY KEY, value TEXT NOT NULL) is present in the schema but **no CRUD methods are implemented on the `Database` class**. The table is available for direct SQL access or future implementation.

---

## 14. Migration / Schema Evolution

Schema evolution is managed by **Alembic** (`migrations/`), not by ad-hoc `ALTER TABLE` statements. `initialize()` creates the engine and then runs `alembic upgrade head` against it (`src/database/engine.py`); a pre-Alembic database with tables but no `alembic_version` row is stamped at the baseline revision first.

After any change to `src/database/tables.py`:

```bash
alembic revision --autogenerate -m "description of change"
# review the generated file in migrations/versions/ — autogenerate sees a
# rename as drop+add
alembic upgrade head
```

Migrations must work on both SQLite and PostgreSQL. `aq doctor`'s `db.migrations` check compares the stamped revision against the script head and reports an error when the database is behind.

The full list of migrations applied in order:

| Statement | Effect |
|---|---|
| `ALTER TABLE projects ADD COLUMN workspace_path TEXT` | Legacy migration — column is now deprecated/unused (workspace paths managed via `workspaces` table) |
| `ALTER TABLE repos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'clone'` | Adds repo source type enum |
| `ALTER TABLE repos ADD COLUMN source_path TEXT NOT NULL DEFAULT ''` | Adds local path for linked/initialized repos |
| `ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0` | Historical: added the approval requirement flag (later replaced by `integration_mode` and dropped by Alembic `c4d5e6f7a8b9`) |
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

`alembic_version` records the applied revision. Destructive changes (DROP COLUMN, renames, type changes) are expressible but must be written by hand and reviewed — autogenerate will not infer them correctly.

Alembic revision `c4d5e6f7a8b9` (integration mode) adds `integration_mode` to `tasks`, `archived_tasks`, and `projects`, backfills the old `requires_approval` flag (`1`→`'pull_request'`, `0`→`'direct'`) on both task tables, and drops `requires_approval` and `auto_approve_plan`. It carries a PREFLIGHT that fails the upgrade with per-row remediation SQL if any active task is still in the deleted `AWAITING_APPROVAL`/`AWAITING_PLAN_APPROVAL` statuses — see `docs/guides/upgrade-integration-mode.md`.

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

### Repos (additional)
- `update_repo()` — update repo fields
