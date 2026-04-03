# Agent Queue

**Put your AI agents to work. Go touch grass.**

Agent Queue is a task queue and orchestrator for AI coding agents (primarily Claude Code) on throttled/subsidized plans. It keeps agents busy across your projects, handles rate limits automatically, and queues the next task before the current one finishes. Manage everything from Discord on your phone — queue up tasks, come back to completed PRs.

<table>
<tr>
<td><img src="docs/img/project-chat-00.png" alt="Chatting with the bot — status, suggestions, agent management" width="450"></td>
<td><img src="docs/img/project-chat-01.png" alt="Task started and completed — token usage, change summary" width="450"></td>
</tr>
</table>

## Getting Started

**Prerequisites:** Python 3.12+, a [Discord bot token](https://discord.com/developers/applications), Claude Code installed.

```bash
git clone https://github.com/ElectricJack/agent-queue.git
cd agent-queue
./setup.sh
```

The setup script handles dependencies, Discord config, API keys, and first agent creation.

Once running, talk to the bot in your Discord channel:

```
You:  link ~/code/my-app as my-app
You:  create a project called my-app
You:  create agent claude-1 and assign it to my-app
You:  add a task to add rate limiting to the API
```

## Development

After cloning, install dev dependencies and set up the pre-commit hooks to enforce formatting:

```bash
pip install -e ".[dev,cli]"
pip install -e packages/aq-client      # typed API client (generated)
pre-commit install
```

This runs [ruff](https://docs.astral.sh/ruff/) formatting and linting on every commit. To format the entire codebase manually:

```bash
ruff format .
```

## Documentation

- **[Full docs](https://electricjack.github.io/agent-queue/)** — architecture, commands, hooks, adapters
- **[profile.md](profile.md)** — project architecture, conventions, design decisions

## License

MIT
