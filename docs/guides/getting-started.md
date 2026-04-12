---
tags: [getting-started, setup]
---

# Getting Started

## Prerequisites

- Python 3.12 or later
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- Claude Code CLI or API access

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/ElectricJack/agent-queue.git
cd agent-queue
```

### 2. Run the [[specs/setup-wizard|Setup Wizard]]

The interactive setup wizard will walk you through configuration:

```bash
./setup.sh
```

This creates a configuration file at `~/.agent-queue/config.yaml` with your Discord bot token, guild ID, and project settings.

### 3. Start the Daemon

```bash
./run.sh start
```

Other useful commands:

```bash
./run.sh status   # check if the daemon is running
./run.sh logs     # tail the daemon log
./run.sh stop     # stop the daemon
./run.sh restart  # restart the daemon
```

## Configuration

Agent Queue uses a YAML configuration file. The setup wizard creates this for you, but you can also edit it manually. See the [[specs/config|Configuration Spec]] for full details.

Key configuration sections:

- **discord** — Bot token, guild ID, channel names
- **agents_default** — Heartbeat intervals, timeouts
- **scheduling** — Rolling window, token budgets
- **projects** — Your project definitions with workspace paths and repo settings

## First Steps

Once the daemon is running and connected to Discord:

1. **Add a project** — Tell the bot about your project in the Discord channel, or use `/new-project` for a guided wizard
2. **Create a task** — Describe what you want done in natural language, or use `/add-task`
3. **Watch it work** — The bot creates a thread and streams agent progress
4. **Review the PR** — When the task completes, review the generated pull request

The bot uses a **Supervisor** — an LLM-powered conversation interface that translates your natural language into system commands. You can also use Discord slash commands (type `/` to see them) for structured operations. Both methods call the same underlying logic.

### Alternative Interfaces

- **CLI:** Run `aq` commands in your terminal (install with `pip install -e ".[cli]"`)
- **MCP client:** Connect from Claude Code, Cursor, or any MCP-compatible client — the embedded MCP server auto-exposes ~100 tools

### What Happens Next

As agents complete tasks, the system starts learning:

- **Reflection** extracts insights from each completed task
- **Memory** accumulates project conventions, error patterns, and successful strategies
- **Playbooks** automate recurring workflows (task review, knowledge consolidation, etc.)
- **Vault** (`~/.agent-queue/vault/`) stores all knowledge, playbooks, and profiles as browsable markdown — open with Obsidian for a rich editing experience

The longer Agent Queue runs, the better it gets at your projects.

For a complete reference of all available commands, see the [[discord-commands|Discord Commands Guide]].
