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
    Column("agent_type", Text, nullable=True),
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
    Column("agent_type", Text, nullable=False),
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
    Column("locked_by_agent_id", Text, ForeignKey("agents.id"), nullable=True),
    Column("locked_by_task_id", Text, ForeignKey("tasks.id"), nullable=True),
    Column("locked_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("project_id", "workspace_path"),
)

hooks = Table(
    "hooks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("trigger", Text, nullable=False),
    Column("context_steps", Text, nullable=False, server_default="'[]'"),
    Column("prompt_template", Text, nullable=False),
    Column("llm_config", Text, nullable=True),
    Column("cooldown_seconds", Integer, nullable=False, server_default="3600"),
    Column("max_tokens_per_run", Integer, nullable=True),
    Column("last_triggered_at", Float, nullable=True),
    Column("plugin_id", Text, nullable=True),
    Column("source_hash", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index("idx_hooks_plugin_id", "plugin_id"),
)

hook_runs = Table(
    "hook_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("hook_id", Text, ForeignKey("hooks.id"), nullable=False),
    Column("project_id", Text, nullable=False),
    Column("trigger_reason", Text, nullable=False),
    Column("event_data", Text, nullable=True),
    Column("context_results", Text, nullable=True),
    Column("prompt_sent", Text, nullable=True),
    Column("llm_response", Text, nullable=True),
    Column("actions_taken", Text, nullable=True),
    Column("skipped_reason", Text, nullable=True),
    Column("tokens_used", Integer, nullable=False, server_default="0"),
    Column("status", Text, nullable=False, server_default="'running'"),
    Column("started_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
)

agent_profiles = Table(
    "agent_profiles",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("description", Text, nullable=False, server_default="''"),
    Column("model", Text, nullable=False, server_default="''"),
    Column("permission_mode", Text, nullable=False, server_default="''"),
    Column("allowed_tools", Text, nullable=False, server_default="'[]'"),
    Column("mcp_servers", Text, nullable=False, server_default="'{}'"),
    Column("system_prompt_suffix", Text, nullable=False, server_default="''"),
    Column("install", Text, nullable=False, server_default="'{}'"),
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
    Column("status", Text, nullable=False, server_default="'pending'"),
    Column("created_at", Float, nullable=False),
    Column("resolved_at", Float, nullable=True),
    Column("context_snapshot", Text, nullable=True),
    Index("idx_chat_analyzer_project", "project_id", "status"),
    Index("idx_chat_analyzer_hash", "suggestion_hash"),
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
    Column("agent_type", Text, nullable=True),
    Column("affinity_agent_id", Text, nullable=True),
    Column("affinity_reason", Text, nullable=True),
    Column("workspace_mode", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("archived_at", Float, nullable=False),
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
