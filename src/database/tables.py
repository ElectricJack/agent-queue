"""SQLAlchemy Core table definitions for all database tables.

This module defines the complete schema using SQLAlchemy's ``Table`` and
``MetaData`` objects.  Every adapter (SQLite, PostgreSQL) shares these
definitions — dialect differences are handled by SQLAlchemy automatically.

The tables mirror the legacy DDL in ``schema.py`` exactly.  Column names,
types, defaults, and constraints are preserved so that existing databases
continue to work without migration.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)

metadata = MetaData()

projects = Table(
    "projects",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("credit_weight", Float, nullable=False, server_default="1.0"),
    Column("max_concurrent_agents", Integer, nullable=False, server_default="2"),
    Column("status", Text, nullable=False, server_default="ACTIVE"),
    Column("total_tokens_used", Integer, nullable=False, server_default="0"),
    Column("budget_limit", Integer, nullable=True),
    Column("workspace_path", Text, nullable=True),
    Column("discord_channel_id", Text, nullable=True),
    Column("discord_control_channel_id", Text, nullable=True),
    Column("repo_url", Text, nullable=True, server_default=""),
    Column("repo_default_branch", Text, nullable=True, server_default="main"),
    Column("default_profile_id", Text, ForeignKey("agent_profiles.id"), nullable=True),
    Column("assignment_playbook_id", Text, nullable=True),
    # Project-level integration policy: 'direct' | 'pull_request' | NULL
    # (NULL = inherit config ``integration.default_mode``).
    Column("integration_mode", Text, nullable=True),
    Column("created_at", Float, nullable=False),
)

repos = Table(
    "repos",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("url", Text, nullable=False),
    Column("default_branch", Text, nullable=False, server_default="main"),
    Column("checkout_base_path", Text, nullable=False),
    Column("source_type", Text, nullable=False, server_default="clone"),
    Column("source_path", Text, nullable=False, server_default=""),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("parent_task_id", Text, ForeignKey("tasks.id"), nullable=True),
    Column("repo_id", Text, ForeignKey("repos.id"), nullable=True),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("status", Text, nullable=False, server_default="DEFINED"),
    Column("verification_type", Text, nullable=False, server_default="auto_test"),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("assigned_agent_id", Text, ForeignKey("agents.id"), nullable=True),
    Column("branch_name", Text, nullable=True),
    Column("resume_after", Float, nullable=True),
    # Integration policy override: 'direct' | 'pull_request' | NULL (inherit
    # project policy, then config ``integration.default_mode``).
    Column("integration_mode", Text, nullable=True),
    Column("pr_url", Text, nullable=True),
    Column("plan_source", Text, nullable=True),
    Column("is_plan_subtask", Integer, nullable=False, server_default="0"),
    Column("task_type", Text, nullable=True),
    Column("profile_id", Text, ForeignKey("agent_profiles.id"), nullable=True),
    Column(
        "preferred_workspace_id",
        Text,
        ForeignKey(
            "workspaces.id",
            use_alter=True,
            name="fk_tasks_preferred_workspace",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    Column("attachments", Text, nullable=True, server_default="[]"),
    Column("deliverables", Text, nullable=False, server_default="[]"),
    Column("skip_verification", Integer, nullable=False, server_default="0"),
    Column("workflow_id", Text, ForeignKey("workflows.workflow_id", use_alter=True), nullable=True),
    Column("affinity_agent_id", Text, nullable=True),
    Column("affinity_reason", Text, nullable=True),
    Column("workspace_mode", Text, nullable=True),
    # Persisted blocked-state projection (work-graph spec §2.2).  Integer
    # 0/1 matches the table's existing flag style (e.g. is_plan_subtask).
    # Recomputed by the query layer; never written directly.
    Column("is_blocked", Integer, nullable=False, server_default="0"),
    Column("dedup_key", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    # Discord thread opened for this task.  Persisted because the bot's
    # in-memory task->thread map is lost on every daemon restart, which used
    # to make it open a *new* thread for a task it had already threaded.
    Column("discord_thread_id", Text, nullable=True),
    # Hierarchy (swarm-work-model §4, §6): per-parent ordinal counter for
    # dotted child ids.  Incremented atomically by
    # ``task_names.reserve_child_ordinal``; never read for anything else.
    Column("next_child_ordinal", Integer, nullable=False, server_default="1"),
    # Provenance (swarm-work-model §9): who created the row.  Stamped by
    # CommandHandler.execute from the request scope (Plan 2); nullable so
    # rows created by legacy paths stay valid.
    Column("created_by_kind", Text, nullable=True),
    Column("created_by_id", Text, nullable=True),
    # Per-claim fence (swarm-work-model §10).  Plan 2 increments it.
    Column("claim_epoch", Integer, nullable=False, server_default="0"),
    # Worker-filing quota counter (swarm-work-model §12).  Plan 2 reserves it.
    Column("filed_count", Integer, nullable=False, server_default="0"),
    Index("idx_tasks_project_dedup", "project_id", "dedup_key"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # Serves _check_defined_tasks, the scheduler filter, and `aq project ready`.
    Index("idx_tasks_project_status_blocked", "project_id", "status", "is_blocked"),
    # Group progress and tree queries walk parent_task_id.
    Index("idx_tasks_parent", "parent_task_id"),
    # Pool work query (swarm-work-model §10): ready tasks for a profile.
    Index("idx_tasks_ready_by_profile", "project_id", "profile_id", "status", "is_blocked"),
    # Status-only lists (``list_tasks(status=...)`` in the monitoring cycle,
    # ``aq task list --status``) had no index leading with status and
    # seq-scanned ``tasks`` as completed history grew.
    Index("idx_tasks_status_project", "status", "project_id"),
)

task_criteria = Table(
    "task_criteria",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("type", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("sort_order", Integer, nullable=False, server_default="0"),
)

TASK_DEP_TYPES = (
    "blocks",
    "parent-child",
    "waits-for",
    "conditional-blocks",
    "discovered-from",
    "related",
    "duplicates",
    "supersedes",
)
"""Typed dependency edge kinds — see docs/specs/design/work-graph.md.

Only ``blocks``/``parent-child``/``waits-for``/``conditional-blocks`` gate
readiness; the rest are informational.  The tuple order is the check
constraint's order and must stay stable (it is embedded in the migration).
"""

_TASK_DEP_TYPE_CHECK = "dep_type IN (" + ", ".join(f"'{t}'" for t in TASK_DEP_TYPES) + ")"

task_dependencies = Table(
    "task_dependencies",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    Column("depends_on_task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    # Typed edges (work-graph spec §2.1).  Part of the PK so one task pair
    # can carry several edge kinds (e.g. ``blocks`` + ``discovered-from``).
    # Existing rows read back as ``'blocks'`` — zero behavior change.
    Column("dep_type", Text, nullable=False, server_default="blocks", primary_key=True),
    # Human explanation of why the edge exists.  Spawn/provenance writers
    # populate this with the reason the follow-up was created; nullable keeps
    # legacy and purely structural edges valid.
    Column("description", Text, nullable=True),
    # Named explicitly with the name PostgreSQL auto-assigned in the baseline
    # revision (``<table>_check``).  The constraint has always been declared
    # here, but *unnamed* metadata check constraints are invisible to
    # autogenerate's comparison, so every ``alembic revision --autogenerate``
    # run saw an unmatched ``task_dependencies_check`` in the live schema and
    # emitted a spurious "drop the self-dependency guard" operation.  The name
    # is deployed on every existing database, so pinning it here converges
    # metadata and schema without a migration.
    CheckConstraint("task_id != depends_on_task_id", name="task_dependencies_check"),
    CheckConstraint(_TASK_DEP_TYPE_CHECK, name="ck_task_deps_dep_type"),
    # Composite indexes replace the former single-column pair: the leading
    # column keeps every existing lookup covered, the second serves the
    # blocked-state recompute predicate's dep_type filters.
    Index("idx_task_deps_task_type", "task_id", "dep_type"),
    Index("idx_task_deps_depson_type", "depends_on_task_id", "dep_type"),
    # Exactly one parent per task (swarm-work-model §4): a partial unique
    # index over parent-child edges only.  Created by revision B after the
    # data is canonicalised.
    Index(
        "uq_task_deps_single_parent",
        "task_id",
        unique=True,
        sqlite_where=text("dep_type = 'parent-child'"),
        postgresql_where=text("dep_type = 'parent-child'"),
    ),
)

# ── Task graph layout (spatial-layout design §4.10) ─────────────────────────
LAYOUT_VARIANTS = ("all", "active")
LAYOUT_KINDS = ("card", "container", "stub")

task_layouts = Table(
    "task_layouts",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    Column("container_id", Text, nullable=True),
    Column("path", Text, nullable=False),
    Column("depth", Integer, nullable=False),
    Column("rank", Integer, nullable=False),
    Column("order_key", Text, nullable=False),
    Column("w", Float, nullable=False),
    Column("h", Float, nullable=False),
    Column("rel_x", Float, nullable=False),
    Column("rel_y", Float, nullable=False),
    Column("abs_x", Float, nullable=False),
    Column("abs_y", Float, nullable=False),
    Column("kind", Text, nullable=False),
    Column("agg_children", Integer, nullable=False, server_default="0"),
    Column("agg_descendants", Integer, nullable=False, server_default="0"),
    Column("agg_completed", Integer, nullable=False, server_default="0"),
    Column("agg_running", Integer, nullable=False, server_default="0"),
    Column("agg_blocked", Integer, nullable=False, server_default="0"),
    Column("agg_active", Integer, nullable=False, server_default="0"),
    CheckConstraint("variant IN ('all', 'active')", name="ck_task_layouts_variant"),
    CheckConstraint("kind IN ('card', 'container', 'stub')", name="ck_task_layouts_kind"),
    # ``load_paths_by_prefixes`` / ``load_subtree_ids`` filter ``path LIKE
    # '/a/b/%'``.  On PostgreSQL a plain btree under a non-C collation cannot
    # serve a LIKE prefix, so the run recorded zero scans of this index;
    # ``text_pattern_ops`` makes the prefix scan index-driven.  SQLite
    # ignores the op class.
    Index(
        "idx_task_layouts_path",
        "project_id",
        "variant",
        "path",
        postgresql_ops={"path": "text_pattern_ops"},
    ),
    Index("idx_task_layouts_depth", "project_id", "variant", "depth"),
    Index("idx_task_layouts_container", "project_id", "variant", "container_id"),
)

task_layout_cells = Table(
    "task_layout_cells",
    metadata,
    Column("project_id", Text, nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("cell_x", Integer, nullable=False, primary_key=True),
    Column("cell_y", Integer, nullable=False, primary_key=True),
    Column("task_id", Text, nullable=False, primary_key=True),
    Index("idx_task_layout_cells_cell", "project_id", "variant", "cell_x", "cell_y"),
    Index("idx_task_layout_cells_task", "project_id", "variant", "task_id"),
)

project_layout_meta = Table(
    "project_layout_meta",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False, primary_key=True),
    Column("variant", Text, nullable=False, primary_key=True),
    Column("layout_version", Integer, nullable=False, server_default="0"),
    Column("extent_w", Float, nullable=False, server_default="0"),
    Column("extent_h", Float, nullable=False, server_default="0"),
    Column("node_count", Integer, nullable=False, server_default="0"),
    Column("updated_at", Float, nullable=False),
    Column("reconciled_at", Float, nullable=True),
)

layout_dirty = Table(
    "layout_dirty",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Index("idx_layout_dirty_project", "project_id", "seq"),
)

layout_jobs = Table(
    "layout_jobs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("variant", Text, nullable=False),
    Column("kind", Text, nullable=False),  # 'tidy' | 'backfill'
    Column("status", Text, nullable=False),  # queued | running | done | failed
    Column("requested_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    Column("error", Text, nullable=True),
    Index("idx_layout_jobs_project_status", "project_id", "status"),
)

task_context = Table(
    "task_context",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("type", Text, nullable=False),
    Column("label", Text, nullable=True),
    Column("content", Text, nullable=False),
)

task_comments = Table(
    "task_comments",
    metadata,
    Column("id", Text, primary_key=True),
    # Stable identity spans active and archived tasks. Hard-delete paths
    # clean up explicitly after deleting the parent to serialize with append.
    Column("task_id", Text, nullable=False),
    # NULL preserves legacy comments whose project cannot be proven.
    Column("project_id", Text, nullable=True),
    Column("body", Text, nullable=False),
    Column("author_kind", Text, nullable=False),
    Column("author_id", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    CheckConstraint("author_kind IN ('user','agent','supervisor')", name="ck_task_comment_author_kind"),
    CheckConstraint("length(body) BETWEEN 1 AND 16000", name="ck_task_comment_body_length"),
    Index("idx_task_comments_task_created", "task_id", "created_at", "id"),
    Index("idx_task_comments_project_created", "task_id", "project_id", "created_at", "id"),
)

task_metadata = Table(
    "task_metadata",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# ---------------------------------------------------------------------------
# Gates and labels (work-graph spec §2.3 / §2.4).
#
# Substrate only — no query layer or command surface reads these yet.
# ---------------------------------------------------------------------------

GATE_TYPES = ("human", "timer", "pr-merged", "ci-run", "event", "task", "routing")
GATE_STATUSES = ("open", "resolved", "expired")

gates = Table(
    "gates",
    metadata,
    Column("id", Text, primary_key=True),  # "gate-" + uuid4[:12]
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("gate_type", Text, nullable=False),  # human|timer|pr-merged|ci-run|event|task
    Column("title", Text, nullable=False),
    Column("question", Text, nullable=False, server_default=""),
    Column("await_id", Text, nullable=True),
    Column("timeout_at", Float, nullable=True),
    Column("status", Text, nullable=False, server_default="open"),  # open|resolved|expired
    Column("resolved_by", Text, nullable=True),
    Column("resolution", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "gate_type IN (" + ", ".join(f"'{t}'" for t in GATE_TYPES) + ")",
        name="ck_gates_type",
    ),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in GATE_STATUSES) + ")",
        name="ck_gates_status",
    ),
    Index("idx_gates_project_status", "project_id", "status"),
    # The sweep scans open gates by type.
    Index("idx_gates_status_type", "status", "gate_type"),
    # Partial unique index prevents concurrent create_gate calls from
    # racing past the SELECT-then-INSERT dedup on Postgres (READ COMMITTED
    # can let two txs both see zero matches). Only ``open`` gates are
    # constrained; resolved/expired gates may accumulate freely.
    Index(
        "uq_gates_open_dedup",
        "project_id",
        "gate_type",
        "await_id",
        unique=True,
        sqlite_where=text("status = 'open'"),
        postgresql_where=text("status = 'open'"),
    ),
)

task_gates = Table(
    "task_gates",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    Column("gate_id", Text, ForeignKey("gates.id"), primary_key=True),
    # resolve → find waiters.
    Index("idx_task_gates_gate", "gate_id"),
)

task_labels = Table(
    "task_labels",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    Column("label", Text, primary_key=True),
    Index("idx_task_labels_label", "label"),
)

hierarchy_migration_rejects = Table(
    "hierarchy_migration_rejects",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("parent_id", Text, nullable=True),
    Column("source", Text, nullable=False),  # duplicate_edge | column_only | edge
    Column("reason", Text, nullable=False),  # cross_project | cycle | depth | not_found | duplicate
    Column("detail", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("idx_hier_rejects_run", "run_id"),
)

# ---------------------------------------------------------------------------
# Task proposals (design spec §8, Phase 6 — spec ingestion).
#
# Staged batch of tasks + edges awaiting human approval before being
# committed into the live work graph.  ``payload`` is a JSON blob of
# ``{"tasks": [...], "edges": [...]}`` — see the plan's Interfaces block.
# ---------------------------------------------------------------------------

TASK_PROPOSAL_STATUSES = ("draft", "ready", "committed", "discarded")
_TASK_PROPOSAL_STATUS_CHECK = (
    "status IN (" + ", ".join(f"'{s}'" for s in TASK_PROPOSAL_STATUSES) + ")"
)

task_proposals = Table(
    "task_proposals",
    metadata,
    Column("id", Text, primary_key=True),  # "prop-" + uuid4[:12]
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    # Provenance — free-form; e.g. "spec:projects/foo/specs/2026-08-21-thing.md".
    Column("source", Text, nullable=False),
    # JSON blob: {"tasks":[{tempId,title,description,priority?},...],
    #             "edges":[{from,to,dep_type},...]}
    Column("payload", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(_TASK_PROPOSAL_STATUS_CHECK, name="ck_task_proposals_status"),
    Index("idx_task_proposals_project_status", "project_id", "status"),
)

task_tools = Table(
    "task_tools",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("type", Text, nullable=False),
    Column("config", Text, nullable=False),
)

agents = Table(
    "agents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("profile_id", Text, nullable=False),  # soft reference to agent_profiles.id
    Column("state", Text, nullable=False, server_default="IDLE"),
    Column(
        "current_task_id",
        Text,
        ForeignKey("tasks.id", use_alter=True, name="fk_agents_current_task", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("checkout_path", Text, nullable=True),
    Column("repo_id", Text, ForeignKey("repos.id"), nullable=True),
    Column("pid", Integer, nullable=True),
    Column("last_heartbeat", Float, nullable=True),
    Column("total_tokens_used", Integer, nullable=False, server_default="0"),
    Column("session_tokens_used", Integer, nullable=False, server_default="0"),
    Column("role", Text, nullable=False, server_default="worker"),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    Column("harness", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    Column("deleted_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
)

token_ledger = Table(
    "token_ledger",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=True),
    # ``agent_id`` and ``task_id`` are deliberately NOT foreign keys.  The
    # ledger is an append-only *audit* record of money spent: it has to
    # outlive the ephemeral rows it refers to.  Agents are reaped whenever
    # their profile stops resolving or a project drops below its concurrency
    # cap, and tasks are moved out of ``tasks`` into ``archived_tasks`` the
    # moment they complete.  While these were real FKs, every one of those
    # routine lifecycle events had to cascade-delete the matching ledger
    # rows, so a 24h/7d ``token_audit`` could only ever see spend from tasks
    # that had not been archived yet — in practice, zero.  Both columns are
    # now best-effort attribution strings, which is what the readers already
    # assumed (``get_cost_rollup`` outer-joins ``agents``).
    Column("agent_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("tokens_used", Integer, nullable=False),
    # Pricing split (trust-and-ops spec §6.1).  Nullable — rows written
    # before the split existed (and runtimes that don't report it)
    # aggregate into "unpriced_tokens" in the cost rollup.
    Column("model", Text, nullable=True),
    Column("input_tokens", Integer, nullable=True),
    Column("output_tokens", Integer, nullable=True),
    # Cache tokens, kept apart from the priced input/output split.  A cached
    # read is billed at a different rate from fresh input and a cache write
    # at a third, so folding either into ``input_tokens`` would silently
    # overprice the row.  They are the bulk of the volume on a long-lived
    # session — without their own columns ``tokens_used`` minus the split is
    # a six-figure "unattributed" number that the metrics tab cannot explain.
    Column("cache_read_tokens", Integer, nullable=True),
    Column("cache_write_tokens", Integer, nullable=True),
    Column("timestamp", Float, nullable=False),
    # The metrics sampler reads a trailing window off this append-only,
    # unbounded table every few seconds to compute tokens/minute.
    Index("idx_token_ledger_timestamp", "timestamp"),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", Text, nullable=False),
    Column("project_id", Text, nullable=True),
    Column("task_id", Text, nullable=True),
    Column("agent_id", Text, nullable=True),
    Column("payload", Text, nullable=True),
    Column("timestamp", Float, nullable=False),
    Index("idx_events_type_project_id", "event_type", "project_id", "id"),
)

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("id", Text, primary_key=True),
    Column("agent_type", Text, nullable=False),
    Column("limit_type", Text, nullable=False),
    Column("max_tokens", Integer, nullable=False),
    Column("current_tokens", Integer, nullable=False, server_default="0"),
    Column("window_start", Float, nullable=False),
)

task_results = Table(
    "task_results",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("agent_id", Text, ForeignKey("agents.id"), nullable=False),
    Column("result", Text, nullable=False),
    Column("summary", Text, nullable=False, server_default=""),
    Column("files_changed", Text, nullable=False, server_default="[]"),
    Column("error_message", Text, nullable=True),
    Column("tokens_used", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
)

task_completion_records = Table(
    "task_completion_records",
    metadata,
    Column("id", Text, primary_key=True),
    # Logical reference, not an FK: archival deletes the active task row,
    # while its completion history must remain available after restore.
    Column("task_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("work_outcome", Text, nullable=True),
    Column("failure_class", Text, nullable=True),
    Column("changes", Text, nullable=False, server_default=""),
    Column("verification", Text, nullable=False, server_default=""),
    Column("tests", Text, nullable=False, server_default="[]"),
    Column("commands", Text, nullable=False, server_default="[]"),
    Column("branch", Text, nullable=True),
    Column("commits", Text, nullable=False, server_default="[]"),
    Column("pr_url", Text, nullable=True),
    Column("summary", Text, nullable=False, server_default=""),
    Column("notes", Text, nullable=False, server_default=""),
    Column("deliverables", Text, nullable=False, server_default="[]"),
    Column("completed_at", Float, nullable=False),
    Index("idx_task_completion_records_task_time", "task_id", "completed_at"),
    # Completions-per-hour scans by time alone; the composite above cannot
    # serve it because ``task_id`` is the leading column.
    Index("idx_task_completion_records_completed_at", "completed_at"),
)

system_config = Table(
    "system_config",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("workspace_path", Text, nullable=False),
    Column("source_type", Text, nullable=False, server_default="clone"),
    Column("name", Text, nullable=True),
    # Soft reference to (workspace_kinds.project_id, workspace_kinds.id) — resolved
    # at use time against the project-scoped row, then the system row.
    # Nullable during the workspaces-v2 migration window; tightened to NOT NULL
    # in a follow-up migration after one minor version (spec §3.2 / §9.5).
    Column("kind_id", Text, nullable=True),
    Column("locked_by_agent_id", Text, ForeignKey("agents.id"), nullable=True),
    Column("locked_by_task_id", Text, ForeignKey("tasks.id"), nullable=True),
    Column("locked_at", Float, nullable=True),
    Column("lock_mode", Text, nullable=True),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    # Worktree slots (worktree-execution spec §3.2).  NULL for clones,
    # links and base rows; 0..N-1 for slot worktrees.
    Column("slot_index", Integer, nullable=True),
    # Soft self-reference to the base clone's workspaces.id (no FK — matches
    # the kind_id soft-ref precedent).  Set only on slot rows.
    Column("base_workspace_id", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("project_id", "workspace_path"),
    # Partial unique index: one row per (base, slot) but many NULL/NULL rows
    # (every clone).  Written by hand in the migration — autogenerate
    # handles partial indexes poorly.
    Index(
        "uq_workspaces_base_slot",
        "base_workspace_id",
        "slot_index",
        unique=True,
        sqlite_where=text("base_workspace_id IS NOT NULL AND slot_index IS NOT NULL"),
        postgresql_where=text("base_workspace_id IS NOT NULL AND slot_index IS NOT NULL"),
    ),
)

workspace_kinds = Table(
    "workspace_kinds",
    metadata,
    # Composite PK (project_id, id).  project_id uses sentinel '__system__'
    # for system-wide rows so the column can be NOT NULL on Postgres.
    # See docs/specs/design/workspaces-v2.md §3.1.
    Column("project_id", Text, nullable=False, primary_key=True),
    Column("id", Text, nullable=False, primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("writable", Boolean, nullable=False, server_default=true()),
    Column("lockable", Boolean, nullable=False, server_default=true()),
    Column("is_git_repo", Boolean, nullable=False, server_default=true()),
    Column("repo_url", Text, nullable=True),
    # Lowercase enum value: 'exclusive' | 'branch_isolated' | 'directory_isolated'
    # — matches WorkspaceMode.value.
    Column("default_lock_mode", Text, nullable=True),
    Column("auto_attach", Boolean, nullable=False, server_default="false"),
    # Git provisioning strategy (worktree-execution spec §3.1):
    # 'worktree' | 'exclusive-clone' | 'directory-isolated'.  Meaningful
    # only when is_git_repo is true.  The shipped default is 'worktree',
    # but the substrate migration backfills every pre-existing row to
    # 'exclusive-clone' so upgrades keep their current behavior.
    Column("mode", Text, nullable=False, server_default="worktree"),
    # JSON-encoded list[str] of shell commands run after a slot is created.
    # Text for SQLite/PG parity, matching the existing JSON-in-Text usage.
    Column("worktree_setup", Text, nullable=False, server_default="[]"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

merge_slots = Table(
    "merge_slots",
    metadata,
    # One row per project, created lazily on first acquire
    # (worktree-execution spec §3.3).
    Column("project_id", Text, ForeignKey("projects.id"), primary_key=True),
    # NULL = free.  Soft ref to tasks.id (survives task archival).
    Column("holder_task_id", Text, nullable=True),
    Column("acquired_at", Float, nullable=True),
    # Lease expiry; renewed by the integration pipeline.
    Column("expires_at", Float, nullable=True),
    Column("updated_at", Float, nullable=False),
)

task_workspace_requirements = Table(
    "task_workspace_requirements",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    # kind_id is a soft reference (no FK) — resolution depends on
    # (project_id, kind_id) and may target either the project-scoped
    # row or the system row.  See spec §3.2 / §3.5.
    Column("kind_id", Text, nullable=False, primary_key=True),
    Column("position", Integer, nullable=False, server_default="0", primary_key=True),
    Column("alias", Text, nullable=True),
    Index("idx_task_ws_reqs_task_id", "task_id"),
)

# hooks and hook_runs tables removed (playbooks spec §13 Phase 3).
# Migration drops these tables from existing databases.

agent_profiles = Table(
    "agent_profiles",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("model", Text, nullable=False, server_default=""),
    Column("permission_mode", Text, nullable=False, server_default=""),
    Column("codex_full_auto", Boolean, nullable=False, server_default=false()),
    Column(
        "claude_dangerously_skip_permissions",
        Boolean,
        nullable=False,
        server_default=false(),
    ),
    Column("allowed_tools", Text, nullable=False, server_default="[]"),
    # Normalized capability namespaces (Playbook V2 Package 0 §3.1), stored
    # as JSON arrays of text like ``allowed_tools`` above.  NULL is
    # meaningful: it is the signal that the legacy ``allowed_tools`` adapter
    # should run.  Backfilling would erase the distinction between "authored
    # as none" ('[]') and "not authored" (NULL).
    Column("harness_tools", Text, nullable=True),
    Column("aq_commands", Text, nullable=True),
    Column("plugin_tools", Text, nullable=True),
    Column("mcp_servers", Text, nullable=False, server_default="{}"),
    Column("system_prompt_suffix", Text, nullable=False, server_default=""),
    Column("install", Text, nullable=False, server_default="{}"),
    # Optional override: when set, memory reads/writes for this profile
    # target scope ``agenttype_{memory_scope_id}`` instead of
    # ``agenttype_{id}``.  Lets multiple profiles share one memory scope
    # (e.g. claude-opus + claude-sonnet both set ``memory_scope_id='claude'``
    # so insights accumulate in a single pool).
    Column("memory_scope_id", Text, nullable=True),
    # Which runtime executes tasks for this profile.  Empty (the default)
    # means the profile runs as a tmux **session**, selected by ``harness`` —
    # the path for every coding agent.  ``"supervisor"`` routes to the
    # in-process Supervisor singleton (tool-call-only, no workspace) and is
    # the only non-empty value.
    Column("runtime", Text, nullable=False, server_default=""),
    # -- Named-session pass-through storage (supervisor-agent spec §3.2) --
    # Values are validated at profile parse time; the harness *schema*
    # (what "claude" means) is owned by the session-runtime spec.
    Column("harness", Text, nullable=True),
    Column("lifecycle", Text, nullable=False, server_default="task"),
    Column("mode", Text, nullable=True),
    Column("wake_mode", Text, nullable=True),
    Column("idle_timeout", Integer, nullable=True),
    Column("max_session_age", Integer, nullable=True),
    # lifecycle: pool (swarm-work-model §9).  NULL = unlimited claims.
    Column("min_active", Integer, nullable=True),
    Column("max_active", Integer, nullable=True),
    Column("max_claims_per_session", Integer, nullable=True),
    # Authored config for a thin project pool override.  The effective fields
    # above stay materialized for scheduling/query paths that predate overlays.
    Column("overlay_config", Text, nullable=True),
    Column("default_class", Text, nullable=False, server_default=""),
    Column("needs_workspace", Boolean, nullable=False, server_default=true()),
    # Declarative statement of write intent: a ``read_only`` profile lists
    # no write/edit/commit/push tools.  It no longer alters workspace
    # acquisition — that path handed read-only agents the base checkout.
    Column(
        "read_only",
        Boolean,
        nullable=False,
        server_default=false(),
    ),
    # Opt-in for the base-checkout launch guard: without it a session whose
    # ``work_dir`` is a base workspace (the clone hosting the slot
    # worktrees, often a human's own tree) is refused.
    Column(
        "allow_base_checkout",
        Boolean,
        nullable=False,
        server_default=false(),
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

# ---------------------------------------------------------------------------
# Session runtime, messaging, and API auth (substrate only).
#
# See docs/specs/implementation/session-runtime.md §2,
# supervisor-agent.md §3, aq-surface.md §4.
# ---------------------------------------------------------------------------

sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),  # uuid4 hex
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=True),  # NULL for named
    Column("project_id", Text, ForeignKey("projects.id"), nullable=True),
    Column("profile_id", Text, nullable=False),  # soft ref, like agents.profile_id
    Column("harness", Text, nullable=False),  # e.g. "claude"
    Column("provider", Text, nullable=False),  # "tmux" | "subprocess" | "fake"
    Column("name", Text, nullable=False),  # "s-<task_id>" | "n-<profile>[--<pid>]"
    Column("lifecycle", Text, nullable=False),  # "task" | "named"
    # starting | running | draining | stopped | sleeping | quarantined.
    # "stalled" is derived (lease TTL vs last_activity), never stored.
    Column("state", Text, nullable=False, server_default="starting"),
    # What the daemon *wants*: running | sleeping | stopped.  ``state`` is
    # the runtime projection (what we last observed); this is the intent it
    # converges toward.  One column served both roles until 2026-08-27,
    # which is why _step_named could only converge downward -- "sleeping"
    # and "should be sleeping" were the same value.
    Column("desired_state", Text, nullable=False, server_default="running"),
    # Pool lifecycle (swarm-work-model §9–§11).  Plan 2 writes these.
    Column("claims", Integer, nullable=False, server_default="0"),
    Column("agent_id", Text, nullable=True),
    Column("claim_phase", Text, nullable=True),
    Column("claim_phase_at", Float, nullable=True),
    Column("last_claim_epoch", Integer, nullable=True),
    Column("last_claim_result", Text, nullable=True),
    Column("session_key", Text, nullable=True),  # harness resume key
    Column("work_dir", Text, nullable=False),
    Column("epoch", Text, nullable=False),  # AQ_DAEMON_EPOCH at launch
    Column("instance_token", Text, nullable=False),  # AQ_INSTANCE_TOKEN (kill fence)
    Column("started_at", Float, nullable=False),
    Column("last_activity", Float, nullable=True),
    Column("restarts", Integer, nullable=False, server_default="0"),
    Column("quarantined_at", Float, nullable=True),
    Column("sleep_reason", Text, nullable=True),
    Column("ended_at", Float, nullable=True),
    Column("end_reason", Text, nullable=True),
    Column("llm_provider", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    # Did this launch actually wire the harness's subagent hooks?  Written
    # once at insert from the SessionSpec, never inferred later: whether a
    # live session reports native subagents depends on the argv it was
    # launched with, and today's harness file may have been edited since.
    # This is what lets ``subagent_counts`` say "complete" instead of
    # "unknown" -- and say "unknown" honestly for the sessions that lack it.
    Column("hooks_provisioned", Boolean, nullable=False, server_default=false()),
    Index("idx_sessions_agent", "agent_id", "state"),
    Index("idx_sessions_task_id", "task_id"),
    Index("idx_sessions_state", "state"),
    Index("idx_sessions_name", "name"),
    Index("idx_sessions_pool", "lifecycle", "project_id", "profile_id", "state"),
)


# Audit associations deliberately use soft references: history outlives task
# archival, agent deletion, and administrative session deletion.
task_session_attempts = Table(
    "task_session_attempts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("project_id", Text, nullable=True),
    Column("agent_id", Text, nullable=True),
    Column("agent_name", Text, nullable=True),
    Column("profile_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("lifecycle", Text, nullable=False),
    Column("model", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    Column("llm_provider", Text, nullable=True),
    Column("harness", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("work_dir", Text, nullable=False),
    Column("started_at", Float, nullable=False),
    Column("session_started_at", Float, nullable=False),
    Column("ended_at", Float, nullable=True),
    Column("end_reason", Text, nullable=True),
    Column("outcome", Text, nullable=True),
    Column("session_key", Text, nullable=True),
    Index("idx_task_session_attempts_task", "task_id", "started_at"),
    Index("idx_task_session_attempts_session", "session_id", "started_at"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Text, primary_key=True),  # "msg-<uuid7>"
    Column("project_id", Text, ForeignKey("projects.id"), nullable=True),
    Column("from_kind", Text, nullable=False),  # session|user|system
    Column("from_id", Text, nullable=False),
    Column("to_kind", Text, nullable=False),  # session|task|profile|user
    Column("to_id", Text, nullable=False),
    Column("thread_id", Text, nullable=True),
    Column("subject", Text, nullable=True),
    Column("body", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("created_at", Float, nullable=False),
    Column("delivered_at", Float, nullable=True),
    Column("read_at", Float, nullable=True),
    Column("archive_after_inject", Integer, nullable=False, server_default="0"),
    Column("archived_at", Float, nullable=True),
    Column("reply_to_id", Text, ForeignKey("messages.id"), nullable=True),
    Column("via", Text, nullable=True),  # null | "transcript_tail"
    Column("body_kind", Text, nullable=True),
    Column("pane_open", Text, nullable=True),  # JSON blob {view, args}
    CheckConstraint(
        "from_kind IN ('session','user','system')",
        name="ck_messages_from_kind",
    ),
    CheckConstraint(
        "to_kind IN ('session','task','profile','user')",
        name="ck_messages_to_kind",
    ),
    Index("idx_messages_pending", "to_kind", "to_id", "delivered_at"),
    Index("idx_messages_project_created", "project_id", "created_at"),
    Index("idx_messages_thread", "thread_id"),
)

api_session_tokens = Table(
    "api_session_tokens",
    metadata,
    Column("token_hash", Text, primary_key=True),  # sha256 hex
    Column("session_id", Text, nullable=False),
    Column("task_id", Text, nullable=True),
    # Soft ref (matches the agents.profile_id pattern).
    Column("project_id", Text, nullable=True),
    # Float epoch, matching every other timestamp in this schema.  The spec
    # table says DateTime; Float keeps dialect parity and house style.
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked_at", Float, nullable=True),
    # Trusted-scope flag. When True, ``check_command_scope`` allows any
    # command (still enforces project_id match). Currently set by
    # per-project supervisor sessions so the supervisor can run every
    # ``aq`` command on behalf of the operator; task sessions and other
    # workers stay on the narrow AGENT_COMMAND_SET.
    Column("elevated", Boolean, nullable=False, server_default=false()),
    Index("idx_api_session_tokens_session", "session_id"),
    Index("idx_api_session_tokens_expires", "expires_at"),
)

chat_analyzer_suggestions = Table(
    "chat_analyzer_suggestions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("channel_id", Integer, nullable=False),
    Column("suggestion_type", Text, nullable=False),
    Column("suggestion_text", Text, nullable=False),
    Column("suggestion_hash", Text, nullable=False),
    # Status values:
    #   pending       — proposed and awaiting user action
    #   accepted      — user clicked Accept (task created)
    #   dismissed     — user clicked Dismiss
    #   auto_executed — fired without confirmation
    #   suppressed    — never shown; ``suppressed_by`` records the gate
    Column("status", Text, nullable=False, server_default="pending"),
    Column("created_at", Float, nullable=False),
    Column("resolved_at", Float, nullable=True),
    Column("context_snapshot", Text, nullable=True),
    # Phase 8: which gate suppressed this suggestion (NULL when not
    # suppressed). Values are ``"confidence"``, ``"dedup"``,
    # ``"in_flight_active_task"``, ``"dismiss_cooldown"``, etc. The column
    # is nullable so the existing rows (and every non-suppressed row going
    # forward) need no backfill. Indexed for efficient
    # ``suppression_count_by_gate`` aggregation.
    Column("suppressed_by", Text, nullable=True),
    Index("idx_chat_analyzer_project", "project_id", "status"),
    Index("idx_chat_analyzer_hash", "suggestion_hash"),
    Index("idx_chat_analyzer_suppressed_by", "suppressed_by"),
)

archived_tasks = Table(
    "archived_tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("parent_task_id", Text, nullable=True),
    Column("repo_id", Text, nullable=True),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("status", Text, nullable=False),
    Column("verification_type", Text, nullable=False, server_default="auto_test"),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("assigned_agent_id", Text, nullable=True),
    Column("branch_name", Text, nullable=True),
    Column("resume_after", Float, nullable=True),
    Column("integration_mode", Text, nullable=True),
    Column("pr_url", Text, nullable=True),
    Column("plan_source", Text, nullable=True),
    Column("is_plan_subtask", Integer, nullable=False, server_default="0"),
    Column("task_type", Text, nullable=True),
    Column("profile_id", Text, nullable=True),
    Column("preferred_workspace_id", Text, nullable=True),
    Column("attachments", Text, nullable=True, server_default="[]"),
    Column("skip_verification", Integer, nullable=False, server_default="0"),
    Column("workflow_id", Text, nullable=True),
    Column("affinity_agent_id", Text, nullable=True),
    Column("affinity_reason", Text, nullable=True),
    Column("workspace_mode", Text, nullable=True),
    # Mirrors tasks.is_blocked so archiving stays lossless (work-graph §2.2).
    Column("is_blocked", Integer, nullable=False, server_default="0"),
    Column("dedup_key", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    Column("created_by_kind", Text, nullable=True),
    Column("created_by_id", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("archived_at", Float, nullable=False),
)

project_constraints = Table(
    "project_constraints",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), primary_key=True),
    Column("exclusive", Integer, nullable=False, server_default="0"),
    Column("max_agents_by_type", Text, nullable=False, server_default="{}"),
    Column("pause_scheduling", Integer, nullable=False, server_default="0"),
    Column("created_by", Text, nullable=True),
    Column("created_at", Float, nullable=False),
)

plugins = Table(
    "plugins",
    metadata,
    Column("id", Text, primary_key=True),
    Column("version", Text, nullable=False, server_default="0.0.0"),
    Column("source_url", Text, nullable=False, server_default=""),
    Column("source_rev", Text, nullable=False, server_default=""),
    Column("source_branch", Text, nullable=False, server_default=""),
    Column("install_path", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="installed"),
    Column("config", Text, nullable=False, server_default="{}"),
    Column("permissions", Text, nullable=False, server_default="[]"),
    Column("error_message", Text, nullable=True),
    Column("installed_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

plugin_data = Table(
    "plugin_data",
    metadata,
    Column("plugin_id", Text, ForeignKey("plugins.id"), nullable=False, primary_key=True),
    Column("key", Text, nullable=False, primary_key=True),
    Column("value", Text, nullable=False, server_default="{}"),
    Column("updated_at", Float, nullable=False),
    Index("idx_plugin_data_plugin_id", "plugin_id"),
)

# Playbook V2 durable storage.

playbook_artifacts = Table(
    "playbook_artifacts",
    metadata,
    Column("artifact_sha256", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="system"),
    Column("scope_identifier", Text, nullable=False, server_default=""),
    Column("schema_generation", Integer, nullable=False, server_default="2"),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("source_digest", Text, nullable=False),
    Column("contract_fingerprint", Text, nullable=False),
    Column("profile_fingerprint", Text, nullable=False, server_default=""),
    Column("compiler_build", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False, server_default="0"),
    Column("validation", Text, nullable=False, server_default="{}"),
    Column("compiled_at", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "scope IN ('system', 'project', 'agent_type', 'supervisor')",
        name="ck_playbook_artifacts_scope",
    ),
    Index("idx_playbook_artifacts_playbook", "playbook_id", "version"),
    Index("idx_playbook_artifacts_source", "source_digest"),
    Index("idx_playbook_artifacts_created", "created_at"),
)

playbook_activations = Table(
    "playbook_activations",
    metadata,
    Column("activation_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="system"),
    Column("scope_identifier", Text, nullable=False, server_default=""),
    Column(
        "active_artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("enabled", Boolean, nullable=False, server_default=false()),
    Column("health", Text, nullable=False, server_default="disabled"),
    Column("reasons", Text, nullable=False, server_default="[]"),
    Column("activated_at", Float, nullable=True),
    Column("activated_by", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "health IN ('ready', 'question_required', 'invalid', 'disabled', "
        "'stale_contract', 'unavailable')",
        name="ck_playbook_activations_health",
    ),
    UniqueConstraint(
        "playbook_id", "scope", "scope_identifier",
        name="uq_playbook_activations_scope",
    ),
    Index("idx_playbook_activations_health", "health"),
)

playbook_v2_runs = Table(
    "playbook_v2_runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column(
        "artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("rule_id", Text, nullable=False),
    Column("lifecycle", Text, nullable=False, server_default="running"),
    Column("mode", Text, nullable=False, server_default="live"),
    Column("current_step_id", Text, nullable=True),
    Column("snapshot_version", Integer, nullable=False, server_default="0"),
    Column("snapshot", Text, nullable=False, server_default="{}"),
    Column("snapshot_bytes", Integer, nullable=False, server_default="0"),
    Column("event_type", Text, nullable=False, server_default=""),
    Column("event_id", Text, nullable=True),
    Column("dispatch_id", Text, nullable=True),
    Column("parent_run_id", Text, nullable=True),
    Column("parent_step_id", Text, nullable=True),
    Column("deadline_at", Float, nullable=True),
    Column("cancel_requested_at", Float, nullable=True),
    Column("cancel_requested_by", Text, nullable=True),
    Column("cancel_reason", Text, nullable=True),
    Column("summary", Text, nullable=False, server_default=""),
    Column("error", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    CheckConstraint(
        "lifecycle IN ('running', 'paused', 'cancelling', 'completed', "
        "'failed', 'timed_out', 'cancelled')",
        name="ck_playbook_v2_runs_lifecycle",
    ),
    CheckConstraint(
        "mode IN ('live', 'dry_run', 'shadow')", name="ck_playbook_v2_runs_mode"
    ),
    Index(
        "uq_playbook_v2_runs_dispatch_rule",
        "playbook_id", "dispatch_id", "rule_id",
        unique=True,
        sqlite_where=text("dispatch_id IS NOT NULL"),
        postgresql_where=text("dispatch_id IS NOT NULL"),
    ),
    Index("idx_playbook_v2_runs_playbook", "playbook_id", "started_at"),
    Index("idx_playbook_v2_runs_lifecycle", "lifecycle"),
    Index("idx_playbook_v2_runs_artifact", "artifact_sha256"),
)

playbook_step_receipts = Table(
    "playbook_step_receipts",
    metadata,
    Column("receipt_id", Text, primary_key=True),
    Column(
        "run_id", Text,
        ForeignKey("playbook_v2_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("artifact_sha256", Text, nullable=False),
    Column("rule_id", Text, nullable=False),
    Column("step_id", Text, nullable=False),
    Column("step_kind", Text, nullable=False),
    Column("receipt_kind", Text, nullable=False, server_default="step"),
    Column("turn_index", Integer, nullable=False, server_default="-1"),
    Column("operator_decision_id", Text, nullable=True),
    Column("iteration", Integer, nullable=False, server_default="-1"),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("idempotency_key", Text, nullable=False),
    Column("snapshot_version", Integer, nullable=False, server_default="0"),
    Column("contract_fingerprint", Text, nullable=False, server_default=""),
    Column("principal", Text, nullable=False, server_default="{}"),
    Column("inputs", Text, nullable=False, server_default="{}"),
    Column("result", Text, nullable=False, server_default="{}"),
    Column("outcome", Text, nullable=False),
    Column("selected_transition", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=True),
    Column("wait_id", Text, nullable=True),
    Column("timed_out", Boolean, nullable=False, server_default=false()),
    Column("cancelled_at", Float, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    CheckConstraint(
        "outcome IN ('success', 'failure', 'skipped', 'timeout', 'cancelled', "
        "'operator_decision_required', 'started')",
        name="ck_playbook_step_receipts_outcome",
    ),
    CheckConstraint(
        "receipt_kind IN ('step', 'tool_turn', 'llm_call', 'interrupted', "
        "'operator_decision', 'attempt_start')",
        name="ck_playbook_step_receipts_kind",
    ),
    CheckConstraint(
        "(receipt_kind = 'step' AND turn_index = -1) OR "
        "(receipt_kind <> 'step' AND turn_index >= 0)",
        name="ck_playbook_step_receipts_turn_index",
    ),
    CheckConstraint(
        "(receipt_kind IN ('interrupted', 'operator_decision') AND "
        "operator_decision_id IS NOT NULL) OR "
        "(receipt_kind NOT IN ('interrupted', 'operator_decision') AND "
        "operator_decision_id IS NULL)",
        name="ck_playbook_step_receipts_decision_ref",
    ),
    UniqueConstraint(
        "run_id", "step_id", "iteration", "attempt", "turn_index", "receipt_kind",
        name="uq_playbook_step_receipts_boundary",
    ),
    Index("idx_playbook_step_receipts_run", "run_id", "started_at"),
    Index("idx_playbook_step_receipts_key", "idempotency_key"),
    Index(
        "idx_playbook_step_receipts_turn",
        "run_id", "step_id", "iteration", "attempt", "turn_index",
    ),
)

playbook_waits = Table(
    "playbook_waits",
    metadata,
    Column("wait_id", Text, primary_key=True),
    Column(
        "run_id", Text,
        ForeignKey("playbook_v2_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("step_id", Text, nullable=False),
    Column("iteration", Integer, nullable=False, server_default="-1"),
    Column("kind", Text, nullable=False),
    Column("event_type", Text, nullable=False, server_default=""),
    Column("correlation_key", Text, nullable=False, server_default=""),
    Column("match", Text, nullable=False, server_default="{}"),
    Column("deadline_at", Float, nullable=True),
    Column("snapshot_version", Integer, nullable=False),
    Column("state", Text, nullable=False, server_default="active"),
    Column("claimed_event_id", Text, nullable=True),
    Column("claimed_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "kind IN ('event', 'timer', 'human', 'agent_task')",
        name="ck_playbook_waits_kind",
    ),
    CheckConstraint(
        "state IN ('active', 'claimed', 'expired', 'cleared')",
        name="ck_playbook_waits_state",
    ),
    Index(
        "uq_playbook_waits_active_step",
        "run_id", "step_id", "iteration",
        unique=True,
        sqlite_where=text("state = 'active'"),
        postgresql_where=text("state = 'active'"),
    ),
    Index("idx_playbook_waits_match", "state", "event_type"),
    Index("idx_playbook_waits_deadline", "state", "deadline_at"),
)

playbook_pending_events = Table(
    "playbook_pending_events",
    metadata,
    Column("pending_event_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("activation_id", Text, nullable=True),
    Column(
        "artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("scope", Text, nullable=False, server_default="system"),
    Column("scope_identifier", Text, nullable=False, server_default=""),
    Column("event_type", Text, nullable=False),
    Column("event", Text, nullable=False, server_default="{}"),
    Column("event_id", Text, nullable=True),
    Column("dedup_key", Text, nullable=False, server_default=""),
    Column("reason", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=True),
    Column("received_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("dispatch_claim_token", Text, nullable=True),
    Column("dispatch_claimed_by", Text, nullable=True),
    Column("dispatch_claimed_at", Float, nullable=True),
    Column("resolved_at", Float, nullable=True),
    Column("resolved_by", Text, nullable=True),
    Column("resolution", Text, nullable=True),
    # Why the row was resolved, in the resolver's own words: an operator's
    # discard justification, or the policy that dropped or expired it.  The
    # resolution vocabulary says *what* happened; this says why, which is the
    # difference between an audit trail and a counter.
    Column("resolution_reason", Text, nullable=True),
    # Correctness-critical integration events are released only by successful
    # playbook dispatch. Generic quota, expiry, and operator-discard paths may
    # not resolve them.
    Column("protected", Boolean, nullable=False, server_default=false()),
    CheckConstraint(
        "reason IN ('stale_contract', 'invalid_artifact', 'disabled', "
        "'unavailable', 'question_required', 'wait_registration')",
        name="ck_playbook_pending_events_reason",
    ),
    CheckConstraint(
        "resolution IS NULL OR resolution IN ('dispatched', 'discarded', 'expired')",
        name="ck_playbook_pending_events_resolution",
    ),
    CheckConstraint(
        "(resolved_at IS NULL AND ((dispatch_claim_token IS NULL AND "
        "dispatch_claimed_by IS NULL AND dispatch_claimed_at IS NULL) OR "
        "(dispatch_claim_token IS NOT NULL AND dispatch_claimed_by IS NOT NULL AND "
        "dispatch_claimed_at IS NOT NULL))) OR (resolved_at IS NOT NULL AND "
        "dispatch_claim_token IS NULL AND dispatch_claimed_by IS NULL AND "
        "dispatch_claimed_at IS NULL)",
        name="ck_playbook_pending_events_dispatch_claim",
    ),
    Index(
        "uq_playbook_pending_events_dedup",
        "playbook_id", "dedup_key",
        unique=True,
        sqlite_where=text("resolved_at IS NULL AND dedup_key <> ''"),
        postgresql_where=text("resolved_at IS NULL AND dedup_key <> ''"),
    ),
    Index("idx_playbook_pending_events_playbook", "playbook_id", "received_at"),
    Index("idx_playbook_pending_events_expiry", "expires_at"),
)

task_assignment_routes = Table(
    "task_assignment_routes",
    metadata,
    Column(
        "task_id",
        Text,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("input_hash", Text, nullable=False),
    Column("task_updated_at", Float, nullable=False),
    Column("options_hash", Text, nullable=False),
    Column("intelligence_class", Text, nullable=False),
    Column("provider", Text, nullable=True),
    Column("playbook_id", Text, nullable=False),
    Column("playbook_version", Integer, nullable=False),
    Column(
        "playbook_run_id",
        Text,
        ForeignKey("playbook_v2_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("reason", Text, nullable=False),
    Column("decided_at", Float, nullable=False),
    Index("idx_task_assignment_routes_project", "project_id"),
)

workflows = Table(
    "workflows",
    metadata,
    Column("workflow_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("playbook_run_id", Text, ForeignKey("playbook_v2_runs.run_id"), nullable=False),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column(
        "status",
        Text,
        nullable=False,
        server_default="running",
    ),
    Column("current_stage", Text, nullable=True),
    Column("task_ids", Text, nullable=False, server_default="[]"),
    Column("agent_affinity", Text, nullable=False, server_default="{}"),
    Column("stages", Text, nullable=False, server_default="[]"),
    Column("created_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    CheckConstraint(
        "status IN ('running', 'paused', 'completed', 'failed')",
        name="ck_workflows_status",
    ),
    Index("idx_workflows_playbook_id", "playbook_id"),
    Index("idx_workflows_project_id", "project_id"),
    Index("idx_workflows_status", "status"),
    Index("idx_workflows_playbook_run_id", "playbook_run_id"),
)


# Durable completed-turn questions. Provenance is retained as text instead of
# cascading foreign keys so stopped/deleted session history stays auditable.
agent_questions = Table(
    "agent_questions", metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("session_name", Text, nullable=False),
    Column("instance_token", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("project_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("turn_id", Text, nullable=False),
    Column("claim_epoch", Integer, nullable=False),
    Column("question", Text, nullable=False),
    Column("requires_human", Boolean, nullable=False),
    Column("state", Text, nullable=False),
    Column("answer", Text),
    Column("answered_by", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("source_ts", Float, nullable=False),
    Column("discord_channel_id", Text),
    Column("discord_message_id", Text),
    Column("supervisor_routed_at", Float),
    Column("notification_next_at", Float, nullable=False, server_default="0"),
    Column("notification_attempts", Integer, nullable=False, server_default="0"),
    Column("delivery_token", Text),
    Column("delivery_lease_until", Float),
    Column("delivered_at", Float),
    Column("reason", Text),
    UniqueConstraint("session_id", "instance_token", "task_id", "claim_epoch", "turn_id", name="uq_agent_question_turn"),
    CheckConstraint("state IN ('supervisor','human','answered','delivered','resolved','stale')", name="ck_agent_question_state"),
    Index("idx_agent_questions_pending", "state", "created_at"),
    Index("idx_agent_questions_session", "session_id", "instance_token"),
)

# Authoritative native subagent lifecycle, delivered by the harness's own
# SubagentStart / SubagentStop hooks (``aq subagent event --hook-json``).
# Append-only: an event is a fact about a moment, and the derived "how many
# are running" is a fold over the facts rather than a mutable counter that a
# duplicate delivery or a lost Stop could corrupt.
#
# ``id`` is a deterministic digest of (session_id, event, subagent_id) so a
# re-delivered hook collapses onto the row it already wrote.  Provenance is
# soft text, like ``task_session_attempts``: the events outlive the session
# row's deletion and stay readable as history.
subagent_events = Table(
    "subagent_events", metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("harness", Text, nullable=False),
    Column("project_id", Text, nullable=True),
    Column("task_id", Text, nullable=True),
    # The harness's own id for the child agent.  Both Claude Code and Codex
    # send one on Start *and* Stop, which is what makes the pairing exact.
    Column("subagent_id", Text, nullable=False),
    Column("agent_type", Text, nullable=True),
    Column("turn_id", Text, nullable=True),
    # "start" | "stop" -- the two halves of one child's lifetime.
    Column("event", Text, nullable=False),
    #: When the harness fired the hook (daemon clock, at receipt).
    Column("occurred_at", Float, nullable=False),
    CheckConstraint("event IN ('start','stop')", name="ck_subagent_events_event"),
    Index("idx_subagent_events_session", "session_id", "event"),
    Index("idx_subagent_events_occurred", "occurred_at"),
)

# How far the transcript watcher has consumed each on-disk transcript file.
#
# Keyed by the transcript *path*, deliberately not by session id.  A session
# that dies and is relaunched onto the same workspace adopts the same
# transcript file, and the watcher's in-process offset starts at 0 for the
# new session id -- so the whole history was re-emitted to Discord and, worse,
# re-charged to the token ledger under the new id.  Three successive
# supervisor incarnations each carried an identical 133 rows for the same
# window.  The high-water mark has to outlive the session that set it, which
# means it has to be keyed by the thing that actually persists: the file.
#
# ``last_entry_uuid`` is the second half of the dedupe key from the same
# defect report: the newest assistant entry whose usage was charged, so a
# reader that resumes exactly on a record boundary cannot re-charge it.
transcript_checkpoints = Table(
    "transcript_checkpoints", metadata,
    Column("transcript_path", Text, primary_key=True),
    Column("byte_offset", Integer, nullable=False, default=0),
    Column("last_entry_uuid", Text, nullable=True),
    # Soft provenance: which session last advanced the mark.  Diagnostic
    # only -- nothing reads it to decide whether to advance.
    Column("session_id", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
)

# ---------------------------------------------------------------------------
# Fleet metrics time series (dashboard Metrics tab).
#
# One row per (resolution, bucket).  ``payload`` is the JSON sample body
# rather than a wide column set: the metric surface is dict-shaped (counts
# per harness, per profile, per model) and still growing, and a schema
# migration per new series would make the sampler expensive to extend.
#
# ``UNIQUE(resolution, bucket_ts)`` is what makes the writer idempotent —
# a tick that fires twice for the same second, or a roll-up re-run after a
# restart, updates the bucket it already wrote instead of duplicating it.
# ---------------------------------------------------------------------------
metrics_samples = Table(
    "metrics_samples",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # "1s" | "1m" | "1h" -- the retention tier as well as the step.
    Column("resolution", Text, nullable=False),
    # Sample time floored to the resolution, so buckets line up across
    # restarts and two daemons cannot interleave half-seconds.
    Column("bucket_ts", Float, nullable=False),
    Column("payload", Text, nullable=False),
    UniqueConstraint("resolution", "bucket_ts", name="uq_metrics_samples_bucket"),
    Index("idx_metrics_samples_res_ts", "resolution", "bucket_ts"),
)

message_discord_receipts = Table(
    "message_discord_receipts", metadata,
    Column("message_id", Text, primary_key=True),
    Column("discord_channel_id", Text),
    Column("discord_message_id", Text),
)

# ---------------------------------------------------------------------------
# Hierarchical delivery and integration trains.
#
# These records deliberately retain task identifiers as text rather than task
# foreign keys.  A delivery receipt is audit evidence: deleting or archiving a
# task must not delete the proof that its reviewed change reached a branch.
# JSON is confined to frozen evidence and policy/artifact snapshots; mutable
# progress remains normalized scalar state.
# ---------------------------------------------------------------------------

task_integration_checkpoints = Table(
    "task_integration_checkpoints",
    metadata,
    Column("task_id", Text, primary_key=True),
    Column("repository_id", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("generation", Integer, nullable=False, server_default="0"),
    Column("checkpoint_sha", Text, nullable=True),
    Column("verified_sha", Text, nullable=True),
    Column("verified_generation", Integer, nullable=True),
    Column("state", Text, nullable=False, server_default="working"),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("last_transition_id", Text, nullable=True),
    Column("playbook_activation_id", Text, nullable=True),
    Column("branch_owner_id", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("generation >= 0", name="ck_task_integration_checkpoints_generation"),
    CheckConstraint("version >= 0", name="ck_task_integration_checkpoints_version"),
    CheckConstraint(
        "verified_generation IS NULL OR verified_generation >= 0",
        name="ck_task_integration_checkpoints_verified_generation",
    ),
    CheckConstraint(
        "state IN ('working', 'awaiting_children', 'integration_ready', 'verifying')",
        name="ck_task_integration_checkpoints_state",
    ),
)

task_branch_origins = Table(
    "task_branch_origins",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, nullable=False),
    Column("repository_id", Text, nullable=False),
    Column("parent_task_id", Text, nullable=True),
    Column("parent_repository_id", Text, nullable=True),
    Column("parent_ref", Text, nullable=True),
    Column("base_sha", Text, nullable=False),
    Column("creation_generation", Integer, nullable=False),
    Column("reserved", Boolean, nullable=False, server_default=false()),
    Column("materialized", Boolean, nullable=False, server_default=false()),
    Column("retired_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("materialized_at", Float, nullable=True),
    CheckConstraint("creation_generation >= 0", name="ck_task_branch_origins_generation"),
    CheckConstraint(
        "materialized = false OR reserved = true",
        name="ck_task_branch_origins_materialized_reserved",
    ),
    Index(
        "uq_task_branch_origins_live_task_repo",
        "task_id",
        "repository_id",
        unique=True,
        sqlite_where=text("retired_at IS NULL"),
        postgresql_where=text("retired_at IS NULL"),
    ),
)

integration_branch_owners = Table(
    "integration_branch_owners",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repository_id", Text, nullable=False),
    Column("ref", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("owner_role", Text, nullable=False),
    Column("fence_token", Integer, nullable=False),
    Column("handoff_state", Text, nullable=False, server_default="reserved"),
    Column("session_id", Text, nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("confirmed_workspace_id", Text, nullable=True),
    Column("expires_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("repository_id", "ref", name="uq_integration_branch_owners_ref"),
    CheckConstraint("fence_token >= 0", name="ck_integration_branch_owners_fence"),
    CheckConstraint(
        "handoff_state IN ('reserved', 'attached', 'handoff_pending', 'released')",
        name="ck_integration_branch_owners_handoff_state",
    ),
)

integration_promotion_intents = Table(
    "integration_promotion_intents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("domain_key", Text, nullable=False),
    Column("receipt_id", Text, nullable=False),
    Column("source_task_id", Text, nullable=True),
    Column("source_head", Text, nullable=False),
    Column("source_base", Text, nullable=False),
    Column("repository_id", Text, nullable=False),
    Column("target_branch", Text, nullable=False),
    Column("expected_target", Text, nullable=False),
    Column("prepared_sha", Text, nullable=True),
    Column("recovery_ref", Text, nullable=True),
    Column("fence_owner_id", Text, nullable=False),
    Column("fence_token", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("remote_evidence", JSON, nullable=True),
    Column("committed_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("domain_key", name="uq_integration_promotion_intents_domain_key"),
    CheckConstraint("fence_token >= 0", name="ck_integration_promotion_intents_fence"),
    CheckConstraint(
        "state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', 'conflict')",
        name="ck_integration_promotion_intents_state",
    ),
    CheckConstraint(
        "(state <> 'committed' OR (committed_at IS NOT NULL AND remote_evidence IS NOT NULL)) "
        "AND (committed_at IS NULL OR remote_evidence IS NOT NULL)",
        name="ck_integration_promotion_intents_committed_evidence",
    ),
)

task_delivery_receipts = Table(
    "task_delivery_receipts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("domain_key", Text, nullable=False),
    Column("source_task_id", Text, nullable=True),
    Column("target_task_id", Text, nullable=True),
    Column("repository_id", Text, nullable=False),
    Column("target_branch", Text, nullable=False),
    Column("workspace_kind", Text, nullable=True),
    Column("source_pr", Text, nullable=True),
    Column("reviewed_head_sha", Text, nullable=True),
    Column("reviewed_tree_sha", Text, nullable=True),
    Column("before_sha", Text, nullable=True),
    Column("squash_sha", Text, nullable=True),
    Column("after_sha", Text, nullable=True),
    Column("review_evidence", JSON, nullable=True),
    Column("verification_evidence", JSON, nullable=True),
    Column("resolution_evidence", JSON, nullable=True),
    Column("batch_id", Text, nullable=True),
    Column("member_ordinal", Integer, nullable=True),
    Column("candidate_revision", Integer, nullable=True),
    Column("disposition", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("domain_key", name="uq_task_delivery_receipts_domain_key"),
    CheckConstraint(
        "disposition IN ('code', 'noop', 'ineligible', 'skipped', 'failed')",
        name="ck_task_delivery_receipts_disposition",
    ),
    CheckConstraint(
        "disposition = 'code' OR resolution_evidence IS NOT NULL",
        name="ck_task_delivery_receipts_disposition_evidence",
    ),
    CheckConstraint(
        "member_ordinal IS NULL OR member_ordinal >= 0",
        name="ck_task_delivery_receipts_member_ordinal",
    ),
    CheckConstraint(
        "candidate_revision IS NULL OR candidate_revision >= 0",
        name="ck_task_delivery_receipts_candidate_revision",
    ),
    Index("idx_task_delivery_receipts_source", "source_task_id", "repository_id"),
)

integration_batches = Table(
    "integration_batches",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("repository_id", Text, nullable=False),
    Column("trigger", Text, nullable=True),
    Column("source_manifest_digest", Text, nullable=False),
    Column("base_sha", Text, nullable=True),
    Column("lifecycle", Text, nullable=False),
    Column("current_revision", Integer, nullable=False, server_default="0"),
    Column("integration_branch", Text, nullable=True),
    Column("pr_url", Text, nullable=True),
    Column("repair_stage_ordinal", Integer, nullable=True),
    Column("tested_candidate_sha", Text, nullable=True),
    Column("ci_evidence_id", Text, nullable=True),
    Column("final_main_sha", Text, nullable=True),
    Column("human_abort_reason", Text, nullable=True),
    Column("policy_snapshot", JSON, nullable=False),
    Column("artifact_snapshot", JSON, nullable=False),
    Column("cleanup_state", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("current_revision >= 0", name="ck_integration_batches_revision"),
    CheckConstraint(
        "repair_stage_ordinal IS NULL OR repair_stage_ordinal >= 0",
        name="ck_integration_batches_repair_stage",
    ),
    CheckConstraint(
        "lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', "
        "'human_blocked', 'promoting', 'cleanup_pending', 'promoted', 'aborted', 'failed')",
        name="ck_integration_batches_lifecycle",
    ),
    Index(
        "uq_integration_batches_active_project",
        "project_id",
        unique=True,
        sqlite_where=text(
            "lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', "
            "'human_blocked', 'promoting', 'cleanup_pending')"
        ),
        postgresql_where=text(
            "lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', "
            "'human_blocked', 'promoting', 'cleanup_pending')"
        ),
    ),
)

integration_batch_members = Table(
    "integration_batch_members",
    metadata,
    Column("batch_id", Text, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("task_id", Text, nullable=False),
    Column("pr_url", Text, nullable=True),
    Column("repository_id", Text, nullable=False),
    Column("source_base_sha", Text, nullable=False),
    Column("reviewed_head_sha", Text, nullable=False),
    Column("reviewed_tree_sha", Text, nullable=False),
    Column("review_evidence", JSON, nullable=False),
    UniqueConstraint("batch_id", "task_id", name="uq_integration_batch_members_task"),
    CheckConstraint("ordinal >= 0", name="ck_integration_batch_members_ordinal"),
)

integration_candidate_revisions = Table(
    "integration_candidate_revisions",
    metadata,
    Column("batch_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("construction_base_sha", Text, nullable=False),
    Column("next_member_ordinal", Integer, nullable=False, server_default="0"),
    Column("repair_parent_revision", Integer, nullable=True),
    Column("head_sha", Text, nullable=True),
    Column("ci_evidence_id", Text, nullable=True),
    Column("state", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("revision >= 0", name="ck_integration_candidate_revisions_revision"),
    CheckConstraint(
        "next_member_ordinal >= 0", name="ck_integration_candidate_revisions_next_member"
    ),
    CheckConstraint(
        "repair_parent_revision IS NULL OR repair_parent_revision >= 0",
        name="ck_integration_candidate_revisions_repair_parent",
    ),
    CheckConstraint(
        "state IN ('constructing', 'built', 'testing', 'green', 'red', 'superseded', 'promoted')",
        name="ck_integration_candidate_revisions_state",
    ),
)

integration_candidate_member_results = Table(
    "integration_candidate_member_results",
    metadata,
    Column("batch_id", Text, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("member_ordinal", Integer, primary_key=True),
    Column("input_head_sha", Text, nullable=False),
    Column("input_tree_sha", Text, nullable=False),
    Column("generated_squash_sha", Text, nullable=True),
    Column("result", Text, nullable=False),
    Column("conflict_evidence", JSON, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "revision >= 0", name="ck_integration_candidate_member_results_revision"
    ),
    CheckConstraint(
        "member_ordinal >= 0", name="ck_integration_candidate_member_results_member_ordinal"
    ),
    CheckConstraint(
        "result IN ('pending', 'applied', 'conflict', 'skipped')",
        name="ck_integration_candidate_member_results_result",
    ),
    CheckConstraint(
        "result <> 'applied' OR generated_squash_sha IS NOT NULL",
        name="ck_integration_candidate_member_results_applied_sha",
    ),
    ForeignKeyConstraint(
        ["batch_id", "revision"],
        ["integration_candidate_revisions.batch_id", "integration_candidate_revisions.revision"],
        name="fk_integration_candidate_member_results_revision",
    ),
    ForeignKeyConstraint(
        ["batch_id", "member_ordinal"],
        ["integration_batch_members.batch_id", "integration_batch_members.ordinal"],
        name="fk_integration_candidate_member_results_member",
    ),
)

integration_repair_operations = Table(
    "integration_repair_operations",
    metadata,
    Column("id", Text, primary_key=True),
    Column("target_kind", Text, nullable=False),
    Column("batch_id", Text, nullable=True),
    Column("parent_task_id", Text, nullable=True),
    Column("episode_id", Text, nullable=False),
    Column("active_stage", Integer, nullable=False, server_default="0"),
    Column("state", Text, nullable=False),
    Column("policy_snapshot", JSON, nullable=False),
    Column("artifact_snapshot", JSON, nullable=False),
    Column("required_check_version", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("active_stage >= 0", name="ck_integration_repair_operations_active_stage"),
    CheckConstraint(
        "(target_kind = 'batch' AND batch_id IS NOT NULL AND parent_task_id IS NULL) OR "
        "(target_kind = 'parent' AND parent_task_id IS NOT NULL AND batch_id IS NULL)",
        name="ck_integration_repair_operations_target",
    ),
    CheckConstraint(
        "state IN ('active', 'escalated', 'human_required', 'completed', 'cancelled')",
        name="ck_integration_repair_operations_state",
    ),
    Index(
        "uq_integration_repair_operations_active_batch",
        "batch_id",
        unique=True,
        sqlite_where=text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"),
        postgresql_where=text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"),
    ),
    Index(
        "uq_integration_repair_operations_active_parent",
        "parent_task_id",
        unique=True,
        sqlite_where=text(
            "parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"
        ),
        postgresql_where=text(
            "parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"
        ),
    ),
)

integration_repair_stages = Table(
    "integration_repair_stages",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("policy", JSON, nullable=False),
    Column("intelligence_class", Text, nullable=False),
    Column("profile_id", Text, nullable=True),
    Column("repair_task_id", Text, nullable=True),
    Column("starting_sha", Text, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("deadline_at", Float, nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("dossier", JSON, nullable=True),
    Column("state", Text, nullable=False),
    Column("completed_at", Float, nullable=True),
    CheckConstraint("ordinal IN (0, 1)", name="ck_integration_repair_stages_ordinal"),
    CheckConstraint("attempts >= 0", name="ck_integration_repair_stages_attempts"),
    CheckConstraint(
        "state IN ('pending', 'active', 'passed', 'failed', 'expired', 'cancelled')",
        name="ck_integration_repair_stages_state",
    ),
)

integration_check_evidence = Table(
    "integration_check_evidence",
    metadata,
    Column("id", Text, primary_key=True),
    Column("operation_id", Text, nullable=True),
    Column("batch_id", Text, nullable=True),
    Column("candidate_revision", Integer, nullable=True),
    Column("parent_task_id", Text, nullable=True),
    Column("parent_generation", Integer, nullable=True),
    Column("parent_head_sha", Text, nullable=True),
    Column("producer_id", Text, nullable=False),
    Column("workflow_id", Text, nullable=False),
    Column("run_id", Text, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("required_check_version", Text, nullable=False),
    Column("checks", JSON, nullable=False),
    Column("conclusion", Text, nullable=False),
    Column("classification", Text, nullable=False),
    Column("observed_at", Float, nullable=False),
    UniqueConstraint(
        "producer_id", "run_id", "attempt", "required_check_version",
        name="uq_integration_check_evidence_producer_run_attempt_checks",
    ),
    CheckConstraint("attempt >= 0", name="ck_integration_check_evidence_attempt"),
    CheckConstraint(
        "(batch_id IS NOT NULL AND candidate_revision IS NOT NULL AND parent_task_id IS NULL "
        "AND parent_generation IS NULL AND parent_head_sha IS NULL) OR "
        "(batch_id IS NULL AND candidate_revision IS NULL AND parent_task_id IS NOT NULL "
        "AND parent_generation IS NOT NULL AND parent_head_sha IS NOT NULL)",
        name="ck_integration_check_evidence_subject",
    ),
    CheckConstraint(
        "conclusion IN ('success', 'failure', 'pending', 'cancelled', 'inconclusive')",
        name="ck_integration_check_evidence_conclusion",
    ),
)

project_integration_schedules = Table(
    "project_integration_schedules",
    metadata,
    Column("project_id", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default=false()),
    Column("interval_seconds", Integer, nullable=False),
    Column("next_due_at", Float, nullable=False),
    Column("last_observed_window", Float, nullable=True),
    Column("request_sequence", Integer, nullable=False, server_default="0"),
    Column("outstanding_request_id", Text, nullable=True),
    Column("outstanding_trigger", Text, nullable=True),
    Column("outstanding_requested_at", Float, nullable=True),
    Column("last_completed_sweep_at", Float, nullable=True),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("interval_seconds > 0", name="ck_project_integration_schedules_interval"),
    CheckConstraint("request_sequence >= 0", name="ck_project_integration_schedules_sequence"),
    CheckConstraint(
        "(outstanding_request_id IS NULL AND outstanding_trigger IS NULL "
        "AND outstanding_requested_at IS NULL) OR "
        "(outstanding_request_id IS NOT NULL AND outstanding_trigger IS NOT NULL "
        "AND outstanding_requested_at IS NOT NULL)",
        name="ck_project_integration_schedules_outstanding_request",
    ),
)

project_integration_leases = Table(
    "project_integration_leases",
    metadata,
    Column("project_id", Text, primary_key=True),
    Column("repository_id", Text, nullable=False),
    Column("batch_id", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("fence_token", Integer, nullable=False),
    Column("heartbeat_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    CheckConstraint("fence_token >= 0", name="ck_project_integration_leases_fence"),
    CheckConstraint("expires_at >= heartbeat_at", name="ck_project_integration_leases_expiry"),
)

integration_outbox = Table(
    "integration_outbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("dedup_key", Text, nullable=False),
    Column("project_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("destination_manifest", JSON, nullable=True),
    Column("acceptance_cursor", Integer, nullable=False, server_default="0"),
    Column("available_at", Float, nullable=False),
    Column("delivered_at", Float, nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("dedup_key", name="uq_integration_outbox_dedup_key"),
    CheckConstraint("attempts >= 0", name="ck_integration_outbox_attempts"),
    CheckConstraint(
        "acceptance_cursor >= 0", name="ck_integration_outbox_acceptance_cursor"
    ),
    Index(
        "idx_integration_outbox_pending_available",
        "available_at",
        sqlite_where=text("delivered_at IS NULL"),
        postgresql_where=text("delivered_at IS NULL"),
    ),
)

integration_outbox_artifact_pins = Table(
    "integration_outbox_artifact_pins",
    metadata,
    Column(
        "event_id",
        Text,
        ForeignKey("integration_outbox.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Index("idx_integration_outbox_artifact_pins_sha", "artifact_sha256"),
)
