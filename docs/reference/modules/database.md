# Module catalog — database

Schema, query modules, migrations and data lifecycle: the 61 production modules
that own every durable row in an AQ install, plus the 20 supporting files that
define and migrate the schema.

Prose for everything here lives on five pages:

* [The database](../database/README.md) — concepts, layering, entity diagrams, write ownership.
* [Table families](../database/tables.md) — all 98 tables, grouped.
* [Query modules](../database/queries.md) — the query layer in prose.
* [Migrations](../database/migrations.md) — Alembic, scope policy, backup and restore.
* [Data lifecycle](../database/data-lifecycle.md) — close, archive, delete, retention.

## Schema, engine and guards

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/__init__.py`](../../../src/database/__init__.py) | Builds the one backend from config, refuses a non-PostgreSQL DSN outright, and redacts passwords out of any DSN that reaches a log. | [reference/database/README.md](../database/README.md) | `Database` is an alias for the PostgreSQL adapter, kept so existing imports work. `tests/test_database_backend_selection.py` |
| [`src/database/tables.py`](../../../src/database/tables.py) | The schema's source of truth: 98 SQLAlchemy Core `Table` objects with their columns, named check constraints and indexes. | [reference/database/tables.md](../database/tables.md) | Migrations follow this file, never the reverse. Every `CheckConstraint` must be named or autogenerate emits a spurious drop. `tests/test_database.py` |
| [`src/database/base.py`](../../../src/database/base.py) | The `DatabaseBackend` protocol — the full read/write contract a backend must satisfy, organised by domain. | [reference/database/queries.md](../database/queries.md) | What type checkers see; adding a query method means adding it here too. |
| [`src/database/engine.py`](../../../src/database/engine.py) | Async engine creation and the schema lifecycle: the already-at-head fast path, the Alembic preflight, the legacy-stamp path, and the idempotent startup row normalisations. | [reference/database/migrations.md](../database/migrations.md) | Uses `engine.connect()`, not `begin()`, so Alembic owns per-revision transaction boundaries. `tests/test_database_engine.py` |
| [`src/database/migration_guard.py`](../../../src/database/migration_guard.py) | Who may migrate which database: `current_scope()` (who is asking) × `is_production_database()` (what they point at) → migrate or verify. | [reference/database/migrations.md](../database/migrations.md) | Exists because of the 2026-09-02 incident; `AQ_DB_SCOPE` in the environment deliberately outranks a process-declared scope. `tests/test_migration_guard.py`, `tests/test_migration_guard_restoration.py` |
| [`src/database/schema_key.py`](../../../src/database/schema_key.py) | Backend-agnostic identity for "what a fully migrated schema looks like" — a content hash over `tables.py`, `env.py`, the guards and every revision. | [reference/database/migrations.md](../database/migrations.md) | Keys the PostgreSQL test template database, which is why a fresh test database costs milliseconds. Lives here so the test substrate need not import an engine module. |
| [`src/database/hierarchy_migration.py`](../../../src/database/hierarchy_migration.py) | The hierarchy canonicalisation: snapshot pointers and edges, choose one canonical parent, validate the graph, reject offenders, then apply. | [reference/database/migrations.md](../database/migrations.md) | Sync SQLAlchemy Core on purpose — Alembic hands it a sync connection, and `aq system db-preflight-hierarchy` runs the same code. `tests/test_hierarchy_migration.py` |
| [`src/database/legacy_sqlite_import.py`](../../../src/database/legacy_sqlite_import.py) | One-way carry-over of a pre-PostgreSQL SQLite database: FK-safe table order, deferred columns restored, sequences reset. | [reference/database/migrations.md](../database/migrations.md) | Refuses a non-empty target. Behind `aq db import-sqlite` and the optional `sqlite-import` extra. `tests/test_migrate_sqlite_to_pg.py` |
| [`src/models.py`](../../../src/models.py) | The shared vocabulary: every enum and dataclass the orchestrator, commands, database and dashboard communicate through — `Task`, `Agent`, `Project`, `Workspace`, `SessionRecord`, `TaskCompletion`, `TaskStatus`, `DepType`, `ClaimResult`. | [reference/database/README.md](../database/README.md) | Query methods return these, not raw rows. Kept in one file to prevent circular imports. Run `aq schema` rather than guessing an enum value. |

## Adapter

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/adapters/__init__.py`](../../../src/database/adapters/__init__.py) | Exports the one adapter. PostgreSQL is the only backend. | [reference/database/README.md](../database/README.md) | `tests/test_sqlite_removal.py` is the ratchet that keeps it that way. |
| [`src/database/adapters/postgresql.py`](../../../src/database/adapters/postgresql.py) | Composes the 47 query mixins into the single object every caller uses; owns engine lifecycle, schema setup and the test-only truncate. | [reference/database/queries.md](../database/queries.md) | `reset_for_tests` refuses unless the DSN is `POSTGRES_TEST_DSN` or `AQ_ALLOW_DB_RESET=1`. `tests/test_database_postgresql.py` |

## Query modules — tasks and the work graph

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/queries/__init__.py`](../../../src/database/queries/__init__.py) | Package marker for the query mixins. | [reference/database/queries.md](../database/queries.md) | Its docstring still describes the removed SQLite adapter; the mixins are backend-neutral SQLAlchemy Core. |
| [`src/database/queries/task_queries.py`](../../../src/database/queries/task_queries.py) | The `tasks` table and its satellites: create with the routing gate in one transaction, filter, update, pause/resume, the state-machine-checked transition, delete, metadata, labels, dedup lookup. | [reference/database/tables.md](../database/tables.md) | 41 public methods, the largest module in the layer. `tests/test_database.py`, `tests/test_task_controls.py` |
| [`src/database/queries/hierarchy_queries.py`](../../../src/database/queries/hierarchy_queries.py) | The single writer of parent/child membership, plus subtree reads, depth and height, container settlement, `abandon_subtree` and the integration guards. | [reference/database/queries.md](../database/queries.md) | `tasks.parent_task_id` is a cache; the `parent-child` edge is truth. Never opens its own transaction. `tests/test_hierarchy_queries.py`, `tests/test_hierarchy_settlement.py` |
| [`src/database/queries/dependency_queries.py`](../../../src/database/queries/dependency_queries.py) | Typed edges on `task_dependencies`; only four of the eight kinds gate readiness. | [reference/database/tables.md](../database/tables.md) | Every mutation recomputes `is_blocked` in its own transaction. `tests/test_dependency_queries.py` |
| [`src/database/queries/blocked_state.py`](../../../src/database/queries/blocked_state.py) | The `tasks.is_blocked` projection: graph blockedness only, recomputed inside the caller's transaction. | [reference/database/queries.md](../database/queries.md) | Capacity, budget and cooldown are deliberately *not* persisted — they belong to explain, not the row. `tests/test_blocked_state.py` |
| [`src/database/queries/gate_queries.py`](../../../src/database/queries/gate_queries.py) | `gates` and `task_gates` — open, resolve, expire, and the projection recompute for waiters. | [reference/database/tables.md](../database/tables.md) | A partial unique index on open gates is what makes gate creation idempotent. `tests/test_gate_queries.py` |
| [`src/database/queries/task_comment_queries.py`](../../../src/database/queries/task_comment_queries.py) | Append-only agent comments, operator edits, and fenced description updates. | [reference/database/tables.md](../database/tables.md) | `kind='progress'` is the only comment the digest may treat as progress. `tests/test_task_comments.py` |
| [`src/database/queries/result_queries.py`](../../../src/database/queries/result_queries.py) | `task_results` (per-attempt output) and `task_completion_records` (the append-only close receipt). | [reference/database/data-lifecycle.md](../database/data-lifecycle.md) | `save_task_completion` only ever inserts. `tests/test_task_close_summary_enforcement.py` |
| [`src/database/queries/archive_queries.py`](../../../src/database/queries/archive_queries.py) | Moving a terminal subtree into `archived_tasks`, the retention sweep, and permanent removal of an archived task. | [reference/database/data-lifecycle.md](../database/data-lifecycle.md) | Reuses the delete path's FK cleanup with comments and completion records preserved. `tests/test_archive.py`, `tests/test_hierarchy_archive_delete.py` |
| [`src/database/queries/activity_queries.py`](../../../src/database/queries/activity_queries.py) | One read spanning `tasks`, `archived_tasks`, completion records and attempts: what moved in a window, and which model did it. | [reference/database/queries.md](../database/queries.md) | An attempt with no recorded model is counted unattributed, never inferred from the profile. `tests/test_task_activity.py` |
| [`src/database/queries/proposal_queries.py`](../../../src/database/queries/proposal_queries.py) | Staged task/edge proposals awaiting approval, with cross-graph cycle detection. | [reference/database/tables.md](../database/tables.md) | Module-level functions rather than a mixin, so tests can call them without inheritance. `tests/test_proposal_api.py` |
| [`src/database/queries/triage_queries.py`](../../../src/database/queries/triage_queries.py) | The atomic, history-preserving lifecycle of a project's routing task. Module-level functions rather than a mixin. | [reference/database/tables.md](../database/tables.md) | Open routing gates are the durable inbox; the saved gate-id set stops a completed task restarting every tick. `tests/test_triage_api_scope.py` |
| [`src/database/queries/task_recovery_queries.py`](../../../src/database/queries/task_recovery_queries.py) | Durable supervisor incidents and bounded recovery state. | [reference/database/tables.md](../database/tables.md) | Records only — no terminal I/O in this module. `tests/test_supervisor_recovery.py` |
| [`src/database/queries/assignment_route_queries.py`](../../../src/database/queries/assignment_route_queries.py) | The current successful assignment-playbook decision for a task. | [reference/database/tables.md](../database/tables.md) | Caller-transaction-owned. `tests/test_assignment_routing.py` |
| [`src/database/queries/layout_queries.py`](../../../src/database/queries/layout_queries.py) | Graph layout storage: atomic publish of a whole layout, tile and extent reads, dirty-mark consumption, rebuild jobs. | [reference/database/tables.md](../database/tables.md) | 33 public methods; the persisted layout is the fully expanded geometry. `tests/test_api_graph_layout.py` |
| [`src/database/queries/event_queries.py`](../../../src/database/queries/event_queries.py) | The audit log: append and replay typed events. | [reference/database/tables.md](../database/tables.md) | `tests/test_events_replay.py` |

## Query modules — execution

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/queries/claim_queries.py`](../../../src/database/queries/claim_queries.py) | The claim transaction and the epoch fence: session slot first, then task, then agent, workspace and metadata — all conditional `UPDATE`s. | [reference/database/queries.md](../database/queries.md) | `activate_claim` and `release_claim` race by design; exactly one wins. `tests/test_claim_commands.py` |
| [`src/database/queries/session_queries.py`](../../../src/database/queries/session_queries.py) | The `sessions` row: create, update, pool listing, quarantine, restart counters. | [reference/database/tables.md](../database/tables.md) | Restart counters are persisted so the stall ladder survives a restart; `sessions.name` is deliberately non-unique. `tests/test_session_queries.py` |
| [`src/database/queries/task_session_queries.py`](../../../src/database/queries/task_session_queries.py) | `task_session_attempts` — the durable task/session association, written inside lifecycle transactions. | [reference/database/tables.md](../database/tables.md) | The only source of model attribution. `tests/test_task_session_attempts.py` |
| [`src/database/queries/agent_queries.py`](../../../src/database/queries/agent_queries.py) | Agent identity, state, heartbeat and lifetime spend. | [reference/database/tables.md](../database/tables.md) | `tests/test_agent_task_routing.py` |
| [`src/database/queries/profile_queries.py`](../../../src/database/queries/profile_queries.py) | The `agent_profiles` projection of the vault's markdown profiles. | [reference/database/tables.md](../database/tables.md) | The markdown file is the source of truth; the row is rewritten from it. `tests/test_profile_sync.py` |
| [`src/database/queries/subagent_queries.py`](../../../src/database/queries/subagent_queries.py) | Append-only subagent start/stop events, and the folds the flock reads. | [reference/database/tables.md](../database/tables.md) | A deterministic id collapses a re-delivered hook onto its own row. `tests/test_subagent_events.py` |
| [`src/database/queries/transcript_queries.py`](../../../src/database/queries/transcript_queries.py) | The transcript watcher's read position, keyed by file path rather than session id. | [reference/database/tables.md](../database/tables.md) | Keying by session id re-charged the same 133 ledger rows three times. `tests/test_transcript_checkpoints.py` |
| [`src/database/queries/token_queries.py`](../../../src/database/queries/token_queries.py) | The token ledger and its roll-ups. | [reference/database/data-lifecycle.md](../database/data-lifecycle.md) | Ledger rows survive task deletion on purpose. `tests/test_transcript_completions.py` |
| [`src/database/queries/agent_question_queries.py`](../../../src/database/queries/agent_question_queries.py) | Durable question identity, the answer compare-and-set, and transport receipts. | [reference/database/tables.md](../database/tables.md) | Unique per turn, so a re-asked question collapses. `tests/test_agent_questions.py` |
| [`src/database/queries/api_session_token_queries.py`](../../../src/database/queries/api_session_token_queries.py) | Mint, look up and revoke scoped session tokens. | [reference/database/tables.md](../database/tables.md) | Stores only the sha256; the plaintext is returned once and persisted nowhere. `tests/test_supervisor_global_token_loopback.py` |

## Query modules — workspaces and projects

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/queries/workspace_queries.py`](../../../src/database/queries/workspace_queries.py) | Workspace CRUD and locking: acquire, release, reap, slot affinity. | [reference/database/tables.md](../database/tables.md) | 19 public methods. `tests/test_workspace_commands.py`, `tests/test_workspace_attachments.py` |
| [`src/database/queries/workspace_kinds_queries.py`](../../../src/database/queries/workspace_kinds_queries.py) | Workspace-kind CRUD and resolution, project override shadowing system scope. | [reference/database/tables.md](../database/tables.md) | `tests/test_workspace_kinds_queries.py` |
| [`src/database/queries/task_requirements_queries.py`](../../../src/database/queries/task_requirements_queries.py) | A task's declared `requires_kinds`, in canonical lock order. | [reference/database/tables.md](../database/tables.md) | The order is what makes multi-kind acquisition deadlock-free. `tests/test_task_requirements_queries.py` |
| [`src/database/queries/merge_slot_queries.py`](../../../src/database/queries/merge_slot_queries.py) | The per-project integration lease: seed, then one atomic conditional `UPDATE`. | [reference/database/tables.md](../database/tables.md) | `rowcount == 1` means acquired; mutators are also serialised in-process. `tests/test_merge_slot.py` |
| [`src/database/queries/project_queries.py`](../../../src/database/queries/project_queries.py) | Project CRUD, constraints and the cascading project delete. | [reference/database/data-lifecycle.md](../database/data-lifecycle.md) | The bulk escape hatch that does purge a project's ledger. `tests/test_cli_projects.py` |
| [`src/database/queries/repo_queries.py`](../../../src/database/queries/repo_queries.py) | CRUD for the legacy `repos` table, still read by a few path-resolution fallbacks. | [reference/database/tables.md](../database/tables.md) | Superseded by the `repo_*` columns on `projects`. `tests/test_resolve_repo_path.py` |
| [`src/database/queries/onboarding_queries.py`](../../../src/database/queries/onboarding_queries.py) | The project-onboarding saga's idempotency and recovery record. | [reference/database/tables.md](../database/tables.md) | Phase and resource ledger may only advance while pending; `finish` is one-way. `tests/test_onboarding_queries.py` |
| [`src/database/queries/plugin_queries.py`](../../../src/database/queries/plugin_queries.py) | Installed plugins and their key/value storage. | [reference/database/tables.md](../database/tables.md) | `tests/test_plugin_commands.py` |
| [`src/database/queries/chat_queries.py`](../../../src/database/queries/chat_queries.py) | Chat-analyzer suggestions and their suppression. | [reference/database/tables.md](../database/tables.md) | `tests/test_chat_analyzer_metrics.py` |

## Query modules — messaging, escalations and the digest

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/queries/message_queries.py`](../../../src/database/queries/message_queries.py) | The `messages` table: create, inbox reads, delivery and archive marks. | [reference/database/tables.md](../database/tables.md) | `mark_delivered` is a compare-and-set on `delivered_at IS NULL` — the whole guard against double delivery. `tests/test_message_queries.py` |
| [`src/database/queries/escalation_queries.py`](../../../src/database/queries/escalation_queries.py) | Escalations, delivery leases and digest windows: 27 methods owning the concurrency boundaries the transport packages rely on. | [reference/database/tables.md](../database/tables.md) | Accepting a verified reply appends the fact, advances the revision and queues the supervisor message in one transaction. `tests/test_escalation_queries.py` |
| [`src/database/queries/digest_queries.py`](../../../src/database/queries/digest_queries.py) | The digest's only row access: durable evidence in a window, and what is executing right now. | [reference/database/tables.md](../database/tables.md) | An ordinary comment is not evidence of progress. `tests/test_digest_queries.py` |

## Query modules — playbooks, integration and telemetry

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/database/queries/playbook_run_queries.py`](../../../src/database/queries/playbook_run_queries.py) | Durable V2 run state: snapshots and receipts, 36 methods. | [reference/database/tables.md](../database/tables.md) | Every advance is a `snapshot_version` CAS plus exactly one receipt; a replayed boundary is rejected by a unique index, not by memory. `tests/test_playbook_run_repository.py`, `tests/test_v2_engine_repository.py` |
| [`src/database/queries/playbook_artifact_queries.py`](../../../src/database/queries/playbook_artifact_queries.py) | Immutable compiled artifacts and explicit activation records. | [reference/database/tables.md](../database/tables.md) | Artifacts are content-addressed by their own sha256. `tests/test_playbook_pending_events_policy.py` |
| [`src/database/queries/workflow_queries.py`](../../../src/database/queries/workflow_queries.py) | The coordination-workflow view over a playbook run. | [reference/database/tables.md](../database/tables.md) | `tests/test_workflow_pipeline_view.py` |
| [`src/database/queries/integration_state_queries.py`](../../../src/database/queries/integration_state_queries.py) | Read projections over durable hierarchical-integration state. | [reference/database/tables.md](../database/tables.md) | Reads only. `tests/test_integration_hierarchy.py` |
| [`src/database/queries/integration_delivery_queries.py`](../../../src/database/queries/integration_delivery_queries.py) | Review evidence, promotion intents and delivery receipts — the evidence spine of delivery. | [reference/database/tables.md](../database/tables.md) | Append-only tables here are enforced by trigger, not by convention. `tests/test_migration_integration_promotion.py` |
| [`src/database/queries/integration_control_queries.py`](../../../src/database/queries/integration_control_queries.py) | Rollout controls: waivers, transitions, consumptions and legacy suppression. | [reference/database/tables.md](../database/tables.md) | Caller-transaction-owned; only the suppression projection is reversible. `tests/test_migration_integration_controls.py` |
| [`src/database/queries/integration_train_queries.py`](../../../src/database/queries/integration_train_queries.py) | The projections used to seal an integration train atomically. | [reference/database/tables.md](../database/tables.md) | `tests/test_integration_workflow.py` |
| [`src/database/queries/integration_schedule_queries.py`](../../../src/database/queries/integration_schedule_queries.py) | Per-project sweep schedules and their outstanding/catch-up requests. | [reference/database/tables.md](../database/tables.md) | Sequence monotonicity is enforced by trigger. `tests/test_migration_integration_service.py` |
| [`src/database/queries/integration_reconciliation_queries.py`](../../../src/database/queries/integration_reconciliation_queries.py) | Bounded read-only selectors for one reconciliation tick. | [reference/database/tables.md](../database/tables.md) | Bounded on purpose: a tick must not scan the whole control plane. `tests/test_integration_completion_recovery.py` |
| [`src/database/queries/metrics_queries.py`](../../../src/database/queries/metrics_queries.py) | The per-second aggregates the sampler folds, and the idempotent store, range read and retention sweep for `metrics_samples`. | [reference/database/tables.md](../database/tables.md) | Nothing here interprets a sample; shape and roll-up arithmetic belong to the sampler. `tests/test_metrics_sampler.py`, `tests/test_api_metrics.py` |
| [`src/database/queries/provider_usage_queries.py`](../../../src/database/queries/provider_usage_queries.py) | Append-only provider quota readings from a transcript line and a probe. | [reference/database/tables.md](../database/tables.md) | A duplicate reading bumps `last_seen_at` rather than inserting, but is still recorded as evidence the probe ran. `tests/test_provider_usage_queries.py` |
| [`src/database/queries/transaction_queries.py`](../../../src/database/queries/transaction_queries.py) | `immediate()` — the write-transaction context every caller-owned query method composes inside. | [reference/database/queries.md](../database/queries.md) | On PostgreSQL this is `engine.begin()`; the module still carries an unreachable SQLite branch and a docstring describing it. |

## Supporting files — schema definition and migration environment

Not production modules: these define and migrate the schema rather than running
in the daemon. They are covered here because they decide what the schema *is*.

| File | Purpose | Component |
|---|---|---|
| [`alembic.ini`](../../../alembic.ini) | Alembic configuration. `sqlalchemy.url` is empty on purpose — the URL is supplied programmatically — and a `ruff check --fix` post-write hook formats generated revisions. | [reference/database/migrations.md](../database/migrations.md) |
| [`migrations/__init__.py`](../../../migrations/__init__.py) | Makes `migrations` importable, so a revision can import `migrations.integration_guards`. | [reference/database/migrations.md](../database/migrations.md) |
| [`migrations/README`](../../../migrations/README) | Alembic's generated placeholder readme. | [reference/database/migrations.md](../database/migrations.md) |
| [`migrations/env.py`](../../../migrations/env.py) | The migration environment: `target_metadata` is `tables.metadata`, and `transaction_per_migration=True` because one revision opens a second connection to inspect the previous one's DDL. | [reference/database/migrations.md](../database/migrations.md) |
| [`migrations/script.py.mako`](../../../migrations/script.py.mako) | The template new revisions are generated from. | [reference/database/migrations.md](../database/migrations.md) |
| [`migrations/integration_guards.py`](../../../migrations/integration_guards.py) | 29 PL/pgSQL functions and 50 triggers over 31 integration tables, snapshotted at the pre-squash head. Immutable — behaviour changes go in new revisions. | [reference/database/tables.md](../database/tables.md) |
| [`migrations/versions/__init__.py`](../../../migrations/versions/__init__.py) | Package marker for the revision directory. | [reference/database/migrations.md](../database/migrations.md) |
| [`src/schema.sql`](../../../src/schema.sql) | Zero bytes and referenced by nothing. A leftover of the pre-Alembic schema, recorded in the [known-inaccuracies ledger](../../plans/documentation-overhaul/known-inaccuracies.md) as something a code change should delete. | [reference/database/migrations.md](../database/migrations.md) |

### Revisions

The chain is a squashed baseline plus eleven revisions;
[Migrations](../database/migrations.md#what-the-chain-looks-like-today) explains
what each one does and why.

| File | Revision |
|---|---|
| [`a00000000001_squashed_baseline.py`](../../../migrations/versions/a00000000001_squashed_baseline.py) | The whole schema in one revision, from live metadata, plus the guards and the built-in workspace kinds. |
| [`a00000000002_restore_integration_guards.py`](../../../migrations/versions/a00000000002_restore_integration_guards.py) | Reinstalls guards the first cut of the baseline omitted. |
| [`a00000000003_add_min_per_project.py`](../../../migrations/versions/a00000000003_add_min_per_project.py) | `agent_profiles.min_per_project`. |
| [`a00000000004_task_branch_origin_discard.py`](../../../migrations/versions/a00000000004_task_branch_origin_discard.py) | Branch-discard intent on `task_branch_origins`. |
| [`a00000000005_resolution_push_start_fence.py`](../../../migrations/versions/a00000000005_resolution_push_start_fence.py) | The push-start fence and its unknown-start sentinel. |
| [`a00000000006_legacy_resolution_recovery_evidence.py`](../../../migrations/versions/a00000000006_legacy_resolution_recovery_evidence.py) | An audited remote observation for legacy resolution recovery. |
| [`a00000000007_candidate_rejection_recovery.py`](../../../migrations/versions/a00000000007_candidate_rejection_recovery.py) | Retains rejected candidate repairs and permits one replacement. |
| [`a00000000008_recover_unwritten_resolution.py`](../../../migrations/versions/a00000000008_recover_unwritten_resolution.py) | A pre-write marker plus links for an auditable successor. |
| [`a00000000009_durable_escalation_state.py`](../../../migrations/versions/a00000000009_durable_escalation_state.py) | The escalation and digest tables. |
| [`a0000000000a_add_task_comment_kind.py`](../../../migrations/versions/a0000000000a_add_task_comment_kind.py) | `task_comments.kind`. |
| [`a0000000000b_escalation_actions.py`](../../../migrations/versions/a0000000000b_escalation_actions.py) | Verified-reply action reservations. |
| [`a0000000000c_development_integration.py`](../../../migrations/versions/a0000000000c_development_integration.py) | The development delivery journal. Current head. |
