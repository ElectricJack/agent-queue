---
tags: [cli, interface]
---

# AgentQueue CLI

A modern, interactive terminal interface for AgentQueue that mirrors Discord
slash commands with rich formatting, interactive menus, and fuzzy search.

## Installation

Install the CLI extra dependencies:

```bash
pip install -e ".[cli]"
```

This installs:
- `click` — Command-line framework
- `rich` — Beautiful terminal formatting
- `prompt-toolkit` — Interactive menus and fuzzy completion

## Quick Start

```bash
# Show system status (default command)
aq

# List active tasks
aq task list

# Create a task interactively
aq task create

# Show task details
aq task details <task-id>

# Search tasks
aq task search "login bug"
```

## Configuration

The CLI connects directly to the AgentQueue SQLite database. It finds the
database using this resolution order:

1. `--db` command-line flag
2. `AGENT_QUEUE_DB` environment variable
3. `database.path` from `~/.agent-queue/config.yaml`
4. Default: `~/.agent-queue/agentqueue.db`

### Environment Variable

```bash
export AGENT_QUEUE_DB=/path/to/agentqueue.db
```

### Shell Alias

Add to your `.bashrc` or `.zshrc`:

```bash
# Quick aliases
alias aql="aq task list"
alias aqs="aq status"
alias aqt="aq task"
```

## Command Reference

### System Status

```bash
aq status                    # Full system overview
aq                           # Same as aq status (default)
```

Shows task counts by status, agent status summary, and project overview.

### Task Commands

```bash
# List tasks
aq task list                           # Active tasks (default)
aq task list --all                     # All tasks including completed
aq task list -p <project-id>           # Filter by project
aq task list -s IN_PROGRESS            # Filter by status
aq task list --limit 100               # Show more results

# Task details
aq task details <task-id>              # Full task info with deps

# Create tasks
aq task create                         # Interactive wizard
aq task create -p proj -t "Title" -d "Description"  # CLI flags
aq task create --type bugfix --priority 200 ...

# Task actions
aq task approve <task-id>              # Approve for execution
aq task approve <task-id> -y           # Skip confirmation
aq task stop <task-id>                 # Stop (marks as FAILED)
aq task stop <task-id> -y              # Skip confirmation
aq task restart <task-id>              # Restart (resets to READY)
aq task restart <task-id> -y           # Skip confirmation

# Search
aq task search "query"                 # Search by title/description
aq task search "query" -p <project>    # Search within a project

# Interactive selection
aq task select                         # Fuzzy-search task picker
aq task select -p <project>            # Scoped to project
```

### Agent Commands

```bash
aq agent list                          # List all agents
aq agent details <agent-id>            # Agent detail view
```

### Hook Commands

```bash
aq hook list                           # List all hooks
aq hook list -p <project>              # Filter by project
aq hook list --enabled                 # Only enabled hooks
aq hook details <hook-id>              # Hook configuration
aq hook runs <hook-id>                 # Execution history
aq hook runs <hook-id> --limit 50      # More history
```

### Project Commands

```bash
aq project list                        # List all projects
aq project list -s ACTIVE              # Filter by status
aq project details <project-id>        # Full project info
```

### MCP Commands

The `aq mcp` group is auto-generated from the `mcp_commands` mixin. The
`mcp-` prefix is stripped, so e.g. `list_mcp_servers` → `aq mcp list-servers`.

```bash
aq mcp list-servers                    # Registry entries (system + project scope)
aq mcp get-server <name>               # Show one entry's config
aq mcp create-server <name> ...        # Write a new entry to vault/mcp-servers/
aq mcp edit-server <name> ...          # Partial update
aq mcp delete-server <name>            # Refuses if any profile still uses the name
aq mcp probe-server <name>             # Spawn and refresh the tool catalog
aq mcp list-tool-catalog               # Cached tool catalog across all servers
```

### System Config Commands

```bash
aq system config get                   # Print the full YAML, env-var refs preserved
aq system config get --section logging # Print just one section
aq system config set logging.level DEBUG
aq system config edit                  # Open $EDITOR on the full file
aq system config schema                # JSON schema from AppConfig
aq system config schema --section logging
```

All writes go through the same validate-then-swap path: changes are loaded
into a temp file via `load_config()` first, then a `.bak` is written, then
the new file lands. An invalid edit never reaches disk. Comments, quoting,
and `${ENV_VAR}` references are preserved by the ruamel-based round-trip
writer.

## Interactive Features

### Task Creation Wizard

Running `aq task create` without flags launches a 6-step wizard:

1. **Project** — Select from available projects (with fuzzy completion)
2. **Title** — Enter task title
3. **Description** — Task description
4. **Priority** — Numeric priority (1-300, default 100)
5. **Type** — Task type (feature/bugfix/refactor/test/docs/chore/research/plan)
6. **Approval** — Whether human approval is required

Press `Ctrl+C` at any step to cancel.

### Fuzzy Task Selection

`aq task select` shows an interactive picker:
- Lists tasks with status icons and titles
- Type part of a task ID to fuzzy-match
- Tab completion for task IDs

### Confirmation Prompts

Destructive actions (stop, approve, restart) show confirmation dialogs.
Use `-y` / `--yes` flag to skip.

## Status Colors and Icons

| Status | Icon | Color |
|--------|------|-------|
| DEFINED | ⚪ | Gray |
| READY | 🔵 | Blue |
| ASSIGNED | 📋 | Purple |
| IN_PROGRESS | 🟡 | Yellow |
| WAITING_INPUT | 💬 | Cyan |
| PAUSED | ⏸️ | Gray |
| VERIFYING | 🔍 | Blue |
| AWAITING_APPROVAL | ⏳ | Yellow |
| COMPLETED | 🟢 | Green |
| FAILED | 🔴 | Red |
| BLOCKED | ⛔ | Red |

## Architecture

The CLI follows the same adapter pattern as the Discord and Telegram bots:

```
┌────────────┐
│  CLI (aq)  │──── Presentation layer (Rich formatting)
└─────┬──────┘
      │
┌─────┴──────┐
│  CLIClient │──── Data access (reads from shared SQLite DB)
└─────┬──────┘
      │
┌─────┴──────┐
│  Database  │──── Shared persistence (WAL mode for concurrent access)
└────────────┘
```

The CLI reads directly from the same SQLite database the daemon uses.
WAL journal mode ensures reads don't block the running daemon.
Write operations (create, approve, stop, restart) use the same models
and state machine validation as the rest of AgentQueue.
