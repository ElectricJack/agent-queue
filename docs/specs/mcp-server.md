# MCP Server Specification

The MCP server exposes all [[command-handler|CommandHandler]] commands as MCP tools via the
[Model Context Protocol](https://modelcontextprotocol.io). Claude agents
(or any MCP-compatible client) connect over stdio and get the same
capabilities as the Discord bot and Supervisor LLM tool-use loop.

## Architecture

### CommandHandler Delegation

The MCP server does **not** reimplement business logic. Every tool call
delegates to `CommandHandler.execute(name, args)` and returns the JSON
result. This guarantees feature parity with all other entry points:

```
MCP Client  -->  FastMCP tool handler  -->  CommandHandler.execute()
                                               |
Discord Bot  -->  slash command  ------------>  |
Supervisor   -->  LLM tool use  ------------>  |
```

### Initialization

On startup the MCP server:

1. Loads `AppConfig` from `--config` path (default `~/.agent-queue/config.yaml`)
2. Creates an `Orchestrator` and calls `await orchestrator.initialize()`
   (DB, event bus, git manager, etc.)
3. Creates a `CommandHandler` wired to the orchestrator
4. Does **not** call `orchestrator.run()` -- the scheduling loop is not needed

On shutdown it calls `await orchestrator.shutdown()`.

### Dynamic Tool Registration

Tools are auto-registered at module load time from `_ALL_TOOL_DEFINITIONS`
in `src/tool_registry.py`. For each non-excluded definition the server:

1. Creates a closure: `async handler(**kwargs) -> json.dumps(ch.execute(name, kwargs))`
2. Constructs a `mcp.server.fastmcp.tools.Tool` with the definition's
   `name`, `description`, and `input_schema`
3. Uses a permissive `_AnyArgs` Pydantic model (actual validation is done
   by CommandHandler)

Result: ~100 MCP tools auto-registered from ~107 definitions (minus exclusions).

## Exposed Tools

All tools from `_ALL_TOOL_DEFINITIONS` are exposed unless excluded.
They are grouped by category (see `src/tool_registry.py`):

### Core (always-loaded in Supervisor context)

| Tool | Purpose |
|------|---------|
| `list_tasks` | List tasks for a project |
| `create_task` | Create a new task |
| `get_task` | Get task details |
| `edit_task` | Modify task fields |
| `memory_search` | Search project memory |

### Project Management

| Tool | Purpose |
|------|---------|
| `list_projects` | List all projects |
| `create_project` | Create a project |
| `pause_project` | Pause a project |
| `resume_project` | Resume a paused project |
| `edit_project` | Edit project settings |
| `set_default_branch` | Set repo default branch |
| `get_project_channels` | Get Discord channel config |
| `get_project_for_channel` | Find project for a channel |
| `delete_project` | Delete a project |
| `add_workspace` | Add a workspace to a project |
| `list_workspaces` | List project workspaces |
| `find_merge_conflict_workspaces` | Find workspaces with merge conflicts |
| `release_workspace` | Release a workspace from its task |
| `remove_workspace` | Remove a workspace |
| `queue_sync_workspaces` | Queue workspace sync job |
| `set_active_project` | Set active project context |

### Task Operations (system category)

| Tool | Purpose |
|------|---------|
| `list_active_tasks_all_projects` | Active tasks across all projects |
| `get_task_tree` | Task dependency tree |
| `stop_task` | Stop a running task |
| `restart_task` | Restart a task |
| `reopen_with_feedback` | Reopen with feedback |
| `delete_task` | Delete a task |
| `archive_tasks` | Archive completed tasks |
| `archive_task` | Archive a single task |
| `list_archived` | List archived tasks |
| `restore_task` | Restore an archived task |
| `approve_task` | Approve a task |
| `process_task_completion` | Process task completion |
| `approve_plan` | Approve a plan |
| `reject_plan` | Reject a plan |
| `delete_plan` | Delete a plan |
| `process_plan` | Process a plan |
| `skip_task` | Skip a task |
| `get_task_dependencies` | Get task dependencies |
| `add_dependency` | Add a dependency |
| `remove_dependency` | Remove a dependency |
| `get_chain_health` | Check dependency chain health |
| `get_status` | System status overview |
| `get_recent_events` | Recent events |
| `get_task_result` | Get task result |
| `get_task_diff` | Get task diff |
| `get_token_usage` | Token usage stats |
| `list_prompts` | List available prompts |
| `read_prompt` | Read a prompt |
| `render_prompt` | Render a prompt |
| `orchestrator_control` | Orchestrator control operations |

### Agent & Profile Management

| Tool | Purpose |
|------|---------|
| `list_agents` | List all agents |
| `get_agent_error` | Get agent error details |
| `list_profiles` | List agent profiles |
| `create_profile` | Create an agent profile |
| `get_profile` | Get profile details |
| `edit_profile` | Edit an agent profile |
| `delete_profile` | Delete a profile |
| `list_available_tools` | List tools available to agents |
| `check_profile` | Validate a profile |
| `install_profile` | Install a profile |
| `export_profile` | Export a profile |
| `import_profile` | Import a profile |

### Git Operations

| Tool | Purpose |
|------|---------|
| `get_git_status` | Repository status |
| `git_commit` | Create a commit |
| `git_pull` | Pull from remote |
| `git_push` | Push to remote |
| `git_create_branch` | Create a branch |
| `git_merge` | Merge branches |
| `git_create_pr` | Create a pull request |
| `git_changed_files` | List changed files |
| `git_log` | View git log |
| `git_diff` | View diff |
| `checkout_branch` | Check out a branch |

### Hooks

| Tool | Purpose |
|------|---------|
| `create_hook` | Create a hook |
| `list_hooks` | List hooks |
| `edit_hook` | Edit a hook |
| `delete_hook` | Delete a hook |
| `list_hook_runs` | List hook run history |
| `fire_hook` | Fire a hook manually |
| `hook_schedules` | View hook schedules |
| `fire_all_scheduled_hooks` | Fire all due hooks |
| `schedule_hook` | Schedule a hook |
| `list_scheduled` | List scheduled items |
| `cancel_scheduled` | Cancel a scheduled item |

### Memory

| Tool | Purpose |
|------|---------|
| `memory_search` | Search memory |
| `memory_stats` | Memory statistics |
| `memory_reindex` | Reindex memory |
| `view_profile` | View project profile |
| `regenerate_profile` | Regenerate profile |
| `compact_memory` | Compact old memories |
| `list_notes` | List notes |
| `write_note` | Write a note |
| `delete_note` | Delete a note |
| `read_note` | Read a note |
| `append_note` | Append to a note |
| `promote_note` | Promote a note to profile |
| `compare_specs_notes` | Compare specs vs notes |

### Files

| Tool | Purpose |
|------|---------|
| `read_file` | Read a file |
| `write_file` | Write a file |
| `edit_file` | Edit a file |
| `glob_files` | Glob pattern match |
| `grep` | Search file contents |
| `search_files` | Search files |
| `list_directory` | List directory contents |

## Exclusion Configuration

### Default Exclusions

These commands are excluded by default (dangerous or irrelevant for MCP):

| Command | Reason |
|---------|--------|
| `shutdown` | Stops the daemon |
| `restart_daemon` | Restarts the daemon |
| `update_and_restart` | Pulls updates and restarts |
| `run_command` | Arbitrary shell execution |
| `browse_tools` | LLM context management meta-tool |
| `load_tools` | LLM context management meta-tool |

### Config YAML

Add an `mcp_server` section to `~/.agent-queue/config.yaml`:

```yaml
mcp_server:
  excluded_commands:
    - some_command
    - another_command
```

Config exclusions are **merged** (unioned) with the defaults -- they add
to the exclusion set, they don't replace it.

### Environment Variable

Set `AGENT_QUEUE_MCP_EXCLUDED` as a comma-separated list:

```bash
export AGENT_QUEUE_MCP_EXCLUDED="some_command,another_command"
```

### Merge Order

All three sources are combined via set union:

```
effective_exclusions = DEFAULT_EXCLUDED_COMMANDS
                     | config.mcp_server.excluded_commands
                     | AGENT_QUEUE_MCP_EXCLUDED (env var)
```

## Resources (Read-Only Views)

Resources provide read-only access to system state without going through
CommandHandler. They're useful for MCP clients that want to browse data.

| URI | Description |
|-----|-------------|
| `agentqueue://tasks` | All active and recent tasks |
| `agentqueue://tasks/active` | Tasks with status IN_PROGRESS, ASSIGNED, or READY |
| `agentqueue://tasks/{task_id}` | Single task with dependencies and context |
| `agentqueue://tasks/by-project/{project_id}` | Tasks for a project |
| `agentqueue://tasks/by-status/{status}` | Tasks by status |
| `agentqueue://projects` | All projects |
| `agentqueue://projects/{project_id}` | Single project |
| `agentqueue://agents` | All agents |
| `agentqueue://agents/active` | Busy agents |
| `agentqueue://profiles` | All agent profiles |
| `agentqueue://profiles/{profile_id}` | Single profile |
| `agentqueue://events/recent` | Last 50 system events |
| `agentqueue://workspaces` | All workspaces |
| `agentqueue://workspaces/by-project/{project_id}` | Workspaces for a project |

## Prompt Templates

| Name | Description |
|------|-------------|
| `create_task_prompt` | Structured prompt for creating a well-formed task |
| `review_task_prompt` | Prompt for reviewing a completed task |
| `project_overview_prompt` | Comprehensive project overview prompt |

## Connecting Claude Agents via MCP

### Claude Code Configuration

Add to your Claude Code MCP config (`.mcp.json` or project settings):

```json
{
  "mcpServers": {
    "agent-queue": {
      "command": "agent-queue-mcp",
      "args": ["--config", "~/.agent-queue/config.yaml"]
    }
  }
}
```

### Entry Point

```bash
# Default (stdio transport, default config path)
agent-queue-mcp

# Custom config
agent-queue-mcp --config /path/to/config.yaml

# SSE transport on custom port
agent-queue-mcp --transport sse --port 9000

# Debug logging
agent-queue-mcp --debug
```

CLI arguments:

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | `~/.agent-queue/config.yaml` | Path to config YAML |
| `--db` | *(from config)* | SQLite path (deprecated, use --config) |
| `--transport` | `stdio` | Transport: `stdio`, `sse`, `streamable-http` |
| `--port` | `8000` | Port for SSE/HTTP transport |
| `--debug` | off | Enable debug logging to stderr |

The entry point is defined in `pyproject.toml`:

```toml
[project.scripts]
agent-queue-mcp = "packages.mcp_server.mcp_server:main"
```

## Key Files

| File | Purpose |
|------|---------|
| `packages/mcp_server/mcp_server.py` | Main server -- lifespan, tool registration, resources, prompts, CLI |
| `packages/mcp_server/mcp_interfaces.py` | Serialization helpers, URI schemes |
| `packages/mcp_server/test/test_mcp_server.py` | Tests -- registration, delegation, drift detection |
| `src/tool_registry.py` | `_ALL_TOOL_DEFINITIONS` -- the source of truth for tool schemas |
| `src/command_handler.py` | [[command-handler|CommandHandler]].execute() -- the single execution layer |

> **Future evolution:** MCP tools expand to include [[design/vault-and-memory|memory tools]] (memory_search, memory_recall, memory_save, memory_store, memory_get).
