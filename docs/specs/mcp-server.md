---
tags: [spec, mcp, api]
---

# MCP Server Specification

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../README.md) for what AQ does today, and
> see [historical material](../history/README.md) for how this material is
> organised.

The MCP server exposes all [CommandHandler](command-handler.md) commands as MCP tools via the
[Model Context Protocol](https://modelcontextprotocol.io). Claude agents
(or any MCP-compatible client) connect over stdio to the operational command
surface. Discord is notification-only and intentionally does not mirror it.

## Architecture

### CommandHandler Delegation

The MCP server does **not** reimplement business logic. Every tool call
delegates to `CommandHandler.execute(name, args)` and returns the JSON
result. This guarantees feature parity with the CLI/API command entry points:

```
MCP Client  -->  FastMCP tool handler  -->  CommandHandler.execute()
Supervisor  -->  LLM tool use  ---------------->  |
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
in `src/tools/registry.py`. For each non-excluded definition the server:

1. Creates a closure: `async handler(**kwargs) -> json.dumps(ch.execute(name, kwargs))`
2. Constructs a `mcp.server.fastmcp.tools.Tool` with the definition's
   `name`, `description`, and `input_schema`
3. Uses a permissive `_AnyArgs` Pydantic model (actual validation is done
   by CommandHandler)

Result: ~100 MCP tools auto-registered from ~107 definitions (minus exclusions).

## Exposed Tools

All tools from `_ALL_TOOL_DEFINITIONS` are exposed unless excluded.
They are grouped by category (see `src/tools/registry.py`):

### Core (always-loaded in Supervisor context)

| Tool | Purpose |
|------|---------|
| `list_tasks` | List tasks for a project |
| `create_task` | Create a new task (accepts `integration_mode`: `direct` \| `pull_request`) |
| `get_task` | Get task details (returns `integration_mode`, `effective_integration_mode`, `integration_mode_source`) |
| `edit_task` | Modify task fields (accepts `integration_mode`, or `null` to clear the override) |
| `memory_search` | Search project memory |

### Project Management

| Tool | Purpose |
|------|---------|
| `list_projects` | List all projects |
| `create_project` | Create a project |
| `onboard_project` | Link, initialize, or clone a root-scoped repository as a project |
| `get_project_onboarding` | Get durable onboarding progress or result by request ID |
| `list_project_roots` | List configured roots and their capabilities |
| `browse_project_root` | Browse safe root-relative directory entries |
| `get_github_auth_status` | Report daemon-host `gh` availability without credentials |
| `list_github_owners` | List owners available for GitHub repository creation |
| `search_github_repositories` | Find host-accessible GitHub repositories for cloning |
| `pause_project` | Pause a project |
| `resume_project` | Resume a paused project |
| `edit_project` | Edit project settings |
| `set_default_branch` | Set repo default branch |
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
| `read_prompt` | Read a prompt (by `(project_id, name)` or absolute `path`) |
| `render_prompt` | Render a prompt (by `(project_id, name)` or absolute `path`) |
| `orchestrator_control` | Orchestrator control operations |

### Agent & Profile Management

| Tool | Purpose |
|------|---------|
| `list_agents` | List all agents |
| `get_agent_error` | Get agent error details |
| `list_profiles` | List agent profiles (system scope) |
| `create_profile` | Create an agent profile |
| `get_profile` | Get profile details |
| `edit_profile` | Edit an agent profile |
| `delete_profile` | Delete a profile |
| `show_effective_profile` | Resolve `(project, agent_type)` to its effective profile |
| `list_available_tools` | List tools available to agents |
| `check_profile` | Validate a profile |
| `install_profile` | Install a profile |
| `export_profile` | Export a profile |
| `import_profile` | Import a profile |

### MCP Server Registry

Profiles reference MCP servers by name only; full configs live in the
vault registry. See the [MCP Server Registry](#mcp-server-registry-1)
section below for details.

| Tool | Purpose |
|------|---------|
| `list_mcp_servers` | List registry entries (system + project scope) |
| `get_mcp_server` | Get one entry's config |
| `create_mcp_server` | Add a new entry (writes to `vault/[projects/<pid>/]mcp-servers/<name>.md`) |
| `edit_mcp_server` | Update an entry |
| `delete_mcp_server` | Remove an entry (refuses if any profile still references the name) |
| `probe_mcp_server` | Probe an entry and refresh its tool catalog |
| `list_mcp_tool_catalog` | List the tools every registered server exposes (cached probe results) |

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

## MCP Server Registry

The registry is the source of truth for MCP server configurations referenced
by agent profiles. Profiles store **names** (`mcp_servers: list[str]`); the
orchestrator resolves them through the registry at task launch.

### Vault Layout

| Path | Scope | Notes |
|------|-------|-------|
| `vault/mcp-servers/<name>.md` | System | Available to every project |
| `vault/projects/<pid>/mcp-servers/<name>.md` | Project | Shadows the system entry of the same name |

### Source Files

| File | Purpose |
|------|---------|
| `src/profiles/mcp_registry.py` | In-memory registry; vault watcher refreshes on change |
| `src/profiles/mcp_probe.py` | Spawns and probes a server (10s per-probe timeout, runs in parallel; never blocks startup) |
| `src/profiles/mcp_catalog.py` | Caches probed tool catalogs |
| `src/profiles/mcp_inline_migration.py` | One-shot startup extractor that moves legacy inline configs from config.yaml profiles and old `profile.md` `## MCP Servers` blocks into registry files (idempotent) |

### Resolution Order

1. Project scope (`vault/projects/<pid>/mcp-servers/`)
2. System scope (`vault/mcp-servers/`)
3. Builtin: the embedded `agent-queue` server is always available; it is
   computed in-process from `CommandHandler` tool definitions plus plugin
   tools — there is no circular probe of the daemon's own MCP endpoint.

### CRUD

CRUD goes through `mcp_commands` on the CommandHandler, which writes the
markdown file directly. The vault watcher then syncs the in-memory
registry. `delete_mcp_server` refuses if any profile still references
the name.

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

The MCP server is **embedded in the daemon** (`src/embedded_mcp.py`): it shares
the daemon's `Orchestrator`, `Database`, `EventBus` and `CommandHandler` rather
than starting a process of its own, and is served over streamable-http on the
same uvicorn app as the REST API. There is no standalone `agent-queue-mcp`
console script — starting the daemon (`agent-queue`, `./run.sh start`) starts
the MCP server.

### Endpoint

`http://<mcp_server.host>:<mcp_server.port>/mcp` — `127.0.0.1:8081` by default.

```yaml
# ~/.agent-queue/config.yaml
mcp_server:
  enabled: true
  host: 127.0.0.1
  port: 8081
  excluded_commands: []
  inject_into_tasks: true
```

| Key | Default | Description |
|------|---------|-------------|
| `enabled` | `true` | Serve MCP from the daemon at all |
| `host` | `127.0.0.1` | Bind address (shared with the REST API) |
| `port` | `8081` | Bind port (shared with the REST API) |
| `excluded_commands` | `[]` | Merged with the built-in exclusions and `AQ_MCP_EXCLUDED_COMMANDS` |
| `inject_into_tasks` | `true` | Auto-add the server to every task's `mcp_servers` |

### Claude Code Configuration

With `inject_into_tasks` left on, the daemon writes this entry into every
task's `mcp_servers` itself and no per-workspace file is needed. To wire up a
client by hand:

```json
{
  "mcpServers": {
    "agent-queue": {
      "type": "http",
      "url": "http://127.0.0.1:8081/mcp"
    }
  }
}
```

## Key Files

| File | Purpose |
|------|---------|
| `src/embedded_mcp.py` | Embedded server -- lifespan, uvicorn supervision, FastAPI mount |
| `src/mcp_registration.py` | Tool, resource and prompt registration from the shared registry |
| `src/mcp_interfaces.py` | Serialization helpers, URI schemes |
| `tests/test_mcp_server.py` | Tests -- registration, delegation, drift detection |
| `tests/test_embedded_mcp.py` | Tests -- mount, lifespan, restart behaviour |
| `src/tools/registry.py` | `_ALL_TOOL_DEFINITIONS` -- the source of truth for tool schemas |
| `src/command_handler.py` | [CommandHandler](command-handler.md).execute() -- the single execution layer |

MCP tools include [memory tools](design/memory-scoping.md) (memory_search, memory_recall, memory_save, memory_store, memory_get).
