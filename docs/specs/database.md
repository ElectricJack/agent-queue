---
tags: [spec, database]
---

# Database Specification

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../README.md) for what AQ does today, and
> see [historical material](../history/README.md) for how this material is
> organised.

## 1. Overview

`Database` (an alias for `PostgreSQLDatabaseAdapter`) is the sole persistence layer. **PostgreSQL is the only supported backend** — SQLite was removed on 2026-09-07, see [superpowers/specs/2026-09-07-sqlite-removal-implementation](../superpowers/specs/2026-09-07-sqlite-removal-implementation.md). It wraps a SQLAlchemy async engine over `asyncpg`, exposed through async methods organized by domain (projects, repos, tasks, dependencies, agents, token ledger, task results, events, system config, rate limits).

All database interaction is async. The `Database` object is constructed with a **PostgreSQL DSN** — anything else is a hard error, not a fall-through to a file — then explicitly initialized with `initialize()` before use. `initialize()` runs the Alembic chain, which returns immediately when the database is already stamped at this checkout's head.

The one-way legacy SQLite importer excludes the durable record and knowledge tables
introduced by PostgreSQL revision `a00000000055`; legacy SQLite files never contained
them. Import requires an empty target apart from Alembic bookkeeping and the immutable
`record_installation` identity seed created with the schema. That installation identity
is preserved. Any other target data, including record scopes or knowledge, refuses import.
Task record backfill and knowledge import remain separate, explicit operations.

The class uses a convention of thin `_row_to_<model>` private methods to map result rows into typed dataclass instances from `src/models.py` (see [specs/models-and-state-machine](models-and-state-machine.md)). Update methods accept arbitrary `**kwargs` and build parameterized `SET` clauses dynamically, converting enum values to their `.value` string automatically.

---

## Source Files
- `src/database/tables.py` — SQLAlchemy Core `Table` definitions (the schema)
- `src/database/engine.py` — engine factory and the Alembic startup upgrade
- `src/database/schema_key.py` — schema identity, shared by the engine and the test template
- `src/database/adapters/postgresql.py` — the backend
- `src/database/legacy_sqlite_import.py` — one-way importer for pre-PostgreSQL installs (`aq db import-sqlite`)
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

1. Opens a SQLAlchemy async engine over `asyncpg`.
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

Every table is declared as a SQLAlchemy Core `Table` in `src/database/tables.py`, which is the single source of truth; DDL is applied by Alembic (`migrations/`). Foreign keys are declared with `ForeignKey(...)`. A `CHECK` constraint exists on `task_dependencies`. Booleans are declared with `Boolean` and `sa.false()`/`sa.true()` server defaults. Timestamps are stored as `REAL` (Unix epoch, floating-point seconds).

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

Question reads also project a nullable `escalation_id` from the durable escalation whose
`source_kind='question'` and `source_identity` matches the question ID. This is a derived link,
not a mutable question column, so it cannot disagree with the escalation's authoritative source
binding.

### Table: `escalations`

Transport-neutral, supervisor-owned human incidents. The unique project/incident key makes source replay idempotent while `source_identity` distinguishes separate attempts. Task and source references are soft audit identity so an incident survives task archival. Conversation changes compare and increment `revision`; terminal states retain an explicit outcome and optional structured evidence.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `project_id` | TEXT | NOT NULL, REFERENCES projects(id) ON DELETE CASCADE |
| `task_id` | TEXT | nullable soft reference |
| `source_kind` | TEXT | NOT NULL |
| `source_identity` | TEXT | NOT NULL |
| `incident_key` | TEXT | NOT NULL |
| `supervisor_owner` | TEXT | NOT NULL logical recipient |
| `task_title` | TEXT | nullable bounded snapshot |
| `task_status` | TEXT | nullable bounded snapshot |
| `summary` | TEXT | NOT NULL |
| `investigation` | TEXT | NOT NULL |
| `decision_requested` | TEXT | NOT NULL |
| `choices` | JSON | nullable |
| `severity` | TEXT | critical, high, medium or low |
| `state` | TEXT | needs_human, reply_received, resolving, resolved, cancelled or stale |
| `revision` | INTEGER | NOT NULL, monotone CAS revision |
| `terminal_outcome` | TEXT | required in terminal states |
| `terminal_evidence` | JSON | nullable |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL |
| `terminal_at` | FLOAT | required in terminal states |

Unique: (`project_id`, `incident_key`) and (`project_id`, `source_kind`, `source_identity`). Indexes: `idx_escalations_project_state`, `idx_escalations_task`.

### Table: `escalation_messages`

Immutable conversation facts. Verified actor identity is supplied by a trusted command boundary. Transport/external-message uniqueness collapses replay. For an accepted open reply, `supervisor_message_id` points to the supervisor notice inserted in the same transaction. `direction` names the author: `inbound` is a verified human reply, `outbound` a supervisor answer (the only direction relayed into the channel thread), and `system` the daemon's own audit note — the §5.6 sweep's `sweep: <rule>` trail, which is neither answered nor relayed.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `escalation_id` | TEXT | NOT NULL, REFERENCES escalations(id) ON DELETE CASCADE |
| `direction` | TEXT | inbound, outbound or system |
| `transport` | TEXT | NOT NULL |
| `verified_actor` | TEXT | NOT NULL |
| `text` | TEXT | NOT NULL, 1–16000 characters |
| `external_message_id` | TEXT | nullable |
| `received_sequence` | BIGINT | nullable |
| `received_at` | FLOAT | NOT NULL |
| `supervisor_message_id` | TEXT | nullable, REFERENCES messages(id) |
| `created_at` | FLOAT | NOT NULL |

Unique: (`transport`, `external_message_id`). Index: `idx_escalation_messages_history`.

### Table: `escalation_actions`

Durable at-most-once reservations for applying a verified inbound human reply through an
action-specific supervisor service. The reply foreign key is the human-evidence binding;
the executor remains the authenticated supervisor. A processing reservation is created
before the external action and completed with its exact outcome, so replay never repeats
task recovery.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `escalation_id` | TEXT | NOT NULL, REFERENCES escalations(id) ON DELETE CASCADE |
| `reply_id` | TEXT | NOT NULL, REFERENCES escalation_messages(id) ON DELETE RESTRICT |
| `idempotency_key` | TEXT | NOT NULL |
| `action_kind` | TEXT | question_answer, gate_resolve or task_recover |
| `target_id` | TEXT | NOT NULL |
| `parameters` | JSON | NOT NULL |
| `executor` | TEXT | NOT NULL authenticated supervisor identity |
| `started_revision` | INTEGER | NOT NULL CAS revision after reservation |
| `status` | TEXT | processing, succeeded or failed |
| `outcome` | TEXT | nullable until completion |
| `result` | JSON | nullable action-specific result |
| `error` | TEXT | nullable failure detail |
| `created_at` | FLOAT | NOT NULL |
| `completed_at` | FLOAT | required after completion |

Unique: (`escalation_id`, `idempotency_key`). Index: `idx_escalation_actions_history`.

### Table: `escalation_deliveries`

External send ownership and receipts, deliberately independent of conversation state. Pending and retry rows are claimable at `next_attempt_at`; expired sending leases are recoverable. An ambiguous external result becomes `unknown` and is not blindly reclaimed.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `escalation_id` | TEXT | NOT NULL, REFERENCES escalations(id) ON DELETE CASCADE |
| `escalation_message_id` | TEXT | nullable, REFERENCES escalation_messages(id) ON DELETE CASCADE |
| `dedup_key` | TEXT | NOT NULL UNIQUE |
| `kind` | TEXT | NOT NULL |
| `payload` | JSON | NOT NULL |
| `priority` | INTEGER | NOT NULL DEFAULT 10 |
| `generation` | INTEGER | NOT NULL DEFAULT 0 |
| `status` | TEXT | pending, sending, sent, retry or unknown |
| `attempt_count` | INTEGER | NOT NULL DEFAULT 0 |
| `next_attempt_at` | FLOAT | NOT NULL |
| `lease_owner` | TEXT | present only while sending |
| `lease_expires_at` | FLOAT | present only while sending |
| `channel_id` | TEXT | nullable |
| `root_message_id` | TEXT | nullable |
| `thread_id` | TEXT | nullable |
| `external_receipt_id` | TEXT | required when sent |
| `receipt_confirmed_at` | FLOAT | required when sent |
| `last_error` | TEXT | nullable |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL |

Indexes: `idx_escalation_deliveries_due`, `idx_escalation_deliveries_escalation`.

### Table: `digest_windows`

One installation-wide durable evaluation per destination/configuration generation/window, including suppressed idle windows. The activity cursor and output hash make restart/catch-up processing deterministic; delivery uses the same recoverable lease and confirmed-receipt semantics as escalation sends.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY |
| `destination` | TEXT | NOT NULL |
| `config_generation` | INTEGER | NOT NULL |
| `window_start` | FLOAT | NOT NULL |
| `window_end` | FLOAT | NOT NULL and greater than window_start |
| `activity_cursor` | JSON | nullable |
| `due_at` | FLOAT | NOT NULL |
| `is_catchup` | BOOLEAN | NOT NULL DEFAULT false |
| `output_hash` | TEXT | nullable |
| `payload` | JSON | nullable |
| `send_status` | TEXT | pending, suppressed, sending, sent, retry or unknown |
| `suppression_reason` | TEXT | required when suppressed |
| `attempt_count` | INTEGER | NOT NULL DEFAULT 0 |
| `lease_owner` | TEXT | present only while sending |
| `lease_expires_at` | FLOAT | present only while sending |
| `external_receipt_id` | TEXT | required when sent |
| `receipt_confirmed_at` | FLOAT | required when sent |
| `last_error` | TEXT | nullable |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL |

Unique: (`destination`, `config_generation`, `window_start`, `window_end`). Index: `idx_digest_windows_due`.

### Table: `supervisor_report_requests`

Durable author requests for supervisor-written digest reports: one row per reserved report window, `reserved` when the scheduler claims an author slot, `requested` once the wake message is queued, and — on submit — the report text lands on the owning `digest_windows` row while this row records the submission.  Each state move bumps `version` for the caller's CAS (`brief_hash` and `expected_version` guard submit against a changed brief); `deadline` bounds the live window (`idx_supervisor_report_requests_state_deadline` walks open work).  `uq_supervisor_report_requests_owner` keeps one request per (`kind`, `owner_ref`) — the digest `window_id` for hourly requests.  Revision `a00000000026` creates the table; its downgrade refuses to drop stored submissions.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY, `report-hourly-<window_id>` |
| `kind` | TEXT | NOT NULL, one of `hourly`, `morning` (`ck_supervisor_report_requests_kind`) |
| `owner_ref` | TEXT | NOT NULL, the `digest_windows.id` the request authors; unique with `kind` (`uq_supervisor_report_requests_owner`) |
| `destination` | TEXT | NOT NULL, copied from the owned window |
| `visibility` | JSON | NOT NULL, the delivery visibility generation at reservation (`invalidate_hourly_visibility` retires stale rows) |
| `brief` | JSON | NOT NULL, the bounded, paged brief an author reads |
| `brief_hash` | TEXT | NOT NULL, digest of `brief`; submit must match it, so an edited brief invalidates a stale author |
| `fallback_text` | TEXT | NOT NULL, what is delivered if no submission lands before the deadline |
| `author_session_id` | TEXT | NOT NULL, the supervisor session that may read the brief and submit |
| `state` | TEXT | NOT NULL, `reserved` (server default), then `requested` when the wake message exists, then `submitted` / `fallback` or `cancelled` (`ck_supervisor_report_requests_state`) |
| `deadline` | FLOAT | NOT NULL, wall time after which the request is closed and the fallback stands; indexed with `state` |
| `version` | INTEGER | NOT NULL, server default 1 (`ck_supervisor_report_requests_version`); bumped on every state move, submitted via `expected_version` |
| `request_message_id` | TEXT | nullable, the queued wake message while `requested` |
| `submitted_text` | TEXT | nullable, the submitted report, mirrored onto the owned window's `payload.text` |
| `submitted_hash` | TEXT | nullable, its digest, recorded as the window's `output_hash` |
| `evidence_refs` | JSON | nullable, evidence cited by the submission; every ref must resolve against the request's `brief` facts at submit |
| `source_links` | JSON | nullable, the `source_url` of each cited evidence ref, resolved server-side at submit |
| `submitted_at` | FLOAT | nullable, set with the submission |
| `skip_reason` | TEXT | nullable, why an open request was retired without a submission (`authoring_disabled`, `visibility_changed`) |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL |

### Table: `supervisor_conversations`

One operator conversation with the addressed supervisor, opened by an allowlisted message in the configured channel (Discord mention-routing spec §4.1, chat-extension spec §2.2, opt-in via `discord.conversation.enabled`). The row, its first input and the supervisor notice are written in one transaction before any transport side effect. `thread_id` is the internal `messages.thread_id` both directions share; `external_thread_id` is set, and the state moves `opening` → `open`, only when the thread-open delivery confirms — or, for a direct message or a thread the operator started, on the first post that has nowhere else to go. `guild_id` is `dm` for a direct message, which has no guild.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY, `conv-<uuid4>` |
| `transport` | TEXT | NOT NULL |
| `guild_id` | TEXT | NOT NULL configured guild |
| `channel_id` | TEXT | NOT NULL configured channel |
| `external_root_message_id` | TEXT | NOT NULL, the opening mention |
| `external_thread_id` | TEXT | nullable until the thread is confirmed |
| `thread_id` | TEXT | NOT NULL UNIQUE, `conversation:<id>` |
| `created_by` | TEXT | NOT NULL verified actor, `human:discord:<id>` |
| `audience` | JSON | NOT NULL allowlist snapshot at open |
| `state` | TEXT | opening, open, closed or delivery_blocked |
| `kind` | TEXT | `thread` (one conversation per opened thread) or `channel` (the channel's one conversation) |
| `created_at` | FLOAT | NOT NULL |
| `updated_at` | FLOAT | NOT NULL, bumped by each input and reply |
| `closed_at` | FLOAT | nullable |

Unique: (`transport`, `external_root_message_id`), `thread_id`, (`transport`, `external_thread_id`) where the thread is set, and (`transport`, `channel_id`) where `kind = 'channel'` and the conversation is not closed — the last is what makes "one conversation per channel" an invariant rather than a convention. Check: `ck_supervisor_conversations_kind`. Index: `idx_supervisor_conversations_state`. Added by revision `a00000000069`.

### Table: `conversation_inputs`

One accepted operator message in a conversation. Transport/external-message uniqueness collapses gateway, backfill and replay duplicates, and the row outlives its text: after 30 days `text` is nulled (`text_expired_at` set, an unanswered input becomes `expired`) and the row stays as the dedup tombstone until it is deleted after 90 days, so expired text cannot replay into fresh work. `supervisor_message_id` is the deterministic `msg-<input id>` notice inserted in the same transaction; `reply_message_id` points at the supervisor's latest recorded answer.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY, `cinput-<uuid4>` |
| `conversation_id` | TEXT | NOT NULL, REFERENCES supervisor_conversations(id) ON DELETE CASCADE |
| `transport` | TEXT | NOT NULL |
| `external_message_id` | TEXT | NOT NULL |
| `verified_actor` | TEXT | NOT NULL, `human:discord:<id>` |
| `author_id` | TEXT | NOT NULL bare author id, for rate limits |
| `channel_id` | TEXT | NOT NULL, for rate limits |
| `text` | TEXT | nullable once expired |
| `text_sha256` | TEXT | NOT NULL |
| `char_count` | INTEGER | NOT NULL, 1–4000 |
| `received_at` | FLOAT | NOT NULL |
| `source` | TEXT | gateway, backfill, replay or test |
| `state` | TEXT | accepted, answered, expired or revoked |
| `supervisor_message_id` | TEXT | nullable, REFERENCES messages(id) |
| `reply_message_id` | TEXT | nullable, REFERENCES messages(id) |
| `delay_notified_at` | FLOAT | nullable, set once by the delay watchdog |
| `text_expired_at` | FLOAT | nullable |
| `created_at` | FLOAT | NOT NULL |

Unique: (`transport`, `external_message_id`). Indexes: `idx_conversation_inputs_history`, `idx_conversation_inputs_author_window`, `idx_conversation_inputs_channel_window`.

### Table: `conversation_backfill_cursors`

Reconnect backfill position for the configured channel and each bound conversation thread. The cursor advances only after the page it covers is persisted.

| Column | Type | Constraints |
|---|---|---|
| `transport` | TEXT | PRIMARY KEY (with `channel_id`) |
| `channel_id` | TEXT | PRIMARY KEY (with `transport`), channel or thread id |
| `last_external_message_id` | TEXT | NOT NULL |
| `advanced_at` | FLOAT | NOT NULL |

### Table: `conversation_intake_gaps`

A stretch of channel history the reconnect backfill could not read, recorded so status reports possibly missed messages instead of claiming delivery.

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | PRIMARY KEY, `gap-<uuid4>` |
| `transport` | TEXT | NOT NULL |
| `channel_id` | TEXT | NOT NULL |
| `gap_from` | FLOAT | NOT NULL |
| `gap_to` | FLOAT | NOT NULL |
| `reason` | TEXT | cursor_expired, history_forbidden or pass_cap |
| `recorded_at` | FLOAT | NOT NULL |

Index: `idx_conversation_intake_gaps_channel`.

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

Authorized project moves transfer known active-task comment ownership in the same transaction. A
move is refused while the task has a parent, children, or an active hierarchy/train branch origin;
the check and write share the source project's hierarchy lock so a concurrent reparent cannot
create a cross-project edge. Moves that would merge a source or destination archive identity, and
archival over a different-project ID, are refused without modifying either history.

### Table: `task_subtasks`

Durable per-task checklist rows (work-graph spec §13c, revision `a00000000010`). A subtask is ticked off by the agent holding its task: it is never on the claim frontier, never assigned, and has no branch of its own — it is delivered with its parent task. Like `task_comments`, `task_id` plus `project_id` is a logical reference with no foreign key, so rows survive archiving and restoration; permanent task or project deletion removes them. Rows are appended after the task's current maximum `ordinal` (starting at 1) and `id` is `<task_id>#s<ordinal>`. Queries live in `src/database/queries/task_subtask_queries.py`, surfaced through `task_subtask_add` / `task_subtasks` / `task_subtask_get` / `task_subtask_update` (`src/commands/task_subtask_commands.py`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `<task_id>#s<ordinal>` |
| `task_id` | TEXT | NOT NULL | Owning task (no FK) |
| `project_id` | TEXT | NOT NULL | Owning project |
| `ordinal` | INTEGER | NOT NULL | 1-based position within the task; the CLI's subtask number |
| `title` | TEXT | NOT NULL | 1–300 characters |
| `context` | TEXT | NOT NULL DEFAULT '' | At most 16000 characters; returned only by single-row reads and never leaves the row in events |
| `status` | TEXT | NOT NULL DEFAULT 'pending' | One of: pending, in_progress, done, skipped |
| `note` | TEXT | nullable | Set on update; a close with `--skip-open-subtasks` fills an empty one with `skipped at close` |
| `created_at` | FLOAT | NOT NULL | Unix timestamp |
| `updated_at` | FLOAT | NOT NULL | Unix timestamp, bumped on every update |

Constraints: `ck_task_subtasks_status` (status in the four values above), `ck_task_subtasks_title_length` (`length(title) BETWEEN 1 AND 300`), `ck_task_subtasks_context_length` (`length(context) <= 16000`), `uq_task_subtasks_task_ordinal` (`task_id`, `ordinal`) UNIQUE. Index: `idx_task_subtasks_task` (`task_id`, `ordinal`).

At most `MAX_SUBTASKS_PER_TASK` (200) rows per task, and at most `MAX_SUBTASKS_PER_CALL` (50) per authoring act; both are enforced by the writer, not the schema. `pending` and `in_progress` are open; `done` and `skipped` are settled.

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
| `preferred_provider` | TEXT | nullable | Operator preference for which provider serves this project's work (set by `aq pool provider apply`); NULL defers to global provider selection and failover. Since mandatory task routing it is a router input only — the planner filters the candidates to this provider — and never a filing argument. Added by Alembic `a00000000037` |
| `assignment_playbook_id` | TEXT | NOT NULL DEFAULT 'default-assignment-routing' | The project's router binding: the routing playbook that routes its tasks (mandatory-task-routing spec §8). A new project is bound to `routing.default_router` (default `default-assignment-routing`); `a00000000039` bound every unbound project and `a00000000043` made the binding NOT NULL. Re-bind with `aq project set <p> router <playbook-id>` (local operator only); `aq doctor --check routing.bypassed` reports a missing or non-routing binding. A project has no default profile: `a00000000043` dropped `default_profile_id`. Added by Alembic `a7c91e4d2b63` |
| `integration_mode` | TEXT | nullable | Project-level integration policy: `'direct'`, `'pull_request'`, or NULL (fall through to config `integration.default_mode`). Added by Alembic `c4d5e6f7a8b9` |
| `hierarchical_integration_mode` | TEXT | NOT NULL DEFAULT 'disabled' | *Effective* hierarchical-integration rollout mode: one of `disabled`, `observe`, `hierarchy`, `train` (`ck_projects_hierarchical_integration_mode`). Only the orchestrator advances it, via a compare-and-set on `hierarchical_integration_generation`. Added by Alembic `c7a1e5d92f40` |
| `integration_repository_id` | TEXT | nullable | The one `repos.id` designated as the hierarchical-integration repository (child branches, candidate trains and root promotion all target it). NULL leaves the project `repository_not_designated` and blocks every mode above `disabled`. Added by Alembic `c7a1e5d92f40` |
| `hierarchical_integration_policy` | JSON | nullable | Frozen policy pins (required checks, repair tiers, source-branch retention, legacy-route suppression) snapshotted into each batch and repair operation; NULL uses config defaults. Added by Alembic `e4c6a8b20d31` |
| `promotion_flow` | JSONB | nullable | Validated ordered promotion steps, configured separately from hierarchical policy pins; NULL means no promotion flow. Added by Alembic `a00000000085` |
| `default_branch_cutover` | JSONB | nullable | Generation-fenced cutover binding: repository ID, old and new defaults, activated generation and cutover time. Resolves pre-cutover top-level origins to the current default without rewriting immutable provenance; nested and later origins keep their recorded targets. Reverse clears the binding in the same transaction. Added by Alembic `a00000000086` |
| `hierarchical_integration_desired_mode` | TEXT | NOT NULL DEFAULT 'disabled' | Mode the operator asked for with `integration_enable`; same value set as `hierarchical_integration_mode`. Differs from the effective mode while a drain is in progress. Added by Alembic `a11a5e1e4f04` |
| `hierarchical_integration_draining` | BOOLEAN | NOT NULL DEFAULT false | True while in-flight batches/repairs are being drained before the effective mode drops to the desired one. Added by Alembic `a11a5e1e4f04` |
| `hierarchical_integration_generation` | INTEGER | NOT NULL DEFAULT 0 | Monotone rollout fence (`>= 0`); every mode transition increments it and is recorded in `integration_rollout_transitions`. Operator controls pass `expected_generation` and are rejected on mismatch. Added by Alembic `a11a5e1e4f04` |
| `review_delegate_to` | TEXT | nullable, `ck_projects_review_delegate_to` | Who decides the project's new document reviews: `user` or `supervisor`; NULL means `user`. Sets a new review's `doc_reviews.decider` (`supervisor` → `user_or_supervisor`). Added by Alembic `a00000000014` |
| `git_identity_name` | TEXT | nullable, `ck_projects_git_identity_pair` | This project's Git commit identity override, set together with `git_identity_email`; a NULL pair inherits the installation default `git_identity` ([git identity](git-identity.md)). Added by Alembic `a00000000053` |
| `git_identity_email` | TEXT | nullable, `ck_projects_git_identity_pair` | The override's email; `(git_identity_name IS NULL) = (git_identity_email IS NULL)`. Added by Alembic `a00000000053` |
| `created_at` | REAL | NOT NULL | Unix timestamp, set on insert |

No `updated_at` on projects. The `discord_control_channel_id` column exists for backward compatibility — `_row_to_project` falls back to it when `discord_channel_id` is NULL.

### Table: `collaboration_threads`

Ordered, bounded message threads between two tasks. A thread is created idempotently by its creator with a deadline (never more than two hours after creation) and a hard cap on messages, which closes the thread `budget_exhausted` once spent. `final_result` holds the negotiated outcome recorded at close. Task and profile references are soft so archival preserves collaboration history.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `thread-<uuid4>` |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `created_by_kind` | TEXT | NOT NULL | Creator's role kind (`agent` / `user` / `supervisor`) |
| `created_by_id` | TEXT | NOT NULL | Soft reference to the creator (task or user id) |
| `idempotency_key` | TEXT | NOT NULL, UNIQUE with `project_id`, `created_by_id` (`uq_collaboration_threads_idempotency`) | Retries of one create do not open a second thread |
| `request_hash` | TEXT | NOT NULL | Hash of the normalized create request; checked against the idempotency key |
| `goal` | TEXT | nullable | Free-text negotiation goal |
| `state` | TEXT | NOT NULL DEFAULT 'active', `ck_collaboration_threads_state` | `active`, `closed` or `expired` |
| `close_reason` | TEXT | nullable, `ck_collaboration_threads_reason` | `closed`, `budget_exhausted`, `expired` or `members_below_two` |
| `created_at` | FLOAT | NOT NULL | Unix timestamp |
| `deadline_at` | FLOAT | NOT NULL, `ck_collaboration_threads_deadline` | Strictly after `created_at`, at most `created_at + 7200` |
| `closed_at` | FLOAT | nullable | Unix timestamp of close |
| `message_budget` | INTEGER | NOT NULL DEFAULT 40, `ck_collaboration_threads_budget` | 1–40 inclusive |
| `message_count` | INTEGER | NOT NULL DEFAULT 0, `ck_collaboration_threads_budget` | Never exceeds `message_budget` |
| `last_seq` | BIGINT | NOT NULL DEFAULT 0 | Last message ordinal issued for this thread |
| `version` | INTEGER | NOT NULL DEFAULT 1 | Optimistic-concurrency fence on state transitions |
| `final_result` | JSON | nullable | Negotiated outcome recorded at close |

Indexes: `idx_collaboration_threads_due`, `idx_collaboration_threads_project`.

### Table: `collaboration_members`

Membership of a collaboration thread, one row per task. Invitation is recorded at `invited_at`; a task becomes a speaking member only by accepting with the claim epoch it holds, and removal stamps `removed_at`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `thread_id` | TEXT | PRIMARY KEY, FK → collaboration_threads.id (CASCADE) | Thread the membership belongs to |
| `task_id` | TEXT | PRIMARY KEY | Member task (soft reference) |
| `state` | TEXT | NOT NULL DEFAULT 'invited', `ck_collaboration_members_state` | `invited`, `accepted` or `removed` |
| `invited_at` | FLOAT | NOT NULL | Unix timestamp of the invitation |
| `accepted_at` | FLOAT | nullable | Unix timestamp of acceptance |
| `accepted_claim_epoch` | INTEGER | nullable | Claim epoch the accepting task held |
| `removed_at` | FLOAT | nullable | Unix timestamp of removal |

Index: `idx_collaboration_members_task`.

### Table: `collaboration_messages`

Append-only ordered messages within a collaboration thread, sequenced by `seq` per thread. Senders are identified by task, the claim epoch they held and their session, and one message per (thread, sender, epoch, client key) is enforced by `uq_collaboration_messages_client_key`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `thread_id` | TEXT | PRIMARY KEY, FK → collaboration_threads.id (CASCADE) | Thread the message belongs to |
| `seq` | BIGINT | PRIMARY KEY | Per-thread ordinal, assigned from `collaboration_threads.last_seq` |
| `message_id` | TEXT | nullable | The AQ message id that carries the body |
| `sender_task_id` | TEXT | NOT NULL | Soft reference to the sending task |
| `sender_claim_epoch` | INTEGER | NOT NULL | Claim epoch the sender held when writing |
| `sender_session_id` | TEXT | NOT NULL | Session that wrote the message |
| `client_key` | TEXT | NOT NULL | Client-side deduplication key |
| `body_bytes` | INTEGER | NOT NULL | Length of the message body in bytes |
| `created_at` | FLOAT | NOT NULL | Unix timestamp |

Unique: `uq_collaboration_messages_client_key` over (`thread_id`, `sender_task_id`, `sender_claim_epoch`, `client_key`). Index: `idx_collaboration_messages_sender`.

### Table: `dashboard_state_documents`

Durable, server-backed dashboard state: one JSON document per (`scope`, `owner_id`, `namespace`, `subject`). The namespace registry lives in `src/dashboard_state/namespaces.py` and decides each namespace's scope, whether it is subject-addressed and whether writes are compare-and-swap or last-write-wins; this table stays generic and stores whatever the registry validates. Added by Alembic `a0000000000e`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `scope` | TEXT | PRIMARY KEY | One of: workspace, user (`ck_dashboard_state_scope`) |
| `owner_id` | TEXT | PRIMARY KEY | The user for `user` scope; the empty string for `workspace` scope, which is installation-wide (`ck_dashboard_state_owner`) |
| `namespace` | TEXT | PRIMARY KEY | Registry name, e.g. `nav_organization` or `command_center_project_view` |
| `subject` | TEXT | PRIMARY KEY | Project ID for a subject-addressed namespace; the empty string for a global one. SQL NULL cannot participate in the natural primary key, so the API's null subject is stored as `''` |
| `revision` | INTEGER | NOT NULL DEFAULT 0, `>= 1` once a row exists (`ck_dashboard_state_revision`) | Monotone per-document counter; a CAS write updates only when it matches the caller's `base_revision` |
| `value` | JSON | nullable | The document, validated against the namespace's pydantic model. `none_as_null` is set, so a reset stores SQL NULL rather than JSON `null` and stays distinguishable in Python |
| `created_at` | REAL | NOT NULL | Unix timestamp, set on insert |
| `updated_at` | REAL | NOT NULL | Unix timestamp, bumped on every accepted write |

The four key columns are the composite primary key, so the write path is a single upsert: a CAS write is an `UPDATE ... WHERE revision = base_revision` (returning no row means the caller lost the race, and the current row is read back for the conflict response), and a first write is an `ON CONFLICT DO UPDATE` that bumps `revision`. A last-write-wins namespace passes no base revision and always wins.

There is no foreign key from `subject` to `projects(id)` — the column is also the empty string for global namespaces — so deleting a project does not cascade. Project deletion removes that project's documents explicitly (`delete_dashboard_documents_for_project`), and anything left behind is reported and reaped by `aq doctor --check dashboard_state.orphans`.

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
| `repo_id` | TEXT | nullable REFERENCES repos(id) | The repository whose publisher collects the task: the project's `integration_repository_id` in a `development`/`hierarchy`/`train` project at creation, NULL otherwise. A project move rebinds it, and the development sweep rebinds (and comments on) a live task still naming another project's repository |
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
| `task_type` | TEXT | nullable | Task type classification (added via migration): feature, bugfix, refactor, test, docs, chore, research, plan, sync, design, art. Free text, no check constraint. The filer's kind hint to the router (`design` and `art` added with mandatory task routing, no DDL) |
| `profile_id` | TEXT | nullable REFERENCES agent_profiles(id) | Agent profile for execution (added via migration). Written only by the project's router (`task_route_apply`), an audited override, a role creator, or a failover/spill/undo move among `route.candidates` — never by a filer. NULL exactly when `route_source = 'unrouted'` |
| `intelligence_class` | TEXT | nullable | The class the route runs at, written with `profile_id` by the router, an override or a role creator; NULL on a new, unrouted task and cleared by `aq task route`. The filer's class is `class_hint` |
| `preferred_workspace_id` | TEXT | nullable REFERENCES workspaces(id) | Preferred workspace (added via migration) |
| `attachments` | TEXT | DEFAULT '[]' | JSON-encoded list of attachment paths/URLs (added via migration) |
| `next_child_ordinal` | INTEGER | NOT NULL DEFAULT 1 | Per-parent counter for dotted child ids (swarm-work-model §4, §6); incremented atomically by `task_names.reserve_child_ordinal`; never read for anything else |
| `created_by_kind` | TEXT | nullable | Provenance (swarm-work-model §9): who created the row; stamped by `CommandHandler.execute` from the request scope (Plan 2); nullable so rows from legacy paths stay valid |
| `created_by_id` | TEXT | nullable | Provenance (swarm-work-model §9), paired with `created_by_kind` |
| `provider_intent` | TEXT | NOT NULL DEFAULT 'class_only' | `pinned`, `preferred` or `class_only` (`ck_tasks_provider_intent`): whether anyone meant the provider `profile_id` names (provider-failover D8). A pinned task holds while its provider is unavailable; the other two fail over. `pinned`/`preferred` with a NULL `profile_id` reads as `class_only`. Since mandatory task routing no filer sets it: the router writes `pinned` for a hold lane and `class_only` otherwise, an override writes `pinned`, a role task is `preferred` |
| `rerouted_from` | TEXT | nullable | The profile the task was on before its first automatic re-route that has not been undone (D17); NULL means "where it was put". Partial index `idx_tasks_rerouted` on (`profile_id`) WHERE `rerouted_from IS NOT NULL` is what the re-route trickle counts |
| `route_source` | TEXT | NOT NULL DEFAULT 'unrouted' | Who wrote the route in `profile_id` (mandatory-task-routing spec 2026-09-28 §3, `ck_tasks_route_source`): `unrouted`, `router`, `override`, `role` (a stage profile: triage, spec-ingest, reviewer, final-reviewer) or `legacy`. `ck_tasks_route_source_profile` (`a00000000042`) holds `(profile_id IS NULL) = (route_source = 'unrouted')`: a profile write declares its source, and the query layer refuses one that does not (`src/routing/sources.py`). Once a project's router is ready, only `router`, `override` and `role` are claimable; `legacy` is claimable before that (spec §9.1). `delete_profile` sends the tasks naming the profile back to `unrouted`, keeping the lost route in `route.legacy`. Added by `a00000000039` |
| `class_hint` | TEXT | nullable | The filer's intelligence-class hint to the router (`--intelligence-class`), honoured up to the kind's `max_class` in the routing policy; `a00000000039` backfilled it from `intelligence_class` |
| `route` | JSONB | nullable | The router's record of the route it chose: hints, classification, rule, lane, candidates and scores, policy digest, playbook run. `candidates` bounds every later move (failover, spill, reroute-undo, `aq provider reroute`); an override adds `override: {by, at, reason}`, the honoured preference adds `preference: {target, mode, kind, honoured, fallback_reason}`, and a lost pre-cutover route is kept as `legacy`. Added by `a00000000039` as JSON; `a00000000040` retyped it JSONB because plain JSON breaks whole-row `SELECT DISTINCT tasks.*` (outage 2026-09-28). Downgrade keeps JSONB to avoid restoring the outage. |
| `prefer_target` | TEXT | nullable | The filer's routing preference (`aq task create --prefer`, `aq task route --prefer`): a harness id or a profile id the router weighs before scoring, never a route. NULL — the state of every row before `a00000000076` — means the task names no preference and routing is unchanged. Filing refuses a name that is neither an installed harness nor an enabled worker profile |
| `prefer_mode` | TEXT | nullable CHECK IN ('soft','strict') | How the router may refuse `prefer_target`: `soft` (the default when a target is named) takes it when it has headroom and routes normally when it does not; `strict` allows only that target and holds the task rather than falling back (`ck_tasks_prefer_mode`, `a00000000076`). NULL alongside a NULL target |
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

### Table: `epic_dependencies`

Declared ordering edges between epic tasks for integration batches. A dependent
epic follows its dependency when both are in the batch; a missing or cyclic
dependency defers the dependent epic. These declarations are separate from task
readiness edges in `task_dependencies`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `dependent_task_id` | TEXT | NOT NULL | Epic whose integration waits for the dependency |
| `dependency_task_id` | TEXT | NOT NULL | Epic that must be integrated first |
| `declared_at` | FLOAT | NOT NULL | Timestamp of the first declaration; duplicate declarations leave it unchanged |
| (composite PK) | | PRIMARY KEY (dependent_task_id, dependency_task_id) | One declaration per ordered pair (`pk_epic_dependencies`) |
| (check) | | CHECK (dependent_task_id <> dependency_task_id) | No self-dependency (`ck_epic_dependencies_not_self`) |

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

### Table: `layout_reflow_requests`

Durable, generation-fenced requests to compact an `active` layout scope after a finished
leaf leaves a hole. A newer generation survives acknowledgement of an older claimed request.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | TEXT | PRIMARY KEY, NOT NULL | Project whose layout needs compaction |
| `variant` | TEXT | PRIMARY KEY, NOT NULL | Layout variant |
| `scope_key` | TEXT | PRIMARY KEY, NOT NULL | Task id or the layout root sentinel; deliberately no task FK |
| `generation` | INTEGER | NOT NULL DEFAULT 1 | Monotonic request generation |
| `state` | TEXT | NOT NULL | `queued`, `running`, or `failed` |
| `requested_at` | FLOAT | NOT NULL | Unix timestamp of the newest request |
| `retry_after` | FLOAT | NOT NULL | Earliest retry time |
| `attempts` | INTEGER | NOT NULL DEFAULT 0 | Claim attempts |
| `last_error` | TEXT | nullable | Most recent failure detail |
| `claimed_generation` | INTEGER | nullable | Generation held by the current lease |
| `lease_token` | TEXT | nullable | Claim token |
| `lease_expires_at` | FLOAT | nullable | Claim expiry |

Composite primary key: (`project_id`, `variant`, `scope_key`). Index
`idx_layout_reflow_requests_due` orders due work by state, retry time, request time, and scope.

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

### Table: `layout_tidy_requests`

Fleet-wide Tidy requests. The request is a durable batch; its project/variant pairs are
released into the ordinary layout-job queue one at a time.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Request identifier |
| `reason` | TEXT | NOT NULL | Operator-supplied reason |
| `status` | TEXT | NOT NULL | `queued`, `running`, or `completed` |
| `requested_at` | FLOAT | NOT NULL | Unix timestamp when requested |
| `finished_at` | FLOAT | nullable | Unix timestamp when all pairs settled |

Index: `idx_layout_tidy_requests_status_requested` (`status`, `requested_at`).

### Table: `layout_tidy_request_pairs`

One project/variant item in a fleet-wide Tidy request. `job_id` is a soft reference so
project deletion and restart reconciliation can settle a missing job without wedging the batch.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `request_id` | TEXT | PRIMARY KEY, NOT NULL, REFERENCES layout_tidy_requests(id) ON DELETE CASCADE | Owning Tidy request |
| `project_id` | TEXT | PRIMARY KEY, NOT NULL, REFERENCES projects(id) ON DELETE CASCADE | Project to lay out |
| `variant` | TEXT | PRIMARY KEY, NOT NULL | Layout variant |
| `job_id` | TEXT | nullable | Soft reference to the released layout job |
| `status` | TEXT | NOT NULL | `pending`, `queued`, `running`, `completed`, or `failed` |
| `error` | TEXT | nullable | Failure detail |
| `finished_at` | FLOAT | nullable | Unix timestamp when the pair settled |

Composite primary key: (`request_id`, `project_id`, `variant`). Index
`idx_layout_tidy_request_pairs_status` (`status`, `request_id`).

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
| `model_source` | TEXT | nullable | Transcript evidence source, such as `assistant_response` or `turn_context`; added by revision 47 |
| `session_id` | TEXT | nullable | Session captured when usage is ingested |
| `attempt_id` | TEXT | nullable | Open task-session attempt captured at ingest; historical rows stay unattributed |
| `call_id` | TEXT | nullable | Stable transcript entry identity |
| `input_tokens` | INTEGER | nullable | Input half of the split; NULL when unknown |
| `output_tokens` | INTEGER | nullable | Output half of the split; NULL when unknown |
| `timestamp` | REAL | NOT NULL | Unix timestamp, set on insert |

The three pricing columns are nullable by design: rows written before they existed cannot be priced accurately, so `get_cost_rollup` / `aq costs` report them as `unpriced_tokens` rather than pricing them at a guessed rate (`docs/specs/design/trust-and-ops.md` §7).

No deletes on this table during normal operation. Deleted only as part of cascading `delete_project` or `delete_task`.

Indexes: `idx_token_ledger_task_attempt` (`task_id`, `attempt_id`) and unique
`uq_token_ledger_call` (`session_id`, `call_id`). Nullable identities preserve
historical rows without inventing attribution. Added by Alembic `a00000000047`;
`idx_token_ledger_call_id` (`call_id`, for adopting legacy rows) by `a00000000056`.

### Table: `transcript_usage_calls`

Durable per-API-call usage maxima for transcript ingestion (`azure-vault-92.1`,
Alembic `a00000000056`). Claude streams one API call as several transcript
content rows that repeat the same usage; the ledger used to charge each row.
`record_transcript_usage` (`src/database/queries/token_queries.py`) locks the
call's row, raises each category to the newly observed maximum and appends only
the positive increase to `token_ledger` in the same transaction, so replays,
restarts and concurrent readers never charge a call twice. Existing ledger rows
are never rewritten.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `usage_key` | TEXT | PRIMARY KEY | `<provider>:<sha256>` of provider, transcript conversation and API call id (`transcript_usage_key`); content UUID when the API id is absent |
| `first_ledger_id` | TEXT | nullable | First ledger row charged for the call; later increases keep its attribution |
| `input_tokens` | INTEGER | NOT NULL, ≥ 0 | Maximum uncached input observed |
| `output_tokens` | INTEGER | NOT NULL, ≥ 0 | Maximum output observed |
| `cache_read_tokens` | INTEGER | NOT NULL, ≥ 0 | Maximum cache read observed |
| `cache_write_tokens` | INTEGER | NOT NULL, ≥ 0 | Maximum cache write observed |
| `updated_at` | REAL | NOT NULL | Daemon clock at the last raise |

`ck_transcript_usage_calls_nonnegative` enforces the bounds. Downgrading drops
the table and restores per-row charging; ledger rows survive.
The legacy SQLite importer excludes this table because it was introduced after
SQLite removal.

### Table: `benchmark_stage_spans`

Idempotent monotonic stage measurements recorded through `benchmark_stage_record`.
Added by Alembic `a00000000047`. The legacy SQLite importer excludes this table
because it was introduced after SQLite removal.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Caller-supplied stable span ID |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) ON DELETE CASCADE | Owning project |
| `task_id` | TEXT | NOT NULL | Soft task reference retained after archival |
| `session_attempt_id` | TEXT | nullable | Open worker attempt at recording time |
| `stage` | TEXT | NOT NULL | Measured benchmark stage |
| `started_monotonic_ns` | BIGINT | NOT NULL | Start on the same host and clock as the end |
| `ended_monotonic_ns` | BIGINT | NOT NULL | End timestamp |
| `duration_ms` | FLOAT | NOT NULL | Difference converted to milliseconds |
| `recorded_at` | FLOAT | NOT NULL | Recording wall-clock timestamp |

Index: `idx_benchmark_stage_task` (`task_id`, `session_attempt_id`). An identical
span retry returns `inserted: false`; conflicting evidence for the same ID is refused.

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

`route` is nullable JSONB, preserving the active task's routing record, including
benchmark arm, requested model and policy digest. Added by Alembic `a00000000047`.

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
| `gate_type` | TEXT | NOT NULL (`ck_gates_type`) | Kind of decision being requested: one of `human`, `timer`, `pr-merged`, `ci-run`, `event`, `task`, `routing`, `review` (`GATE_TYPES`). A `review` gate belongs to a document review (`await_id` = the review id) |
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

### Table: `doc_reviews`

A markdown document (spec, plan or other) an agent submitted for a human
decision — document-review spec §3.2
(`vault/projects/agent-queue/specs/2026-09-21-document-review-design.md`).
The database is the source of truth; the vault file at `vault_path` is a copy
the daemon writes after each commit. Task ids are soft references with **no
foreign key to `tasks`**, so a review never blocks a task's archive or delete.
Queries: `src/database/queries/review_queries.py`. Added by Alembic
`a00000000014`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `rev-<adjective>-<noun>`, task-id style (`generate_review_id`) |
| `project_id` | TEXT | NOT NULL | Owning project |
| `author_task_id` | TEXT | nullable, no FK | The task that submitted the review |
| `kind` | TEXT | NOT NULL (`ck_doc_reviews_kind`) | One of: spec, plan, other |
| `title` | TEXT | NOT NULL | Document title |
| `vault_path` | TEXT | NOT NULL, UNIQUE (`uq_doc_reviews_vault_path`) | Relative to the vault root: `projects/<pid>/specs/…` (spec, other) or `projects/<pid>/plans/…` (plan) |
| `current_revision` | INTEGER | NOT NULL DEFAULT 1, `>= 1` (`ck_doc_reviews_revision`) | Latest revision number; state changes compare-and-set on it (`transition_review`) |
| `state` | TEXT | NOT NULL (`ck_doc_reviews_state`) | One of: in_review, changes_requested, approved, withdrawn |
| `gate_id` | TEXT | nullable | The review's `review` gate; only an approval resolves it |
| `decider` | TEXT | NOT NULL DEFAULT 'user' (`ck_doc_reviews_decider`) | One of: user, user_or_supervisor |
| `decided_by` | TEXT | nullable | Principal label of the latest decision or withdrawal |
| `decided_at` | REAL | nullable | Unix timestamp of the latest decision or withdrawal |
| `decision_note` | TEXT | nullable | Note on the latest decision, or the withdrawal reason |
| `notified_revision` | INTEGER | NOT NULL DEFAULT 0 | Last revision announced on Discord; the outbox is every row with `notified_revision < current_revision` |
| `created_at` | REAL | NOT NULL | Unix timestamp, set on insert |
| `updated_at` | REAL | NOT NULL | Unix timestamp, bumped on every transition |

Indexes: `idx_doc_reviews_project_state` (`project_id`, `state`), `idx_doc_reviews_author_task` (`author_task_id`).

### Table: `doc_review_revisions`

The text of every submitted revision of a review — a second copy of the vault
document. Added by Alembic `a00000000014`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `review_id` | TEXT | PRIMARY KEY REFERENCES doc_reviews(id) ON DELETE CASCADE | Composite PK part 1 |
| `revision` | INTEGER | PRIMARY KEY | Composite PK part 2; 1, 2, … |
| `content` | TEXT | NOT NULL | The submitted markdown with any leading frontmatter stripped (at most 256 KB) |
| `content_sha256` | TEXT | NOT NULL | sha256 of `content` in UTF-8 |
| `submitted_by` | TEXT | NOT NULL | Principal label |
| `submitted_task_id` | TEXT | nullable, no FK | The submitting task |
| `changes_note` | TEXT | nullable | What changed since the previous revision |
| `submitted_at` | REAL | NOT NULL | Unix timestamp |
| `responder_class` / `responder_profile` / `responder_profile_source` | TEXT | nullable | Who revises after a feedback decision on this revision. Since mandatory task routing `aq review decide --responder-profile` is refused: `responder_class` (`--responder-class`) is the revision task's class hint, `responder_profile` stays NULL and `responder_profile_source` is `router`; a named profile appears only on pre-routing rows |
| `playbook` | JSON | nullable | A playbook review's pin: `playbook_id`, `artifact_sha256`, `source_sha256`, `source_path`, `contract_fingerprint`, `scope`, `scope_identifier`, `activate_on_approval`, diagnostic `counts`. Added by Alembic `a00000000036` |
| `playbook_artifact` | TEXT | nullable | The pinned artifact's exact canonical bytes; approval stores them in the artifact store. Added by Alembic `a00000000036` |

### Table: `doc_review_attachments`

Immutable image evidence attached to one document review revision. Files live
under the daemon's `review-attachments` directory and retain their content hash;
later revisions cannot alter an earlier packet. Added by Alembic `a00000000046`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Attachment identifier |
| `review_id` | TEXT | NOT NULL | Review identity; composite revision FK |
| `revision` | INTEGER | NOT NULL | Owning submitted revision |
| `path` | TEXT | NOT NULL, UNIQUE | Stored image path |
| `sha256` | TEXT | NOT NULL | SHA-256 of image bytes |
| `content_type` | TEXT | NOT NULL | Verified PNG, JPEG, GIF, or WebP type |
| `size` | INTEGER | NOT NULL, > 0 (`ck_doc_review_attachments_size`) | Image bytes |
| `caption` | TEXT | NOT NULL | Human-readable evidence caption |
| `view_id` | TEXT | NOT NULL | Stable view identifier |
| `candidate_id` | TEXT | NOT NULL | Candidate shown in the image |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Composite FK `fk_doc_review_attachments_revision` references
`doc_review_revisions` (`review_id`, `revision`) ON DELETE CASCADE. Index:
`idx_doc_review_attachments_revision` (`review_id`, `revision`). No task FK:
archiving or deleting the source task preserves the review evidence.

### Table: `object_loops`

Durable object evaluation state. A row lock serializes loop transitions and
sibling budget reservations. Images and logs live in the artifact store; this
row retains identities, evidence references, budgets and the next task intent.
Added by Alembic `a00000000045`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `object_id` | TEXT | PRIMARY KEY | Stable evaluated object identity |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `epic_task_id` | TEXT | NOT NULL, UNIQUE (`uq_object_loops_epic_task_id`) | Soft reference to the root epic |
| `finalization_task_id` | TEXT | NOT NULL | Soft reference to the finalizer |
| `terminal_gate_id` | TEXT | NOT NULL | Soft reference to the terminal hold |
| `version` | INTEGER | NOT NULL DEFAULT 1, >= 1 (`ck_object_loops_version`) | Compare-and-set version |
| `state` | JSONB | NOT NULL | Loop identities, reservations, receipts, checkpoints and stop reason |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Latest transition timestamp |

Index: `idx_object_loops_project` (`project_id`). Soft task and gate references
preserve loop evidence through task archival and cleanup.

### Table: `pull_request_inbox_snapshot`

The Reviews tab's atomic durable projection of known pull requests. A failed
GitHub refresh retains the last successful snapshot and timestamp across daemon
restarts. Added by Alembic `a00000000044`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY, = 1 (`ck_pull_request_inbox_snapshot_singleton`) | Singleton row |
| `payload` | JSONB | NOT NULL | Last successful pull-request projection |
| `updated_at` | REAL | NOT NULL | Last successful refresh timestamp |

### Table: `doc_review_comments`

Anchored comments on a review revision. Added by Alembic `a00000000014`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Comment id |
| `review_id` | TEXT | NOT NULL REFERENCES doc_reviews(id) ON DELETE CASCADE | The review |
| `revision` | INTEGER | NOT NULL | The revision the comment was made on |
| `quote` | TEXT | nullable | The exact text selected; NULL for a whole-section comment |
| `heading_path` | JSON | NOT NULL | Heading texts from the top of the document down to the section |
| `body` | TEXT | NOT NULL, 1–16000 characters (`ck_doc_review_comments_body`) | The comment |
| `author` | TEXT | NOT NULL | Principal label, e.g. `human:local-operator` |
| `resolved_in_revision` | INTEGER | nullable | Set when a later revision addresses the comment or it is marked resolved |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Index: `idx_doc_review_comments_review` (`review_id`, `revision`).

### Table: `doc_review_dispatches`

Records each dispatch of a document review to a reviewer task, including forced
repeat dispatches. Review and task ids are soft references so the history
survives task archival or deletion. Added by Alembic `a0000000001a`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Dispatch id |
| `review_id` | TEXT | NOT NULL, no FK | The document review |
| `profile_id` | TEXT | nullable | Reviewer profile a pre-routing dispatch named. Since mandatory task routing (`a00000000041`) a dispatch names no profile: the project's router picks each reviewer's profile, so new rows leave it NULL |
| `intelligence_class` | TEXT | nullable | The class hint the dispatch filed its reviewer task with (`aq review dispatch --class`, default `deep-high`). Added by `a00000000041` |
| `revision` | INTEGER | NOT NULL | Review revision sent to the reviewer |
| `with_comments` | BOOLEAN | NOT NULL | Whether review comments were included |
| `focus` | TEXT | nullable | Optional focus for the reviewer |
| `task_id` | TEXT | NOT NULL, UNIQUE, no FK | Reviewer task created for this dispatch |
| `dispatched_by` | TEXT | NOT NULL | Principal that requested the dispatch |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Index: `idx_doc_review_dispatches_review` (`review_id`, `profile_id`, `revision`).

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

A task change set proposed as one reviewable graph: new tasks, edits to existing
tasks, dependency additions/removals, reparenting, lifecycle controls and comments.
Written by spec ingest and supervisors via `task_batch_propose`. Propose/update
validate the same connection-scoped implementation as commit in a rolled-back
savepoint; no task or comment becomes visible until an approved commit.

`task_batch_commit` locks the proposal row, revalidates the PostgreSQL `xmin`
versions of referenced tasks and their hierarchy, graph edges, metadata, gates
and integration checkpoints, and applies all changes, the receipt and one
`task.change_set_committed` audit event in a single transaction. It takes the
fleet routing lock, project hierarchy lock and task/graph table write locks, so
scheduler, routing and claim writes serialize with commit. The final graph's
blocked projection and container settlement are computed before publishing;
listeners run after commit. A stale scheduler promotion repeats its observed
status, timestamp and unblocked predicate in the guarded write.

An approved proposal is immutable once a human gate awaits it. A version
conflict leaves it ready and requires a fresh proposal and decision. A committed
proposal replays its stored receipt, even after its tasks have been archived.
Live-holder controls, integration-owned placement/disposition and invalid state
transitions are refused rather than bypassing existing lifecycle guards.

`payload` is a JSON blob rather than normalised rows on purpose: a proposal is
reviewed and committed or discarded as a unit, never queried edge-by-edge, and
its tasks do not have real ids until the commit creates them.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `"prop-"` + uuid4[:12] |
| `project_id` | TEXT | NOT NULL REFERENCES projects(id) | Owning project |
| `source` | TEXT | NOT NULL | Provenance, e.g. `spec:projects/foo/specs/2026-08-21-thing.md`; stamped onto every task the commit creates |
| `payload` | TEXT | NOT NULL | JSON: `tasks`, `edits`, `edges`, `remove_edges`, `comments`, validated `diff`, optimistic `expected` read set, and committed `receipt`; see [task change sets](../guides/task-change-sets.md) |
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
| `git_identity_digest` | TEXT | nullable | Digest of the Git identity injected into this launch's env; a pool claim retires the session when the project now resolves to another identity. `legacy` marks rows from before Alembic `a00000000053` (always stale); NULL means none was recorded ([git identity](git-identity.md) §7) |

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
The legacy-import copy inventory (`aq db import-sqlite`) includes this audit table.

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
The legacy-import copy inventory (`aq db import-sqlite`) includes this table.

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

### Table: `provider_usage_snapshots`

Provider quota readings from transcripts and probes. Repeated readings update
`last_seen_at` while preserving the original `observed_at` for usage history.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Auto-increment primary key |
| provider | TEXT | Provider identifier |
| account_label | TEXT | Provider-reported plan or limit label; defaults to empty |
| window | TEXT | Provider quota window |
| scope | TEXT | Provider-reported model scope; defaults to empty |
| used_percent | FLOAT | Observed quota utilization |
| resets_at | FLOAT | Nullable reset time, Unix epoch seconds |
| observed_at | FLOAT | First observation time, Unix epoch seconds |
| last_seen_at | FLOAT | Latest confirmation time, Unix epoch seconds |
| source | TEXT | `transcript` or `probe`, enforced by a check constraint |

The series index covers `(provider, window, scope, observed_at DESC)`.
This table has no foreign keys.

### Table: `provider_availability`

One row per provider key (the harness login: `claude`, `codex`, ... and the
reserved `llm` for the direct path), written only by the daemon's availability
service ([provider failover](provider-failover.md) D7). `state` is the state
derived from evidence and never holds `disabled`; an operator override lives in
the `override_*` columns and the effective state is computed from both.

| Column | Type | Description |
|--------|------|-------------|
| provider | TEXT | Primary key: the provider key |
| vendor | TEXT | Display attribute (`anthropic`, `openai`, ...); defaults to empty |
| state | TEXT | Derived state; `ck_provider_availability_state` names the six values |
| reason_code | TEXT | Machine-readable reason; defaults to empty |
| reason | TEXT | Human reason; defaults to empty |
| since | FLOAT | When the derived state began, Unix epoch seconds |
| until | FLOAT | Nullable expected recovery |
| level | INTEGER | Flap backoff level; defaults to 0 |
| last_trip_at | FLOAT | Nullable time of the last trip |
| consecutive_failures | INTEGER | Generic launch failures since the last success |
| last_failure_at | FLOAT | Nullable |
| last_success_at | FLOAT | Nullable |
| evidence | JSON | Bounded evidence ring, newest first; defaults to `[]` |
| override_state | TEXT | Nullable `disabled` or `available` (`ck_provider_availability_override_state`) |
| override_until | FLOAT | Nullable override expiry |
| override_by | TEXT | Nullable principal |
| override_reason | TEXT | Nullable |
| override_set_at | FLOAT | Nullable |
| generation | INTEGER | Incremented on every change of effective state |
| probation_from | TEXT | Nullable unavailable state a recovering provider came from |
| counters_reset_at | FLOAT | Nullable; evidence at or before it no longer counts toward a trip |
| last_probe_at | FLOAT | Nullable time the auth probe last answered |
| notified_generation | INTEGER | Generation the state-change message last went out for |
| updated_at | FLOAT | Last write, Unix epoch seconds |

This table has no foreign keys.

### Table: `provider_availability_transitions`

Append-only audit trail of effective provider state changes: `aq provider
history` and the dashboard card's history.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Auto-increment primary key |
| provider | TEXT | Provider key |
| from_state | TEXT | Effective state before |
| to_state | TEXT | Effective state after |
| reason_code | TEXT | Defaults to empty |
| reason | TEXT | Defaults to empty |
| until | FLOAT | Nullable expected recovery |
| generation | INTEGER | The row's generation after the change |
| actor | TEXT | `system` or a principal; defaults to `system` |
| detail | JSON | Derived states and the evidence that caused it; defaults to `{}` |
| at | FLOAT | Unix epoch seconds |

Index `idx_provider_availability_transitions_provider_at` covers (`provider`,
`at DESC`). This table has no foreign keys.

### Table: `task_reroutes`

Append-only history of provider re-routes (provider-failover D17): every
automatic move, operator-forced move and undo. `tasks.rerouted_from` is only
its cheap current projection. Written by `provider_reroute` /
`provider_reroute_undo` (`src/providers/reroute.py`).

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Auto-increment primary key |
| `task_id` | TEXT | REFERENCES tasks(id) ON DELETE CASCADE (`fk_task_reroutes_task`) |
| `project_id` | TEXT | The task's project |
| `from_profile_id` | TEXT | Nullable profile before the move |
| `to_profile_id` | TEXT | Nullable profile after the move |
| `from_provider` | TEXT | Provider key before; defaults to empty |
| `to_provider` | TEXT | Provider key after; defaults to empty |
| `intelligence_class` | TEXT | Nullable class (never changed by a re-route) |
| `reason_code` | TEXT | `provider_unavailable`, `operator_forced` or `operator_undo` (`ck_task_reroutes_reason_code`) |
| `provider_state` | TEXT | The source provider's effective state at the time; defaults to empty |
| `provider_generation` | INTEGER | Nullable source provider generation |
| `batch_id` | TEXT | Nullable; `prb-<provider>-<generation>` for automatic moves, so one outage is one batch |
| `actor` | TEXT | `system` or a principal; defaults to `system` |
| `at` | FLOAT | Unix epoch seconds |
| `undone_at` | FLOAT | Nullable; set on the move an operator undid |

Indexes `idx_task_reroutes_task_at` (`task_id`, `at`) and
`idx_task_reroutes_batch` (`batch_id`).

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

### Table: `integration_source_ci`

Exact source CI observation and durable repair lineage. Source identity is the
composite primary key; policy generation fences admission. Logical task IDs retain
history after archive and refuse hard deletion through the integration guard.

Discarding a task's completion (including reopen with feedback) atomically
retires its source-CI repairs, including previous attempts and repairs of those
repairs. Open and completed repairs become terminal failures with no retry,
their branch origins retire, and a comment plus `source_ci_retirement` metadata
names the source head and reopen context. Retained lineage remains audit history.
The train checks that every repair ancestor still has the bound current completion
head before admission and publication; otherwise it reports
`source_ci_repair_superseded`. A repair of the current head remains deliverable,
including when that source has already delivered.

Admission withholds a repair that is superseded *or* terminal — it reports
`source_ci_repair_failed` for a repair whose own task is FAILED, which owes no
target any delivery — however the repair reached it, including as a frozen batch
member re-added to the candidate window. A repair that cannot publish therefore
never holds a batch, and never holds a target: the open batch carrying one is
released by the same automatic supersession a refreshed stack uses, so the
corrected source the repair was filed against is admitted on the next visit
instead of waiting behind it. An explicitly paused batch is never released this
way, and a member whose task identity cannot be resolved to exactly one project
names the operator abort instead.

Only `red` files a repair. `cancelled` — every non-success required check
cancelled, nothing pending, no required check failed — is infrastructure: the
observation waits, re-requests the cancelled check suites of that exact head
under bounded backoff (`infra_*`), and files no task; after
`root.repair.source_ci_infra_attempts` consecutive such observations it names
`source_ci_infrastructure` instead of looping.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY | Source task |
| `repository_id` | TEXT | PRIMARY KEY, FK `repos.id` | Repository |
| `source_base` | TEXT | PRIMARY KEY | Frozen source base |
| `source_head` | TEXT | PRIMARY KEY | Exact observed source head |
| `generation` | INTEGER | PRIMARY KEY, `>= 0` | Source checkpoint generation |
| `policy_generation` | INTEGER | NOT NULL | Observed project policy generation |
| `state` | TEXT | NOT NULL | green, red, cancelled, pending or conflict (no required check ran and GitHub reports the PR unmergeable) |
| `evidence` | JSONB | NOT NULL | Trusted required-check facts and links |
| `repair_task_id` | TEXT | nullable, indexed | Current deduplicated repair |
| `repair_attempt` | INTEGER | NOT NULL, default 0, `>= 0` | Successor count |
| `repair_history` | JSONB | NOT NULL, default `[]` | Previous repair task/attempt identities |
| `observed_at` | REAL | NOT NULL | Unix timestamp |
| `infra_observations` | INTEGER | NOT NULL, default 0, `>= 0`, `a00000000079` | Consecutive infrastructure-only (cancellation-only) observations of this exact head; reset by any other observation |
| `infra_rerun_at` | REAL | nullable, `a00000000079` | Earliest time a re-run of this exact head may be requested again (bounded exponential backoff) |
| `infra_reruns` | INTEGER | NOT NULL, default 0, `>= 0`, `a00000000079` | Re-run requests issued for this exact head |

### Table: `integration_root_authorizations`

Append-only operator authorization of one exact train root source. Root
`admission: authorized` admits a COMPLETED root whose kind the policy does not
(not feature/bugfix, not in `root.authorized_task_ids`) only while a row matches
its exact source; holds, open gates, rejected reviews, generation-pinned
authorization evidence and source CI still bind. Written by `aq integration
authorize-root`; there is no update or delete path. See
`docs/superpowers/specs/2026-10-01-explicit-root-authorization-design.md`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `root-authorization-<uuid5>` of the exact identity |
| `project_id` | TEXT | NOT NULL | Project |
| `task_id` | TEXT | NOT NULL, UNIQUE with the source identity | Authorized root task |
| `repository_id` | TEXT | NOT NULL | Designated integration repository |
| `source_base` | TEXT | NOT NULL | Branch-origin base of the source |
| `source_head` | TEXT | NOT NULL | Exact authorized head |
| `generation` | INTEGER | NOT NULL, `>= 0` | Source checkpoint generation |
| `review_kind` | TEXT | NOT NULL | `leaf` or `parent` |
| `pr_url` | TEXT | NOT NULL | Pull request of the source |
| `task_type` | TEXT | nullable | Task kind when authorized (audit) |
| `policy_generation` | INTEGER | NOT NULL, `>= 0` | Project generation when authorized (audit only; never a fence) |
| `operator_id` | TEXT | NOT NULL | Operator or supervisor label |
| `reason` | TEXT | NOT NULL | Audit reason |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Unique constraint `uq_integration_root_authorizations_source` on `(task_id,
repository_id, source_base, source_head, generation)`.

### Table: `integration_subjects`

One durable row per integration *subject* the level-triggered reconciler
visits: a root batch, a parent episode or a source (review `rev-agile-ridge`
revision 2, §3.2). A subject has one owner (`engine`), one due time and one
pinned policy artifact. Added additively in revision `a00000000057`; nothing
drives it yet, and every subject defaults to the `legacy` engine. Typed model:
`src/integration/subjects.py`; queries: `IntegrationSubjectQueriesMixin`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Subject id |
| `project_id` | TEXT | NOT NULL | Project |
| `repository_id` | TEXT | NOT NULL | Designated integration repository |
| `kind` | TEXT | NOT NULL | `root_batch`, `parent_episode` or `source` |
| `subject_key` | TEXT | NOT NULL, UNIQUE with `(project_id, kind)` | Natural key, `<kind>:<repository>:<parts>`; makes creation idempotent |
| `engine` | TEXT | NOT NULL, default `legacy` | `legacy` or `reconciler`: the one engine that may mutate the subject |
| `phase` | TEXT | NOT NULL | `admitting`, `building`, `testing`, `repairing`, `promotable`, `publishing`, `published`, `cleaning`, `done` |
| `policy_playbook_id` | TEXT | NOT NULL, immutable | Policy playbook the subject runs under |
| `policy_artifact_sha256` | TEXT | NOT NULL, FK `playbook_artifacts` RESTRICT, immutable | Pinned compiled policy; a new activation never changes a running subject, and artifact collection keeps it |
| `task_id` | TEXT | nullable, immutable | Source or parent task; NULL exactly for a root batch |
| `batch_id` | TEXT | nullable, unique when set, immutable once set | Legacy `integration_batches` row a sealed root subject maps onto |
| `parent_episode_id` | TEXT | nullable, unique when set, immutable once bound; parent/task FK RESTRICT | Additive parent bridge (revision 58); binds the exact episode independently of the moving collection generation and validates repository identity |
| `target_ref` | TEXT | nullable | Ref the head belongs to; required with `head_sha` |
| `head_sha` | TEXT | nullable, 40 hex | Exact head the current phase refers to |
| `base_sha` | TEXT | nullable, 40 hex | Base of that head |
| `generation` | INTEGER | NOT NULL, default 0, never decreases | Domain generation of the head (review, collection or candidate revision) |
| `next_due_at` | REAL | nullable | Next visit; NULL only when held by a gate or done |
| `due_set_at` | REAL | NOT NULL | When the due time was set; the wait bound is measured from it |
| `max_wait_seconds` | INTEGER | NOT NULL, `> 0` | Pinned bound on any wait |
| `wait_reason` | TEXT | nullable | Why the subject idles; requires `next_due_at` |
| `gate_id` | TEXT | nullable | Explicit human gate holding the subject |
| `refusal_streak` | INTEGER | NOT NULL, default 0, `>= 0` | Consecutive transient refusals (backoff input) |
| `wake_requested_at` | REAL | nullable | Last event wake; a visit started before it stays due now |
| `last_visit_at` | REAL | nullable | Last reconciler visit |
| `last_journal_seq` | BIGINT | nullable | Newest `integration_subject_journal.seq` |
| `closed_reason` | TEXT | nullable | Required exactly when `phase = 'done'` |
| `writer_status` | TEXT | NOT NULL, default `none` | `none`, `filed`, `claimed`, `working`, `stopped`, `unknown` |
| `writer_task_id` | TEXT | nullable, indexed | Writer task; required unless `writer_status = 'none'` |
| `writer_fence_token` | INTEGER | nullable, `>= 0` | Lease fence |
| `writer_session_id` | TEXT | nullable | Writer session |
| `writer_claimed_at` | REAL | nullable | When the writer was claimed |
| `writer_last_push_at` | REAL | nullable | Writer's last observed push |
| `writer_stop_proof` | JSONB | nullable | Proof the writer stopped |
| `budget_ordinal` | INTEGER | nullable, `>= 0` | Current writer ordinal; the budget columns are all set or all NULL |
| `budget_class` | TEXT | nullable | Intelligence class of the ordinal |
| `budget_started_at` | REAL | nullable | Budget clock start |
| `budget_deadline_at` | REAL | nullable, `>= budget_started_at` | Budget clock deadline |
| `budget_attempts` | INTEGER | NOT NULL, default 0, `>= 0` | Counted conclusive exact-head attempts |
| `budget_attempt_limit` | INTEGER | nullable, `> 0` | Attempt bound of the ordinal |
| `version` | INTEGER | NOT NULL, default 0, never decreases | Optimistic-concurrency token of visit writes |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

`ck_integration_subjects_never_blocked` is the schema half of the never-blocked
guarantee: a `done` subject has a `closed_reason` and no due time, wait or gate;
any other subject has a `gate_id`, or a `next_due_at` no later than
`due_set_at + max_wait_seconds`. Partial unique index
`uq_integration_subjects_admitting_root` allows one admitting root subject per
`(project_id, repository_id)`. Seeding the subject for a newly outstanding sweep
request first closes an unbound admitting one keyed to an earlier request
(`closed_reason` `superseded: …`), which sealing would refuse anyway; a bound
subject is never closed this way, and a remaining one defers the seed rather than
raising. `idx_integration_subjects_due` serves the due
scan. Trigger `integration_subject_identity_pinned` refuses a change of id,
project, repository, kind, key, task, pinned policy or a bound batch, a
decreasing version or generation, and reopening a `done` subject.

### Table: `integration_subject_journal`

Append-only journal of what the reconciler observed, decided and did for one
subject: policy decisions (shadow and active), primitive outcomes, counted
attempts and receipts, each with the exact identity and the artifact it ran
under. Trigger `integration_subject_journal_append_only` refuses UPDATE and
DELETE. Added in revision `a00000000057`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `seq` | BIGINT | PRIMARY KEY, identity | Append order |
| `subject_id` | TEXT | NOT NULL, FK `integration_subjects` RESTRICT | Subject |
| `entry_kind` | TEXT | NOT NULL | `decision`, `action`, `attempt` or `receipt` |
| `idempotency_key` | TEXT | NOT NULL, UNIQUE with `subject_id` | Makes every append replay-safe |
| `visit_id` | TEXT | nullable | Visit that wrote the entry |
| `mode` | TEXT | NOT NULL | `shadow` or `active` |
| `policy_artifact_sha256` | TEXT | NOT NULL, FK `playbook_artifacts` RESTRICT | Artifact the entry ran under; artifact collection keeps it |
| `subject_version` | INTEGER | NOT NULL, `>= 0` | Subject version observed |
| `phase` | TEXT | NOT NULL | Subject phase observed |
| `head_sha` | TEXT | nullable, 40 hex | Exact head; required for attempts and receipts |
| `generation` | INTEGER | NOT NULL, `>= 0` | Generation of that head |
| `rule` | TEXT | nullable | Decision-table line; required for decisions |
| `primitive` | TEXT | nullable | One of the twenty primitives; required for decisions and actions |
| `outcome` | TEXT | nullable | Primitive outcome; NULL for decisions, required otherwise |
| `facts_digest` | TEXT | nullable | `sha256:` of the observation; required for decisions |
| `payload` | JSONB | NOT NULL, default `{}` | Arguments, details and evidence |
| `recorded_at` | REAL | NOT NULL | Unix timestamp |

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
| `lifecycle` | TEXT | NOT NULL | One of: sealing, sealed, building, testing, repairing, human_blocked, promoting, cleanup_pending, promoted, aborted, failed, empty. Cannot return to `sealing`; identity columns are immutable after sealing (triggers). `empty` rows are history only: an empty frontier now consumes its request without inserting a row (`TrainService` answers `batch_id` `integration-empty:<request_id>`) |
| `current_revision` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Latest candidate revision; monotone (trigger) |
| `integration_branch` | TEXT | nullable | Candidate branch; NULL only when empty |
| `pr_url` | TEXT | nullable | Audit PR for the candidate |
| `repair_stage_ordinal` | INTEGER | nullable, `>= 0` | Active repair stage |
| `tested_candidate_sha` | TEXT | nullable | Candidate head CI ran on |
| `baseline_candidate_sha` | TEXT | nullable, `a00000000080` | Exact candidate whose failing required checks the target also failed, or has not decided |
| `baseline_generation` | INTEGER | NOT NULL DEFAULT 0, `>= 0`, `a00000000080` | Repair generation those counters describe |
| `baseline_observations` | INTEGER | NOT NULL DEFAULT 0, `>= 0`, `a00000000080` | Consecutive unrepairable red observations of that exact candidate; reset by any other candidate or repair generation |
| `baseline_reruns` | INTEGER | NOT NULL DEFAULT 0, `>= 0`, `a00000000080` | Re-requests issued for that exact candidate |
| `baseline_rerun_at` | REAL | nullable, `a00000000080` | Earliest time the candidate's failing suites may be re-requested again (bounded exponential backoff) |
| `ci_evidence_id` | TEXT | nullable | Green `integration_check_evidence` |
| `final_main_sha` | TEXT | nullable | `main` after promotion |
| `human_abort_reason` | TEXT | nullable | Operator abort reason |
| `policy_snapshot` | JSON | NOT NULL | Project policy frozen at seal |
| `artifact_snapshot` | JSON | NOT NULL | Playbook artifact pins frozen at seal |
| `cleanup_state` | TEXT | NOT NULL | Aggregate state of `integration_cleanup_items` |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `updated_at` | REAL | NOT NULL | Unix timestamp |

Partial unique index `uq_integration_batches_active_project` allows one
non-terminal batch per project over `target_ref IS NULL` rows only. A row with a
`target_ref` is the Git-first train's additive input/intent shape, advanced
solely by that train: it is never mutually exclusive with a legacy batch, never
reads as busy to the legacy scheduler, and never holds its members out of the
legacy frontier — so a rollback to `integration.git_first: shadow` leaves it
inert rather than blocking. See
[the cutover runbook](../guides/git-first-cutover-runbook.md#rollback).

A red Git-first candidate is repaired only where the target commit it was built
on does not also fail the same required check. An epic whose only failures are
proven pre-existing may receive one ordinary sync repair to merge the current
default branch's exact head, when that head is train-produced or attested and
its own trusted checks pass every failing name. The repair input pins the
default ref, SHA, check names and provenance. Its ordinary filing dedup key
(`repair:<batch-id>:sync-default-branch`) bounds sync to one task per batch across
candidate generations, restarts and archival. The worker retains every frozen
member, resolves source conflicts and regenerates generated files; publication
still requires trusted checks and attestation on the repaired exact candidate.
The train inspects the pinned merge without changing refs or resolving code,
and passes conflicting filenames through the existing conflict repair brief.
A completed epic with every required child already contained can freeze a
`train-epic-sync-` batch over those exact retained completion sources and the
collected head. Retired origins and archived children are allowed; tasks are
never reopened and no new child is filed. This batch owes its own tested
candidate publication despite the children's prior inclusion. Its identity
does not change when the default branch moves, so spent or aborted syncs do
not become new attempts against the same collected head.
Root targets, unproven failures, and default heads with red, missing, pending or
untrusted evidence keep the existing behavior. The `baseline_*` columns bound the
re-requests of that candidate's own suites and name
`candidate_pre_existing_failure` for a human once three consecutive observations
were unrepairable.

Only an observed `FAILURE` on the exact target commit establishes a pre-existing
failure. A `MISSING` required target check makes the baseline unavailable: the
target may never have run the required workflow, as with a hand-pushed `main`.
Without a comparable baseline, candidate failures remain repairable. This does
not satisfy any candidate check or change the trusted evidence needed to publish.
Train status retains the baseline decision on both repair and re-request visits.

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
| `stage_ordinal` | INTEGER | NOT NULL, `>= 0` | Repair stage that reserved the resolution; any retained successor stage |
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
| `operation_stage` | INTEGER | NOT NULL, `>= 0` | Active repair stage the mutation was reserved under; any retained successor stage |
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
| `ordinal` | INTEGER | PK, `>= 0` | Stage; exhausted stages are retained and successors take the next ordinal |
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
| `attempts` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Counted attempts; monotone (trigger) except when a human resume re-arms a `failed`/`expired`/`cancelled` stage back to `active` (a12a5e1e4f05) |
| `dossier` | JSON | nullable | Debug dossier handed to the next stage / human |
| `state` | TEXT | NOT NULL | One of: pending, active, awaiting_completion, passed, failed, expired, cancelled |
| `completed_at` | REAL | nullable | Unix timestamp |

### Table: `operator_decisions`

Shared, durable operator instructions on an integration object (a task, batch or
repair operation), written by `src/operator_decisions.py`. Rows are append-only:
a `hold` stays active until a later `release` row names it, and the integration
engine refuses to advance a held object. Any supervisor reads the same history,
so no instruction lives only in one session's local state.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PK | Decision id |
| `project_id` | TEXT | NOT NULL | Owning project of the object |
| `object_kind` | TEXT | NOT NULL | `task`, `batch` or `operation` (`ck_operator_decisions_object_kind`) |
| `object_id` | TEXT | NOT NULL | Id of the object the decision targets |
| `effect` | TEXT | NOT NULL | `note`, `hold` or `release` (`ck_operator_decisions_effect`) |
| `operator` | TEXT | NOT NULL | Who made the decision |
| `decision` | TEXT | NOT NULL | The instruction text |
| `source` | TEXT | NOT NULL | Where the decision came from |
| `source_ref` | TEXT | NOT NULL | Reference within that source |
| `recorded_by` | TEXT | NOT NULL | Who recorded the row |
| `created_at` | REAL | NOT NULL | Unix timestamp |
| `idempotency_key` | TEXT | NOT NULL | Unique per project (`uq_operator_decisions_request`) |
| `releases` | TEXT | FK operator_decisions.id, nullable, UNIQUE | Set exactly when `effect = 'release'` (`ck_operator_decisions_release`); a hold is released at most once |

Index `ix_operator_decisions_object` on (`project_id`, `object_kind`, `object_id`).

### Table: `integration_delegate_releases`

Audit trail for delegates released because their integration operation ended
(cancelled, superseded, or completed without them). One row per release.

Deliberately carries **no** foreign key to `tasks` or to
`integration_repair_operations`: its whole job is to outlive both, so the
answer to "why did this task end, and who ended it" survives a later delete or
archive of the task. See
`docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PK | `rel-<uuid4[:12]>` |
| `operation_id` | TEXT | NOT NULL | The operation that ended (plain id, no FK) |
| `operation_state` | TEXT | NOT NULL | `completed` or `cancelled` |
| `task_id` | TEXT | NOT NULL | The delegate (plain id, no FK) |
| `project_id` | TEXT | NOT NULL | Project the delegate belonged to |
| `role` | TEXT | NOT NULL | `verifier`, `repair_stage` or `candidate_member` |
| `disposition` | TEXT | NOT NULL | `cancelled` (operation cancelled) or `superseded` (completed without it) |
| `previous_status` | TEXT | NOT NULL | Ticket status before the release |
| `reason` | TEXT | NOT NULL | Sentence shown to operators |
| `released_by` | TEXT | NOT NULL | `integration_service`, `integration_abort`, `integration_cancel_preserving` or `doctor` |
| `released_at` | REAL | NOT NULL | Unix timestamp |
| `cleanup` | JSON | nullable | What the delegate still holds: `{"state": clear\|blocked, "blockers": [...]}` |

### Table: `integration_owner_recoveries`

Append-only audit of attempts to recover a stranded integration branch-owner row. All
identities are soft text so the evidence outlives the owner, task, session, and workspace.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Recovery audit id |
| `owner_row_id` | TEXT | NOT NULL | Recovered branch-owner row id |
| `repository_id` | TEXT | NOT NULL | Repository observed by the recovery |
| `ref` | TEXT | NOT NULL | Protected integration ref |
| `task_id` | TEXT | nullable | Soft task identity, when known |
| `outcome` | TEXT | NOT NULL | `released`, `preserved_and_released`, or `not_eligible` |
| `reason` | TEXT | nullable | Named refusal when not eligible |
| `evidence` | JSON | NOT NULL | Origin/local tips, checkout, preservation ref, and stop proof |
| `principal` | TEXT | NOT NULL | Sweep, local operator, or supervisor that ran recovery |
| `created_at` | FLOAT | NOT NULL | Unix timestamp |

Check `ck_integration_owner_recoveries_outcome` restricts `outcome`; index
`idx_integration_owner_recoveries_owner` (`owner_row_id`, `created_at`) orders an owner's
recovery history.

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
| `outstanding_request_id` | TEXT | nullable | Set together with `outstanding_trigger` and `outstanding_requested_at` (`ck_project_integration_schedules_outstanding_request`). Cleared by an empty seal or the promoted batch's release; a request whose batch ended any other way is freed by `src/integration/stale_schedule.py` |
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

### Retired: `development_deliveries`

The development-mode receipt journal is gone: revision `a00000000038` dropped
it once every reader asked git instead (`src/integration/delivery_truth.py`).
No table records that a task is delivered.  What the journal held that was not
a delivery answer lives in the existing `events` table:

* `development.operation` — one event per revision of a publisher action
  (`prepared` → `publishing` → `finished`, or `parked`/`cancelled`); the payload
  is `id`, `project_id`, `repository_id`, `target_ref`, `expected_sha`,
  `prepared_sha`, `state`, `manifest`, `evidence`, `reason`, `created_at`,
  `updated_at`.  `finished` means the action ended, never that work is
  delivered.  An outstanding legacy row was retained as `legacy-operation:<id>`.
* `development.legacy_provenance` — one immutable event per retired row that
  named a source: `legacy_id`, `project_id`, `repository_id`, `target_ref`,
  `expected_sha`, `prepared_sha`, `created_at`, `kind`, the `manifest` members
  (`task_id`, `source_sha`, `parent_task_id`, `superseded_by`, `acceptance`),
  `completion_sources`, `resolved_by_delivered_repair`, and the recorded
  `tests` (`validation`, `conclusion`, `checks`, `failing_tests`, `head_sha`) or
  merge `conflict` evidence.  It carries no `state` and no delivery conclusion;
  only the operator provenance migration, a legacy repair close's filing fence
  and train-mode legacy adoption read it, and each re-proves every source in
  git.
* `development.legacy_retirement` — one summary per project: row count,
  retained actions, marked tasks, archived tasks of the same shape, malformed
  rows.

A live branchless COMPLETED task a retired manifest named with a source its
current completion never recorded carries the `task_metadata` key
`development_legacy_artifact` (`legacy_id`, `source_sha`, `completion_id`,
`reason`).  Fenced to that completion generation, it keeps the task's delivery
unknown until an exact generation is retained in git.  The revision is
idempotent and its downgrade recreates an empty journal; the events and markers
remain.

### Table: `integration_legacy_deliveries`

Legacy delivered children: one row per terminal child of a terminal parent
that the train never collected and never will.  These are parents that
finished under the development publisher or before trains existed, or whose
collection was cancelled.  Train receipts cannot be backfilled for such
children, so `aq integration adopt-legacy-deliveries`
(`src/integration/legacy_deliveries.py`) records this separate, audited fact
instead.  It writes a row after proving the child's work is on the default
branch (by ancestry, or because merging it changes nothing), or after an
explicit, named, reasoned operator decision.  Integration status then settles
the child instead of reporting `missing_receipt`.  The row never feeds parent
completion.  Revision `a00000000022` creates the table conditionally;
`a00000000023` widens `proof` to the re-delivered, superseded and abandoned
values.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `task_id` | TEXT | PRIMARY KEY | The adopted child; a soft ref (no foreign key), so archive keeps the row.  Inserts use `ON CONFLICT DO NOTHING`, which keeps the command idempotent |
| `project_id` | TEXT | NOT NULL | Project; indexed with `parent_task_id` (`idx_integration_legacy_deliveries_parent`) |
| `parent_task_id` | TEXT | NOT NULL | The terminal parent that was never collected |
| `repository_id` | TEXT | NOT NULL | The designated integration repository the proof ran against |
| `target_ref` | TEXT | NOT NULL | Default-branch ref, e.g. `refs/heads/main` |
| `target_sha` | TEXT | NOT NULL | Default-branch tip the proof was checked against |
| `delivered_sha` | TEXT | nullable | Commit whose work is proven on `target_sha`: an ancestor of it, a commit whose merge into it changes nothing (`content_equivalent`), or the operator-named re-delivering commit on the default branch (`superseded`); NULL only for `operator_accepted` and `abandoned` (`ck_integration_legacy_deliveries_delivered_sha`) |
| `proof` | TEXT | NOT NULL | One of: development_delivery, branch_tip, content_equivalent, superseded, operator_accepted, abandoned (`ck_integration_legacy_deliveries_proof`) |
| `development_delivery_id` | TEXT | nullable | The retired development journal row (kept as a `development.legacy_provenance` event) that located the proven source; its state never proved anything |
| `operator_id` | TEXT | NOT NULL | Audit label of the local operator or supervisor session that ran the command |
 | `reason` | TEXT | NOT NULL | The supplied reason, or `legacy delivery proven by <proof>` |
 | `created_at` | REAL | NOT NULL | Unix timestamp |

### Table: `test_selections`

Immutable smart-test-selection record: one row per selection request, capturing the exact change snapshot, the provenance digests (catalogue, rules, policy), the module decision with its stage breakdown, and the execution/usage metadata.  Result evidence (local runs, CI, replays) is never amended into it — it is appended to `test_selection_observations` instead.  `task_id` is a soft reference (no foreign key): archive removes the live task row, so the selection history is preserved until retention expires, as escalation audit rows are.  `src/database/queries/test_selection_queries.py` is the sole reader/writer.  Revision `a00000000028` creates the table.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Selection id |
| `project_id` | TEXT | NOT NULL, FK `projects.id` | Project; indexed with `created_at` (`idx_test_selections_project_created`) |
| `task_id` | TEXT | nullable | Soft ref so archive keeps the row; indexed with `created_at` (`idx_test_selections_task_created`) |
| `session_id` | TEXT | nullable | Session that produced the selection |
| `claim_epoch` | INTEGER | nullable | Claim epoch for the session |
| `mode` | TEXT | NOT NULL | One of: plan_only, shadow, enforce (`ck_test_selections_mode`) |
| `workspace` | TEXT | NOT NULL | Workspace root the selection ran against |
| `base_ref` | TEXT | NOT NULL | Base ref (e.g. `refs/heads/main`) |
| `base_sha` | TEXT | NOT NULL | Base commit |
| `head_sha` | TEXT | NOT NULL | Head commit |
| `dirty_fingerprint` | TEXT | NOT NULL | Fingerprint of dirty/untracked files |
| `snapshot_fingerprint` | TEXT | NOT NULL | Fingerprint of the change snapshot |
| `snapshot_complete` | BOOLEAN | NOT NULL DEFAULT true | FALSE only when `incomplete_reason` explains the gap |
| `incomplete_reason` | TEXT | nullable | Why the snapshot was incomplete |
| `catalogue_digest` | TEXT | NOT NULL | Selection catalogue digest in force |
| `rules_digest` | TEXT | NOT NULL | Selection rules digest |
| `policy_digest` | TEXT | NOT NULL | Omission policy digest |
| `question_schema_version` | INTEGER | NOT NULL | Question-schema generation the model answered |
| `static_engine` | TEXT | NOT NULL | Static-engine identifier |
| `marker_policy` | TEXT | NOT NULL DEFAULT 'default' | One of: default, all (`ck_test_selections_marker_policy`) |
| `cache_key` | TEXT | NOT NULL | Cache key over the selection inputs |
| `jev_requested_model` | TEXT | nullable | Model name requested from the provider |
| `jev_returned_model` | TEXT | nullable | Model name the provider actually returned |
| `jev_status` | TEXT | NOT NULL | One of: ok, partial, disabled, unconfigured, unavailable, invalid, over_budget, timeout, model_drift (`ck_test_selections_jev_status`) |
| `fallback_reason` | TEXT | nullable | Why the static fallback engaged, if it did |
| `full_required` | BOOLEAN | NOT NULL DEFAULT false | The change mandates the full suite regardless of selection |
| `jev_used_for_omission` | BOOLEAN | NOT NULL DEFAULT false | The model's answer shaped module omission |
| `promotion_id` | TEXT | nullable | Soft ref to the `test_selection_promotions` row whose policy shaped the run |
| `area_decisions` | JSON | NOT NULL | Per-area decision trace |
| `mandatory_modules` | JSON | NOT NULL | Modules required by hard rules |
| `static_modules` | JSON | NOT NULL | Modules added by the static engine |
| `jev_modules` | JSON | nullable | Modules the model answered for |
| `fallback_modules` | JSON | NOT NULL | Modules added on fallback, if any |
| `final_modules` | JSON | NOT NULL | The complete final selection |
| `reasons` | JSON | NOT NULL | Per-module reasons (`mandatory_rule`, `jev_unknown`, `fallback_timeout`, …) |
| `pending_obligations` | JSON | NOT NULL | Obligations the selection deferred |
| `argv` | JSON | NOT NULL | The resolved pytest argv |
| `elapsed_ms` | JSON | NOT NULL | Per-stage elapsed time |
| `usage` | JSON | NOT NULL | Provider usage metadata |
| `created_at` | REAL | NOT NULL | Unix timestamp |

Indexes: `idx_test_selections_project_created` (`project_id`, `created_at`); `idx_test_selections_task_created` (`task_id`, `created_at`).

### Table: `test_selection_observations`

Evidence rows appended to a selection after the run: the local execution, the CI result, and any replay.  Appends only, never edits, so an observation always records what happened, not what the selection claimed.  `selection_id` is a real foreign key with `ON DELETE CASCADE`, and reads are ordered by (`selection_id`, `observed_at`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Observation id |
| `selection_id` | TEXT | NOT NULL, FK `test_selections.id` (`ON DELETE CASCADE`) | The selection this is evidence for; indexed with `observed_at` (`idx_test_selection_observations_selection`) |
| `kind` | TEXT | NOT NULL | One of: execution, ci, replay (`ck_test_selection_observations_kind`) |
| `source` | TEXT | NOT NULL | Provenance label (runner, CI provider, replay origin) |
| `exit_code` | INTEGER | nullable | Exit code the run produced |
| `duration_ms` | INTEGER | nullable | Wall-clock duration |
| `executed_modules` | JSON | NOT NULL | Modules that actually ran |
| `failed_node_ids` | JSON | NOT NULL | Failed node ids, if any |
| `payload` | JSON | NOT NULL | Runner-specific detail |
| `observed_at` | REAL | NOT NULL | Unix timestamp |

### Table: `test_selection_promotions`

Append-only (except revocation) record of an omission policy a project earned: the model, question-schema version and the catalogue/rules/policy digests the policy was computed under, plus the evidence that justified promoting it.  A project has at most one active promotion — the partial unique index `uq_test_selection_promotions_active` on `project_id` WHERE `revoked_at IS NULL` enforces it, and `insert_test_selection_promotion` lands with `ON CONFLICT DO NOTHING` on that index, so re-promoting the same policy is idempotent while revoking it is not.  `test_selections.promotion_id` references these rows by id (soft, no foreign key) when the selection ran under a promoted policy.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Promotion id |
| `project_id` | TEXT | NOT NULL, FK `projects.id` | Project; active-promotion uniqueness (`uq_test_selection_promotions_active`, `revoked_at IS NULL`) |
| `model` | TEXT | NOT NULL | Model the policy was earned under |
| `question_schema_version` | INTEGER | NOT NULL | Question-schema generation — a version change invalidates the policy |
| `catalogue_digest` | TEXT | NOT NULL | Catalogue digest the policy was computed under |
| `rules_digest` | TEXT | NOT NULL | Rules digest the policy was computed under |
| `policy_digest` | TEXT | NOT NULL | The policy itself, digested |
| `evidence` | JSON | NOT NULL | The held-out evidence that justified the promotion |
| `promoted_by` | TEXT | NOT NULL | Operator/supervisor session id that promoted it |
| `promoted_at` | REAL | NOT NULL | Unix timestamp |
| `revoked_at` | REAL | nullable | NULL while active |
| `revoke_reason` | TEXT | nullable | Why it was revoked |

### Table: `agent_cron`

Session-owned recurring prompts introduced by PostgreSQL revision `a00000000086`
([recurrence design](design/agent-cron.md)). A registration belongs to one live
session instance; worker registrations also bind to their task claim. Projectless
supervisors retain NULL project scope. Session, task and pending-message references
are soft so history survives owner turnover. This table stores prompt scheduling
and message delivery diagnostics; it does not record code integration or retain a
worker claim as a durable wait does.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Stable schedule id |
| `project_id` | TEXT | nullable, FK → projects | NULL for projectless supervisors |
| `session_id` | TEXT | NOT NULL | Registering session |
| `session_instance_token` | TEXT | NOT NULL | Owning live session instance |
| `owner_task_id` | TEXT | nullable | Held task for a worker registration |
| `claim_epoch` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Worker claim fence |
| `idempotency_key` | TEXT | NOT NULL | Unique with session id and instance token |
| `prompt` | TEXT | NOT NULL | Prompt to deliver to the owner |
| `recurrence` | JSONB | NOT NULL | Validated interval or restricted cron expression and timezone |
| `state` | TEXT | NOT NULL DEFAULT 'active', CHECK: active, cancelled, expired | Schedule lifecycle |
| `created_at` | REAL | NOT NULL | Registration time |
| `next_fire_at` | REAL | NOT NULL | Next strictly future tick |
| `checked_at` | REAL | NOT NULL DEFAULT 0 | Last bounded daemon scan |
| `stopped_at` | REAL | nullable | Cancellation or owner-expiry time |
| `tick_count` | INTEGER | NOT NULL DEFAULT 0 | Emitted prompt count |
| `coalesced_count` | INTEGER | NOT NULL DEFAULT 0 | Missed ticks coalesced instead of replayed |
| `pending_message_id` | TEXT | nullable | At most one pending prompt message |
| `pending_until` | REAL | nullable | Pending prompt expiry |
| `delivery_attempts` | INTEGER | NOT NULL DEFAULT 0, `0 <= value <= 5` | Terminal submission attempts |
| `next_attempt_at` | REAL | NOT NULL DEFAULT 0 | Retry backoff deadline |
| `last_delivery_at` | REAL | nullable | Last successful submission time |
| `last_delivery_status` | TEXT | nullable | Prompt delivery diagnostic |
| `last_error` | TEXT | nullable | Last delivery failure |

`uq_agent_cron_idempotency` covers `(session_id, session_instance_token,
idempotency_key)`. `ck_agent_cron_state` bounds lifecycle values and
`ck_agent_cron_attempts_epoch` bounds attempts and claim epoch.
`idx_agent_cron_scan` indexes `(state, checked_at, next_fire_at)`.
Legacy SQLite files predate this table and have no schedules to import.

### Table: `agent_waits`

One row per durable condition an agent keeps open while it stays blocked on
something external: a managed job, another task, an incoming message on a
thread, or a timer (`docs/guides/agent-waits.md`). A wait holds the task
`IN_PROGRESS` and retains its claim, workspace and pool seat; registration
returns immediately, and resolution or expiry is a state change on the row,
never a daemon-side timer. Soft owner references (`owner_kind`, `owner_id`,
`claim_epoch`) survive archival and claim turnover — only `project_id` is a
hard foreign key — and delivery receipts live exclusively on the result
message (`result_message_id`), not here. `uq_agent_waits_idempotency` makes a
repeated registration with the same key idempotent while a changed one is
refused, and `uq_agent_waits_active_claim` (partial unique on
`owner_id, claim_epoch` WHERE `state = 'active' AND owner_kind = 'task'`)
keeps at most one live wait per task claim. Every wait is bounded: `deadline_at`
must stay within 24 hours of `created_at`, and `version` fences optimistic
updates against the epoch.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Wait id |
| `project_id` | TEXT | NOT NULL, FK → projects | Project the wait belongs to |
| `owner_kind` | TEXT | NOT NULL, CHECK: task, supervisor | Who is blocked on the condition |
| `owner_id` | TEXT | NOT NULL | Task or supervisor id the wait is held for |
| `session_id` | TEXT | NOT NULL | Session that registered the wait |
| `session_instance_token` | TEXT | NOT NULL | Instance token of the registering session |
| `claim_epoch` | INTEGER | NOT NULL | Claim epoch the wait is recorded under |
| `kind` | TEXT | NOT NULL, CHECK: job, task, message, timer | Adapter that resolves the condition |
| `match` | JSON | NOT NULL | Adapter-specific condition (ref, thread, due time…) |
| `state` | TEXT | NOT NULL DEFAULT 'active', CHECK: active, satisfied, expired, cancelled | Lifecycle state |
| `version` | INTEGER | NOT NULL DEFAULT 1 | Optimistic-update fence |
| `created_at` | REAL | NOT NULL | Registration time |
| `deadline_at` | REAL | NOT NULL, `> created_at` and within 24 h of it | Latest time the wait stays live |
| `resolved_at` | REAL | nullable | When it left `active` |
| `wait_resumed_at` | REAL | nullable | When its owner resumed work |
| `checked_at` | REAL | NOT NULL DEFAULT 0 | Last probe time; sweep candidate index |
| `result_ref` | TEXT | nullable | Adapter resolution reference |
| `digest` | JSON | nullable | Resolution evidence summary |
| `idempotency_key` | TEXT | NOT NULL | Registration key (`uq_agent_waits_idempotency`) |
| `result_message_id` | TEXT | nullable | Message that carried the result to the owner |

### Table: `jobs`

One row per managed job: a detached, harness-independent command with an
admission contract, a deterministic queue position and an authoritative
result (`docs/specs/implementation/managed-jobs.md`). Submission is
idempotent per owner via `uq_jobs_owner_key` on
(`project_id`, `owner_kind`, `owner_id`, `idempotency_key`), and
`request_hash` plus `preset_version` pin the exact request so a re-submitted
key only replays an identical one. Managed jobs are independent of harness
sessions: `submitter_session_id` is a soft reference, and the workspace pin
is carried by `workspace_id` + `workspace_generation` with `job_workspace_pins`.
Scheduling is bounded — `queue_deadline`, `run_timeout` and the derived
`run_deadline` are mandatory, `weight > 0` and `priority_band BETWEEN 0 AND 2`
(`ck_jobs_capacity`) — and the runner is fenced by `runner_nonce`, `boot_id`,
`start_ticks` and `pid` so a recovered job can tell which process is
authoritative. `result_version` / `result_ref` / `result` record the observed
exit, integrity and rendered result; `cleanup_blocked` and
`output_reservation_bytes` (default 64 MiB) keep the runner's cleanup and
output-reservation contract visible on the row.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Job id |
| `project_id` | TEXT | NOT NULL | Project (soft reference, no foreign key) |
| `task_id` | TEXT | nullable | Owning task when `owner_kind = 'task'` |
| `owner_kind` | TEXT | NOT NULL, CHECK: task, integration | Submission owner |
| `owner_id` | TEXT | NOT NULL | Owner's task or integration operation id |
| `submitter_session_id` | TEXT | nullable | Session that submitted, if any |
| `claim_epoch` | INTEGER | nullable | Claim epoch the submission belonged to |
| `integration_operation_id` | TEXT | nullable | Integration operation for `owner_kind = 'integration'` |
| `idempotency_key` | TEXT | NOT NULL | Submission key (`uq_jobs_owner_key`) |
| `request_hash` | TEXT | NOT NULL | Digest of the exact submission request |
| `preset` | TEXT | NOT NULL | Preset id the job runs under |
| `preset_version` | INTEGER | NOT NULL | Preset generation at submission |
| `argv` | JSON | NOT NULL | The command to run |
| `contract` | JSON | NOT NULL | Admission contract (budgets, limits, expectations) |
| `workspace_id` | TEXT | NOT NULL | Workspace the job runs in |
| `workspace_generation` | INTEGER | NOT NULL | Workspace generation pinned for the run |
| `input_mode` | TEXT | NOT NULL, CHECK: live, snapshot | Whether inputs are live or a pinned snapshot |
| `input_ref` | TEXT | nullable | Snapshot reference in `snapshot` mode |
| `input_fingerprint` | TEXT | nullable | Fingerprint of the pinned inputs |
| `input_stability` | TEXT | NOT NULL DEFAULT 'unverified' | Whether pinned inputs were verified stable |
| `job_class` | TEXT | NOT NULL, CHECK: shared, exclusive | Admission class |
| `weight` | INTEGER | NOT NULL, `> 0` | Queue weight |
| `priority_band` | INTEGER | NOT NULL, `BETWEEN 0 AND 2` | Priority band |
| `state` | TEXT | NOT NULL DEFAULT 'queued', CHECK: queued, starting, running, cancelling, succeeded, failed, cancelled, lost | Lifecycle state |
| `state_version` | INTEGER | NOT NULL DEFAULT 0 | Fence for state transitions |
| `submitted_at` | REAL | NOT NULL | Submission time |
| `launch_at` | REAL | nullable | When launch was admitted |
| `started_at` | REAL | nullable | When the runner started it |
| `ended_at` | REAL | nullable | When it ended |
| `queue_deadline` | REAL | NOT NULL | Latest time the job may start |
| `run_timeout` | REAL | NOT NULL | Run budget in seconds |
| `run_deadline` | REAL | nullable | Derived latest end time |
| `runner_nonce` | TEXT | NOT NULL | Per-launch nonce binding a runner process |
| `boot_id` | TEXT | nullable | Host boot id of the runner |
| `pid` | INTEGER | nullable | Runner process id |
| `start_ticks` | BIGINT | nullable | Ticks at process start |
| `exit_code` | INTEGER | nullable | Observed exit code |
| `signal` | INTEGER | nullable | Terminating signal, if any |
| `infra_reason` | TEXT | nullable | Infrastructure failure reason, if any |
| `measurements` | JSON | nullable | Runner measurements |
| `result_version` | INTEGER | nullable | Result envelope generation |
| `result_ref` | TEXT | nullable | Reference to the rendered result |
| `result` | JSON | nullable | Result body |
| `output_retention` | TEXT | NOT NULL DEFAULT 'reserved' | Retention contract for retained output |
| `output_reservation_bytes` | BIGINT | NOT NULL DEFAULT 67108864 | Reserved output budget (default 64 MiB) |
| `cleanup_blocked` | BOOLEAN | NOT NULL DEFAULT false | Cleanup blocked on an unresolved receipt |
| `retry_of` | TEXT | nullable | Job id this job is a retry of |

### Table: `job_workspace_pins`

One row per job: the workspace generation the job is admitted to. Pins have
no expiry and no cancellation path — only verified process cleanup may
release them, including after a lost receipt — so a crashed runner can never
leave a silently-reusable pin behind. `created_at` records admission for
diagnostics; `idx_job_pins_workspace` lists the jobs currently pinning a
workspace.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `job_id` | TEXT | PRIMARY KEY, FK → jobs | Pinned job |
| `workspace_id` | TEXT | NOT NULL, FK → workspaces | Pinned workspace |
| `generation` | INTEGER | NOT NULL | Workspace generation the job runs against |
| `created_at` | REAL | NOT NULL | Admission time |

### Table: `job_outbox`

Bounded delivery of job receipts to their owner: one row per outbox `key`,
with the rendered `payload` and the delivery watermark. `key` is the
idempotency anchor (owner-scoped), so a crashed dispatch retries the same
payload and `delivered_at` records when the receipt landed. Digest and
escalation identities live in their own domain tables; this table is the
job-domain's half of the receipt pipeline.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | Owner-scoped outbox key |
| `job_id` | TEXT | NOT NULL, FK → jobs | Job the receipt belongs to |
| `created_at` | REAL | NOT NULL | When the outbox row was written |
| `payload` | JSON | NOT NULL | Frozen receipt delivered to the owner |
| `delivered_at` | REAL | nullable | When delivery completed; NULL until then |

### Table: `outbound_deliveries`

Shared outbox for the newer report and conversation lifecycles that deliver
prose to a user or channel rather than to a session. Digest and escalation
identities remain in their established domain tables; rows here are
transport-shaped and transport-neutral: a typed `destination`, a frozen
`payload` (with `payload_hash` binding delivery to exactly that content) and
a lease (`lease_owner`, `lease_expires_at`) that `ck_outbound_deliveries_lease`
keeps non-null only while `state = 'sending'` — no two dispatchers can hold
the same row. `dedup_key` (`uq_outbound_deliveries_dedup`) makes retrying the
same business event idempotent, while `marker`
(`uq_outbound_deliveries_marker`) anchors a bounded stream so re-sends stay
within one marker window. `state = 'sent'` is only reachable with an
`external_receipt_id` and `receipt_confirmed_at`
(`ck_outbound_deliveries_receipt`): a network ack the platform confirms is
what makes a delivery sent — `idx_outbound_deliveries_due`
(`state`, `due_at`) is the dispatch loop's scan path.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Delivery id |
| `owner_kind` | TEXT | NOT NULL | Domain owning the delivery (report, conversation, …) |
| `owner_id` | TEXT | NOT NULL | Owner id in that domain |
| `dedup_key` | TEXT | NOT NULL, UNIQUE | Business-event dedup anchor |
| `destination` | JSON | NOT NULL | Typed delivery destination |
| `payload` | JSON | NOT NULL | Frozen delivery content |
| `payload_hash` | TEXT | NOT NULL | Digest of `payload`; delivery proves this bytes |
| `marker` | TEXT | NOT NULL, UNIQUE | Bounded-stream anchor for re-sends |
| `state` | TEXT | NOT NULL DEFAULT 'pending', CHECK: pending, sending, sent, retry, unknown, cancelled | Dispatch state |
| `due_at` | REAL | NOT NULL | Earliest dispatch time |
| `lease_owner` | TEXT | nullable | Dispatcher holding the row; NULL outside `sending` |
| `lease_expires_at` | REAL | nullable | Lease end; NULL outside `sending` |
| `attempt_count` | INTEGER | NOT NULL DEFAULT 0, `>= 0` | Delivery attempts so far |
| `external_receipt_id` | TEXT | nullable | Platform receipt id; required once `sent` |
| `receipt_confirmed_at` | REAL | nullable | Receipt confirmation time; required once `sent` |
| `last_error` | TEXT | nullable | Last dispatch error, if any |
| `created_at` | REAL | NOT NULL | When the row was written |
| `updated_at` | REAL | NOT NULL | Last state change |

### Table: `morning_reports`

One row per (schedule, local date) for a durable morning/brief report
(`docs/guides/supervisor-hourly-reports.md`), built by a lease-bounded
author under an explicit `author_deadline`. The report is fully local on the
row: `config_snapshot`, `build_context`, `brief` (+ `brief_hash`) are what the
author received, `source_cursors` / `source_heads` are what it read, and
`report` is the final rendered output (with `fallback` for the degraded
path). `uq_morning_reports_day` keeps at most one report per schedule per
local date, and `ck_morning_reports_window` keeps the observation window
sane. `state` walks building → ready → authoring → final, with
suppressed/skipped/failed as terminal non-final states, and
`finalized_at` closes it out. `lease_owner` / `lease_expires_at` bind the
author seat so a dead author cannot hold the day's report hostage.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | Report id |
| `schedule_id` | TEXT | NOT NULL | Schedule the report belongs to |
| `local_date` | TEXT | NOT NULL | Local date the report covers (`uq_morning_reports_day`) |
| `config_snapshot` | JSON | NOT NULL | Schedule config as resolved for this build |
| `scope_key` | TEXT | NOT NULL | Coverage scope this report builds against |
| `timezone` | TEXT | NOT NULL | Timezone the local date was computed in |
| `planned_at` | REAL | NOT NULL | Planned build time |
| `window_start` | REAL | NOT NULL | Observation window start |
| `window_end` | REAL | NOT NULL, `> window_start` | Observation window end |
| `build_context` | JSON | NOT NULL | Bounded context the author works from |
| `brief` | JSON | nullable | The brief an author reads |
| `brief_hash` | TEXT | nullable | Digest of `brief`; bounds the author's input |
| `source_cursors` | JSON | nullable | Cursors the build read up to |
| `source_heads` | JSON | nullable | Heads the build read up to |
| `state` | TEXT | NOT NULL DEFAULT 'building', CHECK: building, ready, authoring, final, suppressed, skipped, failed | Lifecycle state |
| `reason` | TEXT | nullable | State reason (suppression, failure, …) |
| `fallback` | JSON | nullable | Fallback build when the primary path failed |
| `report` | JSON | nullable | Final rendered report |
| `coverage` | JSON | nullable | Coverage summary the build produced |
| `author_deadline` | REAL | NOT NULL | Latest time the author may hold the seat |
| `lease_owner` | TEXT | nullable | Author session holding the seat |
| `lease_expires_at` | REAL | nullable | Author lease end |
| `created_at` | REAL | NOT NULL | Row creation time |
| `finalized_at` | REAL | nullable | When the report left authoring |

### Table: `morning_report_coverage`

Coverage is independent of external transport receipts: a schedule × scope ×
source row records what has already been consumed so a changed project
selection from the same source does not re-consume evidence. Scope keys keep
cursors per (schedule, scope) — the same source serving two scopes holds two
rows — and `report_id` binds each advancement to the `morning_reports` row
that produced it, so coverage gaps trace back to a report rather than to a
delivery.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `schedule_id` | TEXT | PRIMARY KEY | Covered schedule |
| `scope_key` | TEXT | PRIMARY KEY | Coverage scope this row tracks |
| `source` | TEXT | PRIMARY KEY | Source the cursor applies to |
| `covered_until` | REAL | NOT NULL | Latest event time consumed |
| `head_sha` | TEXT | nullable | Source head the cursor was observed at |
| `report_id` | TEXT | NOT NULL | `morning_reports.id` that advanced this row |

### Table: `morning_report_facts`

Evidence facts a report cited — one row per (report id, fact key) — with the
source and the record id the fact references. `project_id` is nullable
because some facts are fleet-scoped, and `at` is the source-side timestamp the
fact records, not the build time (that lives on the report). This is the
audit trail for the `brief` an author read: a fact the report cites must be
resolvable to a row here.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `report_id` | TEXT | PRIMARY KEY, FK → morning_reports (CASCADE) | Report the fact belongs to |
| `fact_key` | TEXT | PRIMARY KEY | Fact identity within the report |
| `source` | TEXT | NOT NULL | Source system the fact came from |
| `record_id` | TEXT | NOT NULL | Record in that source the fact references |
| `project_id` | TEXT | nullable | Project the fact is scoped to; NULL for fleet facts |
| `at` | REAL | NOT NULL | Source timestamp the fact records |

## Durable knowledge & records

An independent domain introduced by revision `a00000000055` (K01 of the
work-and-knowledge-records plan, 2026-10-01).  Records are content-addressed,
revisioned units of knowledge; task and project identities inside it are
deliberate **soft references** so that archive/delete of a task never depends
on knowledge, and knowledge rows outlive the tasks that produced them.  The
domain is gated off by `knowledge` config and is inert until its lane ships.
`record_scopes` is the identity boundary every other table in this section
keys against: a `global` scope with no project, or a `project:<uuid>` scope
bound to exactly one project.

### Table: `record_scopes`

The scope ledger.  Every durable-records row that is scoped carries a
`scope_key` that must resolve here (`ON DELETE RESTRICT`), and the identity
check constrains each scope to either the single `global` row or a
`project:`-prefixed row bound to one `project_id`.

Concurrent scope creation is idempotent across both the `scope_key` primary key
and `uq_record_scopes_project`. Backfills and lazy record creation share the
canonical scope without aborting either caller's transaction; the identity
check continues to reject any noncanonical scope.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `scope_key` | TEXT | PRIMARY KEY | Scope identity |
| `scope_kind` | TEXT | NOT NULL | One of: global, project (`ck_record_scopes_identity`) |
| `project_id` | TEXT | nullable, FK → projects (RESTRICT) | Bound project; unique per scope (`uq_record_scopes_project`) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Insert time |

### Table: `record_installation`

A singleton marker row recording that the durable-records schema is installed,
with an installation id used to distinguish installations. The `downgrade` path
refuses to drop the schema while any other table still holds data.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `singleton` | SMALLINT | PRIMARY KEY | Row marker; fixed to 1 (`ck_record_installation_singleton`) |
| `installation_id` | UUID | NOT NULL | Per-installation id (`uq_record_installation_id`) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Install time |

### Table: `records`

The identity ledger: one row per durable record.  A record is either a `task`
record (bound to a task id, no alias) or a `knowledge` record (no task, a
`kn-<32 hex>` alias).  `task_id` and `knowledge_alias` are NOT foreign keys —
archive of a task removes the task row but the record row survives.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `record_id` | UUID | PRIMARY KEY | Record identity |
| `kind` | TEXT | NOT NULL | One of: task, knowledge (`ck_records_domain`) |
| `scope_key` | TEXT | NOT NULL, FK → record_scopes (RESTRICT) | Owning scope |
| `task_id` | TEXT | nullable | Soft ref to the producing task (unique, `uq_records_task`) |
| `knowledge_alias` | TEXT | nullable | `kn-<32 hex>` alias (unique, `uq_records_alias`) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Create time |
| `created_by` | TEXT | NOT NULL | Actor that created the record |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Last touch time |

### Table: `knowledge_records`

The mutable head of a knowledge record: which revision is current, as a
deferrable head pointer so a revision and its head can be created in one
transaction.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `record_id` | UUID | PRIMARY KEY, FK → records (RESTRICT) | Owning record |
| `kind` | TEXT | NOT NULL, DEFAULT 'knowledge' | Always knowledge (`ck_knowledge_records_kind`) |
| `current_revision_id` | UUID | NOT NULL | Head revision (`fk_knowledge_records_head`) |
| `current_sequence` | BIGINT | NOT NULL | Head sequence (`ck_knowledge_records_sequence`) |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Head move time |

### Table: `knowledge_revisions`

The append-only history of a knowledge record.  Each row is one content
revision with a per-record `sequence` and an optional parent, forming a
content-linked tree.  `content_sha256` is the canonical-hash of the payload.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `revision_id` | UUID | PRIMARY KEY | Revision identity |
| `record_id` | UUID | NOT NULL, FK → knowledge_records (RESTRICT) | Owning record |
| `sequence` | BIGINT | NOT NULL | Monotonic per record (`ck_knowledge_revisions_sequence`) |
| `parent_revision_id` | UUID | nullable | Tree parent (unique with record, `uq_knowledge_revisions_sequence`) |
| `actor_id` | TEXT | NOT NULL | Actor that authored the revision |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Author time |
| `change_kind` | TEXT | NOT NULL | Classified edit kind |
| `content_sha256` | TEXT | NOT NULL | 64-hex canonical content hash (`ck_knowledge_revisions_hash`) |
| `hash_version` | SMALLINT | NOT NULL | Hash scheme version (fixed to 1, `ck_knowledge_revisions_hash_version`) |

### Table: `knowledge_revision_payloads`

The payload side of a revision: either the snapshot is present and the
revision is unredacted, or the snapshot has been redacted and a redaction id /
timestamp record when.  Named boundary checks enforce title / body / category /
lifecycle / verification / tags / sources / links / metadata shapes.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `revision_id` | UUID | PRIMARY KEY, FK → knowledge_revisions (RESTRICT) | Owning revision |
| `snapshot` | JSONB | nullable | Content snapshot (`knowledge_snapshot_valid_v1`) |
| `redacted_at` | TIMESTAMPTZ | nullable | Redaction time (mutually exclusive with snapshot) |
| `redaction_id` | UUID | nullable | Redaction reference (mutually exclusive with snapshot) |

### Table: `knowledge_search`

The search index, one row per record (the head revision at the time of last
reindex): title, summary, category, lifecycle, verification, and a GIN-indexed
`tsvector` used for full-text lookup.  `FK → record_scopes` keeps the row
within its scope.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `record_id` | UUID | PRIMARY KEY, FK → knowledge_records (RESTRICT) | Indexed record |
| `revision_id` | UUID | NOT NULL | Head revision the row reflects |
| `scope_key` | TEXT | NOT NULL, FK → record_scopes (RESTRICT) | Search scope |
| `title` | TEXT | NOT NULL | Record title |
| `summary` | TEXT | nullable | Short summary |
| `category` | TEXT | NOT NULL | fact / decision / policy / procedure / incident / reference / note |
| `lifecycle` | TEXT | NOT NULL | active / retired |
| `verification` | TEXT | NOT NULL | unverified / verified / disputed |
| `valid_until` | TIMESTAMPTZ | nullable | When the fact stops being valid |
| `recheck_at` | TIMESTAMPTZ | nullable | When the fact should be rechecked |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Last reindex time |
| `search_vector` | TSVECTOR | NOT NULL | GIN-indexed full-text vector (`ix_knowledge_search_vector`) |

### Table: `record_link_heads`

The mutable head of a directed link between records: which version is current.
Deferrable so a link and its first version land in one transaction.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link_id` | UUID | PRIMARY KEY | Link identity |
| `source_record_id` | UUID | NOT NULL, FK → records (RESTRICT) | Source record (unique with link, `uq_record_link_heads_source`) |
| `owner_scope_key` | TEXT | NOT NULL, FK → record_scopes (RESTRICT) | Scope that owns the link |
| `current_version` | BIGINT | NOT NULL | Head version (`ck_record_link_heads_version`) |

### Table: `record_link_versions`

The append-only version history of a link.  Each row is one (link, version)
pair with the target record, an optionally pinned target revision, a typed
link relationship, and the actor that made the change.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link_id` | UUID | PRIMARY KEY, FK → record_link_heads (RESTRICT) | Owning link |
| `version` | BIGINT | PRIMARY KEY | Monotonic per link (`ck_record_link_versions_version`) |
| `target_record_id` | UUID | NOT NULL, FK → records (RESTRICT) | Target record |
| `target_revision_id` | UUID | nullable | Pinned revision (if knowledge target) |
| `link_type` | TEXT | NOT NULL | references / motivated_by / produces / supports / contradicts / supersedes |
| `removed` | BOOLEAN | NOT NULL, DEFAULT false | Soft-delete marker |
| `actor_id` | TEXT | NOT NULL | Actor that made the change |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Change time |
| `metadata` | JSONB | NOT NULL | Free-form link metadata (`record_metadata_valid_v1`) |
| `source_revision_id` | UUID | nullable | Revision that introduced the link |

### Table: `task_record_link_state`

A per-record counter/token pair used to track link-related state for a record
produced from a task (sequence and token rotate together).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `record_id` | UUID | PRIMARY KEY, FK → records (RESTRICT) | Owning record |
| `link_sequence` | BIGINT | NOT NULL | Link counter (`ck_task_record_link_state_sequence`) |
| `link_token` | UUID | NOT NULL | Opaque token for the current link |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Last change time |

### Table: `record_source_artifacts`

Content-addressed source artifacts (blobs) referenced by a scope.  Uniqueness
is on (scope, content) — re-uploading identical bytes into the same scope is
idempotent — and the row stays after redaction so provenance survives.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `artifact_id` | UUID | PRIMARY KEY | Artifact identity |
| `scope_key` | TEXT | NOT NULL, FK → record_scopes (RESTRICT) | Owning scope |
| `content_sha256` | TEXT | NOT NULL | 64-hex content hash (`ck_record_source_artifacts_hash`) |
| `byte_size` | BIGINT | NOT NULL | Byte length (`ck_record_source_artifacts_size`) |
| `media_type` | TEXT | NOT NULL | MIME type of the artifact |
| `storage_key` | TEXT | NOT NULL | Where the bytes live |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Upload time |
| `redacted_at` | TIMESTAMPTZ | nullable | When the bytes were redacted |

### Table: `record_requests`

Idempotent request ledger: one row per (scope, actor, operation, idempotency
key) recording the canonical request hash and the object result. The check
enforces a key in 1..128 and a JSON-object result.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `scope_key` | TEXT | PRIMARY KEY, FK → record_scopes (RESTRICT) | Owning scope |
| `actor_key` | TEXT | PRIMARY KEY | Requesting actor |
| `operation` | TEXT | PRIMARY KEY | Operation name |
| `idempotency_key` | TEXT | PRIMARY KEY | 1..128 idempotency key (`ck_record_requests_key`) |
| `request_sha256` | TEXT | NOT NULL | 64-hex canonical request hash |
| `result` | JSONB | NOT NULL | JSON-object result (`ck_record_requests_result`) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Request time |

### Table: `record_outbox`

The transactional outbox of durable-record events.  `dedup_key` is unique so
re-sending is idempotent, and the (aggregate, revision) pair is optional so a
row may reference an aggregate without a specific revision.  A partial index
keeps the pending queue cheap to scan.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `event_id` | UUID | PRIMARY KEY | Event identity |
| `scope_key` | TEXT | NOT NULL, FK → record_scopes (RESTRICT) | Owning scope |
| `aggregate_record_id` | UUID | nullable | Aggregate the event describes |
| `revision_id` | UUID | nullable | Revision the event describes |
| `event_type` | TEXT | NOT NULL | Event class |
| `destination` | TEXT | NOT NULL | Consumer / channel name |
| `dedup_key` | TEXT | NOT NULL | Idempotency key (unique, `uq_record_outbox_dedup`) |
| `payload` | JSONB | NOT NULL | Event body (`ck_record_outbox_payload`) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Enqueue time |
| `available_at` | TIMESTAMPTZ | NOT NULL | Earliest dispatch time |
| `attempts` | INTEGER | NOT NULL, DEFAULT 0 | Delivery attempts (`ck_record_outbox_attempts`) |
| `lease_token` | UUID | nullable | Consumer lease |
| `lease_until` | TIMESTAMPTZ | nullable | Lease expiry (`ck_record_outbox_lease`) |
| `delivered_at` | TIMESTAMPTZ | nullable | Delivery completion time |
| `last_error_code` | TEXT | nullable | Last failure reason |

### Table: `record_consumer_receipts`

Per-(consumer, event) receipt proving a specific consumer processed a specific
outbox event, with a 64-hex digest of the result.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `consumer` | TEXT | PRIMARY KEY | Consumer name |
| `event_id` | UUID | PRIMARY KEY, FK → record_outbox (RESTRICT) | Event that was received |
| `processed_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Receipt time |
| `result_digest` | TEXT | NOT NULL | 64-hex digest of the result |

### Table: `record_export_state`

Per-(record, destination) export state: which revision was exported, at what
time, and when the export stopped tracking the head (divergence).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `record_id` | UUID | PRIMARY KEY | Exported record |
| `destination` | TEXT | PRIMARY KEY | Export target |
| `revision_id` | UUID | NOT NULL | Revision that was exported |
| `sequence` | BIGINT | NOT NULL | Revision sequence (`ck_record_export_state_sequence`) |
| `export_sha256` | TEXT | NOT NULL | 64-hex export payload hash |
| `exported_at` | TIMESTAMPTZ | NOT NULL | Export time |
| `diverged_at` | TIMESTAMPTZ | nullable | When the record moved ahead of this export |

### Table: `record_backfill_state`

A per-source cursor for the records backfill lane (tasks / archived_tasks),
tracking how many rows have been scanned and how many records inserted, with
the invariant `scanned >= inserted >= 0`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `source` | TEXT | PRIMARY KEY | tasks / archived_tasks |
| `cursor` | TEXT | nullable | Resume position |
| `scanned` | BIGINT | NOT NULL, DEFAULT 0 | Rows scanned |
| `inserted` | BIGINT | NOT NULL, DEFAULT 0 | Records inserted |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Cursor update time |

### Table: `record_compatibility_usage`

Migration `a00000000067` retains aggregate attempts through managed core
compatibility paths, keyed by `(scope_key, operation, outcome)`
(`pk_record_compatibility_usage`). Atomic upserts increment `calls` before dispatch,
so a failed dispatch still counts as an attempt. The table stores no source keys,
paths, content, actor IDs or record IDs. `scope_key` is a soft label without a
foreign key, so deleting a project preserves its audit aggregate. Downgrade refuses
to drop a nonempty table and directs operators to read-only rollback.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `scope_key` | TEXT | PRIMARY KEY | global or nonempty project:<id> (`ck_record_compatibility_usage_scope`) |
| `operation` | TEXT | PRIMARY KEY | read, list, write, append, delete or promote (`ck_record_compatibility_usage_operation`) |
| `outcome` | TEXT | PRIMARY KEY | canonical_read, refused_write or guarded_write (`ck_record_compatibility_usage_outcome`) |
| `calls` | BIGINT | NOT NULL, > 0 | Attempt counter (`ck_record_compatibility_usage_calls`) |
| `first_seen_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | First attempt time |
| `last_seen_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Latest attempt time; upserts keep it monotonic |

### Table: `record_index_state`

Per-(provider, record) checkpoint of one optional provider's derived semantic
index: which exact revision was indexed, the core-computed digest of its
text-free chunk manifest, and when the provider acknowledged the erasure of a
redacted revision. Rebuildable derived state — it never carries record content
and never substitutes for the revision it describes.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `provider_id` | TEXT | PRIMARY KEY | Bounded provider id (`ck_record_index_state_provider`) |
| `record_id` | UUID | PRIMARY KEY | Indexed record |
| `revision_id` | UUID | NOT NULL, FK → knowledge_revisions (RESTRICT) | Exact indexed revision |
| `sequence` | BIGINT | NOT NULL | Revision sequence (`ck_record_index_state_sequence`) |
| `chunk_manifest_sha256` | TEXT | NOT NULL | 64-hex digest of the validated chunk manifest |
| `indexed_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Acknowledgment time |
| `redacted_at` | TIMESTAMPTZ | nullable | Erasure acknowledgment; set once, never cleared |

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

Atomic multi-table update inside one transaction:

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

Optional `model_source`, `session_id`, `attempt_id` and `call_id` capture evidence
at ingest. The legacy SQLite importer fills absent revision-47 attribution and
archived-route columns with SQL NULL, retaining historical rows without guessing
their benchmark provenance. Other missing source columns remain an import error.

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
| `ALTER TABLE projects ADD COLUMN default_profile_id TEXT REFERENCES agent_profiles(id)` | Adds default profile to projects (dropped by `a00000000043`, mandatory task routing §8) |
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

## Knowledge protection (K05)

Proposals and authority are independent of document-review state. Redaction targets are permanent evidence tombstones. All five tables are introduced by migration `a00000000059`; feature flags remain disabled.

### Table: `knowledge_proposals`

| Column | Type | Constraints |
|---|---|---|
| `proposal_id` | UUID | PRIMARY KEY |
| `record_id` | UUID | nullable |
| `scope_key` | TEXT | NOT NULL |
| `base_revision_id` | UUID | nullable |
| `proposed_snapshot` | JSONB | nullable |
| `source_descriptors` | JSONB | NOT NULL |
| `content_sha256` | TEXT | NOT NULL |
| `actor_id` | TEXT | NOT NULL |
| `state` | TEXT | NOT NULL |
| `created_at` | DATETIME | NOT NULL |
| `decided_at` | DATETIME | nullable |
| `decided_by` | TEXT | nullable |
| `resulting_record_id` | UUID | nullable |
| `resulting_revision_id` | UUID | nullable |
| `redacted_at` | DATETIME | nullable |

### Table: `knowledge_authority_grants`

| Column | Type | Constraints |
|---|---|---|
| `grant_id` | UUID | PRIMARY KEY |
| `record_id` | UUID | NOT NULL |
| `revision_id` | UUID | NOT NULL |
| `scope_key` | TEXT | NOT NULL |
| `authority_kind` | TEXT | NOT NULL |
| `review_id` | TEXT | nullable |
| `review_revision` | INTEGER | nullable |
| `review_sha256` | TEXT | nullable |
| `actor_id` | TEXT | NOT NULL |
| `reason` | TEXT | NOT NULL |
| `created_at` | DATETIME | NOT NULL |
| `revoked_at` | DATETIME | nullable |

### Table: `knowledge_global_shares`

| Column | Type | Constraints |
|---|---|---|
| `grant_id` | UUID | PRIMARY KEY |
| `record_id` | UUID | NOT NULL |
| `project_id` | TEXT | NOT NULL |
| `actor_id` | TEXT | NOT NULL |
| `created_at` | DATETIME | NOT NULL |
| `revoked_at` | DATETIME | nullable |

### Table: `knowledge_redactions`

| Column | Type | Constraints |
|---|---|---|
| `redaction_id` | UUID | PRIMARY KEY |
| `record_id` | UUID | NOT NULL |
| `revision_id` | UUID | nullable |
| `actor_id` | TEXT | NOT NULL |
| `reason_code` | TEXT | NOT NULL |
| `requested_at` | DATETIME | NOT NULL |
| `completed_at` | DATETIME | nullable |
| `cleanup_state` | JSONB | NOT NULL |

### Table: `knowledge_redaction_targets`

| Column | Type | Constraints |
|---|---|---|
| `revision_id` | UUID | PRIMARY KEY |
| `redaction_id` | UUID | NOT NULL |
| `content_sha256` | TEXT | NOT NULL |

## Legacy knowledge import inventory (K06)

Migration `a00000000060` adds the sealed import run, permanent source identity mapping,
and per-item receipt tables. K06's read-only dry run returns the corresponding report
without writing these tables; the apply path owns their persisted rows. Empty tables can
be downgraded in dependency order. A populated inventory refuses destructive rollback.

### Table: `record_import_runs`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `run_id` | UUID | PK | Import run identity |
| `source_installation_id` | TEXT | NOT NULL | Legacy installation identity |
| `snapshot_id` | TEXT | NOT NULL | Source snapshot identity |
| `manifest_sha256` | TEXT | NOT NULL, 64 lowercase hexadecimal digits | Canonical manifest seal |
| `scope_key` | TEXT | NOT NULL, FK record_scopes.scope_key, RESTRICT | Destination scope |
| `state` | TEXT | NOT NULL | prepared, applying, succeeded, failed, or cancelled |
| `started_at` | DATETIME | NOT NULL, DEFAULT now() | Time with timezone |
| `finished_at` | DATETIME | nullable | Terminal time with timezone |
| `cursor` | JSONB | nullable, object | Apply continuation |
| `report` | JSONB | nullable, object | Reconciliation evidence |

### Table: `record_legacy_mappings`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `source_installation_id` | TEXT | PK (with source_kind, source_scope, source_key) | Legacy installation |
| `source_kind` | TEXT | PK | Legacy source kind |
| `source_scope` | TEXT | PK | Legacy scope |
| `source_key` | TEXT | PK | Permanent legacy identity |
| `record_id` | UUID | nullable | Resulting record identity |
| `latest_source_sha256` | TEXT | nullable, 64 lowercase hexadecimal digits | Last observed source hash |
| `latest_revision_id` | UUID | nullable | Last resulting revision |
| `ownership` | TEXT | NOT NULL | legacy, managed, excluded, or quarantined |
| `decision_reason` | TEXT | nullable | Ownership decision evidence |
| `updated_at` | DATETIME | NOT NULL, DEFAULT now() | Time with timezone |

### Table: `record_import_items`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `run_id` | UUID | PK (with item_key), FK record_import_runs.run_id, RESTRICT | Owning run |
| `item_key` | TEXT | PK | Manifest item identity |
| `source_sha256` | TEXT | NOT NULL, 64 lowercase hexadecimal digits | Source content hash |
| `disposition` | TEXT | NOT NULL | candidate, quarantined, excluded, or unavailable |
| `record_id` | UUID | nullable | Resulting record identity |
| `revision_id` | UUID | nullable, requires record_id | Resulting revision identity |
| `error_code` | TEXT | nullable | Item failure code |

## Prepared knowledge context, delivery and citations (K08)

Migration `a00000000063` adds the prepared evidence bundle, its observed transport
delivery and the exact-revision citation ledger. Preparation, delivery and usage
evidence are three separate facts: a bundle is what the service selected and sealed,
a delivery is what a transport acknowledged, and a citation is that a specific
revision was actually used by a specific attempt or supervisor session. The feature
is gated on `knowledge.context.enabled` (default false, and also `knowledge.enabled`
and `memory.enabled`), so these tables stay empty until an operator opts in. Redaction
erases cached selection bodies in the same transaction while preserving the
identity-only citations and receipts.

### Table: `knowledge_context_bundles`

One prepared, ordered evidence selection for worker prime, named-supervisor
bootstrap and retained prompt assembly. The owner is either a task attempt (with its
claim epoch) or a supervisor session (no claim epoch). `request_fingerprint` folds in
the principal fingerprint, the access epoch, the query, pins, the applied budget and
the omissions, so a repeated preparation reuses its latest unexpired, non-redacted
bundle instead of selecting again (`refresh=True` selects afresh). `content_sha256`
seals the rendered selection; `expires_at` is bounded by
`knowledge.context.bundle_ttl_seconds`.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `bundle_id` | UUID | PRIMARY KEY | Bundle identity |
| `owner_kind` | TEXT | NOT NULL | task_attempt or supervisor_session (`ck_knowledge_context_bundles_owner_kind`) |
| `owner_id` | TEXT | NOT NULL | Attempt id or supervisor session id |
| `session_instance` | TEXT | NOT NULL | Live session instance that requested the bundle |
| `claim_epoch` | BIGINT | nullable | Required for a task attempt, null for a supervisor session (`ck_knowledge_context_bundles_claim`) |
| `principal_fingerprint` | TEXT | NOT NULL | Fingerprint of the authorizing principal |
| `request_fingerprint` | TEXT | NOT NULL | Fingerprint of the request; reuse key for a repeat preparation |
| `scope_keys` | JSONB | NOT NULL, array | Resolved scopes behind the selection (`ck_knowledge_context_bundles_json`) |
| `budget` | JSONB | NOT NULL, object | Applied context budget for this preparation |
| `selection` | JSONB | NOT NULL, object | Rendered ordered evidence selection |
| `content_sha256` | TEXT | NOT NULL, 64 lowercase hexadecimal digits | Seal of the rendered selection (`ck_knowledge_context_bundles_hash`) |
| `prepared_at` | TIMESTAMPTZ | NOT NULL | Preparation time |
| `expires_at` | TIMESTAMPTZ | NOT NULL, >= prepared_at | TTL end (`ck_knowledge_context_bundles_expiry`) |
| `redacted_at` | TIMESTAMPTZ | nullable | When the cached selection body was erased |

Indexed by `idx_knowledge_context_bundles_request` on
`(request_fingerprint, expires_at)` for the reuse and expiry scans.

### Table: `knowledge_context_deliveries`

The observed transport receipt for a bundle, one row per idempotency key: the insert
is `ON CONFLICT DO NOTHING` on `transport_key`, and a replay must match the recorded
bundle, transport and hash or it is refused. Injected citations are written only for
`delivered`; `prepared`, `failed` and `unknown` record the attempt without attributing
usage, and `rendered_sha256` is verified against the bundle's seal.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `delivery_id` | UUID | PRIMARY KEY | Delivery identity |
| `bundle_id` | UUID | NOT NULL, FK knowledge_context_bundles.bundle_id, RESTRICT | Delivered bundle |
| `transport` | TEXT | NOT NULL | Transport that received the bundle |
| `transport_key` | TEXT | NOT NULL, UNIQUE | Caller idempotency key for the delivery |
| `state` | TEXT | NOT NULL | prepared, delivered, failed or unknown (`ck_knowledge_context_deliveries_state`) |
| `observed_at` | TIMESTAMPTZ | NOT NULL | When the outcome was observed |
| `rendered_sha256` | TEXT | NOT NULL, 64 lowercase hexadecimal digits | Hash of the bytes actually rendered (`ck_knowledge_context_deliveries_hash`) |

### Table: `knowledge_citations`

Identity-only usage evidence: one row per exact `(record_id, revision_id)` use, with
no content. The execution owner is a task attempt plus its claim epoch, or a
supervisor session, exclusively (`ck_knowledge_citations_execution_owner`), so a
citation can always be attributed to one attempt. `bundle_id` is the optional soft
link to the bundle that carried the selection, and redaction leaves the row intact.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `citation_id` | UUID | PRIMARY KEY | Citation identity |
| `record_id` | UUID | NOT NULL | Cited record |
| `revision_id` | UUID | NOT NULL | Exact cited revision |
| `owner_kind` | TEXT | NOT NULL | task_attempt or supervisor_session |
| `owner_id` | TEXT | NOT NULL | Attempt id or supervisor session id |
| `session_instance` | TEXT | NOT NULL | Live session instance that used the revision |
| `task_id` | TEXT | nullable | Soft ref to the task, for an attempt owner |
| `attempt_id` | TEXT | nullable | Soft ref to the attempt, for an attempt owner |
| `supervisor_session_id` | TEXT | nullable | Soft ref to the session, for a supervisor owner |
| `claim_epoch` | BIGINT | nullable | Claim epoch of the citing attempt |
| `kind` | TEXT | NOT NULL | injected, attached or explicit_read (`ck_knowledge_citations_kind`) |
| `bundle_id` | UUID | nullable, FK knowledge_context_bundles.bundle_id, RESTRICT | Bundle that carried the selection |
| `actor_id` | TEXT | NOT NULL | Actor recorded for the use |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Citation time |
| `idempotency_key` | TEXT | NOT NULL, 1-128 characters | Per-owner key (`ck_knowledge_citations_key`) |

`(record_id, revision_id)` references
`knowledge_revisions (record_id, revision_id)` with RESTRICT
(`fk_knowledge_citations_revision`), and
`(owner_kind, owner_id, session_instance, kind, idempotency_key)` is unique
(`uq_knowledge_citations_owner_key`) so a replayed citation is one row.

## Durable extraction jobs, receipts and budgets (K13)

Migration `a00000000064` adds the durable generation work ledger, its exact input
receipts, the capture cursor, the per-feature daily allowance and the pre-charge
reservation; `a00000000065` adds `knowledge_feature_budgets.consecutive_failures` for
the persistent failure circuit. Generation is separately gated on
`knowledge.extraction.enabled` and `knowledge.consolidation.enabled` (both default
false, and also `knowledge.enabled`, `knowledge.writes_enabled`, `memory.enabled` and
`knowledge.legacy_memory_mode: disabled`), so the store is inert until an operator
enables a feature and names its provider. Money is reserved before any external
request and settled from the provider's reported usage; an expired paid operation
with no saved output is `unknown`, is quarantined, keeps its reservation and is never
retried automatically. Populated tables refuse destructive rollback.

### Table: `knowledge_extraction_jobs`

The durable work ledger, one row per exact source. Dedupe is
`(scope_key, source_identity, source_sha256, extractor_version, policy_version)`, so a
retention-version change re-runs the source rather than reusing a stale job. The
lease is a 30-second claim token whose expiry fits the request timeout; a crash
releases it by expiry, never by guessing the caller's outcome.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `job_id` | UUID | PRIMARY KEY | Job identity (`pk_knowledge_extraction_jobs`) |
| `scope_key` | TEXT | NOT NULL, FK record_scopes.scope_key, RESTRICT | Owning scope |
| `source_identity` | TEXT | NOT NULL, non-blank | Stable identity of the captured source (`ck_knowledge_extraction_jobs_identity`) |
| `source_sha256` | TEXT | NOT NULL, 64 lowercase hexadecimal digits | Source content hash (`ck_knowledge_extraction_jobs_hash`) |
| `extractor_version` | TEXT | NOT NULL, non-blank | Capture version, e.g. `extraction:1` |
| `policy_version` | TEXT | NOT NULL, non-blank | Generation policy version |
| `state` | TEXT | NOT NULL, DEFAULT 'pending' | pending, leased, succeeded, retry, quarantined or cancelled (`ck_knowledge_extraction_jobs_state`) |
| `attempts` | INTEGER | NOT NULL, DEFAULT 0 | Run count (`ck_knowledge_extraction_jobs_attempts`) |
| `lease_token` | UUID | nullable | Claim token, present only while leased (`ck_knowledge_extraction_jobs_lease`) |
| `lease_until` | TIMESTAMPTZ | nullable | Lease expiry |
| `available_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Earliest due time for the scan |
| `result_artifact_id` | TEXT | nullable | Retained proposal artifact on success |
| `error_code` | TEXT | nullable | Classified failure reason |
| `budget_reservation_id` | UUID | nullable, with `job_id` FK knowledge_budget_reservations, RESTRICT | This job's own reservation (`fk_knowledge_extraction_jobs_reservation`) |

`uq_knowledge_extraction_jobs_source` covers the dedupe tuple and
`uq_knowledge_extraction_jobs_scope` the `(job_id, scope_key)` reference target.
`idx_knowledge_extraction_jobs_due` covers
`(scope_key, available_at, job_id)` where the state is pending, retry or leased.

### Table: `knowledge_extraction_inputs`

The exact retained input receipts a job was built from, at most eight per job
(`input_ordinal` 0-7), each carrying its own artifact, source scope and actor so a
batch cannot inherit the first item's role. `event_id` and `attempt_id` are soft
references: ordinary event retention cannot erase the evidence of what was sent.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `job_id` | UUID | PK with input_ordinal, FK knowledge_extraction_jobs (job_id, scope_key), RESTRICT | Owning job |
| `input_ordinal` | INTEGER | NOT NULL, 0-7 | Position within the batch (`ck_knowledge_extraction_inputs_position`) |
| `event_id` | BIGINT | NOT NULL, >= 0 | Soft ref to the captured event |
| `attempt_id` | TEXT | nullable | Soft ref to the capturing attempt |
| `artifact_id` | TEXT | NOT NULL, non-blank | Retained artifact read for this input |
| `source_scope` | TEXT | NOT NULL | Scope the artifact was read from |
| `actor_id` | TEXT | NOT NULL, non-blank | Actor that produced the input (`ck_knowledge_extraction_inputs_receipt`) |

### Table: `knowledge_capture_checkpoints`

The monotonic per-consumer event cursor. It advances only in the transaction that
retains the exact input receipts, so ordinary event retention or a lost delivery can
be repaired by a rescan without resetting a cursor or skipping evidence.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `consumer_id` | TEXT | PK with scope_key, non-blank | Capturing consumer (`ck_knowledge_capture_checkpoints_consumer`) |
| `scope_key` | TEXT | PK, FK record_scopes.scope_key, RESTRICT | Captured scope |
| `last_event_id` | BIGINT | NOT NULL, >= 0 | Highest retained event (`ck_knowledge_capture_checkpoints_cursor`) |
| `last_reconcile_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Last reconciliation time |

### Table: `knowledge_feature_budgets`

One daily allowance row per scope, feature and UTC day
(`ck_knowledge_feature_budgets_utc_day`). Extraction and consolidation counters are
separate rows, so one feature's spend never starves the other. `circuit_open` is the
persistent failure circuit: five consecutive provider failures open it, and neither a
restart nor a UTC-day change clears it.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `scope_key` | TEXT | PK, FK record_scopes.scope_key, RESTRICT | Owning scope |
| `feature` | TEXT | PK, non-blank | extraction or consolidation (`ck_knowledge_feature_budgets_feature`) |
| `period_start` | TIMESTAMPTZ | PK, truncated to a UTC day | Allowance period |
| `limit_microusd` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Daily microUSD allowance |
| `reserved_microusd` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Reserved but unsettled |
| `spent_microusd` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Settled spend |
| `token_limit` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Daily token allowance |
| `reserved_tokens` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Reserved but unsettled tokens |
| `spent_tokens` | BIGINT | NOT NULL, DEFAULT 0, >= 0 | Settled tokens |
| `circuit_open` | BOOLEAN | NOT NULL, DEFAULT false | Persistent provider failure circuit |
| `consecutive_failures` | INTEGER | NOT NULL, DEFAULT 0, >= 0 | Provider failures since the last success (`ck_knowledge_feature_budgets_failures`) |

The six counters are bounded by `ck_knowledge_feature_budgets_counters`.

### Table: `knowledge_budget_reservations`

The pre-charge reservation for one job — at most one per job
(`uq_knowledge_budget_reservations_job`). Admission reserves money and tokens and
commits the stable provider operation id before the external request, so a crash
afterwards is `unknown` rather than a free retry. A settled reservation carries both
actuals and the operation id; every other state carries no actuals
(`ck_knowledge_budget_reservations_actuals`).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `reservation_id` | UUID | PRIMARY KEY | Reservation identity (`pk_knowledge_budget_reservations`) |
| `job_id` | UUID | NOT NULL, UNIQUE, FK knowledge_extraction_jobs (job_id, scope_key), RESTRICT | Admitting job |
| `scope_key` | TEXT | NOT NULL, FK knowledge_feature_budgets, RESTRICT | Charged scope |
| `feature` | TEXT | NOT NULL, FK knowledge_feature_budgets, RESTRICT | Charged feature |
| `period_start` | TIMESTAMPTZ | NOT NULL, FK knowledge_feature_budgets, RESTRICT | Charged UTC day |
| `estimated_microusd` | BIGINT | NOT NULL, >= 0 | Pre-charge estimate |
| `estimated_tokens` | BIGINT | NOT NULL, 0-8000 | Pre-charge estimate, capped by one call (`ck_knowledge_budget_reservations_amounts`) |
| `actual_microusd` | BIGINT | nullable, >= 0 | Provider-reported usage |
| `actual_tokens` | BIGINT | nullable, >= 0 | Provider-reported usage |
| `state` | TEXT | NOT NULL, DEFAULT 'reserved' | reserved, settled, unknown or released (`ck_knowledge_budget_reservations_state`) |
| `provider_operation_id` | TEXT | nullable | Stable operation id, required once unknown or settled |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Reservation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Last settlement time |
