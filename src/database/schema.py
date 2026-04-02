"""Database schema definitions: DDL, migrations, and indexes.

This module centralizes all schema-related constants so that adapters
can reference them without duplicating SQL strings.
"""

# Complete DDL for all 14 tables. Executed via executescript() on startup,
# so every statement uses CREATE TABLE IF NOT EXISTS for idempotency.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_weight REAL NOT NULL DEFAULT 1.0,
    max_concurrent_agents INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    budget_limit INTEGER,
    workspace_path TEXT,
    discord_channel_id TEXT,
    discord_control_channel_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    url TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    checkout_base_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_task_id TEXT REFERENCES tasks(id),
    repo_id TEXT REFERENCES repos(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'DEFINED',
    verification_type TEXT NOT NULL DEFAULT 'auto_test',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    assigned_agent_id TEXT REFERENCES agents(id),
    branch_name TEXT,
    resume_after REAL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT,
    plan_source TEXT,
    is_plan_subtask INTEGER NOT NULL DEFAULT 0,
    task_type TEXT,
    auto_approve_plan INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_criteria (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_context (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    label TEXT,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_tools (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    config TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT REFERENCES tasks(id),
    checkout_path TEXT,
    repo_id TEXT REFERENCES repos(id),
    pid INTEGER,
    last_heartbeat REAL,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    session_tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    tokens_used INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    payload TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    current_tokens INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    result TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    files_changed TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    workspace_path TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'clone',
    name TEXT,
    locked_by_agent_id TEXT REFERENCES agents(id),
    locked_by_task_id TEXT REFERENCES tasks(id),
    locked_at REAL,
    created_at REAL NOT NULL,
    UNIQUE(project_id, workspace_path)
);

CREATE TABLE IF NOT EXISTS hooks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    trigger TEXT NOT NULL,
    context_steps TEXT NOT NULL DEFAULT '[]',
    prompt_template TEXT NOT NULL,
    llm_config TEXT,
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    max_tokens_per_run INTEGER,
    last_triggered_at REAL,
    source_hash TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_runs (
    id TEXT PRIMARY KEY,
    hook_id TEXT NOT NULL REFERENCES hooks(id),
    project_id TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    event_data TEXT,
    context_results TEXT,
    prompt_sent TEXT,
    llm_response TEXT,
    actions_taken TEXT,
    skipped_reason TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT '',
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    mcp_servers TEXT NOT NULL DEFAULT '{}',
    system_prompt_suffix TEXT NOT NULL DEFAULT '',
    install TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_analyzer_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    suggestion_type TEXT NOT NULL,
    suggestion_text TEXT NOT NULL,
    suggestion_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    resolved_at REAL,
    context_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_analyzer_project
    ON chat_analyzer_suggestions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_chat_analyzer_hash
    ON chat_analyzer_suggestions(suggestion_hash);

CREATE TABLE IF NOT EXISTS archived_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_task_id TEXT,
    repo_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL,
    verification_type TEXT NOT NULL DEFAULT 'auto_test',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    assigned_agent_id TEXT,
    branch_name TEXT,
    resume_after REAL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT,
    plan_source TEXT,
    is_plan_subtask INTEGER NOT NULL DEFAULT 0,
    task_type TEXT,
    auto_approve_plan INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL DEFAULT '0.0.0',
    source_url TEXT NOT NULL DEFAULT '',
    source_rev TEXT NOT NULL DEFAULT '',
    source_branch TEXT NOT NULL DEFAULT '',
    install_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'installed',
    config TEXT NOT NULL DEFAULT '{}',
    permissions TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    installed_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plugin_data (
    plugin_id TEXT NOT NULL REFERENCES plugins(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (plugin_id, key)
);
"""

# Idempotent ALTER TABLE migrations applied on every startup.
# If a column already exists the error is silently caught.
MIGRATIONS = [
    "ALTER TABLE projects ADD COLUMN workspace_path TEXT",
    "ALTER TABLE repos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'clone'",
    "ALTER TABLE repos ADD COLUMN source_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN pr_url TEXT",
    "ALTER TABLE projects ADD COLUMN discord_channel_id TEXT",
    "ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT",
    "ALTER TABLE tasks ADD COLUMN plan_source TEXT",
    "ALTER TABLE tasks ADD COLUMN is_plan_subtask INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN task_type TEXT",
    "ALTER TABLE projects ADD COLUMN repo_url TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN repo_default_branch TEXT DEFAULT 'main'",
    "ALTER TABLE tasks ADD COLUMN profile_id TEXT REFERENCES agent_profiles(id)",
    "ALTER TABLE projects ADD COLUMN default_profile_id TEXT REFERENCES agent_profiles(id)",
    "ALTER TABLE archived_tasks ADD COLUMN profile_id TEXT",
    "ALTER TABLE tasks ADD COLUMN preferred_workspace_id TEXT REFERENCES workspaces(id)",
    "ALTER TABLE archived_tasks ADD COLUMN preferred_workspace_id TEXT",
    "ALTER TABLE tasks ADD COLUMN attachments TEXT DEFAULT '[]'",
    "ALTER TABLE archived_tasks ADD COLUMN attachments TEXT DEFAULT '[]'",
    "ALTER TABLE hooks ADD COLUMN last_triggered_at REAL",
    "ALTER TABLE hooks ADD COLUMN plugin_id TEXT",
    "ALTER TABLE tasks ADD COLUMN auto_approve_plan INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE archived_tasks ADD COLUMN auto_approve_plan INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE hooks ADD COLUMN source_hash TEXT",
]

# Indexes created after migrations (idempotent).
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_task_deps_depends_on "
    "ON task_dependencies(depends_on_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_deps_task_id "
    "ON task_dependencies(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_plugin_data_plugin_id "
    "ON plugin_data(plugin_id)",
    "CREATE INDEX IF NOT EXISTS idx_hooks_plugin_id "
    "ON hooks(plugin_id)",
]
