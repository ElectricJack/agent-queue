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

### Uninstall

To remove everything and return the repo to a fresh-clone state:

```bash
./uninstall.sh
```

This runs `git reset --hard HEAD` + `git clean -ffdx` (removes `.venv`, caches, logs, build artifacts, and any other untracked or ignored files), and prompts before removing `~/.agent-queue/` (your config, vault, database, and Discord token). Pass `-y` to skip prompts; `--keep-user-dir` to preserve `~/.agent-queue/`.

## Configuration

Agent Queue uses a YAML configuration file. The setup wizard creates this for you, but you can also edit it manually. See the [[specs/config|Configuration Spec]] for full details.

Key configuration sections:

- **discord** — Bot token, guild ID, and one shared channel ID
- **agents_default** — Heartbeat intervals, timeouts
- **scheduling** — Rolling window, token budgets
- **projects** — Your project definitions with workspace paths and repo settings

For dashboard or CLI project creation, first configure operator-owned source
roots and GitHub host authentication as needed; see the
[project onboarding guide](project-onboarding.md).

## First Steps

Once the daemon is running:

1. **Add a project** — Use the dashboard's **Add project** wizard or `aq project onboard`; it selects or creates repositories only beneath configured project roots. See the [project onboarding guide](project-onboarding.md).
2. **Create a task** — Use the dashboard or `aq task add`
3. **Watch it work** — Follow the dashboard live session view and recorded attempts
4. **Review the PR** — When the task completes, review the generated pull request

Discord optionally posts an hourly digest and opens a durable thread when the
supervisor needs a human answer. Replies in that thread return to the
supervisor; Discord does not expose task commands or general chat.

### Alternative Interfaces

- **CLI:** Run `aq` commands in your terminal (install with `pip install -e ".[cli]"`)
- **MCP client:** Connect from Claude Code, Cursor, or any MCP-compatible client — the embedded MCP server auto-exposes ~150 tools

### Dashboard terminals

Start the dashboard with `npm -w dashboard run dev` from the repository root.
Agent flock terminals connect through the dashboard's own `/ws/terminal/` endpoint.
Vite proxies this to `AQ_API_TARGET` (default `http://127.0.0.1:8081`), so ordinary
local use needs no additional origin configuration.

Terminal connections intentionally ignore the legacy `VITE_WS_URL` notification
setting and the `VITE_API_URL` HTTP setting. To use a separate terminal endpoint,
set `VITE_TERMINAL_WS_URL` to its base URL before starting or building the dashboard.
The daemon must trust the exact dashboard origin (scheme, host, and port) in
`api_auth.trusted_dashboard_origins`; custom dashboard hostnames also require this
trust. Operator authentication and loopback access restrictions still apply.
Leave the terminal override unset to use the normal dashboard proxy.

### What Happens Next

As agents complete tasks, the system starts learning:

- **Reflection** extracts insights from each completed task
- **Memory** accumulates project conventions, error patterns, and successful strategies
- **Playbooks** automate recurring workflows (task review, knowledge consolidation, etc.)
- **Vault** (`~/.agent-queue/vault/`) stores all knowledge, playbooks, and profiles as browsable markdown — open with Obsidian for a rich editing experience

The longer Agent Queue runs, the better it gets at your projects.

For a complete reference of all available commands, see the [[discord-commands|Discord Commands Guide]].
