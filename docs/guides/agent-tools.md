---
tags: [tools, agent, reference]
---

# Agent Queue — Internal Tool Reference for AI Agents

> **Audience:** AI agents (supervisor LLM, task agents) that call these tools programmatically via the tool-use loop. This is NOT documentation for Discord slash commands or human-facing interfaces.

All tools are called through [[specs/command-handler|CommandHandler]]`.execute(tool_name, params)`. Parameters are passed as a JSON object. Tools return `{"success": bool, ...}` dicts.

---

## Tool Loading System

To optimize context window usage, tools are split into **core** (always loaded) and **categorized** (loaded on demand). See [[specs/tiered-tools|Tiered Tools]] for the design rationale.

**To discover and load tools:**
1. Call `browse_tools` (no params) → returns category names with descriptions and tool counts
2. Call `load_tools(category="git")` → injects that category's tools into your active set

Loading is a context optimization only — all tools are always executable on the backend regardless of loading state.

**Categories:** `git`, `project`, `agent`, `playbook`, `plugin`, `hooks` (deprecated), `memory`, `notes`, `files`, `task`, `rules`, `system`

---

## Core Tools (Always Available)

### Navigation & Response

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `browse_tools` | List available tool categories | *none* |
| `load_tools` | Load a tool category into active set | `category` (string, required) |
| `reply_to_user` | **Must call** to deliver final response | `message` (string, required) |
| `send_message` | Post message to a Discord channel | `channel_id` (string, required), `content` (string, required) |

### Task Management

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `create_task` | Create a new task | `title` (required), `description`, `priority` (int, lower=higher), `requires_approval` (bool), `task_type` (feature/bugfix/refactor/test/docs/chore/research/plan), `profile_id`, `preferred_workspace_id`, `attachments` (array of file paths), `auto_approve_plan` (bool) |
| `list_tasks` | List tasks with filtering | `project_id`, `status` (DEFINED/READY/IN_PROGRESS/etc.), `show_all` (bool), `include_completed` (bool), `completed_only` (bool), `display_mode` (flat/tree/compact), `show_dependencies` (bool) |
| `get_task` | Get full task details | `task_id` (required) |
| `edit_task` | Modify task properties | `task_id` (required), then any of: `project_id`, `title`, `description`, `priority`, `task_type`, `status` (admin override), `max_retries`, `profile_id`, `auto_approve_plan` |

### Memory

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `memory_search` | Semantic search of project memory | `project_id` (required), `query` (required), `top_k` (int, default 10) |

### Automation Rules (Legacy)

> Rules and hooks are deprecated in favor of [[specs/design/playbooks|playbooks]].

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_rules` | List all automation rules | `project_id` (optional) |
| `save_rule` | Create/update an automation rule | `id` (optional, auto-generated), `project_id` (null=global), `type` (required: "active" or "passive"), `content` (required: markdown with `# Title`, `## Trigger`, `## Logic`) |
| `load_rule` | Get rule details | `id` (required) |
| `delete_rule` | Remove rule and its hooks | `id` (required) |
| `refresh_hooks` | Force reconcile all rules/hooks | *none* |

---

## Git Category (11 tools)

All git tools accept optional `project_id` (defaults to active project) and `workspace` (defaults to first workspace).

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `get_git_status` | Current branch, working tree status, recent commits | *optional:* `project_id` |
| `git_commit` | Stage all changes and commit | `message` (required) |
| `git_pull` | Fetch + merge from origin | `branch` (optional, defaults to current) |
| `git_push` | Push branch to origin | `branch` (optional, defaults to current) |
| `git_create_branch` | Create and switch to new branch | `branch_name` (required) |
| `git_merge` | Merge into default branch (auto-aborts on conflict) | `branch_name` (required), `default_branch` (optional) |
| `git_create_pr` | Create GitHub PR via `gh` CLI | `title` (required), `body`, `branch` (defaults to current), `base` (defaults to repo default) |
| `git_changed_files` | List files changed vs base branch | `base_branch` (optional) |
| `git_log` | Show recent commits | `count` (int, default 10) |
| `git_diff` | Show diff (working tree if no base_branch) | `base_branch` (optional) |
| `checkout_branch` | Switch to existing branch | `branch_name` (required) |

---

## Project Category (16 tools)

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_projects` | List all projects | *none* |
| `create_project` | Create new project | `name` (required), `credit_weight` (float, default 1.0), `max_concurrent_agents` (int, default 2), `repo_url`, `default_branch` (default "main"), `auto_create_channels` (bool) |
| `pause_project` | Pause task scheduling | `project_id` (required) |
| `resume_project` | Resume task scheduling | `project_id` (required) |
| `edit_project` | Edit project properties | `project_id` (required), then any of: `name`, `credit_weight`, `max_concurrent_agents`, `budget_limit` (int or null), `discord_channel_id` (string or null), `default_profile_id` (string or null), `repo_default_branch` |
| `set_default_branch` | Set default git branch (creates on remote if missing) | `project_id` (required), `branch` (required) |
| `get_project_channels` | Get Discord channel ID for project | `project_id` (required) |
| `get_project_for_channel` | Find project linked to a channel | `channel_id` (required) |
| `delete_project` | Delete project and all data (fails if task IN_PROGRESS) | `project_id` (required), `archive_channels` (bool) |
| `set_active_project` | Set/clear default project for commands | `project_id` (string, empty/null to clear) |
| `add_workspace` | Add workspace (clone from repo or link existing dir) | `project_id` (required), `source` (required: "clone" or "link"), `path` (required for link), `name` |
| `list_workspaces` | List workspaces with lock status | `project_id` (optional) |
| `find_merge_conflict_workspaces` | Scan for branches with merge conflicts | `project_id` (optional if active set) |
| `release_workspace` | Force-release a stuck workspace lock | `workspace_id` (required) |
| `remove_workspace` | Remove workspace record from DB (not files) | `workspace_id` (required), `project_id` (optional) |
| `queue_sync_workspaces` | Queue sync: pause → wait → merge all branches → resume | `project_id` (optional if active set) |

---

## Agent Category (12 tools)

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_agents` | List agent slots (locked=busy, unlocked=idle) | `project_id` (optional if active set) |
| `get_agent_error` | Last error with classification and suggested fix | `task_id` (required) |
| `list_profiles` | List all agent profiles | *none* |
| `create_profile` | Create new agent profile | `id` (required, slug), `name` (required), `description`, `model`, `permission_mode`, `allowed_tools` (array, e.g. ["Read", "Glob", "Bash"]), `mcp_servers` (object: name → {command, args}), `system_prompt_suffix` |
| `get_profile` | Get profile details | `profile_id` (required) |
| `edit_profile` | Edit profile properties | `profile_id` (required), then same fields as create_profile |
| `delete_profile` | Delete profile (refs in tasks/projects cleared) | `profile_id` (required) |
| `list_available_tools` | Discover Claude Code tools and MCP servers | *none* |
| `check_profile` | Validate profile install dependencies | `profile_id` (required) |
| `install_profile` | Install missing npm/pip deps for profile | `profile_id` (required) |
| `export_profile` | Export profile as YAML (optionally as gist) | `profile_id` (required), `create_gist` (bool) |
| `import_profile` | Import profile from YAML or gist URL | `source` (required: YAML text or gist URL), `id` (override), `name` (override), `overwrite` (bool) |

---

## Playbook Category

Playbooks are DAG-based workflow automation authored as markdown and compiled to executable graphs. See [[specs/design/playbooks|Playbook spec]].

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_playbooks` | List all playbooks with scope and trigger info | `scope` (optional: system/project/agent-type), `project_id` (optional) |
| `compile_playbook` | Compile markdown playbook to executable JSON graph | `path` (required: vault path to `.md` file) |
| `run_playbook` | Manually trigger a playbook run | `playbook_id` (required), `context` (optional: JSON object passed to first node) |
| `list_playbook_runs` | List recent playbook runs | `playbook_id` (optional), `status` (optional), `limit` (int, default 10) |
| `inspect_playbook_run` | Detailed run info: nodes visited, context, tokens | `run_id` (required) |
| `resume_playbook` | Resume a paused playbook run (human-in-the-loop) | `run_id` (required), `input` (optional: human input text) |
| `playbook_health` | Run metrics: timing, token usage, failure rates | `playbook_id` (required) |
| `show_playbook_graph` | Render playbook as ASCII or Mermaid graph | `playbook_id` (required), `format` (optional: ascii/mermaid) |
| `dry_run_playbook` | Simulate playbook execution without side effects | `playbook_id` (required), `context` (optional) |

---

## Plugin Category

Manage the plugin ecosystem. See [[specs/plugin-system|Plugin spec]].

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `plugin_list` | List installed plugins with status | *none* |
| `plugin_info` | Detailed plugin info (version, tools, events, cron) | `name` (required) |
| `plugin_install` | Install plugin from git repo | `url` (required: git URL) |
| `plugin_update` | Update installed plugin | `name` (required) |
| `plugin_remove` | Uninstall plugin | `name` (required) |
| `plugin_enable` | Enable a disabled plugin | `name` (required) |
| `plugin_disable` | Disable plugin (keeps installed) | `name` (required) |
| `plugin_reload` | Reload plugin code | `name` (required) |
| `plugin_config` | Get/set plugin configuration | `name` (required), `key` (optional), `value` (optional) |
| `plugin_prompts` | List prompt templates from a plugin | `name` (required) |

---

## Hooks Category (8 tools) — Deprecated

> Hooks are deprecated in favor of [[specs/design/playbooks|playbooks]]. Use playbooks for new automation.

Hooks are generated from rules. Use `save_rule` (core tool) to create automation — don't create hooks directly.

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_hooks` | List hooks, optionally filtered by project | `project_id` (optional) |
| `list_hook_runs` | Execution history for a hook | `hook_id` (required), `limit` (int, default 10) |
| `fire_hook` | Manually trigger hook (ignores cooldown) | `hook_id` (required) |
| `hook_schedules` | Upcoming executions with next-run times | `project_id` (optional) |
| `fire_all_scheduled_hooks` | Trigger all enabled periodic hooks | `project_id` (optional) |
| `schedule_hook` | Schedule one-shot hook (auto-deletes after firing) | `project_id` (required), `prompt_template` (required), `name`, `fire_at` (epoch or ISO-8601), `delay` (e.g. "30m", "2h", "1d"), `context_steps` (array), `llm_config` (object: provider, model, base_url) |
| `list_scheduled` | List pending one-shot hooks with countdowns | `project_id` (optional) |
| `cancel_scheduled` | Cancel scheduled hook before it fires | `hook_id` (required) |

---

## Memory Category (V2)

Memory V2 uses Milvus-backed vector storage with 4-tier retrieval (L0 Identity → L1 Facts → L2 Topic → L3 Search) and scoped collections (system, agent-type, project).

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `memory_search` | Semantic search across scopes (also a core tool) | `query` (required), `scope` (optional), `project_id` (optional), `top_k` (int, default 10) |
| `memory_save` | Save a memory entry | `content` (required), `scope` (required), `tags` (array, optional), `metadata` (object, optional) |
| `memory_recall` | Retrieve memories by scope and filters | `scope` (required), `project_id` (optional), `tags` (array, optional), `limit` (int) |
| `memory_update` | Update existing memory content or metadata | `memory_id` (required), `content` (optional), `tags` (optional), `metadata` (optional) |
| `memory_delete` | Delete a memory entry | `memory_id` (required) |
| `memory_list` | List all memories in a scope | `scope` (required), `project_id` (optional), `limit` (int) |
| `memory_promote` | Promote memory to a broader scope | `memory_id` (required), `target_scope` (required) |
| `memory_stale` | Find stale or outdated memories | `scope` (optional), `days` (int, default 30) |
| `memory_health` | Memory system health metrics | *none* |

### KV Store

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `memory_kv_set` | Set a key-value pair in a scope | `key` (required), `value` (required), `scope` (required), `project_id` (optional) |
| `memory_kv_get` | Get value by key | `key` (required), `scope` (required), `project_id` (optional) |
| `memory_kv_list` | List all KV pairs in a scope | `scope` (required), `project_id` (optional) |

### Temporal Facts

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `memory_fact_set` | Set a temporal fact with optional validity window | `key` (required), `value` (required), `scope` (required), `valid_from` (optional), `valid_until` (optional) |
| `memory_fact_get` | Get current value of a temporal fact | `key` (required), `scope` (required) |
| `memory_fact_list` | List all active facts in a scope | `scope` (required), `project_id` (optional) |
| `memory_fact_history` | Full history of a fact's values | `key` (required), `scope` (required) |
| `memory_fact_recall` | Recall fact value at a specific point in time | `key` (required), `scope` (required), `at` (ISO-8601 datetime) |

### Tags

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `memory_search_by_tag` | Find memories by tag across scopes | `tags` (array, required), `scope` (optional) |

### Notes

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_notes` | List all notes (name, title, size) | `project_id` (required) |
| `write_note` | Create or overwrite a note | `project_id` (required), `title` (required), `content` (required) |
| `delete_note` | Delete a note | `project_id` (required), `title` (required) |
| `read_note` | Read note contents | `project_id` (required), `title` (required) |
| `append_note` | Append to existing note or create new | `project_id` (required), `title` (required), `content` (required) |
| `promote_note` | Incorporate note into project profile via LLM | `project_id` (required), `title` (required) |

---

## Files Category (7 tools)

Filesystem operations within project workspaces.

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `read_file` | Read file contents (supports pagination) | `path` (required: absolute or relative to workspaces root), `max_lines` (default 2000), `offset` (1-based, default 1), `limit` (overrides max_lines) |
| `write_file` | Write/create file (creates parent dirs) | `path` (required), `content` (required) |
| `edit_file` | Targeted string replacement | `path` (required), `old_string` (required, must be unique unless replace_all), `new_string` (required), `replace_all` (bool, default false) |
| `glob_files` | Find files matching glob pattern | `pattern` (required, e.g. `**/*.py`), `path` (required: directory) |
| `grep` | Regex content search (ripgrep-style) | `pattern` (required), `path` (required), `context` (int: lines around match), `case_insensitive` (bool), `glob` (file filter), `output_mode` (content/files_with_matches/count), `max_results` (default 100) |
| `search_files` | Search content (grep) or filenames (find) | `pattern` (required), `path` (required), `mode` ("grep" default or "find") |
| `list_directory` | List files/dirs at workspace path | `project_id` (required), `path` (relative, default root), `workspace` (name or ID) |

### Resource URIs (`aq://`) — Compile-Time Macros

`aq://` URIs are **compile-time macros for playbook authoring**. The playbook
compiler rewrites each `aq://<authority>/<path>` into an absolute filesystem
path before the playbook is stored or executed — runtime tools never see
`aq://`. Authorities:

| URI | Rewrites to |
|---|---|
| `aq://prompts/<path>` | Bundled `src/prompts/<path>` (ships with the daemon) |
| `aq://vault/<path>` | `{vault_root}/<path>` |
| `aq://logs/<path>` | `{data_dir}/logs/<path>` |
| `aq://tasks/<path>` | `{data_dir}/tasks/<path>` |
| `aq://attachments/<path>` | `{data_dir}/attachments/<path>` |

Runtime placeholders inside a URI (e.g. `<project_id>`) pass through the
rewrite unchanged and are filled by the step's LLM at execution time.
`..` path segments and unknown authorities are rejected at compile time.

---

## System Category (28+ tools)

### Task Operations

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_active_tasks_all_projects` | Cross-project active task overview | `include_completed` (bool) |
| `get_task_tree` | Subtask hierarchy rendered as tree | `task_id` (required), `compact` (bool: root only with progress bar), `max_depth` (int, default 4) |
| `stop_task` | Stop in-progress task → BLOCKED | `task_id` (required) |
| `restart_task` | Reset completed/failed/blocked → READY | `task_id` (required) |
| `reopen_with_feedback` | Reopen task with feedback appended to description | `task_id` (required), `feedback` (required) |
| `delete_task` | Delete task (fails if IN_PROGRESS) | `task_id` (required) |
| `skip_task` | Skip BLOCKED/FAILED to unblock dependents | `task_id` (required) |

### Archive Operations

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `archive_tasks` | Archive completed tasks | `project_id` (optional), `include_failed` (bool) |
| `archive_task` | Archive single task (must be terminal) | `task_id` (required) |
| `list_archived` | List archived tasks | `project_id` (optional), `limit` (int, default 50) |
| `restore_task` | Restore archived task → DEFINED | `task_id` (required) |

### Plan & Approval Operations

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `approve_task` | Approve task AWAITING_APPROVAL | `task_id` (required) |
| `approve_plan` | Approve plan → creates subtasks | `task_id` (required) |
| `reject_plan` | Reject plan with feedback → reopens for revision | `task_id` (required), `feedback` (required) |
| `delete_plan` | Delete plan without creating subtasks → completes task | `task_id` (required) |
| `process_plan` | Scan workspaces for plan.md files | `project_id` (optional), `task_id` (optional) |
| `process_task_completion` | Internal: discover plan files after task completes | `task_id` (required), `workspace_path` (required) |

### Dependency Operations

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `get_task_dependencies` | Full dependency graph (upstream + downstream) | `task_id` (required) |
| `add_dependency` | Add dependency with cycle detection | `task_id` (required: downstream/waits), `depends_on` (required: upstream/completes first) |
| `remove_dependency` | Remove dependency link | `task_id` (required), `depends_on` (required) |
| `get_chain_health` | Find stuck dependency chains | `task_id` (optional), `project_id` (optional) |

### Diagnostics & Control

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `get_status` | System overview: projects, agents, task counts | *none* |
| `get_recent_events` | Recent events (completions, failures, etc.) | `limit` (int, default 10) |
| `get_task_result` | Task output: summary, files changed, error, tokens | `task_id` (required) |
| `get_task_diff` | Git diff for task's branch vs base | `task_id` (required) |
| `get_token_usage` | Token usage by project or task | `project_id` (optional), `task_id` (optional) |
| `run_command` | Execute shell command in workspace | `command` (required), `working_dir` (required: path or project ID), `timeout` (int, default 30, max 120) |
| `restart_daemon` | Restart agent-queue daemon | `reason` (required) |
| `orchestrator_control` | Pause/resume/check task scheduler | `action` (required: "pause", "resume", or "status") |

### Prompt Management

| Tool | What It Does | Parameters |
|------|-------------|------------|
| `list_prompts` | List prompt templates | `project_id` (required), `category` (system/task/hooks/custom), `tag` |
| `read_prompt` | Read prompt template content | `project_id` + `name`, **or** `path` (absolute path to template; in playbooks, use `aq://prompts/<name>` which the compiler rewrites) |
| `render_prompt` | Render template with variable substitution | `project_id` + `name`, **or** `path` (absolute path); plus `variables` (object: key → value). `{{placeholders}}` are substituted server-side. |

---

## Tool Count Summary

| Category | Count | Scope |
|----------|-------|-------|
| Core | ~15 | Tasks, memory search, rules, messaging, tool loading |
| git | 11 | Branch, commit, push, PR, merge |
| project | 16 | Project CRUD, workspaces, channels |
| agent | 12 | Profiles, capabilities, import/export |
| playbook | 9 | Compilation, execution, monitoring, graph visualization |
| plugin | 10 | Install, update, enable/disable, config |
| memory | 20+ | Semantic search, KV store, temporal facts, tags, notes |
| hooks | 8 | Hook execution, scheduling, history (deprecated) |
| files | 7 | Read, write, edit, glob, grep |
| system | 28+ | Diagnostics, archives, dependencies, prompts, daemon |
| **Total** | **~130+** | |

---

## Common Workflows

### Creating and running a task
```
create_task(title="Fix login bug", description="...", task_type="bugfix")
```
The orchestrator automatically assigns it to an available agent/workspace.

### Checking on work
```
list_tasks(display_mode="compact")          # progress overview
get_task_result(task_id="swift-dune")       # see what agent produced
get_task_diff(task_id="swift-dune")         # see code changes
```

### Working with memory
```
memory_search(query="how does auth work", scope="project", project_id="my-proj")
memory_save(content="Auth uses JWT tokens with 24h expiry", scope="project", tags=["auth", "architecture"])
memory_kv_set(key="test_framework", value="pytest", scope="project", project_id="my-proj")
memory_fact_set(key="deploy_status", value="green", scope="project", valid_until="2024-12-31")
```

### Working with playbooks
```
load_tools(category="playbook")
list_playbooks()
run_playbook(playbook_id="task-outcome", context={"task_id": "swift-dune"})
inspect_playbook_run(run_id="run-123")
show_playbook_graph(playbook_id="task-outcome", format="mermaid")
```

### Managing plugins
```
load_tools(category="plugin")
plugin_list()
plugin_info(name="aq-vibecop")
plugin_install(url="https://github.com/user/my-plugin.git")
```

### Git workflow
```
load_tools(category="git")
get_git_status()
git_create_branch(branch_name="fix/login-bug")
git_commit(message="Fix login validation")
git_create_pr(title="Fix login validation bug")
```
