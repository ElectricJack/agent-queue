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
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
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
    Column("status", Text, nullable=False, server_default="'ACTIVE'"),
    Column("total_tokens_used", Integer, nullable=False, server_default="0"),
    Column("budget_limit", Integer, nullable=True),
    Column("workspace_path", Text, nullable=True),
    Column("discord_channel_id", Text, nullable=True),
    Column("discord_control_channel_id", Text, nullable=True),
    Column("repo_url", Text, nullable=True, server_default="''"),
    Column("repo_default_branch", Text, nullable=True, server_default="'main'"),
    Column("default_profile_id", Text, ForeignKey("agent_profiles.id"), nullable=True),
    Column("created_at", Float, nullable=False),
)

repos = Table(
    "repos",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("url", Text, nullable=False),
    Column("default_branch", Text, nullable=False, server_default="'main'"),
    Column("checkout_base_path", Text, nullable=False),
    Column("source_type", Text, nullable=False, server_default="'clone'"),
    Column("source_path", Text, nullable=False, server_default="''"),
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
    Column("status", Text, nullable=False, server_default="'DEFINED'"),
    Column("verification_type", Text, nullable=False, server_default="'auto_test'"),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("assigned_agent_id", Text, ForeignKey("agents.id"), nullable=True),
    Column("branch_name", Text, nullable=True),
    Column("resume_after", Float, nullable=True),
    Column("requires_approval", Integer, nullable=False, server_default="0"),
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
    Column("attachments", Text, nullable=True, server_default="'[]'"),
    Column("auto_approve_plan", Integer, nullable=False, server_default="0"),
    Column("skip_verification", Integer, nullable=False, server_default="0"),
    Column("workflow_id", Text, ForeignKey("workflows.workflow_id", use_alter=True), nullable=True),
    Column("affinity_agent_id", Text, nullable=True),
    Column("affinity_reason", Text, nullable=True),
    Column("workspace_mode", Text, nullable=True),
    # Persisted blocked-state projection (work-graph spec §2.2).  Integer
    # 0/1 matches the table's existing flag style (e.g. requires_approval).
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
    CheckConstraint("task_id != depends_on_task_id"),
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

task_context = Table(
    "task_context",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("type", Text, nullable=False),
    Column("label", Text, nullable=True),
    Column("content", Text, nullable=False),
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
    Column("state", Text, nullable=False, server_default="'IDLE'"),
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
    Column("timestamp", Float, nullable=False),
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
    Column("summary", Text, nullable=False, server_default="''"),
    Column("files_changed", Text, nullable=False, server_default="'[]'"),
    Column("error_message", Text, nullable=True),
    Column("tokens_used", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
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
    Column("source_type", Text, nullable=False, server_default="'clone'"),
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
    Column("description", Text, nullable=False, server_default="''"),
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
    Column("description", Text, nullable=False, server_default="''"),
    Column("model", Text, nullable=False, server_default="''"),
    Column("permission_mode", Text, nullable=False, server_default="''"),
    Column("allowed_tools", Text, nullable=False, server_default="'[]'"),
    Column("mcp_servers", Text, nullable=False, server_default="'{}'"),
    Column("system_prompt_suffix", Text, nullable=False, server_default="''"),
    Column("install", Text, nullable=False, server_default="'{}'"),
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
    Column("runtime", Text, nullable=False, server_default="''"),
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
    Column("default_class", Text, nullable=False, server_default="''"),
    Column("needs_workspace", Boolean, nullable=False, server_default=true()),
    # T3 reviewer follow-up: when True, workspace acquisition refuses to
    # take an exclusive lock on a mutable ``project-repo`` kind so a
    # read-only profile cannot silently mutate a repo it was never meant
    # to write to.
    Column(
        "read_only",
        Boolean,
        nullable=False,
        server_default=text("0"),
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
    Column("llm_provider", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("intelligence_class", Text, nullable=True),
    Index("idx_sessions_agent", "agent_id", "state"),
    Index("idx_sessions_task_id", "task_id"),
    Index("idx_sessions_state", "state"),
    Index("idx_sessions_name", "name"),
    Index("idx_sessions_pool", "lifecycle", "project_id", "profile_id", "state"),
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
    Column("elevated", Boolean, nullable=False, server_default=text("0")),
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
    Column("status", Text, nullable=False, server_default="'pending'"),
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
    Column("verification_type", Text, nullable=False, server_default="'auto_test'"),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("assigned_agent_id", Text, nullable=True),
    Column("branch_name", Text, nullable=True),
    Column("resume_after", Float, nullable=True),
    Column("requires_approval", Integer, nullable=False, server_default="0"),
    Column("pr_url", Text, nullable=True),
    Column("plan_source", Text, nullable=True),
    Column("is_plan_subtask", Integer, nullable=False, server_default="0"),
    Column("task_type", Text, nullable=True),
    Column("profile_id", Text, nullable=True),
    Column("preferred_workspace_id", Text, nullable=True),
    Column("attachments", Text, nullable=True, server_default="'[]'"),
    Column("auto_approve_plan", Integer, nullable=False, server_default="0"),
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
    Column("max_agents_by_type", Text, nullable=False, server_default="'{}'"),
    Column("pause_scheduling", Integer, nullable=False, server_default="0"),
    Column("created_by", Text, nullable=True),
    Column("created_at", Float, nullable=False),
)

plugins = Table(
    "plugins",
    metadata,
    Column("id", Text, primary_key=True),
    Column("version", Text, nullable=False, server_default="'0.0.0'"),
    Column("source_url", Text, nullable=False, server_default="''"),
    Column("source_rev", Text, nullable=False, server_default="''"),
    Column("source_branch", Text, nullable=False, server_default="''"),
    Column("install_path", Text, nullable=False, server_default="''"),
    Column("status", Text, nullable=False, server_default="'installed'"),
    Column("config", Text, nullable=False, server_default="'{}'"),
    Column("permissions", Text, nullable=False, server_default="'[]'"),
    Column("error_message", Text, nullable=True),
    Column("installed_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

plugin_data = Table(
    "plugin_data",
    metadata,
    Column("plugin_id", Text, ForeignKey("plugins.id"), nullable=False, primary_key=True),
    Column("key", Text, nullable=False, primary_key=True),
    Column("value", Text, nullable=False, server_default="'{}'"),
    Column("updated_at", Float, nullable=False),
    Index("idx_plugin_data_plugin_id", "plugin_id"),
)

playbook_runs = Table(
    "playbook_runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("playbook_version", Integer, nullable=False),
    Column("trigger_event", Text, nullable=False, server_default="'{}'"),
    Column(
        "status",
        Text,
        nullable=False,
        server_default="'running'",
    ),
    Column("current_node", Text, nullable=True),
    Column("conversation_history", Text, nullable=False, server_default="'[]'"),
    Column("node_trace", Text, nullable=False, server_default="'[]'"),
    Column("tokens_used", Integer, nullable=False, server_default="0"),
    Column("started_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    Column("error", Text, nullable=True),
    Column("pinned_graph", Text, nullable=True),
    Column("paused_at", Float, nullable=True),
    Column("waiting_for_event", Text, nullable=True),
    Column("event_id", Text, nullable=True),
    Index(
        "uq_playbook_runs_pb_event",
        "playbook_id",
        "event_id",
        unique=True,
        sqlite_where=text("event_id IS NOT NULL"),
        postgresql_where=text("event_id IS NOT NULL"),
    ),
    CheckConstraint(
        "status IN ('running', 'paused', 'completed', 'failed', 'timed_out', 'cancelled')",
        name="ck_playbook_runs_status",
    ),
    Index("idx_playbook_runs_playbook_id", "playbook_id"),
    Index("idx_playbook_runs_status", "status"),
)

workflows = Table(
    "workflows",
    metadata,
    Column("workflow_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("playbook_run_id", Text, ForeignKey("playbook_runs.run_id"), nullable=False),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column(
        "status",
        Text,
        nullable=False,
        server_default="'running'",
    ),
    Column("current_stage", Text, nullable=True),
    Column("task_ids", Text, nullable=False, server_default="'[]'"),
    Column("agent_affinity", Text, nullable=False, server_default="'{}'"),
    Column("stages", Text, nullable=False, server_default="'[]'"),
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
