# Agent Queue MCP Server — Capabilities Report

**Date:** 2026-04-02
**Verified by:** Workflow 1 (fresh-dune), Workflow 3 (amber-summit)
**Server Version:** 1.26.0 | **Protocol:** 2024-11-05

## Connection Details

| Property | Value |
|----------|-------|
| **URL** | `http://localhost:8082/mcp` |
| **Transport** | Streamable HTTP with SSE responses |
| **Config** | `mcp_server.enabled: true` + `mcp_server.port: 8082` in `~/.agent-queue/config.yaml` |
| **Session** | Via `Mcp-Session-Id` response header |
| **Required Accept** | `application/json, text/event-stream` (both required) |
| **Claude Code Config** | `.mcp.json` with `{"type": "http", "url": "http://localhost:8082/mcp"}` |

## Capability Summary

| Type | Count | Description |
|------|-------|-------------|
| **Tools** | 101 | Full CommandHandler operations (6 excluded for safety) |
| **Resources** | 8 static + 6 templates | Read-only views of system state |
| **Prompts** | 3 | Task creation, review, and project overview templates |

## Tools (101 registered)

All tools delegate to `CommandHandler.execute()` — the same entry point used by Discord and chat providers. **MCP has full parity** minus excluded commands.

**Excluded by default:** `shutdown`, `restart_daemon`, `update_and_restart`, `run_command`, `browse_tools`, `load_tools`

### By Category

| Category | Count | Key Tools |
|----------|-------|-----------|
| **Task Management** | ~15 | `list_tasks`, `create_task`, `get_task`, `edit_task`, `stop_task`, `restart_task`, `delete_task`, `archive_task`, `archive_tasks`, `skip_task`, `reopen_with_feedback`, `get_task_result`, `get_task_diff` |
| **Project Management** | ~17 | `list_projects`, `create_project`, `edit_project`, `pause_project`, `resume_project`, `delete_project`, `set_active_project`, `get_project_channels` |
| **Git Operations** | 12 | `get_git_status`, `git_commit`, `git_pull`, `git_push`, `git_create_branch`, `git_merge`, `git_create_pr`, `git_diff`, `git_log`, `git_changed_files`, `checkout_branch`, `set_default_branch` |
| **Agent/Workspace** | ~11 | `list_agents`, `list_workspaces`, `add_workspace`, `release_workspace`, `remove_workspace`, `queue_sync_workspaces`, `find_merge_conflict_workspaces` |
| **Memory** | 12 | `memory_search`, `memory_stats`, `memory_reindex`, `compact_memory`, `view_profile`, `regenerate_profile` |
| **Notes** | 7 | `list_notes`, `write_note`, `read_note`, `append_note`, `delete_note`, `promote_note`, `compare_specs_notes` |
| **File Operations** | 7 | `read_file`, `write_file`, `edit_file`, `glob_files`, `grep`, `search_files`, `list_directory` |
| **Hooks/Scheduling** | 8 | `list_hooks`, `list_hook_runs`, `fire_hook`, `schedule_hook`, `cancel_scheduled`, `hook_schedules`, `fire_all_scheduled_hooks`, `list_scheduled` |
| **Profiles** | 8 | `list_profiles`, `create_profile`, `get_profile`, `edit_profile`, `delete_profile`, `export_profile`, `import_profile`, `install_profile`, `check_profile`, `list_available_tools` |
| **Dependencies** | 4 | `add_dependency`, `remove_dependency`, `get_task_dependencies`, `get_chain_health` |
| **Plan/Approval** | 5 | `approve_plan`, `reject_plan`, `delete_plan`, `process_plan`, `approve_task`, `process_task_completion` |
| **System** | ~7 | `get_status`, `get_recent_events`, `get_token_usage`, `orchestrator_control`, `send_message`, `reply_to_user`, `list_prompts`, `read_prompt`, `render_prompt` |

## Resources

### Static (8)

| URI | Description |
|-----|-------------|
| `agentqueue://tasks` | All active/recent tasks |
| `agentqueue://tasks/active` | Currently active tasks only |
| `agentqueue://projects` | All projects |
| `agentqueue://agents` | All agents |
| `agentqueue://agents/active` | Busy agents only |
| `agentqueue://profiles` | All agent profiles |
| `agentqueue://events/recent` | Last 50 events |
| `agentqueue://workspaces` | All workspaces |

### Templates (6)

| URI Template | Description |
|--------------|-------------|
| `agentqueue://tasks/{task_id}` | Single task details |
| `agentqueue://tasks/by-project/{project_id}` | Tasks by project |
| `agentqueue://tasks/by-status/{status}` | Tasks by status |
| `agentqueue://projects/{project_id}` | Single project |
| `agentqueue://profiles/{profile_id}` | Single profile |
| `agentqueue://workspaces/by-project/{project_id}` | Workspaces by project |

## Prompts (3)

| Name | Parameters | Description |
|------|-----------|-------------|
| `create_task_prompt` | `project_id` (req), `task_type`, `context` | Structured task creation guidance |
| `review_task_prompt` | `task_id` (req) | Task review and approval guidance |
| `project_overview_prompt` | `project_id` (req) | Comprehensive project overview |

## Verified Interactions

All operations tested successfully via `curl` with JSON-RPC 2.0:

- ✅ `initialize` — Session created, capabilities returned
- ✅ `tools/list` — 101 tools enumerated
- ✅ `tools/call` → `get_status` — 4 projects, 14 agents, 26 tasks
- ✅ `tools/call` → `list_projects` — All 4 projects with metadata
- ✅ `tools/call` → `list_tasks` — Filtered by project with limit
- ✅ `tools/call` → `memory_search` — Semantic search returned relevant results
- ✅ `tools/call` → `get_token_usage` — Token breakdown by task/agent
- ✅ `tools/call` → `get_git_status` — Branch info returned
- ✅ `resources/list` — 8 resources
- ✅ `resources/templates/list` — 6 templates
- ✅ `resources/read` → `agentqueue://agents/active` — Active agent state
- ✅ `prompts/list` — 3 prompts
- ✅ `prompts/get` → `project_overview_prompt` — Rendered template

## Architecture

1. **Auto-registration:** `_ALL_TOOL_DEFINITIONS` (124 entries) in `tool_registry.py` → MCP tools registered dynamically at startup
2. **Shared CommandHandler:** Same execution path as Discord bot — consistent behavior guaranteed
3. **Embedded daemon mode:** Lazy-loaded, supervised async task with auto-restart and exponential backoff
4. **Three-layer exclusion:** Hardcoded defaults + `config.yaml` + `AGENT_QUEUE_MCP_EXCLUDED` env var
5. **Serialization:** `mcp_interfaces.py` handles model → dict conversion for resources

## Recommendations

1. **Agent self-awareness:** Agents can query `get_task` for their own task, `memory_search` for project context
2. **Cross-project coordination:** `get_status` and `list_active_tasks_all_projects` provide system-wide visibility
3. **Dependency management:** `add_dependency`, `get_chain_health`, `skip_task` for programmatic chain control
4. **Memory-augmented work:** `memory_search` before starting enables agents to leverage past decisions
5. **Note collaboration:** Agents can write notes (`write_note`) for other agents to read — lightweight inter-agent communication

## Limitations

- No subscription/change notifications (`subscribe: false`, `listChanged: false`)
- Session-based with no persistent recovery
- Some git operations may need explicit workspace_id for multi-workspace projects
- Dangerous commands (shutdown, run_command) properly blocked
