# Agent Queue

**An AI orchestration system that learns, adapts, and improves with every task.**

Agent Queue is a self-improving orchestration platform for AI coding agents. It manages task queues across multiple projects, coordinates multi-agent workflows, and — critically — learns from every task execution to make future work faster and more reliable. Manage everything from Discord, your terminal, or any MCP-compatible client.

Built for Claude Code agents on throttled/subsidized plans, but designed as a general-purpose AI agent orchestrator.

<table>
<tr>
<td><img src="docs/img/project-chat-00.png" alt="Chatting with the bot — status, suggestions, agent management" width="450"></td>
<td><img src="docs/img/project-chat-01.png" alt="Task started and completed — token usage, change summary" width="450"></td>
</tr>
</table>

## Key Capabilities

### Orchestration & Scheduling
- **Multi-agent, multi-project.** Run parallel Claude Code agents across all your projects, each in isolated workspaces with your existing environment.
- **Proportional scheduling.** Weight projects by priority. The deterministic scheduler distributes agent time fairly — zero LLM calls for orchestration.
- **Rate limit recovery.** Auto-pause on throttle, auto-resume when the window resets. While one agent is paused, others keep working.
- **Full task lifecycle.** DEFINED → READY → ASSIGNED → IN_PROGRESS → COMPLETED, with retry, escalation, and dependency management.

### Playbooks — Workflow Automation
- **DAG-based workflows.** Author multi-step automation as markdown files; an LLM compiles them into executable directed graphs with conditional branching.
- **Human-in-the-loop.** Pause playbook execution at checkpoints for human review before proceeding.
- **Event-driven composition.** Playbooks trigger on system events (task completion, git push, timer) and chain together via event-driven composition.
- **Replaces hooks and rules.** Playbooks supersede the older single-shot hook/rule systems with multi-step reasoning and accumulated context.

### Memory & Knowledge Management
- **4-tier memory architecture.** L0 Identity, L1 Critical Facts, L2 Topic Context, L3 Deep Search — each tier loaded at the right time to minimize token usage and maximize relevance.
- **Semantic search + KV store + temporal facts.** Milvus-backed unified storage with vector search, exact key-value lookups, and time-windowed facts with full history.
- **Scoped knowledge.** System → Agent-Type → Project hierarchy. Knowledge flows from broad to specific, with overrides at each level.
- **Automatic deduplication & merging.** New memories are compared against existing ones; near-duplicates are merged via LLM to prevent knowledge sprawl.

### Self-Improvement Loop
- **Reflection engine.** Post-task review extracts generalizable insights — what worked, what failed, and what to remember.
- **Continuous learning.** Every task leaves the system better prepared for the next one. Error patterns, successful strategies, and project conventions accumulate in memory.
- **Knowledge consolidation.** Daily and weekly consolidation distills raw task memories into structured knowledge bases and project factsheets.
- **No manual intervention.** The improvement loop is autonomous — reflection playbooks run automatically, write insights, and feed them back to future agents.

### Plugin System
- **Extensible architecture.** 5 internal plugins ship by default (files, git, memory, notes, vibecop). Install third-party plugins from git repos.
- **Full integration.** Plugins register tools, subscribe to events, add CLI and Discord commands, and run cron-scheduled functions.
- **Circuit breaker protection.** Failing plugins are auto-disabled to prevent cascading failures.

### Developer Experience
- **Discord + CLI + MCP.** Manage from your phone via Discord, your terminal via the CLI, or any MCP client via the auto-exposed tool server (~100 tools).
- **Vault & Obsidian integration.** All knowledge, playbooks, and profiles stored as markdown in `~/.agent-queue/vault/` — browse and edit with Obsidian or any text editor.
- **Agent profiles.** Configure agent behavior, tools, and MCP servers via markdown profiles. Assign profiles per-project or per-task.
- **Live streaming.** Each task gets a Discord thread. Watch agents work in real time. Reply to unblock them.

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

## Architecture at a Glance

```
asyncio event loop
├── Discord Bot / MCP Server     — control planes (human + machine)
├── Supervisor                   — LLM-powered conversation, tool dispatch, reflection
├── Orchestrator                 — deterministic task lifecycle (zero LLM calls)
│   ├── Scheduler                — proportional credit-weight assignment
│   ├── State Machine            — formal task state transitions
│   ├── Plan Parser              — discovers plans, creates subtask chains
│   └── Playbook Engine          — event-triggered DAG workflows
├── Plugin Registry              — modular tool/event/cron extensibility
├── Memory V2 Service            — 4-tier knowledge with Milvus backend
├── Prompt Builder               — 5-layer context assembly pipeline
└── Adapters                     — agent backends (Claude Code, extensible)
```

**Design philosophy:** Zero LLM calls for orchestration. Structure guides intelligence. Files are the source of truth. The system improves with use. See the [design principles](docs/specs/design/guiding-design-principles.md) for the full philosophy.

## Development

```bash
pip install -e ".[dev,cli]"
pip install -e packages/aq-client      # typed API client (generated)
pre-commit install                     # ruff formatting on every commit
pytest tests/                          # run test suite
./run.sh start                         # start the daemon
```

## Documentation

- **[Full docs](https://electricjack.github.io/agent-queue/)** — architecture, commands, playbooks, adapters
- **[Design specs](docs/specs/design/)** — guiding principles, playbooks, memory, self-improvement, coordination
- **[profile.md](profile.md)** — project architecture, conventions, codebase map

## License

MIT
