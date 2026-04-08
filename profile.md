# Agent Queue — Project Profile

## Overview

Agent Queue is a task queue and orchestrator for AI coding agents (primarily Claude Code) on throttled/subsidized token plans. It keeps agents continuously busy across multiple projects, handles rate limits automatically, and provides full control via Discord.

**Key principle:** Zero LLM calls for orchestration — all scheduling is deterministic. Every token goes to agent work.

## Architecture

Single Python asyncio process. Event-driven state machine. SQLAlchemy Core with Alembic migrations (SQLite default, PostgreSQL supported). Discord bot for the control plane. All components communicate through an async EventBus.

```
asyncio event loop
├── Discord Bot          — commands/messages, delegates to Supervisor
├── Orchestrator Loop    — runs every ~5s, manages task lifecycle
│   ├── Promote DEFINED → READY (dependency check)
│   ├── Detect dead agents (heartbeat)
│   ├── Assign READY tasks to idle agents (Scheduler)
│   ├── Execute tasks (stream to Discord threads)
│   ├── Check AWAITING_APPROVAL tasks
│   ├── Run hook engine tick
│   └── Re-check newly generated tasks
└── Shutdown Handler     — graceful cleanup
```

### Task State Machine

```
DEFINED → READY → ASSIGNED → IN_PROGRESS → COMPLETED
                                  │
                        ┌─────────┼──────────┐
                        ▼         ▼          ▼
                     PAUSED   WAITING    FAILED
                     (auto-   _INPUT     (retry →
                     resume)  (Discord)   BLOCKED)

AWAITING_APPROVAL  (post-work, pre-merge — requires manual approve)
```

- DEFINED → READY: all dependencies COMPLETED
- PAUSED tasks always have `resume_after` — never stall permanently
- Failed tasks retry up to configurable limit, then BLOCKED
- Plan-generated tasks: agent produces `.claude/plan.md` → orchestrator parses → chained subtasks with dependencies

## Codebase Map

### Core Files

| File | Purpose |
|------|---------|
| `src/main.py` | Entry point — CLI args, starts async loop |
| `src/orchestrator.py` | **Central brain** — task lifecycle, agent management, rate limit recovery |
| `src/command_handler.py` | **Unified command execution** — 50+ commands, single entry point |
| `src/supervisor.py` | **Supervisor** — multi-turn LLM conversation loop, tool dispatch, streaming |
| `src/chat_agent.py` | Backward-compat shim — re-exports `Supervisor` as `ChatAgent` (deprecated) |
| `src/database/` | SQLAlchemy Core persistence — tables.py (schema), queries/ (mixins), Alembic migrations |
| `src/models.py` | Dataclasses/enums — Task, Agent, Project, Hook, AgentOutput |
| `src/config.py` | YAML config with `${ENV_VAR}` substitution |
| `src/scheduler.py` | Proportional credit-weight scheduling |
| `src/state_machine.py` | Formal state transitions, DAG cycle detection |
| `src/event_bus.py` | Async pub/sub with wildcard support |
| `src/tool_registry.py` | Central registry of all Supervisor tools — definitions and metadata |
| `src/prompt_builder.py` | Assembles the Supervisor system prompt from context tiers |
| `src/prompt_manager.py` | Manages prompt templates and variants |
| `src/rule_manager.py` | User-defined rules injected into Supervisor prompts |
| `src/reflection.py` | Post-task reflection — summarizes work, extracts lessons learned |
| `src/chat_observer.py` | Observes agent chat streams, detects questions and key events |
| `src/llm_logger.py` | Logs all LLM API calls for debugging and token accounting |
| `src/schedule.py` | Scheduled/recurring task support |
| `src/memory.py` | Smart-forge memory — profiles, notes, semantic search, compaction |
| `src/hooks.py` | Hook engine — event/periodic triggers, context steps, LLM invocation |
| `src/plan_parser.py` | Parses plan files into structured steps for task generation |
| `src/health.py` | System health checks and diagnostics |
| `src/file_watcher.py` | Mtime-based change detection, emits file/folder events |
| `src/task_names.py` | Human-readable task IDs (adjective-noun, ~900 combos) |
| `src/agent_names.py` | Creative agent name generation for personality-rich identifiers |
| `src/known_tools.py` | Registry of known Claude Code tools and MCP servers for validation |
| `src/logging_config.py` | Structured JSON logging with correlation IDs |
| `src/setup_wizard.py` | Interactive first-time setup — Discord token, API keys, agent provisioning |

### Subsystems

| Directory | Purpose |
|-----------|---------|
| `src/adapters/` | Agent adapter interface + Claude Code implementation |
| `src/discord/` | Bot, slash commands, notifications, channel routing |
| `src/git/` | Clone, branch, worktree, push/pull operations |
| `src/tokens/` | Token budget calculation and usage ledger |
| `src/chat_providers/` | LLM provider abstraction (Anthropic, Ollama) |
| `src/prompts/` | System prompts and templates (chat agent, memory revision, etc.) |
| `src/messaging/` | Cross-platform messaging abstraction |
| `src/telegram/` | Telegram bot integration |
| `src/plugins/` | Plugin system for extensibility |
| `packages/mcp_server/` | MCP server — auto-exposes all CommandHandler commands as MCP tools |
| `docs/specs/` | Behavioral specifications (source of truth) |
| `docs/` | Documentation and specs |

## Design Decisions

### Why SQLite (with PostgreSQL supported)?
Lightweight, zero-ops. Single process means no need for distributed locking. WAL mode gives concurrent reads. Survives restarts. Runs on a Raspberry Pi. SQLAlchemy Core provides dialect portability — PostgreSQL is fully supported via asyncpg for production deployments.

### Why zero LLM for orchestration?
On throttled plans, every token is precious. Scheduling, dependency resolution, state transitions — all deterministic. The only LLM calls are: (1) agent task execution, (2) Supervisor chat, (3) hooks, (4) memory revision, (5) reflection.

### Why Discord as control plane?
Users manage from their phone. Natural language via Supervisor backed by LLM tools. Each task gets a thread for live streaming. Reply to threads to unblock agents.

### Why the Command Pattern?
`CommandHandler` is the single execution point for all operations. Discord slash commands and Supervisor tools both delegate here — ensures feature parity and consistent error handling.

### Why the Adapter Pattern?
`AgentAdapter` interface allows pluggable agent types. Currently Claude Code; designed for extensibility.

### Why spec-driven development?
Specs in `docs/specs/` are the source of truth. Flow: specs → implementation → tests → docs. When spec and code disagree, the spec is correct.

### Smart-Forge Memory System
Per-project `profile.md` (stored in `~/.agent-queue/memory/{project_id}/`) captures synthesized knowledge. After each task, an LLM call revises the profile. Notes flow bidirectionally. Old task memories get compacted into weekly digests. Context is delivered in tiers: profile → project docs → notes → recent tasks → semantic search.

## Database Schema

21 tables defined as SQLAlchemy Core `Table` objects in `src/database/tables.py`. Migrations managed by Alembic (`migrations/`). Core: `projects`, `repos`, `tasks`, `task_dependencies`, `agents`, `token_ledger`, `events`, `rate_limits`. Supporting: `task_criteria`, `task_context`, `task_tools`, `task_results`, `hooks`, `hook_runs`, `system_config`, `workspaces`, `agent_profiles`, `archived_tasks`, `chat_analyzer_suggestions`, `plugins`, `plugin_data`.

## Configuration

File: `~/.agent-queue/config.yaml` (YAML with `${ENV_VAR}` substitution)

Key sections: `discord`, `scheduling`, `auto_task`, `pause_retry`, `hook_engine`, `chat_provider`, `memory`

## Code Conventions

- Python 3.12+, async/await throughout (asyncio)
- **Async-first I/O:** All production code uses non-blocking I/O. Git ops use `GitManager`'s async API (`a`-prefixed methods). No sync `subprocess.run()` in production.
- Database operations return dicts or dataclass instances from `models.py`
- Commands return structured dicts with `success` boolean and data/error fields
- Notifications via `_notify_channel()` for project-aware Discord routing
- Git operations wrapped in `GitManager` with `GitError` exceptions
- **Linter:** ruff (line-length 100, target py312)
- **Tests:** pytest with pytest-asyncio (`asyncio_mode = "auto"`)
- **Dependencies:** sqlalchemy[asyncio], aiosqlite, alembic, discord.py, claude-agent-sdk, pyyaml

## Infrastructure

- **Docker:** Not currently containerized — runs as a background daemon via `run.sh`
- **Discord bot:** Primary interface, requires bot token + guild ID + channel config
- **GitHub integration:** Git operations for branching, PRs, worktrees per task
- **MCP server:** `packages/mcp_server/` auto-exposes all CommandHandler commands (~100 tools) via Model Context Protocol, with configurable exclusions (see `docs/specs/mcp-server.md`)
- **Multi-provider:** Anthropic direct, AWS Bedrock, Google Vertex AI for LLM calls

## Known Architectural Notes

- `state_machine.py` defines valid transitions but they're not enforced in production — all transitions use direct `db.update_task()`
- Plan-generated tasks can get stuck in DEFINED due to inherited approval settings blocking dependency chains
- The system uses workspace isolation per task (git worktrees or separate clones)
