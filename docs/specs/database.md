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
| `hierarchical_integration_mode` | TEXT | NOT NULL DEFAULT 'disabled' | *Effective* hierarchical-integration rollout mode: one of `disabled`, `observe`, `hierarchy`, `train` (`ck_projects_hierarchical_integration_mode`). Only the orchestrator advances it, via a compare-and-set on `hierarchical_integration_generation`. Added by Alembic `c7a1e5d92f40` |
| `integration_repository_id` | TEXT | nullable | The one `repos.id` designated as the hierarchical-integration repository (child branches, candidate trains and root promotion all target it). NULL leaves the project `repository_not_designated` and blocks every mode above `disabled`. Added by Alembic `c7a1e5d92f40` |
| `hierarchical_integration_policy` | JSON | nullable | Frozen policy pins (required checks, repair tiers, source-branch retention, legacy-route suppression) snapshotted into each batch and repair operation; NULL uses config defaults. Added by Alembic `e4c6a8b20d31` |
| `hierarchical_integration_desired_mode` | TEXT | NOT NULL DEFAULT 'disabled' | Mode the operator asked for with `integration_enable`; same value set as `hierarchical_integration_mode`. Differs from the effective mode while a drain is in progress. Added by Alembic `a11a5e1e4f04` |
| `hierarchical_integration_draining` | BOOLEAN | NOT NULL DEFAULT false | True while in-flight batches/repairs are being drained before the effective mode drops to the desired one. Added by Alembic `a11a5e1e4f04` |
| `hierarchical_integration_generation` | INTEGER | NOT NULL DEFAULT 0 | Monotone rollout fence (`>= 0`); every mode transition increments it and is recorded in `integration_rollout_transitions`. Operator controls pass `expected_generation` and are rejected on mismatch. Added by Alembic `a11a5e1e4f04` |
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
| `source_type` | TEXT | NOT NULL DEFAULT 'clone' | Added by migration; one of: clone, link, init, worktree |
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

### Table: `task_layouts`

Derived spatial-layout projection for task-graph nodes. Each task has at most one row per
project and layout variant. The `all` variant contains the complete task tree; the `active`
variant omits finished tasks and may replace completed containers with stubs. Layout rows can
be dropped and reproduced by running the backfill.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY, NOT NULL, REFERENCES projects(id) | Project whose graph is laid out |
| `variant` | TEXT | PRIMARY KEY, NOT NULL | `all` or `active` |
| `task_id` | TEXT | PRIMARY KEY, NOT NULL, REFERENCES tasks(id) | Projected task |
| `container_id` | TEXT | nullable | Immediate layout container; NULL for root-level nodes |
| `path` | TEXT | NOT NULL | Materialized hierarchy path used for subtree reads and translations |
| `depth` | INTEGER | NOT NULL | Hierarchy depth |
| `rank` | INTEGER | NOT NULL | Dependency rank within the containing layout |
| `order_key` | TEXT | NOT NULL | Stable sibling ordering key |
| `w` | FLOAT | NOT NULL | Allocated box width |
| `h` | FLOAT | NOT NULL | Allocated box height |
| `rel_x` | FLOAT | NOT NULL | X coordinate relative to the containing layout |
| `rel_y` | FLOAT | NOT NULL | Y coordinate relative to the containing layout |
| `abs_x` | FLOAT | NOT NULL | Absolute canvas X coordinate |
| `abs_y` | FLOAT | NOT NULL | Absolute canvas Y coordinate |
| `kind` | TEXT | NOT NULL | `card`, `container`, or `stub` |
| `agg_children` | INTEGER | NOT NULL DEFAULT 0 | Number of immediate children |
| `agg_descendants` | INTEGER | NOT NULL DEFAULT 0 | Number of descendants |
| `agg_completed` | INTEGER | NOT NULL DEFAULT 0 | Number of completed descendants |
| `agg_running` | INTEGER | NOT NULL DEFAULT 0 | Number of running descendants |
| `agg_blocked` | INTEGER | NOT NULL DEFAULT 0 | Number of blocked descendants |
| `agg_active` | INTEGER | NOT NULL DEFAULT 0 | Number of non-finished descendants |

Composite primary key: (`project_id`, `variant`, `task_id`). Checks
`ck_task_layouts_variant` and `ck_task_layouts_kind` restrict `variant` and `kind` to the
values listed above. Indexes: `idx_task_layouts_path` (`project_id`, `variant`, `path`),
`idx_task_layouts_depth` (`project_id`, `variant`, `depth`), and
`idx_task_layouts_container` (`project_id`, `variant`, `container_id`).

### Table: `task_layout_cells`

Cross-database spatial index for task-layout boxes. A task has one membership row for every
8 by 8 unit cell overlapped by its allocated box. Membership rows are rewritten in the same
transaction whenever publishing translates or resizes the corresponding layout row.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY, NOT NULL | Project whose graph is indexed |
| `variant` | TEXT | PRIMARY KEY, NOT NULL | Layout variant |
| `cell_x` | INTEGER | PRIMARY KEY, NOT NULL | Horizontal cell coordinate |
| `cell_y` | INTEGER | PRIMARY KEY, NOT NULL | Vertical cell coordinate |
| `task_id` | TEXT | PRIMARY KEY, NOT NULL | Task occupying the cell |

Composite primary key: (`project_id`, `variant`, `cell_x`, `cell_y`, `task_id`). Indexes:
`idx_task_layout_cells_cell` (`project_id`, `variant`, `cell_x`, `cell_y`) for viewport
queries and `idx_task_layout_cells_task` (`project_id`, `variant`, `task_id`) for replacing
a task's memberships.

### Table: `project_layout_meta`

Publication metadata for one project's layout variant. The version, extent, node count, and
layout-row changes are published atomically so readers never observe a mixed layout version.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY, NOT NULL, REFERENCES projects(id) | Project whose layout is described |
| `variant` | TEXT | PRIMARY KEY, NOT NULL | Layout variant |
| `layout_version` | INTEGER | NOT NULL DEFAULT 0 | Monotonic version incremented on publish |
| `extent_w` | FLOAT | NOT NULL DEFAULT 0 | Published canvas width |
| `extent_h` | FLOAT | NOT NULL DEFAULT 0 | Published canvas height |
| `node_count` | INTEGER | NOT NULL DEFAULT 0 | Number of published layout rows |
| `updated_at` | FLOAT | NOT NULL | Unix timestamp of the latest publish |
| `reconciled_at` | FLOAT | nullable | Unix timestamp of the latest reconciliation sweep |

Composite primary key: (`project_id`, `variant`).

### Table: `layout_dirty`

Durable queue of task mutations that require layout reconciliation. Writers add marks in the
same transaction as the source mutation; a successful publish consumes marks through the
highest processed sequence number in its own transaction.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `seq` | INTEGER | PRIMARY KEY, AUTOINCREMENT | Queue sequence and publish fence |
| `project_id` | TEXT | NOT NULL | Project requiring reconciliation |
| `task_id` | TEXT | NOT NULL | Changed task |
| `reason` | TEXT | NOT NULL | Mutation category that caused the mark |
| `created_at` | FLOAT | NOT NULL | Unix timestamp used to debounce batches |

Index: `idx_layout_dirty_project` (`project_id`, `seq`).

### Table: `layout_jobs`

Lifecycle records for user-requested Tidy work and initial-layout backfills. Only one queued or
running job is admitted for a project/variant pair by the query layer; job failures retain their
error text for inspection.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Job identifier |
| `project_id` | TEXT | NOT NULL | Project to lay out |
| `variant` | TEXT | NOT NULL | Layout variant |
| `kind` | TEXT | NOT NULL | `tidy` or `backfill` |
| `status` | TEXT | NOT NULL | `queued`, `running`, `done`, or `failed` |
| `requested_at` | FLOAT | NOT NULL | Unix timestamp when the job was queued |
| `started_at` | FLOAT | nullable | Unix timestamp when execution began |
| `finished_at` | FLOAT | nullable | Unix timestamp when execution ended |
| `error` | TEXT | nullable | Failure detail |

Index: `idx_layout_jobs_project_status` (`project_id`, `status`).

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

### Table: `project_onboarding_requests`

Durable idempotency and recovery state for the project-onboarding saga. The
service creates the row before filesystem or GitHub mutation, then advances its
phase and owned-resource ledger as work completes. Terminal rows are retained
for bounded replay and later garbage collection.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `request_id` | TEXT | PRIMARY KEY | Operator-supplied idempotency key |
| `input_fingerprint` | TEXT | NOT NULL | SHA-256 of the normalized request; prevents reuse with different inputs |
| `status` | TEXT | NOT NULL DEFAULT 'pending' | One of: pending, succeeded, failed |
| `phase` | TEXT | NOT NULL DEFAULT 'pending' | Current saga phase for status and recovery |
| `created_resources` | JSON | NOT NULL DEFAULT '[]' | Owned paths and non-secret identifiers used for bounded compensation |
| `result` | JSON | nullable | Safe terminal success response |
| `error` | JSON | nullable | Safe, secret-scrubbed terminal error response |
| `created_at` | REAL | NOT NULL | Unix timestamp when the request was first recorded |
| `updated_at` | REAL | NOT NULL | Unix timestamp of the latest phase or ledger change |
| `finished_at` | REAL | nullable | Unix timestamp for terminal rows; NULL while pending |

The status constraint requires pending rows to have no `finished_at` and
terminal rows to have one. The recovery ledger never stores GitHub credentials
or subprocess output. An index on `(status, finished_at)` supports bounded
terminal-record retention.

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
| `source_type` | TEXT | NOT NULL DEFAULT 'clone' | One of: clone, link, init, worktree |
| `name` | TEXT | nullable | Human-readable workspace name |
| `kind_id` | TEXT | nullable | Soft reference resolved project-first, then against the `__system__` workspace kind |
| `locked_by_agent_id` | TEXT | nullable | Agent currently using this workspace |
| `locked_by_task_id` | TEXT | nullable | Task the workspace is locked for |
| `locked_at` | REAL | nullable | Unix timestamp of lock acquisition |
| `lock_mode` | TEXT | nullable | Lock mode used by the current holder; NULL when unlocked |
| `enabled` | BOOLEAN | NOT NULL DEFAULT true | Disabled workspaces are excluded from acquisition |
| `slot_index` | INTEGER | nullable | Stable worktree slot ordinal; NULL for clones, links, and base rows |
| `base_workspace_id` | TEXT | nullable | Soft self-reference to the slot's base workspace |
| `created_at` | REAL | NOT NULL | Set on insert |

UNIQUE constraint on `(project_id, workspace_path)`. A partial unique index on
`(base_workspace_id, slot_index)` when both are non-NULL gives every base a
single row per worktree slot. Has extensive CRUD methods: `create_workspace`,
`get_workspace`, `list_workspaces`, `delete_workspace`, acquisition/release
operations, slot management, and project-path/count queries.

### Table: `agent_profiles`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID string |
| `name` | TEXT | NOT NULL UNIQUE | Human-readable profile name |
| `description` | TEXT | NOT NULL DEFAULT '' | Profile description |
| `model` | TEXT | NOT NULL DEFAULT '' | LLM model identifier |
| `permission_mode` | TEXT | NOT NULL DEFAULT '' | Permission level |
| `codex_full_auto` | BOOLEAN | NOT NULL DEFAULT false | Codex `--full-auto` profile opt-in |
| `claude_dangerously_skip_permissions` | BOOLEAN | NOT NULL DEFAULT false | Claude permission-bypass profile opt-in |
| `allowed_tools` | TEXT | NOT NULL DEFAULT '[]' | Compatibility shape superseded by the three capability namespace columns below |
| `harness_tools` | TEXT | nullable | JSON array for the `harness_tools` capability namespace |
| `aq_commands` | TEXT | nullable | JSON array for the `aq_commands` capability namespace |
| `plugin_tools` | TEXT | nullable | JSON array for the `plugin_tools` capability namespace |
| `mcp_servers` | TEXT | NOT NULL DEFAULT '{}' | JSON-encoded server configurations |
| `system_prompt_suffix` | TEXT | NOT NULL DEFAULT '' | Additional system prompt text |
| `install` | TEXT | NOT NULL DEFAULT '{}' | JSON-encoded install manifest |
| `created_at` | REAL | NOT NULL | Set on insert |
| `updated_at` | REAL | NOT NULL | Set on insert and every update |

Full CRUD: `create_profile`, `get_profile`, `list_profiles`, `update_profile`, `delete_profile`.

The three namespace columns intentionally distinguish `NULL` from `'[]'` and must not be
backfilled. `NULL` means the profile has no `## Capabilities` block, so its policy is reconstructed
from `allowed_tools` through the compatibility adapter. `'[]'` means the operator explicitly
authored that namespace as empty. Capability enforcement uses this distinction to separate an
inferred denial from an explicitly authored denial. See `src/profiles/capabilities.py`.

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
| `writable` | BOOLEAN | NOT NULL DEFAULT true | Whether agents may write to instances |
| `lockable` | BOOLEAN | NOT NULL DEFAULT true | Non-lockable kinds need no lease |
| `is_git_repo` | BOOLEAN | NOT NULL DEFAULT true | Enables Git provisioning behavior |
| `repo_url` | TEXT | nullable | Clone source when the kind is a repo |
| `default_lock_mode` | TEXT | nullable | Lock granularity when lockable |
| `auto_attach` | BOOLEAN | NOT NULL DEFAULT false | Attached without being declared |
| `mode` | TEXT | NOT NULL DEFAULT 'worktree' | Git provisioning strategy: worktree, exclusive-clone, or directory-isolated |
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

### Table: `transcript_checkpoints`

Durable high-water mark for each on-disk harness transcript file. The transcript
watcher (`src/sessions/transcripts/watcher.py`) used to hold its read offset in
process, keyed by session id; a session that died and was relaunched onto the
same workspace adopted the *same* transcript file with a fresh id starting at
offset 0, so the file's whole history was re-emitted as agent output and
re-charged to the token ledger — three consecutive supervisor incarnations each
wrote an identical 133 ledger rows for one window. The key is therefore the
transcript **path**, the one thing that outlives the session that set it.

Written only through `TranscriptQueryMixin`
(`src/database/queries/transcript_queries.py`): the watcher reads the mark on
attach and advances it as it consumes entries.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `transcript_path` | TEXT | PRIMARY KEY | Absolute path of the transcript file — deliberately not the session id |
| `byte_offset` | INTEGER | NOT NULL DEFAULT 0 | Bytes consumed so far; the resume position |
| `last_entry_uuid` | TEXT | nullable | Newest assistant entry whose usage was charged — second half of the dedupe key, so a reader resuming exactly on a record boundary cannot re-charge it |
| `session_id` | TEXT | nullable | Soft provenance: which session last advanced the mark. Diagnostic only — nothing reads it to decide whether to advance |
| `updated_at` | REAL | NOT NULL | Daemon clock at the last advance |

Advances are monotonic: an update is guarded by `byte_offset <= :offset`, so two
live readers pointed at one file cannot undo each other's progress. Truncation
is the single exception — the caller detects a file shorter than the mark and
passes `byte_offset=0`, and a zero offset always wins, because a rewritten file
genuinely has to be read from its start again. A missed update falls through to
an insert, and a racing writer's `IntegrityError` is swallowed: the conflict is
itself proof the row now exists.

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
is live, whether an operator has enabled it, and its readiness health. It stays
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

One row per durable playbook run. `snapshot` holds the whole durable run state as canonical JSON;
the columns beside it are the indexed projection of that same state, so an
operator query is an index scan rather than a JSON parse of every row.
`snapshot_version` is the optimistic-concurrency token every durable advance
compares and increments.  `playbook_id`, `artifact_sha256` and `rule_id` are
pinned when the run is created: a boundary whose snapshot or receipt disagrees
with them is refused with `run_identity_mismatch`, because a run that moved
onto another artifact would render its history against a graph it never ran.

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
| `dispatch_id` | TEXT | nullable | One dispatch creates at most one run per playbook rule |
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
`(playbook_id, dispatch_id, rule_id)` where `dispatch_id IS NOT NULL` makes
"one matching event may create multiple playbook rule runs, but each run
executes exactly one rule" unforgeable — a retried dispatch cannot duplicate
a run within one playbook, while a second matching playbook still gets its
own run.

### Table: `playbook_step_receipts`

One immutable row per durable boundary of a step *attempt*.  Attempt identity
is four-part — `(run_id, step_id, iteration, attempt)` — and receipt identity
extends it with `(turn_index, receipt_kind)`, enforced by
`uq_playbook_step_receipts_boundary`.  An attempt that reaches outside the
engine (command, LLM, agent task) opens with an `attempt_start` receipt
committed before its first external side effect and closes with a `step`
(or `interrupted`) receipt; every `snapshot_version` the run ever had has
exactly one receipt.  A replayed attempt after an ambiguous interruption
keeps its attempt number and idempotency key and is fenced again at the next
start ordinal, so a second fence at the same ordinal is rejected by the
database rather than by an in-memory guard a restart would have forgotten.  `principal`, `inputs` and `result` hold the
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
| `receipt_kind` | TEXT | NOT NULL DEFAULT 'step' | One of: step, tool_turn, llm_call, interrupted, operator_decision, attempt_start |
| `turn_index` | INTEGER | NOT NULL DEFAULT -1 | `-1` for `step`; zero-based turn index for LLM turn boundaries; zero-based start ordinal for `attempt_start` |
| `operator_decision_id` | TEXT | nullable | Set only on `interrupted` and `operator_decision` receipts |
| `outcome` | TEXT | NOT NULL | One of: success, failure, skipped, timeout, cancelled, operator_decision_required, started (`attempt_start` only) |
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

### Table: `playbook_waits`

One row per durable suspension point of a V2 run (design spec §10; Package 3
child plan §6.5).  A wait is inert data, never code: `match` is a flat JSON
mapping of dotted event field path to required literal, evaluated in Python
over the candidate set narrowed by `idx_playbook_waits_match`, because a
predicate read back from the database after a restart must not be able to
execute anything.

Registration happens on `commit_boundary`'s own connection, so a run is never
suspended with its wait invisible.  `uq_playbook_waits_active_step` is a
partial unique index over `state = 'active'`: one live wait per step
instance, so a duplicated resume raises rather than producing two claimable
rows.  `snapshot_version` records the snapshot the run is suspended *on* — a
resume that finds it disagreeing with `playbook_v2_runs.snapshot_version`
refuses with `wait_version_mismatch`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `wait_id` | TEXT | PRIMARY KEY | Caller-assigned wait identifier |
| `run_id` | TEXT | NOT NULL, FK → playbook_v2_runs (CASCADE) | Suspended run |
| `step_id` | TEXT | NOT NULL | Step that opened the wait |
| `iteration` | INTEGER | NOT NULL DEFAULT -1 | `-1` outside a loop, `0..n` inside one |
| `kind` | TEXT | NOT NULL, CHECK | One of: event, timer, human, agent_task |
| `event_type` | TEXT | NOT NULL DEFAULT '' | Empty matches any event type |
| `correlation_key` | TEXT | NOT NULL DEFAULT '' | Digest of (kind, event_type, match), for operator search |
| `match` | TEXT | NOT NULL DEFAULT '{}' | JSON: dotted field path → required literal |
| `deadline_at` | REAL | nullable | NULL never expires |
| `snapshot_version` | INTEGER | NOT NULL | Snapshot version the wait suspends |
| `state` | TEXT | NOT NULL DEFAULT 'active', CHECK | One of: active, claimed, expired, cleared |
| `claimed_event_id` | TEXT | nullable | Event that claimed it; NULL for an expiry |
| `claimed_at` | REAL | nullable | When it was claimed or expired |
| `created_at` | REAL | NOT NULL | Wait-decision time; inbox matches must be this new or newer |

### Table: `playbook_pending_events`

An event that matched an activation which was not `ready` is retained here
rather than dropped (child plan §10.3), so recovery is "rebuild, activate,
dispatch the retained events" and never "the events are gone". Event-wait
delivery also uses resolved `wait_registration` rows as a short durable inbox:
ingestion records the event before scanning waits, and a concurrent wait scans
events received since its decision time before its registration commits. Both
sides serialize and match on `(playbook_id, scope, scope_identifier)`. A replay
with the same routed event id uses the originally stored body and arrival time;
it cannot mutate history to claim a newer wait. An immediate registration match
is copied into the committed run snapshot's `pending_wait_claims`, giving the
engine a durable resume handoff instead of leaving a claimed wait behind a
paused snapshot.

Deduplication is `uq_playbook_pending_events_dedup`, a partial unique index
over `resolved_at IS NULL AND dedup_key <> ''` — the index, not a pre-read,
because a pre-read races.  An empty `dedup_key` therefore never deduplicates.
Replay order is `ORDER BY received_at, pending_event_id`.  `resolved_at` /
`resolved_by` / `resolution` are the operator-audit columns; resolution CASes
on `resolved_at IS NULL`. Dispatch first acquires the all-or-none
`dispatch_claim_*` lease while leaving `resolved_at` NULL, so the deduplication
index continues protecting the event during execution. The owner renews the
lease during a long dispatch and finalizes through its opaque token; a failed
attempt clears the claim and records `attempts` / `last_error`, while a stale
lease may be atomically replaced after process death. The retention sweep
protects a renewed live claim but expires an abandoned claim after the same
lease horizon. Retention is 7 days by default. The configured per-playbook quota
(`playbooks.v2_max_pending_events_per_playbook`) applies independently to
unresolved activation rows and to resolved wait-delivery inbox rows, keeping
either producer path from growing the table without bound.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `pending_event_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook the event was routed to |
| `scope` | TEXT | NOT NULL DEFAULT 'system' | Activation scope |
| `scope_identifier` | TEXT | NOT NULL DEFAULT '' | Project id for project scope, else empty |
| `event_type` | TEXT | NOT NULL | Event type as received |
| `event` | TEXT | NOT NULL DEFAULT '{}' | JSON event body |
| `event_id` | TEXT | nullable | Producer's event id when it has one |
| `dedup_key` | TEXT | NOT NULL DEFAULT '' | Empty disables deduplication |
| `reason` | TEXT | NOT NULL, CHECK | One of: stale_contract, invalid_artifact, disabled, unavailable, question_required, wait_registration |
| `attempts` | INTEGER | NOT NULL DEFAULT 0 | Dispatch attempts made after retention |
| `last_error` | TEXT | nullable | Last dispatch failure |
| `received_at` | REAL | NOT NULL | Arrival time; the replay order |
| `expires_at` | REAL | NOT NULL | `received_at + retention`; collectable past it |
| `dispatch_claim_token` | TEXT | nullable | Opaque owner token while an operator dispatch is in flight |
| `dispatch_claimed_by` | TEXT | nullable | Server-derived principal holding the dispatch lease |
| `dispatch_claimed_at` | REAL | nullable | Last lease renewal time; stale claims may be replaced |
| `resolved_at` | REAL | nullable | NULL while unresolved |
| `resolved_by` | TEXT | nullable | Server-derived principal that resolved it |
| `resolution` | TEXT | nullable, CHECK | One of: dispatched, discarded, expired |

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
| `playbook_run_id` | TEXT | NOT NULL REFERENCES playbook_v2_runs(run_id) ON DELETE CASCADE | Source run |
| `reason` | TEXT | NOT NULL | Decision rationale |
| `decided_at` | REAL | NOT NULL | Unix timestamp of the decision |

### Table: `workflows`

Multi-agent pipelines with stage gates and agent affinity.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `workflow_id` | TEXT | PRIMARY KEY | UUID string |
| `playbook_id` | TEXT | NOT NULL | Playbook that defines the pipeline |
| `playbook_run_id` | TEXT | NOT NULL REFERENCES playbook_v2_runs(run_id) | Owning run |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `status` | TEXT | NOT NULL DEFAULT 'running' | One of: running, completed, failed |
| `current_stage` | TEXT | nullable | Stage the workflow is on |
| `task_ids` | TEXT | NOT NULL DEFAULT '[]' | JSON array of member task ids |
| `agent_affinity` | TEXT | NOT NULL DEFAULT '{}' | JSON map pinning stages to agents |
| `stages` | TEXT | NOT NULL DEFAULT '[]' | JSON stage definitions |
| `created_at` | REAL | NOT NULL | Set on insert |
| `completed_at` | REAL | nullable | NULL until the pipeline finishes |

### Hierarchical integration trains

The tables below back hierarchical delivery and integration trains
(`docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md`,
Alembic `3f30b34c7e7c` through `a11a5e1e4f04`).  A parent task collects its children's reviewed
branches into a *candidate* built from an ordered *batch*, publishes CI
evidence for the candidate, repairs it through bounded *repair operations*,
and promotes the green candidate to `main` under a project lease and branch
fence.  Almost every row is evidence rather than mutable state: the
migrations install dual-dialect triggers that reject `UPDATE`/`DELETE` on
receipts, batch members, root-intent members, release results, waivers and
transitions, and that keep counters (`revision`, `fence_token`, `attempts`,
`acceptance_cursor`) monotone.  Tables that reference a task by `task_id`
without a foreign key do so deliberately so the evidence survives archival.

`repository_id` columns throughout are logical references to `repos.id`
unless a `REFERENCES repos(id)` constraint is listed.

### Table: `task_integration_checkpoints`

Per-task hierarchical delivery state: the task's branch in the integration
repository, its verified checkpoint, and the collection state machine.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY | Task the checkpoint belongs to (logical reference) |
| `repository_id` | TEXT | NOT NULL | Integration repository |
| `branch` | TEXT | NOT NULL | Task branch name |
| `generation` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Bumped each time the branch head is re-collected |
| `checkpoint_sha` | TEXT | nullable | Latest collected head |
| `verified_sha` | TEXT | nullable | Head that last passed parent verification |
| `verified_generation` | INTEGER | nullable, `>= 0` | Generation `verified_sha` was verified at |
| `state` | TEXT | NOT NULL DEFAULT 'working' | One of: working, awaiting_children, integration_ready, verifying |
| `version` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Optimistic-concurrency version |
| `last_transition_id` | TEXT | nullable | Event id of the last state transition |
| `playbook_activation_id` | TEXT | nullable | Activation of the delivery playbook that owns the state machine |
| `branch_owner_id` | TEXT | nullable | Current `integration_branch_owners.id` for the branch |
| `episode_id` | TEXT | nullable | Current collection episode; `(task_id, episode_id)` REFERENCES `integration_parent_episodes(parent_task_id, id)` ON DELETE RESTRICT |
| `current_verification_id` | TEXT | nullable | `(task_id, current_verification_id)` REFERENCES `integration_parent_verifications(parent_task_id, id)` ON DELETE RESTRICT |
| `last_completed_operation_id` | TEXT | nullable | Set together with `last_completed_verification_id` (`ck_task_integration_checkpoints_completion_binding`); the pair plus `task_id` REFERENCES `integration_parent_operation_completions` ON DELETE RESTRICT |
| `last_completed_verification_id` | TEXT | nullable | See above |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `task_branch_origins`

Where a task's branch was cut from.  Child branches are reserved before they
are materialized so a crash between the two leaves an auditable row.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Origin id |
| `task_id` | TEXT | NOT NULL | Task the branch belongs to |
| `repository_id` | TEXT | NOT NULL | Repository the branch lives in |
| `parent_task_id` | TEXT | nullable | Parent whose branch is the base, NULL for roots |
| `parent_repository_id` | TEXT | nullable | Parent's repository |
| `parent_ref` | TEXT | nullable | Immediate parent ref the branch was cut from |
| `base_sha` | TEXT | NOT NULL | Commit the branch was cut at |
| `creation_generation` | INTEGER | NOT NULL, `>= 0` | Parent checkpoint generation at creation |
| `reserved` | BOOLEAN | NOT NULL DEFAULT false | Branch name reserved in the database |
| `materialized` | BOOLEAN | NOT NULL DEFAULT false | Branch exists on the remote; requires `reserved` (`ck_task_branch_origins_materialized_reserved`) and cannot be cleared (trigger) |
| `retired_at` | REAL | nullable | Set when the origin is superseded; only one live origin per `(task_id, repository_id)` (`uq_task_branch_origins_live_task_repo`, partial) |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `materialized_at` | REAL | nullable | When the remote branch appeared |

### Table: `integration_branch_owners`

Exclusive, fenced ownership of a ref in the integration repository.  Every
push to a task/candidate/`main` branch carries the owner's `fence_token`;
a stale owner's push is rejected before it reaches the remote.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Ownership id |
| `repository_id` | TEXT | NOT NULL | With `ref`: UNIQUE (`uq_integration_branch_owners_ref`) |
| `ref` | TEXT | NOT NULL | Owned ref |
| `owner_id` | TEXT | NOT NULL | Session, service or operation holding the ref |
| `owner_role` | TEXT | NOT NULL | Role the owner acts in (worker, repair delegate, train, …) |
| `fence_token` | INTEGER | NOT NULL, `>= 0` | Monotone fence; bumped on every handoff |
| `handoff_state` | TEXT | NOT NULL DEFAULT 'reserved' | One of: reserved, attached, handoff_pending, released |
| `session_id` | TEXT | nullable | Attached session |
| `workspace_id` | TEXT | nullable | Workspace the owner works in |
| `confirmed_workspace_id` | TEXT | nullable | Workspace whose checkout was verified to match the ref |
| `expires_at` | REAL | nullable | Lease expiry for time-bounded owners |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_review_evidence`

Immutable record that a reviewer approved (or rejected) an exact
`(head, tree)` of a source task's branch.  Batches, root-intent members and
receipts reference it by id *and* identity so a re-review of a moved branch
can never satisfy an older pin.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Evidence id |
| `source_task_id` | TEXT | NOT NULL | Reviewed task |
| `repository_id` | TEXT | NOT NULL | Repository |
| `source_base` | TEXT | NOT NULL | Base the branch was reviewed against |
| `reviewed_head_sha` | TEXT | NOT NULL | Reviewed head commit |
| `reviewed_tree_sha` | TEXT | NOT NULL | Reviewed tree |
| `reviewer_task_id` | TEXT | NOT NULL | Reviewer task |
| `reviewer_session_attempt_id` | TEXT | nullable | Reviewer session attempt |
| `review_kind` | TEXT | NOT NULL | Review stage kind |
| `generation` | INTEGER | NOT NULL, `>= 0` | Checkpoint generation reviewed |
| `verdict` | TEXT | NOT NULL | One of: approved, rejected |
| `evidence` | JSON | NOT NULL | Reviewer's structured verdict |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Indexes: `uq_integration_review_evidence_root_identity` (id + identity tuple,
unique, the composite FK target) and `idx_integration_review_evidence_current`
for "latest approval for this head".  Rows are append-only (trigger).

### Table: `integration_promotion_intents`

Crash-safe promotion of a source branch onto a target branch.  A *child*
intent promotes one task's reviewed head onto its parent's branch; a *root*
intent promotes a green candidate onto `main` and additionally carries the
project lease, branch fence and CI evidence that authorised the push.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Intent id |
| `domain_key` | TEXT | NOT NULL UNIQUE | Idempotency key of the promotion |
| `operation_key` | TEXT | nullable | Owning operation key |
| `project_id` | TEXT | nullable | Project |
| `receipt_id` | TEXT | NOT NULL | `task_delivery_receipts.id` this intent will produce |
| `source_task_id` | TEXT | nullable | Child intents only |
| `target_task_id` | TEXT | nullable | Child intents only |
| `source_head` | TEXT | NOT NULL | Source head sha |
| `source_base` | TEXT | NOT NULL | Source base sha |
| `repository_id` | TEXT | NOT NULL | Repository |
| `origin_url` | TEXT | nullable | Remote URL used |
| `target_branch` | TEXT | NOT NULL | Branch being advanced |
| `expected_target` | TEXT | NOT NULL | Target sha the push is conditioned on |
| `prepared_sha` | TEXT | nullable | Squash commit prepared locally; once set the identity columns are immutable (`trg_integration_prepared_identity_immutable`) |
| `recovery_ref` | TEXT | nullable | Ref holding the prepared commit for crash recovery |
| `fence_owner_id` | TEXT | NOT NULL | Branch owner that prepared it |
| `fence_token` | INTEGER | NOT NULL, `>= 0` | Owner's fence token |
| `state` | TEXT | NOT NULL | One of: reserved, prepared, pushed, reconciled, committed, conflict, resolution_reserved, superseded |
| `review_evidence` | JSON | nullable | Pinned review evidence |
| `authors` | JSON | nullable | Author attribution for the squash |
| `provenance` | JSON | nullable | Source commit provenance |
| `commit_metadata` | JSON | nullable | Commit message metadata |
| `conflict_diagnostics` | JSON | nullable | Set when the merge conflicted |
| `resolution_*` | — | — | `resolution_head_sha`, `resolution_tree_sha`, `resolution_commit_shas` (JSON), `resolution_operation_id`, `resolution_stage_ordinal` (`>= 0`), `resolution_task_id`, `resolution_session_id`, `resolution_session_instance_token`, `resolution_workspace_id`, `resolution_fence_owner_id`, `resolution_fence_token` (`>= 0`), `resolution_push_evidence` (JSON): all NULL, or all set with state in (resolution_reserved, committed) (`ck_integration_promotion_intents_resolution_binding`) |
| `remote_evidence` | JSON | nullable | Remote head observed after the push; required once committed (`ck_integration_promotion_intents_committed_evidence`) |
| `committed_at` | REAL | nullable | When the target advanced |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |
| `intent_kind` | TEXT | NOT NULL DEFAULT 'child' | `child` or `root` (`ck_integration_promotion_intents_kind_binding` ties the columns below to `root`) |
| `root_batch_id` | TEXT | nullable | Root only; with `root_candidate_revision` REFERENCES `integration_candidate_revisions` ON DELETE RESTRICT |
| `root_candidate_revision` | INTEGER | nullable, `>= 0` | Root only |
| `project_lease_owner_id` | TEXT | nullable | Root only: `project_integration_leases` owner |
| `project_lease_fence_token` | INTEGER | nullable, `>= 0` | Root only |
| `branch_fence_owner_id` | TEXT | nullable | Root only: `main` branch owner |
| `branch_fence_token` | INTEGER | nullable, `>= 0` | Root only |
| `ci_evidence_id` | TEXT | nullable | Root only: green `integration_check_evidence` |

Partial unique index `uq_integration_promotion_intents_unresolved_target`
allows one unresolved intent per `(repository_id, target_branch)`;
`uq_integration_promotion_intents_root_identity` is the composite FK target
for `integration_root_intent_members`.  Root intents cannot leave a terminal
state (`trg_integration_root_intent_terminal`).

### Table: `task_delivery_receipts`

Append-only proof that a task's work was delivered to (or dispositioned out
of) its target branch.  Parent completion is proven by a chain of receipts,
not by resolution JSON; the `e9b2f1b7c3d5` triggers reject every `UPDATE`
and `DELETE`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Receipt id |
| `domain_key` | TEXT | NOT NULL UNIQUE | Idempotency key |
| `source_task_id` | TEXT | nullable | Delivered task; indexed with `repository_id` |
| `target_task_id` | TEXT | nullable | Parent receiving the delivery |
| `repository_id` | TEXT | NOT NULL | Repository |
| `target_branch` | TEXT | NOT NULL | Branch advanced |
| `workspace_kind` | TEXT | nullable | Workspace kind used |
| `source_pr` | TEXT | nullable | Source PR URL |
| `reviewed_head_sha` | TEXT | nullable | Reviewed head |
| `reviewed_tree_sha` | TEXT | nullable | Reviewed tree |
| `before_sha` | TEXT | nullable | Target before |
| `squash_sha` | TEXT | nullable | Squash commit written |
| `after_sha` | TEXT | nullable | Target after |
| `review_evidence` | JSON | nullable | Pinned review evidence |
| `verification_evidence` | JSON | nullable | Parent verification evidence |
| `resolution_evidence` | JSON | nullable | Required unless `disposition = 'code'` (`ck_task_delivery_receipts_disposition_evidence`) |
| `batch_id` | TEXT | nullable | Root-train receipts: all three of `batch_id`, `member_ordinal`, `candidate_revision` set or none (`ck_task_delivery_receipts_root_tuple`, unique when set); FKs to `integration_batch_members` and `integration_candidate_member_results` ON DELETE RESTRICT |
| `member_ordinal` | INTEGER | nullable, `>= 0` | Sealed member ordinal |
| `candidate_revision` | INTEGER | nullable, `>= 0` | Promoted candidate revision |
| `disposition` | TEXT | NOT NULL | One of: code, noop, ineligible, skipped, failed |
| `disposition_revision` | INTEGER | nullable | `integration_child_dispositions.revision` that produced a non-code receipt |
| `parent_operation_id` | TEXT | nullable | Set with `parent_episode_id` (`ck_task_delivery_receipts_parent_binding`); REFERENCES `integration_repair_operations(id)` ON DELETE RESTRICT |
| `parent_episode_id` | TEXT | nullable | `(target_task_id, parent_episode_id)` REFERENCES `integration_parent_episodes(parent_task_id, id)` ON DELETE RESTRICT |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_batches`

One integration train per sweep: the ordered set of reviewed source branches
that will be built into a candidate and promoted to `main` together.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Batch id |
| `project_id` | TEXT | NOT NULL | With `request_id`: UNIQUE (`uq_integration_batches_project_request`) |
| `repository_id` | TEXT | NOT NULL | Integration repository |
| `request_id` | TEXT | NOT NULL | `project_integration_schedules` request that opened it |
| `trigger` | TEXT | nullable | periodic / manual |
| `source_manifest_digest` | TEXT | NOT NULL | Digest of the sealed member set |
| `base_sha` | TEXT | nullable | `main` at seal time; NULL only when `lifecycle = 'empty'` (`ck_integration_batches_empty_identity`) |
| `lifecycle` | TEXT | NOT NULL | One of: sealing, sealed, building, testing, repairing, human_blocked, promoting, cleanup_pending, promoted, aborted, failed, empty. Cannot return to `sealing`; identity columns are immutable after sealing (triggers) |
| `current_revision` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Latest candidate revision; monotone (trigger) |
| `integration_branch` | TEXT | nullable | Candidate branch; NULL only when empty |
| `pr_url` | TEXT | nullable | Audit PR for the candidate |
| `repair_stage_ordinal` | INTEGER | nullable, `>= 0` | Active repair stage |
| `tested_candidate_sha` | TEXT | nullable | Candidate head CI ran on |
| `ci_evidence_id` | TEXT | nullable | Green `integration_check_evidence` |
| `final_main_sha` | TEXT | nullable | `main` after promotion |
| `human_abort_reason` | TEXT | nullable | Operator abort reason |
| `policy_snapshot` | JSON | NOT NULL | Project policy frozen at seal |
| `artifact_snapshot` | JSON | NOT NULL | Playbook artifact pins frozen at seal |
| `cleanup_state` | TEXT | NOT NULL | Aggregate state of `integration_cleanup_items` |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

Partial unique index `uq_integration_batches_active_project` allows one
non-terminal batch per project.

### Table: `integration_batch_members`

The sealed, ordered members of a batch.  Append-only: the
`trg_integration_members_*` triggers reject insert after sealing and every
update/delete.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PK (with `ordinal`) | Batch |
| `ordinal` | INTEGER | PK, `>= 0` | Application order |
| `task_id` | TEXT | NOT NULL | Source task; UNIQUE per batch (`uq_integration_batch_members_task`) |
| `pr_url` | TEXT | nullable | Source PR |
| `repository_id` | TEXT | NOT NULL | Repository |
| `source_base_sha` | TEXT | NOT NULL | Source branch base |
| `reviewed_head_sha` | TEXT | NOT NULL | Pinned reviewed head |
| `reviewed_tree_sha` | TEXT | NOT NULL | Pinned reviewed tree |
| `source_ref` | TEXT | nullable | `refs/heads/…` retained for cleanup; NULL only for batches sealed before retention was persisted (`ck_integration_batch_members_source_retention`) |
| `source_ref_retention` | TEXT | nullable | `delete` or `retain` |
| `review_evidence_id` | TEXT | NOT NULL REFERENCES integration_review_evidence(id) ON DELETE RESTRICT | Approval pinned |
| `review_evidence` | JSON | NOT NULL | Copy of the approval |

`uq_integration_batch_members_root_identity` (ordinal + identity tuple) is
the composite FK target for `integration_root_intent_members`.

### Table: `integration_candidate_revisions`

Each attempt to build the batch into a candidate branch.  A repair produces a
new revision whose `repair_parent_revision` points at the red one.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PK (with `revision`) | Batch |
| `revision` | INTEGER | PK, `>= 0` | Revision number |
| `construction_base_sha` | TEXT | NOT NULL | `main` the candidate was built on |
| `next_member_ordinal` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Resume cursor for crash-safe construction |
| `repair_parent_revision` | INTEGER | nullable, `>= 0` | Revision this one repairs |
| `head_sha` | TEXT | nullable | Candidate head once built |
| `ci_evidence_id` | TEXT | nullable | CI evidence for `head_sha` |
| `state` | TEXT | NOT NULL | One of: constructing, built, testing, green, red, superseded, promoted |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_candidate_member_results`

Per-member outcome of applying a batch member to a candidate revision.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PK | With `revision` REFERENCES `integration_candidate_revisions`; with `member_ordinal` REFERENCES `integration_batch_members` |
| `revision` | INTEGER | PK, `>= 0` | Candidate revision |
| `member_ordinal` | INTEGER | PK, `>= 0` | Member applied |
| `input_head_sha` | TEXT | NOT NULL | Member head applied |
| `input_tree_sha` | TEXT | NOT NULL | Member tree applied |
| `generated_squash_sha` | TEXT | nullable | Squash commit; required when `result = 'applied'` |
| `result` | TEXT | NOT NULL | One of: pending, applied, conflict, skipped |
| `conflict_evidence` | JSON | nullable | Conflict diagnostics |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

`uq_integration_candidate_results_root_identity` is the composite FK target
for `integration_root_intent_members` and `task_delivery_receipts`.

### Table: `integration_candidate_publications`

Durable authority for publishing a candidate revision: the ref push and the
audit PR are reserved here before GitHub is touched so a crash cannot
publish twice.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PK (with `revision`) | With `revision` REFERENCES `integration_candidate_revisions` ON DELETE RESTRICT |
| `revision` | INTEGER | PK, `>= 0` | Candidate revision |
| `state` | TEXT | NOT NULL | One of: reserved, ref_published, pr_reserved, pr_published (monotone, trigger) |
| `repository_id` | TEXT | NOT NULL | Repository |
| `repository_numeric_id` | INTEGER | NOT NULL, `> 0` | GitHub repository id |
| `repository_full_name` | TEXT | NOT NULL | `owner/name` |
| `base_ref` | TEXT | NOT NULL | PR base |
| `head_ref` | TEXT | NOT NULL | Published candidate ref |
| `head_sha` | TEXT | NOT NULL | Published sha |
| `expected_old_sha` | TEXT | NOT NULL | Compare-and-swap value for the ref push |
| `idempotency_key` | TEXT | NOT NULL UNIQUE | GitHub idempotency key |
| `pr_number` | INTEGER | nullable | Set with `pr_url` only when `pr_published` (`ck_integration_candidate_publications_pr_identity`) |
| `pr_url` | TEXT | nullable | Audit PR |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_candidate_resolutions`

A repair delegate's conflict resolution for one member of a candidate:
reserved with the delegate's exact session/workspace identity, pushed under
a fence, then accepted into the next revision.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Resolution id |
| `batch_id` | TEXT | NOT NULL | With `revision`, `member_ordinal`: UNIQUE and REFERENCES `integration_candidate_member_results` ON DELETE RESTRICT |
| `revision` | INTEGER | NOT NULL, `>= 0` | Candidate revision resolved |
| `member_ordinal` | INTEGER | NOT NULL, `>= 0` | Member resolved |
| `operation_id` | TEXT | NOT NULL | With `stage_ordinal` REFERENCES `integration_repair_stages` ON DELETE RESTRICT |
| `operation_episode_id` | TEXT | NOT NULL | Repair operation episode |
| `stage_ordinal` | INTEGER | NOT NULL | 0 or 1 |
| `stage_deadline_at` | REAL | NOT NULL | Stage deadline the resolution must land by |
| `project_id` | TEXT | NOT NULL | Project |
| `repair_task_id` | TEXT | NOT NULL REFERENCES tasks(id) | Delegate task |
| `repair_session_id` | TEXT | NOT NULL REFERENCES sessions(id) | Delegate session |
| `repair_session_instance_token` | TEXT | NOT NULL | Session instance the writer authenticated as |
| `repair_workspace_id` | TEXT | NOT NULL REFERENCES workspaces(id) | Delegate workspace |
| `repair_workspace_path` | TEXT | NOT NULL | Workspace path |
| `repository_id` | TEXT | NOT NULL | Repository |
| `branch` | TEXT | NOT NULL | Resolution branch |
| `target_branch` | TEXT | NOT NULL | Candidate branch |
| `target_kind` | TEXT | NOT NULL | `qualified` or `legacy_integration` |
| `fence_owner_id` | TEXT | NOT NULL | Branch owner |
| `fence_token` | INTEGER | NOT NULL, `>= 0` | Fence token |
| `handoff_owner_id` | TEXT | nullable | Set with `handoff_fence_token` when ownership is handed back (`ck_integration_candidate_resolutions_handoff`) |
| `handoff_fence_token` | INTEGER | nullable, `>= 0` | See above |
| `partial_head_sha` | TEXT | NOT NULL | Candidate head before the member |
| `source_base_sha` | TEXT | NOT NULL | Member base |
| `source_head_sha` | TEXT | NOT NULL | Member head |
| `resolved_head_sha` | TEXT | NOT NULL | Resolved head |
| `resolved_tree_sha` | TEXT | NOT NULL | Resolved tree |
| `repair_commit_shas` | JSON | NOT NULL | Commits the delegate authored |
| `push_evidence` | JSON | nullable | Required once `pushed`/`accepted`; immutable after (trigger) |
| `state` | TEXT | NOT NULL | One of: reserved, pushed, accepted (monotone) |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_candidate_ref_mutations`

Prewrite log for every external ref mutation the train performs.  A row is
reserved (with expected/desired sha, lease, branch fence and nonce) before
the push, then marked applied with the observed remote sha; on restart the
log is replayed before any new mutation is attempted.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Mutation id |
| `batch_id` | TEXT | NOT NULL | With `revision` REFERENCES `integration_candidate_revisions` ON DELETE RESTRICT |
| `revision` | INTEGER | NOT NULL, `>= 0` | Candidate revision |
| `member_ordinal` | INTEGER | nullable, `>= 0` | Member for partial pushes |
| `resolution_id` | TEXT | nullable REFERENCES integration_candidate_resolutions(id) ON DELETE RESTRICT | Resolution being pushed |
| `purpose` | TEXT | NOT NULL | One of: candidate_final, candidate_partial, repair_resolution, repair_handoff, root_main |
| `repository_id` | TEXT | NOT NULL | Repository |
| `branch` | TEXT | NOT NULL | Ref mutated |
| `target_branch` | TEXT | NOT NULL | Logical target |
| `expected_old_sha` | TEXT | NOT NULL | Compare-and-swap value |
| `desired_sha` | TEXT | NOT NULL | Sha to write |
| `operation_id` | TEXT | NOT NULL | Owning operation |
| `operation_episode_id` | TEXT | NOT NULL | Operation episode |
| `operation_stage` | INTEGER | NOT NULL | 0 or 1 |
| `lease_owner_id` | TEXT | NOT NULL | Project lease owner |
| `lease_fence_token` | INTEGER | NOT NULL, `>= 0` | Lease fence |
| `branch_owner_id` | TEXT | NOT NULL | Branch owner |
| `branch_owner_role` | TEXT | NOT NULL | Owner role |
| `branch_fence_token` | INTEGER | NOT NULL, `>= 0` | Branch fence |
| `nonce` | TEXT | NOT NULL | Push nonce embedded in the commit trailer |
| `state` | TEXT | NOT NULL | reserved → applied (`remote_sha = desired_sha`), or superseded (root_main only) (`ck_integration_candidate_ref_mutations_remote`) |
| `expires_at` | REAL | NOT NULL | Reservation expiry |
| `remote_sha` | TEXT | nullable | Sha observed after the push |
| `prewrite_at` | REAL | nullable | When the prewrite was durably recorded; root prewrites are immutable (`trg_integration_root_prewrite_immutable`) |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_root_intent_members`

Exact, append-only binding of a root promotion intent to the batch members,
candidate results and review evidence it promotes.  Every FK is composite
over the identity tuple so a rebuilt member or re-review cannot satisfy an
older intent.  `receipt_id` is the delivery receipt the promotion writes for
that member.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `intent_id` | TEXT | PK (with `member_ordinal`) | With `batch_id`, `candidate_revision` REFERENCES `integration_promotion_intents(id, root_batch_id, root_candidate_revision)` |
| `member_ordinal` | INTEGER | PK, `>= 0` | Member |
| `receipt_id` | TEXT | NOT NULL UNIQUE | Receipt to be written |
| `batch_id` | TEXT | NOT NULL | Batch |
| `candidate_revision` | INTEGER | NOT NULL, `>= 0` | Candidate revision |
| `source_task_id` | TEXT | NOT NULL | Member task |
| `repository_id` | TEXT | NOT NULL | Repository |
| `reviewed_head_sha` | TEXT | NOT NULL | Pinned head |
| `reviewed_tree_sha` | TEXT | NOT NULL | Pinned tree |
| `generated_squash_sha` | TEXT | NOT NULL | Squash from the candidate result |
| `result_evidence` | JSON | NOT NULL | Candidate result evidence |
| `review_evidence_id` | TEXT | NOT NULL | Pinned review |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Composite FKs (all ON DELETE RESTRICT): `fk_…_exact_member` →
`integration_batch_members`, `fk_…_exact_result` →
`integration_candidate_member_results`, `fk_…_exact_review` →
`integration_review_evidence`.

### Table: `integration_repair_operations`

A bounded repair of either a red candidate batch or a parent task whose
collected checkpoint failed verification.  Exactly one of `batch_id` /
`parent_task_id` is set (`ck_integration_repair_operations_target`) and at
most one non-terminal operation may target each (partial unique indexes).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Operation id |
| `target_kind` | TEXT | NOT NULL | `batch` or `parent` |
| `batch_id` | TEXT | nullable | Batch target; UNIQUE (`uq_integration_repair_operations_batch_episode`) |
| `parent_task_id` | TEXT | nullable | Parent target; `(parent_task_id, episode_id)` UNIQUE and REFERENCES `integration_parent_episodes(parent_task_id, id)` ON DELETE RESTRICT |
| `episode_id` | TEXT | NOT NULL | Episode the operation belongs to |
| `active_stage` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Current `integration_repair_stages.ordinal` |
| `state` | TEXT | NOT NULL | One of: active, escalated, human_required, completed, cancelled |
| `policy_snapshot` | JSON | NOT NULL | Repair policy frozen at start |
| `artifact_snapshot` | JSON | NOT NULL | Artifact pins frozen at start |
| `required_check_version` | TEXT | NOT NULL | Required-checks version the evidence must match |
| `verifier_task_id` | TEXT | nullable REFERENCES tasks(id) ON DELETE RESTRICT | Existing verifier reused as writer |
| `route_playbook_id` | TEXT | nullable | Playbook that routed the repair |
| `route_scope` | TEXT | nullable | Routing scope |
| `route_scope_identifier` | TEXT | nullable | Routing scope id |
| `route_activation_id` | TEXT | nullable | Routing activation |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_parent_episodes`

One collection episode of a parent task: the generation and checkpoint the
children were collected against.  Everything a parent verifies, repairs or
accepts is bound to an episode so historic receipts cannot satisfy a later
collection.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Episode id; `(parent_task_id, id)` UNIQUE is the composite FK target |
| `parent_task_id` | TEXT | NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT | Parent |
| `repository_id` | TEXT | NOT NULL REFERENCES repos(id) ON DELETE RESTRICT | Repository |
| `generation` | INTEGER | NOT NULL, `>= 0` | Parent checkpoint generation |
| `pre_collection_checkpoint_sha` | TEXT | NOT NULL | Parent head before collection |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_child_dispositions`

How a parent's collection treated each child that did not deliver code.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `parent_task_id` | TEXT | PK (with `child_task_id`) | With `parent_episode_id` REFERENCES `integration_parent_episodes(parent_task_id, id)` ON DELETE RESTRICT |
| `child_task_id` | TEXT | PK | Child |
| `revision` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Bumped on every change; copied to `task_delivery_receipts.disposition_revision` |
| `disposition` | TEXT | nullable | One of: noop, ineligible, skipped; NULL = undecided |
| `parent_operation_id` | TEXT | NOT NULL REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Operation that decided |
| `parent_episode_id` | TEXT | NOT NULL | Episode |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_repair_stages`

The (at most two) escalating stages of a repair operation, each with its own
policy, writer and deadline.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `operation_id` | TEXT | PK (with `ordinal`) | Operation |
| `ordinal` | INTEGER | PK | 0 or 1 |
| `policy` | JSON | NOT NULL | Stage policy |
| `intelligence_class` | TEXT | nullable | Class the repair delegate runs at |
| `profile_id` | TEXT | nullable | Delegate profile |
| `repair_task_id` | TEXT | nullable | Set with `writer_kind` (`ck_integration_repair_stages_writer_binding`) |
| `writer_kind` | TEXT | nullable | `repair_delegate` or `existing_verifier` |
| `starting_sha` | TEXT | NOT NULL | Head the stage started from |
| `trigger_id` | TEXT | nullable | Event that started the stage |
| `current_subject` | JSON | nullable | Subject (candidate/parent head) under repair |
| `deadline_event_id` | TEXT | nullable UNIQUE | Scheduled deadline event |
| `success_subject` | JSON | nullable | Subject that passed |
| `success_evidence_id` | TEXT | nullable | Evidence that passed |
| `retained_workspace_id` | TEXT | nullable | Delegate workspace kept for handoff |
| `retained_handoff` | JSON | nullable | Handoff record |
| `started_at` | REAL | nullable | Unix timestamp |
| `deadline_at` | REAL | nullable | Unix timestamp |
| `attempts` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Counted attempts |
| `dossier` | JSON | nullable | Debug dossier handed to the next stage / human |
| `state` | TEXT | NOT NULL | One of: pending, active, awaiting_completion, passed, failed, expired, cancelled |
| `completed_at` | REAL | nullable | Unix timestamp |

### Table: `integration_check_evidence`

Authenticated CI evidence for exactly one subject: a candidate revision
(`batch_id` + `candidate_revision`) or a parent head (`parent_task_id` +
`parent_generation` + `parent_head_sha`) — never both
(`ck_integration_check_evidence_subject`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Evidence id |
| `operation_id` | TEXT | nullable | Repair operation observing it |
| `batch_id` | TEXT | nullable | Candidate subject |
| `candidate_revision` | INTEGER | nullable | Candidate subject |
| `parent_task_id` | TEXT | nullable | Parent subject |
| `parent_generation` | INTEGER | nullable | Parent subject |
| `parent_head_sha` | TEXT | nullable | Parent subject |
| `producer_id` | TEXT | NOT NULL | GitHub App / workflow producer; `(producer_id, run_id, attempt, required_check_version)` UNIQUE |
| `workflow_id` | TEXT | NOT NULL | Workflow id |
| `run_id` | TEXT | NOT NULL | Workflow run |
| `attempt` | INTEGER | NOT NULL, `>= 0` | Run attempt |
| `required_check_version` | TEXT | NOT NULL | Required-checks version evaluated |
| `checks` | JSON | NOT NULL | Per-check results |
| `conclusion` | TEXT | NOT NULL | One of: success, failure, pending, cancelled, inconclusive |
| `classification` | TEXT | NOT NULL | Repair classification of the failure |
| `observed_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_attestation_publications`

Exclusive claim to publish a candidate's attestation (GitHub check run) so a
restart cannot publish the same subject twice; one per `(batch_id, revision)`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Publication id |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `batch_id` | TEXT | NOT NULL | With `revision`: UNIQUE and REFERENCES `integration_candidate_revisions` ON DELETE RESTRICT |
| `revision` | INTEGER | NOT NULL, `>= 0` | Candidate revision |
| `operation_id` | TEXT | NOT NULL REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Operation publishing |
| `head_sha` | TEXT | NOT NULL | Attested head |
| `ci_evidence_id` | TEXT | NOT NULL REFERENCES integration_check_evidence(id) ON DELETE RESTRICT | Evidence attested |
| `external_id` | TEXT | NOT NULL UNIQUE | Check-run external id |
| `execution_nonce` | TEXT | NOT NULL | Nonce for the publishing execution |
| `state` | TEXT | NOT NULL | `reserved` (no `check_run_id`) or `published` (`prewrite_at` set, `check_run_id > 0`) (`ck_integration_attestation_publications_result`) |
| `prewrite_at` | REAL | nullable | Prewrite timestamp |
| `check_run_id` | INTEGER | nullable | GitHub check run id |
| `expires_at` | REAL | NOT NULL | Claim expiry |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_cleanup_items`

Normalized, retryable cleanup work after a batch is promoted or aborted: one
row per source PR, audit PR, remote ref, local ref or worktree.  `kind`
decides which target columns must be set (`ck_integration_cleanup_items_target`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PK, REFERENCES integration_batches(id) ON DELETE RESTRICT | Batch |
| `kind` | TEXT | PK | One of: source_pr, audit_pr, remote_ref, local_ref, worktree |
| `identity` | TEXT | PK | Target identity within the kind |
| `domain_key` | TEXT | NOT NULL UNIQUE | Idempotency key |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `repository_id` | TEXT | NOT NULL REFERENCES repos(id) ON DELETE RESTRICT | Repository |
| `repository_numeric_id` | INTEGER | NOT NULL, `> 0` | GitHub repository id |
| `repository_full_name` | TEXT | NOT NULL | `owner/name` |
| `revision` | INTEGER | NOT NULL, `>= 0` | Candidate revision |
| `member_ordinal` | INTEGER | nullable | `source_pr` (required) / `remote_ref` (optional) |
| `receipt_id` | TEXT | nullable REFERENCES task_delivery_receipts(id) ON DELETE RESTRICT | `source_pr` only |
| `target_ref` | TEXT | nullable | `remote_ref` / `local_ref` |
| `target_pr_number` | INTEGER | nullable | `source_pr` / `audit_pr` |
| `target_pr_url` | TEXT | nullable | `source_pr` / `audit_pr` |
| `workspace_path` | TEXT | nullable | `worktree` |
| `expected_sha` | TEXT | NOT NULL | Lower-case 40-char sha the target must still be at |
| `state` | TEXT | NOT NULL | pending, retryable (open) or complete, conflict, failed (terminal, `terminal_at` set, claim cleared) |
| `attempts` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Attempts so far |
| `next_attempt_at` | REAL | NOT NULL | Due time; indexed for open items |
| `execution_nonce` | TEXT | nullable | Set with `claim_expires_at` while an executor holds the item |
| `claim_expires_at` | REAL | nullable | Claim expiry |
| `irreversible_nonce` | TEXT | nullable | Set with `irreversible_prewrite_at` before an irreversible step (PR close, ref delete); immutable after (trigger) |
| `irreversible_prewrite_at` | REAL | nullable | Prewrite timestamp |
| `last_error` | TEXT | nullable | Last failure |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |
| `terminal_at` | REAL | nullable | When the item reached a terminal state |

### Table: `integration_repair_stage_evidence`

Which CI evidence a repair stage has already consumed, and whether it
counted as an attempt.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `operation_id` | TEXT | PK | With `ordinal` REFERENCES `integration_repair_stages` ON DELETE RESTRICT |
| `ordinal` | INTEGER | PK | Stage |
| `evidence_id` | TEXT | PK, UNIQUE, REFERENCES integration_check_evidence(id) ON DELETE RESTRICT | Evidence consumed |
| `counted_attempt` | BOOLEAN | NOT NULL DEFAULT false | Consumed an attempt |
| `result_outcome` | TEXT | NOT NULL | Outcome derived |
| `result_action` | TEXT | NOT NULL | Action taken |
| `recorded_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_parent_verifications`

A verification of a parent's collected head within an episode.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Verification id |
| `operation_id` | TEXT | NOT NULL REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Operation |
| `parent_task_id` | TEXT | NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT | Parent; `(parent_task_id, episode_id)` REFERENCES `integration_parent_episodes` |
| `episode_id` | TEXT | NOT NULL | Episode |
| `generation` | INTEGER | NOT NULL, `>= 0` | Generation verified |
| `head_sha` | TEXT | NOT NULL | Head verified |
| `required_check_version` | TEXT | NOT NULL | Required-checks version |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Unique: `(operation_id, generation, head_sha)`, `(parent_task_id, id)` and
`(operation_id, id, parent_task_id, episode_id)` (composite FK targets).

### Table: `integration_parent_operation_completions`

The single verification that completed a parent repair operation.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `operation_id` | TEXT | PRIMARY KEY, REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Completed operation |
| `verification_id` | TEXT | NOT NULL UNIQUE | With `operation_id`, `parent_task_id`, `episode_id` REFERENCES `integration_parent_verifications` ON DELETE RESTRICT |
| `parent_task_id` | TEXT | NOT NULL | Parent |
| `episode_id` | TEXT | NOT NULL | Episode |
| `completed_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_episode_receipt_acceptances`

Accepts a receipt written under an earlier episode into the current one,
recording the ancestry check that made it valid.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `episode_id` | TEXT | PK (with `receipt_id`), REFERENCES integration_parent_episodes(id) ON DELETE RESTRICT | Accepting episode |
| `receipt_id` | TEXT | PK, REFERENCES task_delivery_receipts(id) ON DELETE RESTRICT | Accepted receipt |
| `operation_id` | TEXT | NOT NULL REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Accepting operation |
| `previous_episode_id` | TEXT | NOT NULL REFERENCES integration_parent_episodes(id) ON DELETE RESTRICT | Episode the receipt was written under |
| `previous_operation_id` | TEXT | NOT NULL REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Operation that wrote it |
| `previous_verification_id` | TEXT | NOT NULL REFERENCES integration_parent_verifications(id) ON DELETE RESTRICT | Verification that covered it |
| `ancestry_from_sha` | TEXT | NOT NULL | Verified head |
| `ancestry_to_sha` | TEXT | NOT NULL | Current head proven to descend from it |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_parent_verification_evidence`

Link table binding a parent verification to the CI evidence it rests on.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `verification_id` | TEXT | PK, REFERENCES integration_parent_verifications(id) ON DELETE RESTRICT | Verification |
| `evidence_id` | TEXT | PK, UNIQUE, REFERENCES integration_check_evidence(id) ON DELETE RESTRICT | Evidence (used by at most one verification) |

### Table: `integration_operation_artifact_pins`

Playbook artifacts a repair operation was started with, pinned so the
artifact store cannot garbage-collect them while the operation runs.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `operation_id` | TEXT | PK, REFERENCES integration_repair_operations(id) ON DELETE RESTRICT | Operation |
| `artifact_sha256` | TEXT | PK, REFERENCES playbook_artifacts(artifact_sha256) ON DELETE RESTRICT | Pinned artifact; indexed |

### Table: `project_integration_schedules`

Durable sweep scheduling per project: the periodic interval, the request
outstanding, and a catch-up request queued behind it.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY | Project |
| `enabled` | BOOLEAN | NOT NULL DEFAULT false | Sweeps run |
| `interval_seconds` | INTEGER | NOT NULL, `> 0` | Cadence |
| `next_due_at` | REAL | NOT NULL | Next periodic sweep |
| `last_observed_window` | REAL | nullable | Last window the scheduler observed |
| `request_sequence` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Monotone request counter |
| `outstanding_request_id` | TEXT | nullable | Set together with `outstanding_trigger` and `outstanding_requested_at` (`ck_project_integration_schedules_outstanding_request`) |
| `outstanding_trigger` | TEXT | nullable | periodic / manual |
| `outstanding_requested_at` | REAL | nullable | Unix timestamp |
| `catchup_trigger` | TEXT | nullable | `periodic` or `manual`, set together with `catchup_requested_at` and `catchup_after_sequence` (`ck_project_integration_schedules_catchup`) |
| `catchup_requested_at` | REAL | nullable | Unix timestamp |
| `catchup_after_sequence` | INTEGER | nullable, `>= 0` | Request the catch-up waits behind |
| `last_completed_sweep_at` | REAL | nullable | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `project_integration_leases`

The single fenced lease a train holds on a project while it builds, tests
and promotes a batch.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY | Project |
| `repository_id` | TEXT | NOT NULL | Repository |
| `batch_id` | TEXT | NOT NULL | Batch the lease serves |
| `owner_id` | TEXT | NOT NULL | Lease owner |
| `fence_token` | INTEGER | NOT NULL, `>= 0` | Monotone fence |
| `heartbeat_at` | REAL | NOT NULL | Last heartbeat |
| `expires_at` | REAL | NOT NULL, `>= heartbeat_at` | Expiry |

### Table: `integration_release_results`

Immutable record that a promoted batch was released (lease dropped, cleanup
queued); one per batch, `UPDATE`/`DELETE` rejected by trigger.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `batch_id` | TEXT | PRIMARY KEY, REFERENCES integration_batches(id) ON DELETE RESTRICT | Released batch |
| `project_id` | TEXT | NOT NULL | Project |
| `request_id` | TEXT | NOT NULL | Sweep request |
| `operation_id` | TEXT | NOT NULL | Releasing operation |
| `catchup_request_id` | TEXT | nullable | Catch-up request retained for the release |
| `released_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_history_waivers`

Operator waiver of the historic blockers (pre-rollout receipts, legacy gates)
that would otherwise stop a rollout transition.  Append-only.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Waiver id |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `operator_id` | TEXT | NOT NULL, non-empty | Operator |
| `reason` | TEXT | NOT NULL, non-empty | Reason |
| `blocker_digest` | TEXT | NOT NULL | `sha256:` + 64 hex digest of the blocker set waived |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_rollout_transitions`

Append-only log of every rollout mode change, keyed by the project's
generation; the source of truth `projects.hierarchical_integration_*` is
projected from.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Transition id |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE RESTRICT | With `generation`: UNIQUE |
| `generation` | INTEGER | NOT NULL, `> 0` | Generation after the transition |
| `old_effective_mode` | TEXT | NOT NULL | disabled / observe / hierarchy / train |
| `new_effective_mode` | TEXT | NOT NULL | Same set |
| `old_desired_mode` | TEXT | NOT NULL | Same set |
| `new_desired_mode` | TEXT | NOT NULL | Same set |
| `draining` | BOOLEAN | NOT NULL DEFAULT false | Transition started a drain |
| `operator_id` | TEXT | NOT NULL, non-empty | Operator |
| `reason` | TEXT | NOT NULL, non-empty | Reason |
| `blocker_digest` | TEXT | NOT NULL | `sha256:` digest of blockers at transition time |
| `old_legacy_policy` | JSON | NOT NULL | Legacy-route suppression before |
| `new_legacy_policy` | JSON | NOT NULL | Legacy-route suppression after |
| `waiver_id` | TEXT | nullable REFERENCES integration_history_waivers(id) ON DELETE RESTRICT | Waiver consumed |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_history_waiver_consumptions`

A waiver is consumed by exactly one transition, and for the blocker digest
it was issued against.  Append-only.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `waiver_id` | TEXT | PRIMARY KEY, REFERENCES integration_history_waivers(id) ON DELETE RESTRICT | Waiver |
| `transition_id` | TEXT | NOT NULL UNIQUE REFERENCES integration_rollout_transitions(id) ON DELETE RESTRICT | Consuming transition |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `blocker_digest` | TEXT | NOT NULL | `sha256:` digest matched |
| `consumed_by` | TEXT | NOT NULL, non-empty | Actor |
| `consumed_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_legacy_gate_applicability`

Per-gate evidence of whether a pre-rollout legacy gate still applies after a
waived transition.  Append-only.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PK (with `gate_id`), REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `gate_id` | TEXT | PK, REFERENCES gates(id) ON DELETE RESTRICT | Legacy gate |
| `waiver_id` | TEXT | NOT NULL REFERENCES integration_history_waivers(id) ON DELETE RESTRICT | Waiver |
| `transition_id` | TEXT | NOT NULL REFERENCES integration_rollout_transitions(id) ON DELETE RESTRICT | Transition |
| `blocker_digest` | TEXT | NOT NULL | `sha256:` digest |
| `applicable` | BOOLEAN | NOT NULL | Gate still applies |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_legacy_suppression`

The one deliberately mutable rollout projection: which legacy routes (merge
sweep, final-review route, legacy gate creation) the current mode suppresses
for a project.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY, REFERENCES projects(id) ON DELETE RESTRICT | Project |
| `generation` | INTEGER | NOT NULL, `>= 0` | Rollout generation projected |
| `merge_sweep_suppressed` | BOOLEAN | NOT NULL DEFAULT false | Legacy merge sweep off |
| `final_review_route_suppressed` | BOOLEAN | NOT NULL DEFAULT false | Legacy final-review route off |
| `legacy_gate_creation_suppressed` | BOOLEAN | NOT NULL DEFAULT false | Legacy gate creation off |
| `policy_snapshot` | JSON | NOT NULL | Policy the projection derives from |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_outbox`

Durable event outbox for correctness-critical integration events: rows are
written in the same transaction as the state change and delivered to the
bus by the reconciler, with per-destination acceptance tracking.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Outbox row id |
| `dedup_key` | TEXT | NOT NULL UNIQUE | Idempotency key |
| `project_id` | TEXT | NOT NULL | Project |
| `event_type` | TEXT | NOT NULL | Bus event type |
| `payload` | JSON | NOT NULL | Event payload |
| `destination_manifest` | JSON | nullable | Pinned destinations (playbook activations) that must accept it |
| `acceptance_cursor` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Destinations accepted so far; monotone (trigger) |
| `available_at` | REAL | NOT NULL | Earliest delivery; indexed for undelivered rows |
| `delivered_at` | REAL | nullable | Delivered |
| `attempts` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Monotone (trigger) |
| `last_error` | TEXT | nullable | Last delivery error |
| `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `integration_outbox_artifact_pins`

Playbook artifacts an outbox event pins until it is delivered.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `event_id` | TEXT | PK, REFERENCES integration_outbox(id) ON DELETE CASCADE | Outbox row |
| `artifact_sha256` | TEXT | PK, REFERENCES playbook_artifacts(artifact_sha256) ON DELETE RESTRICT | Pinned artifact; indexed |

---

## 4. Projects

### `create_project(project: Project) -> None`

Inserts a new row into `projects`. The `created_at` value is always `time.time()` — the value on the `Project` dataclass is ignored. The `status` field is serialized from `ProjectStatus.value`. The `discord_control_channel_id` column is **not** written by this method (only `discord_channel_id` is). Commits after insert.

`create_project` is a database primitive, not the dashboard repository-onboarding
flow. `onboard_project` owns the user-facing orchestration of a project, its
primary `project-repo` workspace, and vault setup from a configured
root-relative destination; it does not permit arbitrary paths.

### `project_onboarding_requests`

The onboarding service persists an idempotency and recovery record for every
request. Each row stores the request ID, normalized-input fingerprint, status,
current phase, request-owned resource ledger, scrubbed result or error, and
timestamps. The ledger contains only identifiers and paths needed for bounded
recovery; it never contains GitHub credentials. Terminal records follow the
operational-event retention policy. This record permits safe replay after an
interrupted process without treating GitHub, filesystem, vault, and database
operations as one transaction.

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

The `hooks`, `hook_runs`, and Playbook V1 persistence tables were **removed**. Event- and time-triggered automation is expressed as playbooks — markdown DAGs compiled to JSON — and each execution is a row in `playbook_v2_runs` (see `docs/specs/design/playbooks.md`). Durable query support lives under `src/database/queries/`; workflow pipelines build on those V2 runs.

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
