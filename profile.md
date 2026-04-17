# Agent Queue — Project Profile

## Overview

Agent Queue is a self-improving orchestration platform for AI coding agents. It manages task queues across multiple projects, coordinates multi-agent workflows through playbooks, accumulates knowledge via a 4-tier memory system, and continuously improves through automated reflection. The core value proposition: the system gets better with use — every task feeds the reflection engine, insights accumulate in scoped memory, and future agents benefit automatically.

**Core design principles:**
1. Human-readable files as source of truth (vault, playbooks, profiles)
2. Zero LLM calls for orchestration — all scheduling and state management is deterministic
3. Structure guides intelligence — playbooks encode process knowledge, LLMs provide judgment
4. The system improves with use — every task leaves the system better prepared for the next one
5. Communicate through events, not coupling — EventBus for all inter-component coordination
6. Specificity wins — project → agent-type → system hierarchy; local knowledge overrides global
7. Plugins own their dependencies — memory plugin brings its own Milvus backend
8. Simple interfaces, smart routing — `memory_get()` auto-routes KV vs semantic search

## Architecture

Single Python asyncio process. Event-driven state machine. SQLAlchemy Core with Alembic migrations (SQLite default, PostgreSQL supported). Discord bot + MCP server for control planes. All components communicate through an async EventBus.

```
asyncio event loop
├── Discord Bot / MCP Server     — control planes (human + machine)
├── Supervisor                   — LLM conversation, tool dispatch, reflection
│   ├── PromptBuilder            — 5-layer context assembly (L0-L3 + tools)
│   ├── ReflectionEngine         — post-action review with depth tiers
│   └── ToolRegistry             — tiered tool loading (core + on-demand)
├── Orchestrator                 — deterministic task lifecycle
│   ├── Scheduler                — proportional deficit-based assignment
│   ├── State Machine            — formal task state transitions + DAG validation
│   ├── Smart Cascade            — promotion pipeline (approvals → resume → promote → monitor)
│   ├── Plan Parser              — plan discovery → subtask chain creation
│   └── Playbook Engine          — compiled DAG workflows
│       ├── PlaybookCompiler     — markdown → JSON graph (LLM-powered, one-shot)
│       ├── PlaybookRunner       — graph walker with conversation history
│       └── PlaybookManager      — lifecycle, triggers, cooldown, concurrency
├── Workflow Coordination        — multi-agent pipeline orchestration
│   ├── Stage Resume Handler     — auto-resume on workflow.stage.completed events
│   ├── Orphan Recovery          — detect & recover stale/crashed workflows
│   └── Pipeline View            — dashboard-ready visualization
├── Plugin Registry              — modular extensibility (tools, events, cron)
│   ├── aq-files                 — file read/write/glob/grep
│   ├── aq-git                   — branch, commit, push, PR, merge
│   ├── aq-memory-v2             — semantic search, KV, temporal facts
│   ├── aq-notes                 — project notes management
│   └── aq-vibecop               — static analysis for code quality
├── Memory V2 Service            — Milvus-backed 4-tier knowledge
│   ├── Semantic search          — multi-scope weighted vector search
│   ├── KV store                 — fast scalar lookups per scope
│   ├── Temporal facts           — validity-windowed facts with history
│   └── Memory Extractor         — auto-extracts knowledge from events
├── EventBus                     — async pub/sub with wildcard + payload filtering
├── File Watcher                 — mtime-based change detection
├── Workspace Spec Watcher       — syncs project specs to vault
└── Adapters                     — agent backends (Claude Code, extensible)
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
AWAITING_PLAN_APPROVAL  (plan discovered, awaiting approval to split)
```

- DEFINED → READY: all dependencies COMPLETED
- PAUSED tasks always have `resume_after` — never stall permanently
- Failed tasks retry up to configurable limit, then BLOCKED
- Plan-generated tasks: agent produces `.claude/plan.md` → orchestrator parses → chained subtasks with dependencies

### Memory Tiers

| Tier | Name | Budget | When Loaded | Contains |
|------|------|--------|-------------|----------|
| L0 | Identity | ~50 tokens | Always | Agent type profile `## Role` section |
| L1 | Critical Facts | ~200 tokens | Task start | Project + agent-type `facts.md` KV entries |
| L2 | Topic Context | ~500 tokens | On-demand | Memories matching task topic (pre-filtered) |
| L3 | Deep Search | Variable | Explicit query | Full semantic search across all scopes |

### Memory Scopes

| Scope | Collection | Vault Path |
|-------|-----------|------------|
| System | `aq_system` | `vault/system/memory/`, `vault/system/facts.md` |
| Orchestrator | `aq_orchestrator` | `vault/orchestrator/memory/` |
| Agent Type | `aq_agenttype_{type}` | `vault/agent-types/{type}/memory/` |
| Project | `aq_project_{id}` | `vault/projects/{id}/memory/`, `vault/projects/{id}/notes/` |

## Codebase Map

### Core Files

| File | Purpose |
|------|---------|
| `src/main.py` | Entry point — CLI args, starts async loop |
| `src/orchestrator.py` | **Central brain** — task lifecycle, agent management, rate limit recovery |
| `src/commands/` | **Unified command execution** — 150+ commands, single entry point for Discord + MCP + CLI |
| `src/supervisor.py` | **Supervisor** — multi-turn LLM conversation loop, tool dispatch, streaming |
| `src/database/` | SQLAlchemy Core persistence — `tables.py` (schema), `queries/` (mixins), Alembic migrations |
| `src/models.py` | Dataclasses/enums — Task, Agent, Project, Workflow, AgentOutput |
| `src/config.py` | YAML config with `${ENV_VAR}` substitution |
| `src/scheduler.py` | Deficit-based fair-share scheduling (pure function, zero side effects) |
| `src/state_machine.py` | Formal state transitions, DAG cycle detection |
| `src/event_bus.py` | Async pub/sub with wildcard support |

### Supervisor & Prompt System

| File | Purpose |
|------|---------|
| `src/prompt_builder.py` | 5-layer prompt assembly: L0 role → override → L1 facts → L2 context → identity → tools |
| `src/tools/` | Tiered tool loading — ~11 core tools always loaded, ~80 more on-demand in 11 categories |
| `src/reflection.py` | Post-action reflection engine — deep/standard/light tiers with circuit breaker |
| `src/prompt_manager.py` | Manages prompt templates and variants from `src/prompts/` |
| `src/rule_manager.py` | User-defined rules injected into Supervisor prompts (deprecated, migrating to playbooks) |
| `src/chat_observer.py` | Observes agent chat streams, detects questions and key events |
| `src/llm_logger.py` | Logs all LLM API calls — chat provider calls, agent sessions, prompt analytics |

### Playbook System

| File | Purpose |
|------|---------|
| `src/playbooks/models.py` | Data models: CompiledPlaybook, PlaybookNode, PlaybookTransition, PlaybookRun |
| `src/playbooks/compiler.py` | LLM-powered markdown → JSON graph compiler with retry/validation |
| `src/playbooks/runner.py` | Graph walker — steps through nodes maintaining conversation history |
| `src/playbooks/manager.py` | Lifecycle: compilation, versioning, trigger mapping, cooldown, concurrency |
| `src/playbooks/store.py` | Disk storage with scope-mirrored directory structure |
| `src/playbooks/handler.py` | Vault watcher — detects `.md` changes, dispatches to compiler |
| `src/playbooks/state_machine.py` | Formal state machine for run lifecycle |
| `src/playbooks/resume_handler.py` | Human-in-the-loop resume logic |
| `src/playbooks/health.py` | Metrics computation for run analysis |
| `src/playbooks/graph.py` | Graph rendering (ASCII + Mermaid visualization) |

### Workflow Coordination

| File | Purpose |
|------|---------|
| `src/workflow_stage_resume_handler.py` | Auto-resume paused playbooks on `workflow.stage.completed` events |
| `src/orphan_workflow_recovery.py` | Detect & recover workflows whose coordination playbook died (startup + periodic) |
| `src/workflow_pipeline_view.py` | Dashboard-ready pipeline visualization (stages, tasks, agents, progress) |

### Memory & Knowledge

| File | Purpose |
|------|---------|
| `src/memory_v2_service.py` | Memory backend — Milvus via memsearch fork, multi-scope search/KV/temporal |
| `src/memory_extractor.py` | Auto-extracts knowledge from system events (task completion, failures, etc.) |
| `src/facts_parser.py` | Deterministic `facts.md` parser — KV pairs with namespace support |
| `src/profile_parser.py` | Parses hybrid markdown profiles (English + JSON blocks) |
| `src/memory.py` | Legacy memory system (deprecated, replaced by memory_v2_service) |

### Plugin System

| File | Purpose |
|------|---------|
| `src/plugins/base.py` | Plugin base class, PluginContext API, TrustLevel, @cron decorator |
| `src/plugins/registry.py` | Central coordinator — discovery, loading, lifecycle, circuit breaker |
| `src/plugins/loader.py` | Plugin install/update/load mechanics |
| `src/plugins/internal/` | Shipped plugins: aq-files, aq-git, aq-memory-v2, aq-notes, aq-vibecop |

### Subsystems

| Directory | Purpose |
|-----------|---------|
| `src/adapters/` | Agent adapter interface + Claude Code implementation |
| `src/discord/` | Bot, slash commands, notifications, channel routing |
| `src/git/` | Clone, branch, worktree, push/pull, serialized shared-repo ops |
| `src/tokens/` | Token budget calculation, usage ledger, rate limit tracking |
| `src/chat_providers/` | LLM provider abstraction (Anthropic, Gemini, Ollama) |
| `src/prompts/` | System prompts and templates (Mustache-style `{{placeholder}}`) |
| `src/messaging/` | Cross-platform messaging abstraction |
| `src/telegram/` | Telegram bot integration |
| `src/plugins/` | Plugin system for extensibility |
| `packages/mcp_server/` | MCP server — auto-exposes all CommandHandler commands as MCP tools |
| `packages/memsearch/` | Milvus-backed semantic memory engine (fork of zilliztech/memsearch) |
| `packages/aq-client/` | Typed API client (generated) for CLI and external tools |
| `docs/specs/` | Behavioral specifications (source of truth) |
| `docs/specs/design/` | Design specs (playbooks, memory, self-improvement, coordination, vault, profiles, roadmap) |

## Design Decisions

### Why zero LLM for orchestration?
Every token is precious. Scheduling, dependency resolution, state transitions — all deterministic. The only LLM calls are: (1) agent task execution, (2) Supervisor chat, (3) playbook node execution, (4) playbook compilation, (5) memory revision/merging, (6) reflection, (7) knowledge extraction.

### Why playbooks over hooks/rules?
Hooks were single-shot LLM calls with no context accumulation or multi-step reasoning. Playbooks model workflows as directed graphs: each node is a focused LLM decision point, transitions carry context forward, and human checkpoints enable oversight. Markdown authoring keeps them human-readable; LLM compilation makes them executable.

### Why a 4-tier memory architecture?
Not all knowledge is needed at all times. L0/L1 are cheap (always loaded, ~250 tokens). L2 activates on-demand when the task topic is detected. L3 requires explicit search. This keeps prompt size small while ensuring critical context is always available.

### Why files as source of truth?
Playbooks, profiles, facts, and knowledge are all markdown files in `~/.agent-queue/vault/`. This makes them browsable in Obsidian, editable by hand, diffable with git, and transparent. The database and Milvus are derived indexes, not canonical stores.

### Why SQLite (with PostgreSQL supported)?
Lightweight, zero-ops. Single process means no need for distributed locking. WAL mode gives concurrent reads. Survives restarts. Runs on a Raspberry Pi. SQLAlchemy Core provides dialect portability — PostgreSQL supported via asyncpg for production deployments.

### Why Discord as control plane?
Users manage from their phone. Natural language via Supervisor backed by LLM tools. Each task gets a thread for live streaming. Reply to threads to unblock agents.

### Why the Command Pattern?
`CommandHandler` is the single execution point for all operations. Discord slash commands, Supervisor tools, MCP tools, and CLI all delegate here — ensures feature parity and consistent error handling across all interfaces.

### Why plugins?
Internal functionality (file ops, git, memory, code quality) is implemented as plugins with the same API available to third parties. This enforces clean boundaries, enables selective loading, and stress-tests the plugin API with real complexity.

### Why the self-improvement loop?
The core value proposition: the system gets better with use. Reflection extracts insights from completed tasks. Memory extraction captures patterns from events. Knowledge consolidation organizes them. Memory tiers deliver them at the right time. Playbooks automate the consolidation cycle. No manual intervention needed — the loop is autonomous.

### Why workflow coordination via playbooks?
Multi-agent workflows (code → review → QA) are just playbooks with stage gates and agent affinity. The same execution model, same event system, same human-in-the-loop checkpoints. Workflows track stage progress and task assignments; orphan recovery handles daemon restarts and stale state. No separate workflow engine needed.

### Why spec-driven development?
Specs in `docs/specs/` are the source of truth. Flow: specs → implementation → tests → docs. When spec and code disagree, the spec is correct.

## Database Schema

21+ tables defined as SQLAlchemy Core `Table` objects in `src/database/tables.py`. Migrations managed by Alembic (`migrations/`).

**Core:** `projects`, `repos`, `tasks`, `task_dependencies`, `agents`, `token_ledger`, `events`, `rate_limits`
**Workflows:** `workflows` (multi-agent pipeline state, stages, task assignments, agent affinity)
**Playbooks:** `playbook_runs`, `compiled_playbooks` (run state, node traces, compiled graphs)
**Supporting:** `task_criteria`, `task_context`, `task_tools`, `task_results`, `system_config`, `workspaces`, `agent_profiles`, `archived_tasks`, `chat_analyzer_suggestions`, `plugins`, `plugin_data`

## Configuration

File: `~/.agent-queue/config.yaml` (YAML with `${ENV_VAR}` substitution)

Key sections: `discord`, `scheduling`, `auto_task`, `pause_retry`, `hook_engine`, `chat_provider`, `memory`, `mcp_server`, `plugins`

## Vault Structure

```
~/.agent-queue/vault/
├── system/
│   ├── playbooks/          # System-wide automation (e.g., task-outcome.md)
│   ├── memory/             # System-wide knowledge
│   └── facts.md            # System-level KV facts
├── orchestrator/
│   ├── memory/             # Orchestrator operational knowledge
│   └── facts.md
├── agent-types/
│   └── {type}/
│       ├── profile.md      # Role, capabilities, tools, MCP servers
│       ├── playbooks/      # Agent-type automation
│       ├── memory/         # Cross-project agent wisdom
│       └── facts.md
└── projects/
    └── {id}/
        ├── profile.md      # Project-specific profile
        ├── facts.md        # Project KV facts (tech stack, conventions, etc.)
        ├── playbooks/      # Project-specific automation
        ├── memory/         # Project memories and insights
        ├── notes/          # Auto-generated notes
        ├── references/     # Synced workspace docs
        ├── knowledge/      # Organized topic-based knowledge
        │   ├── architecture.md
        │   ├── deployment.md
        │   ├── gotchas.md
        │   └── ...
        └── overrides/
            └── {agent_type}.md  # Project-specific agent guidance
```

## Code Conventions

- Python 3.12+, async/await throughout (asyncio)
- **Async-first I/O:** All production code uses non-blocking I/O. Git ops use `GitManager`'s async API (`a`-prefixed methods). No sync `subprocess.run()` in production.
- Database operations return dicts or dataclass instances from `models.py`
- Commands return structured dicts with `success` boolean and data/error fields
- Notifications via `_notify_channel()` for project-aware Discord routing
- Git operations wrapped in `GitManager` with `GitError` exceptions
- **Linter:** ruff (line-length 100, target py312)
- **Tests:** pytest with pytest-asyncio (`asyncio_mode = "auto"`)
- **Dependencies:** sqlalchemy[asyncio], aiosqlite, alembic, discord.py, claude-agent-sdk, pyyaml, memsearch

## Infrastructure

- **Daemon:** Runs as a background process via `run.sh` (single Python asyncio process)
- **Discord bot:** Primary human interface, requires bot token + guild ID + channel config
- **MCP server:** Embedded in daemon, auto-exposes ~150 CommandHandler commands via streamable-http transport
- **GitHub integration:** Git operations for branching, PRs, worktrees per task
- **Multi-provider:** Anthropic direct, AWS Bedrock, Google Vertex AI, Gemini, Ollama for LLM calls
- **Plugin ecosystem:** Internal plugins + third-party plugins from git repos
- **Obsidian vault:** `~/.agent-queue/vault/` for transparent knowledge browsing/editing

## Known Architectural Notes

- `state_machine.py` defines valid transitions but they're not enforced in production — all transitions use direct `db.update_task()`
- Plan-generated tasks can get stuck in DEFINED due to inherited approval settings blocking dependency chains
- The system uses workspace isolation per task (git worktrees or separate clones)
- Rules/hooks are deprecated; playbooks are the replacement (migration in progress)
- Memory v1 (`src/memory.py`) is deprecated; Memory v2 (`src/memory_v2_service.py` + `aq-memory-v2` plugin) is the active system
