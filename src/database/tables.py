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
        "preferred_workspace_id", Text, ForeignKey("workspaces.id", use_alter=True), nullable=True
    ),
    Column("attachments", Text, nullable=True, server_default="'[]'"),
    Column("auto_approve_plan", Integer, nullable=False, server_default="0"),
    Column("skip_verification", Integer, nullable=False, server_default="0"),
    Column("workflow_id", Text, ForeignKey("workflows.workflow_id", use_alter=True), nullable=True),
    Column("affinity_agent_id", Text, nullable=True),
    Column("affinity_reason", Text, nullable=True),
    Column("workspace_mode", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
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

task_dependencies = Table(
    "task_dependencies",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    Column("depends_on_task_id", Text, ForeignKey("tasks.id"), nullable=False, primary_key=True),
    CheckConstraint("task_id != depends_on_task_id"),
    Index("idx_task_deps_depends_on", "depends_on_task_id"),
    Index("idx_task_deps_task_id", "task_id"),
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
    Column("current_task_id", Text, ForeignKey("tasks.id", use_alter=True), nullable=True),
    Column("checkout_path", Text, nullable=True),
    Column("repo_id", Text, ForeignKey("repos.id"), nullable=True),
    Column("pid", Integer, nullable=True),
    Column("last_heartbeat", Float, nullable=True),
    Column("total_tokens_used", Integer, nullable=False, server_default="0"),
    Column("session_tokens_used", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
)

token_ledger = Table(
    "token_ledger",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("agent_id", Text, ForeignKey("agents.id"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id"), nullable=False),
    Column("tokens_used", Integer, nullable=False),
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
    Column("created_at", Float, nullable=False),
    UniqueConstraint("project_id", "workspace_path"),
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
    Column("created_at", Float, nullable=False),
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
    # Which runtime executes tasks for this profile.  Default
    # ``"claude_sdk"`` matches ``config.default_runtime``; ``"supervisor"``
    # routes to the in-process Supervisor singleton (tool-call-only, no
    # workspace).  Other values must match a name in the RuntimeRegistry.
    Column("runtime", Text, nullable=False, server_default="'claude_sdk'"),
    # ACP agent identifier — only meaningful when ``runtime == "acpx"``.
    # Empty string for every other runtime.  Validated at parse time.
    Column("agent_name", Text, nullable=False, server_default="''"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
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
    CheckConstraint(
        "status IN ('running', 'paused', 'completed', 'failed', 'timed_out')",
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
