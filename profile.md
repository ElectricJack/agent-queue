# Agent Queue — Project Profile

## Overview

Agent Queue orchestrates AI coding agents across project task queues and
playbook-driven workflows. Reviewed documents gate executable work. Durable
knowledge uses versioned PostgreSQL records with explicit scope and authority;
optional context and generation require their own flags and operator gates.
The former four-tier memory and automatic reflection loop are historical.
See [reviews and knowledge](docs/specs/systems/reviews-and-knowledge.md).

**Core design principles:**
1. Vault Markdown owns editable policy; PostgreSQL owns tasks, reviews and knowledge
2. Zero LLM calls for orchestration — all scheduling and state management is deterministic
3. Structure guides intelligence — playbooks encode process knowledge, LLMs provide judgment
4. Knowledge retains sources and history; generation proposes corrections for review
5. Communicate through events, not coupling — EventBus for all inter-component coordination
6. Knowledge scope and authority are checked explicitly; retrieval grants no new authority
7. Optional providers own their dependencies; core knowledge needs no memory plugin
8. Use `aq knowledge` for durable records and `aq record search` for scoped discovery

## Architecture

Single Python asyncio process. Event-driven state machine. SQLAlchemy Core with Alembic migrations (PostgreSQL only). Discord bot + MCP server for control planes. All components communicate through an async EventBus.

```
asyncio event loop
├── Discord Bot / MCP Server     — control planes (human + machine)
├── LLM direct path              — src/llm/: LLMClient.complete/run_tools, intelligence classes
│   ├── PromptBuilder            — role, bounded knowledge context and tools
│   └── ToolRegistry             — tiered tool loading (core + on-demand)
├── Orchestrator                 — deterministic task lifecycle
│   ├── Scheduler                — proportional deficit-based assignment
│   ├── State Machine            — formal task state transitions + DAG validation
│   ├── Smart Cascade            — promotion pipeline (approvals → resume → promote → monitor)
│   ├── Plan Parser              — manual `process_plan` command → subtask chain creation (no auto-discovery)
│   └── Playbook Engine          — compiled DAG workflows
│       ├── PlaybookCompiler     — markdown → JSON graph (LLM-powered, one-shot)
│       ├── PlaybookRunner       — graph walker with conversation history
│       └── PlaybookManager      — lifecycle, triggers, cooldown, concurrency
├── Workflow Coordination        — multi-agent pipeline orchestration
│   ├── Stage Resume Handler     — auto-resume on workflow.stage.completed events
│   ├── Orphan Recovery          — detect & recover stale/crashed workflows
│   └── Pipeline View            — dashboard-ready visualization
├── Plugin Registry              — modular extensibility (tools, events, cron)
│   ├── aq-files                 — file read/write/glob/grep (internal)
│   ├── aq-git                   — branch, commit, push, PR, merge (internal)
│   ├── aq-notes                 — project notes management (internal)
│   ├── aq-vibecop               — static analysis for code quality (internal)
│   └── aq-memory                — semantic search, KV, temporal facts (external; install via `aq plugin install`)
├── Reviews and Knowledge        — PostgreSQL revisions, decisions, records and links
│   ├── Reviews                  — frozen decisions and owned feedback obligations
│   ├── Spec ingestion           — implementation graphs held for fidelity review
│   ├── Context                  — scoped, bounded and observed; explicitly enabled
│   └── Generation               — optional unverified proposals; disabled by default
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
AWAITING_PLAN_APPROVAL  (reachable only via manual `process_plan` command, never auto-discovered;
                         doctor check `tasks.awaiting_plan_approval` flags stranded rows)
```

- DEFINED → READY: all dependencies COMPLETED
- PAUSED tasks always have `resume_after` — never stall permanently
- Failed tasks retry up to configurable limit, then BLOCKED
- Plan-generated tasks: agent produces `.claude/plan.md`; a human (or the requester) runs the `process_plan` command to read and store it for approval — the orchestrator no longer auto-discovers plan files during task completion (see `docs/specs/orchestrator.md` §12)

### Knowledge and legacy memory

Core records work with `memory.enabled: false`; knowledge activation flags
ship disabled and the project allowlist is empty. Context, semantic retrieval
and generation additionally require the memory master and their own gates.
Legacy L1/L2 prompt slots are empty. There is no active four-tier service,
reflection engine or periodic consolidation schedule. The legacy `aq-memory`
and `memory` plugin names are fenced even if installed.

PostgreSQL owns records, revisions, payloads, links and authority. Vault
knowledge exports are derived views; edits mark them diverged rather than
updating the canonical record. Project and global scope are explicit and do
not fall back to each other. See [knowledge records](docs/guides/knowledge-records.md)
and [knowledge context](docs/specs/knowledge-context.md).

## Codebase Map

### Core Files

| File | Purpose |
|------|---------|
| `src/main.py` | Entry point — CLI args, starts async loop |
| `src/orchestrator.py` | **Central brain** — task lifecycle, agent management, rate limit recovery |
| `src/commands/` | **Unified command execution** — 150+ commands, single entry point for Discord + MCP + CLI |
| `src/database/` | SQLAlchemy Core persistence — `tables.py` (schema), `queries/` (mixins), Alembic migrations |
| `src/models.py` | Dataclasses/enums — Task, Agent, Project, Workflow, AgentOutput |
| `src/config.py` | YAML config with `${ENV_VAR}` substitution |
| `src/scheduler.py` | Deficit-based fair-share scheduling (pure function, zero side effects) |
| `src/state_machine.py` | Formal state transitions, DAG cycle detection |
| `src/event_bus.py` | Async pub/sub with wildcard support |

### LLM Direct Path & Prompt System

| File | Purpose |
|------|---------|
| `src/llm/client.py` | `LLMClient.complete`/`run_tools` — the direct LLM path, no in-process Supervisor |
| `src/prompt_builder.py` | Role, overrides, identity and tools; task launch supplies a bounded knowledge bundle in place of legacy L1/L2 memory |
| `src/tools/` | Tiered tool loading — ~11 core tools always loaded, ~80 more on-demand in 11 categories |
| `src/prompt_manager.py` | Manages prompt templates and variants from `src/prompts/` |
| `src/llm_logger.py` | Logs all LLM API calls (`llm.jsonl`) — direct-path calls, agent sessions, prompt analytics |

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

### Hierarchy, Claims & Pools (Swarm Work Model)

Design: `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` (Part I hierarchy,
Part II claims/pools/worker-filed work). Off by default (`swarm.enabled: false`);
`lifecycle: task` profiles are unaffected.

| File | Purpose |
|------|---------|
| `src/database/queries/hierarchy_queries.py` | Single-writer `set_parent`; children/progress reads; close/delete/archive subtree semantics |
| `src/database/hierarchy_migration.py` | Snapshot → canonicalise → validate → apply migration for the parent-edge/column drift |
| `src/database/queries/claim_queries.py` | The one-transaction claim (session-slot CAS → work query → holder rows), `release_claim`, `terminate_pool_session` |
| `src/commands/claim_commands.py` | `_cmd_task_claim` (`task_claim`) — admission checks, long-poll, attempt-outcome resolution |
| `src/orchestrator/pools.py` | `_measure_pools` (`PoolMeasurement`) and the `_reconcile_pools` cascade step — fleet-wide demand/supply aggregation, placement, pool session launch/terminate |
| `src/scheduler.py` | (also) pure `size_pools(...) -> list[PoolAction]` (fleet-wide, one pool per profile) and `place_pool_actions(...)` (which project each start/drain applies to) — table-tested, no I/O |
| `src/sessions/reconciler.py` | (also) pool carve-outs on the session reconciler steps — `_step_prepare_timeout`, pool branches of `_step_orphans`/`_step_exits` |
| `src/doctor/pool_checks.py` | `pools.stuck`, `pools.orphan_agents`, `pools.preparing_stuck`, `pools.stranded_feature_branches`, `pools.global_bounds_migration`, `pools.floor_exceeds_max`, `pools.placement_starved`, `claims.holder_consistency` (report-only), `agents.dangling_current_task` (fixable — resets a BUSY-or-not agent whose `current_task_id` names a missing/non-live task via `reset_stale_busy_agent`), `claims.container_held` (fixable — releases an agent holding a container whose children it did not file; the pool reconcile step does the same for pool holders every tick) |
| `src/profiles/parser.py` | (also) `lifecycle: pool` + `min_active`/`max_active`/`min_per_project`/`max_claims_per_session` parsing |

### Formulas (Swarm Work Model, Part III)

Design: `docs/specs/design/formulas.md`; crosswalk row: swarm-work-model design spec §18 (P7).

| File | Purpose |
|------|---------|
| `src/task_graph/formulas.py` | Registry, vault loading + watcher, `extends` chain resolution, document merge, var validation, `chain_sha` |
| `src/task_graph/creator.py` | `FormulaProvenance` (also carries `create_task_graph`'s plan/report builders); `write_plan` stamps provenance metadata + `formula_snapshot` context row + `formula:<name>` label |
| `src/commands/formula_commands.py` | `formula_list` / `formula_show` (incl. `as_cooked`, read-only) / `formula_cook` (one-transaction create, not agent-scoped) |
| `src/cli/formulas.py` | `aq formula show` / `aq formula cook` — hand-crafted for repeatable `--var k=v`; `aq formula list` is auto-generated |
| `src/doctor/formula_checks.py` | `formulas.parse` — report-only, warns on any formula file that failed to parse |

### Memory & Knowledge

Core owns durable knowledge independently of optional providers.

| File | Purpose |
|------|---------|
| `src/reviews/`, `src/asks/` | Review revisions, frozen decision packets and feedback application |
| `src/records/` | Shared record identities, authorization, search, links and exports |
| `src/knowledge/` | Knowledge history, protection, import, context and optional generation |
| `src/commands/knowledge_commands.py` | Knowledge and records command surface |
| `src/knowledge/legacy.py` | Refuses legacy memory plugin loads |
| `src/facts_parser.py` | Legacy deterministic facts parser; not a knowledge store |
| `src/profile_parser.py` | Profile parser; not a memory backend |

### Plugin System

| File | Purpose |
|------|---------|
| `src/plugins/base.py` | Plugin base class, PluginContext API, TrustLevel, @cron decorator |
| `src/plugins/registry.py` | Central coordinator — discovery, loading, lifecycle, circuit breaker |
| `src/plugins/loader.py` | Plugin install/update/load mechanics |
| `src/plugins/internal/` | Shipped internal plugins: aq-files, aq-git, aq-notes, aq-vibecop (legacy aq-memory loads are fenced) |

### Subsystems

| Directory | Purpose |
|-----------|---------|
| `src/adapters/` | Agent adapter interface + Claude Code implementation |
| `src/discord/` | Notification-only Discord adapter: hourly digest and escalation post/thread delivery out, escalation-thread replies in. One explicit `/delete-task` operator action; the six legacy slash commands and channel routing remain retired |
| `src/git/` | Clone, branch, worktree, push/pull, serialized shared-repo ops |
| `src/tokens/` | Token budget calculation, usage ledger, rate limit tracking |
| `src/llm/` | Direct LLM path — `LLMClient`, provider adapters (Anthropic, Google, OpenAI/Ollama), `LLMCallSpec` |
| `src/prompts/` | System prompts and templates (Mustache-style `{{placeholder}}`) |
| `src/messaging/` | Messaging transport port + adapter factory (`discord`, `none`) |
| `src/plugins/` | Plugin system for extensibility |
| `src/embedded_mcp.py`, `src/mcp_registration.py` | MCP server, embedded in the daemon — auto-exposes all CommandHandler commands as MCP tools |
| `packages/aq-client/` | Typed API client (generated) for CLI and external tools |
| `docs/specs/` | Behavioral specifications (source of truth) |
| `docs/specs/design/` | Design specs (playbooks, memory, self-improvement, coordination, vault, profiles, roadmap) |

## Design Decisions

### Why zero LLM for orchestration?
Every token is precious. Scheduling, dependency resolution, state transitions — all deterministic. Models execute agent tasks and explicitly configured playbook/generation work.
Optional knowledge extraction and consolidation produce proposals; no reflection
engine runs on every completed task.

### Why playbooks over hooks/rules?
Hooks were single-shot LLM calls with no context accumulation or multi-step reasoning. Playbooks model workflows as directed graphs: each node is a focused LLM decision point, transitions carry context forward, and human checkpoints enable oversight. Markdown authoring keeps them human-readable; LLM compilation makes them executable.

### Why bounded knowledge context?

One scoped bundle serves prime and launch paths with a shared token budget and
observed delivery. It replaces the old L1/L2 memory slots. Storage, context,
semantic providers and generation have independent enablement controls.

### Which state is canonical?

Vault Markdown owns editable playbooks and profiles. PostgreSQL owns durable
reviews, decisions and knowledge. A review file mirrors a database revision;
a knowledge export projects a record revision. Neither is a file-watching
import path. Back up the database and retained vault artifacts together.

### Why PostgreSQL only?
Lightweight, zero-ops. Single process means no need for distributed locking. WAL mode gives concurrent reads. Survives restarts. Runs on a Raspberry Pi. SQLAlchemy Core provides dialect portability — PostgreSQL supported via asyncpg for production deployments.

### Why Discord is *not* the control plane
It was one, and the surface grew faster than it could be trusted: 122 mirrored slash commands, task buttons and per-task streaming threads all mutating work from a chat client. The dashboard, CLI and MCP now own browsing, controls, gates and logs; Discord is primarily the phone-shaped notification surface — one shared channel carrying an hourly activity digest and one thread per human decision. A reply in that thread is evidence for the owning project supervisor, never a direct task mutation. See `docs/guides/discord-commands.md` and `docs/guides/discord-migration.md`.

### Why the Command Pattern?
`CommandHandler` is the single execution point for all operations. Playbook nodes, MCP tools, the dashboard API and the CLI all delegate here — ensures feature parity and consistent error handling across all interfaces. The Discord adapter follows the same rule: it calls `escalation_reply` and the explicit operator `delete_task` action; it never touches the database or a session directly. The deletion exception is specified in `docs/superpowers/specs/2026-10-11-unified-task-delete.md`.

### Why plugins?
Internal functionality (file ops, git, notes, code quality) is implemented as plugins with the same API available to third parties. This enforces clean boundaries, enables selective loading, and stress-tests the plugin API with real complexity.

### How does knowledge improve?

Agents can save sourced, unverified findings. Optional extraction and
consolidation propose revisions; authorized decisions confer verification or
policy authority. No autonomous reflection loop or scheduled consolidation
policy is shipped. See [knowledge records](docs/guides/knowledge-records.md).

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

Key sections: `discord`, `scheduling`, `auto_task`, `pause_retry`, `hook_engine`, `llm`, `memory`, `mcp_server`, `plugins`

## Vault Structure

```
~/.agent-queue/vault/
├── system/
│   ├── playbooks/          # System-wide automation (e.g., task-outcome.md)
│   ├── memory/             # Retained legacy memory sources
│   └── facts.md            # System-level KV facts
├── orchestrator/
│   ├── memory/             # Retained legacy memory sources
│   └── facts.md
├── agent-types/
│   └── {type}/
│       ├── profile.md      # Role, capabilities, allowed_tools (bare names), mcp_servers (list of registry names)
│       ├── playbooks/      # Agent-type automation
│       ├── memory/         # Retained legacy memory sources
│       └── facts.md
├── mcp-servers/            # System-scope MCP server registry (one file per server)
└── projects/
    └── {id}/
        ├── agent-types/    # Project profile overrides (per agent-type)
        │   └── {type}/profile.md
        ├── mcp-servers/    # Project-scope MCP registry (shadows system by name)
        ├── facts.md        # Project KV facts (tech stack, conventions, etc.)
        ├── playbooks/      # Project-specific automation
        ├── memory/         # Retained legacy memory sources
        ├── notes/          # Auto-generated notes
        ├── references/     # Synced workspace docs
        ├── knowledge/      # Derived exports; PostgreSQL is canonical
        │   └── records/<kn-alias>.md
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
- **Dependencies:** sqlalchemy[asyncio], asyncpg, alembic, discord.py, claude-agent-sdk, pyyaml (legacy memsearch is not a core dependency)

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
- Durable knowledge is core-owned; legacy `aq-memory` / `memory` plugin loads are fenced
