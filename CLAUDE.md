# CLAUDE.md

Agent Queue — self-improving orchestration platform for AI coding agents. Manages task queues, coordinates multi-agent workflows via playbooks, accumulates knowledge through a 4-tier memory system, and continuously improves through automated reflection. The core value proposition: the system gets better with use — every task feeds the reflection engine, insights accumulate in scoped memory, and future agents benefit automatically. Discord + MCP + CLI controlled, SQLAlchemy-backed (SQLite default, PostgreSQL supported), fully async Python.

## Quick Reference

- **Entry point:** `src/main.py` → orchestrator + Discord bot + embedded MCP server
- **Core files:** `orchestrator.py`, `src/commands/` (handler + mixin modules), `supervisor.py`, `database/` (including `src/database/queries/hierarchy_queries.py` — task hierarchy: `set_parent`, children/progress reads, close/delete/archive subtree semantics), `models.py`
- **Playbooks:** `src/playbooks/` (compiler, runner, manager, models, store, handler, state_machine, health, graph, graph_view, resume_handler)
- **Memory:** External `aq-memory` plugin (install via `aq plugin install`), plus in-tree `facts_parser.py`, `profile_parser.py`
- **Profiles:** `src/profiles/` — `parser.py`, `sync.py`, `migration.py`. Markdown source of truth in `vault/agent-types/<id>/profile.md` (system) and `vault/projects/<pid>/agent-types/<id>/profile.md` (project override). The profile's `## Config.harness` field selects which CLI runs the agent (`claude`, `codex`, `gemini` — see `src/sessions/default_harnesses/`); every coding agent runs as a tmux session. `## Config.runtime` now has exactly one value, `"supervisor"` (in-process, tool-call-only, no workspace). The `claude_sdk` and `acpx` runtimes and the `agent_name` config key were **removed** in the tmux-harness migration; `parser.py` rejects `agent_name` with a pointer to `harness`.
- **Runtimes:** `src/runtimes/` — `base.py` (Runtime ABC, Capability enum, `requires_workspace` ClassVar) and `supervisor.py` (Supervisor — both the chat brain AND a registered Runtime singleton; tool-call-only, no workspace). **That is the whole list**: since the tmux-harness migration every coding agent runs as a session (a CLI wrapped in tmux, chosen by `harness`), so no Runtime class spawns agents. Supervisor stays only because it is not a coding agent — there is no CLI to wrap. `RuntimeRegistry.create(name, profile=...)` returns the supervisor singleton verbatim when registered via `default_registry(supervisor=...)`.
- **MCP registry:** `src/profiles/mcp_registry.py` (in-memory registry + vault watcher), `mcp_probe.py` (parallel probes, 10s timeout), `mcp_catalog.py` (cache), `mcp_inline_migration.py` (legacy extractor). Source of truth: `vault/[projects/<pid>/]mcp-servers/*.md`. Profiles reference servers by name.
- **Workspaces:** `src/orchestrator/workspace.py` + `src/orchestrator/workspace_attachments.py` (multi-kind acquisition), `src/database/queries/workspace_queries.py` + `workspace_kinds_queries.py` + `task_requirements_queries.py`. Tasks declare `requires_kinds` at creation; orchestrator atomically acquires one workspace per declared kind in canonical lock order. Kinds are markdown in `vault/[projects/<pid>/]workspace-kinds/<id>.md` (system + project override) — built-ins seeded by migration: `project-repo` (writable, exclusive lock), `vault` (auto-attached, no lock), `readonly-dir`. See `docs/specs/design/workspaces-v2.md`.
- **Formulas:** `src/task_graph/formulas.py` (registry, `extends` merge, vars), `src/commands/formula_commands.py` (`formula_list|show|cook`), provenance in `creator.write_plan`. Files: `vault/[projects/<pid>/]formulas/<name>.md` (frontmatter + one `aq-graph` block). Spec: design spec Part III.
- **Swarm (claims/pools):** `src/database/queries/claim_queries.py` (claim transaction, epoch fence), `src/commands/claim_commands.py` (`task_claim`), `src/orchestrator/pools.py` (`_reconcile_pools`; pure `size_pools` in `scheduler.py`), pool carve-outs in `src/sessions/reconciler.py`, checks in `src/doctor/pool_checks.py`. Profiles with `lifecycle: pool` pull work via `aq task claim`; `lifecycle: task` keeps push. Off by default (`swarm.enabled`). Spec: `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part II.
- **Config editor:** `src/config_editor.py` — ruamel round-trip writer behind `get_config` / `update_config` / `get_config_schema`. Validates via temp-file `load_config()` before swap.
- **Intelligence:** `prompt_builder.py`, `tools/registry.py`, `reflection.py`, `llm_logger.py`, `chat_observer.py`
- **Workflows:** `workflow_stage_resume_handler.py`, `orphan_workflow_recovery.py`, `workflow_pipeline_view.py`
- **Plugins:** `src/plugins/` (base, registry, loader, internal/)
- **Internal plugins:** `src/plugins/internal/` (aq-files, aq-git, aq-notes, aq-vibecop)
- **Messages:** `src/messages/` — `session_lens.py` (`SessionManagerProto`/`SessionLens`; supervisor messaging address `supervisor-<pid>` → runtime name `n-supervisor--<pid>`), `delivery.py` (`MessageDeliveryEngine`; per-`to_kind` delivery policy; parking for stale session rows; transcript-tail fallback). Cascade step in `run_one_cycle` behind `messages.enabled`; `aq inbox --inject` hook in claude harness; prime surfaces pending messages via `via="prime"`.
- **Subsystems:** `src/runtimes/`, `src/discord/`, `src/git/`, `src/tokens/`, `src/chat_providers/`, `src/messaging/`
- **Specs:** `docs/specs/` (source of truth — specs first, then code)
- **Design specs:** `docs/specs/design/` (principles, playbooks, memory, self-improvement, coordination, vault, profiles, roadmap)
- **Config:** `~/.agent-queue/config.yaml`
- **Vault:** `~/.agent-queue/vault/` (playbooks, profiles, memory, facts — all markdown)
- **Packages:** `packages/aq-client/` (typed API client)

## Development

```bash
pip install -e ".[dev,cli]"
pip install -e packages/aq-client      # typed API client (generated)
pytest tests/ -n auto                  # all tests (parallel via pytest-xdist; ~5× faster)
pytest tests/test_orchestrator.py -v   # specific file — sequential is fine for single-file runs
./run.sh start                         # start daemon
```

- **Swarm end-to-end:** `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` drives a real daemon on real PostgreSQL through the pool/claim/formula/hierarchy scenarios via the real CLI, in ~2½ min and with no LLM. See **[docs/guides/e2e-swarm.md](docs/guides/e2e-swarm.md)**; run it after any change to claims, pools, formulas or the task hierarchy.
- Python 3.12+, ruff (line-length 100, py312), pytest-asyncio (auto mode)
- Async-first: use `GitManager` async API (`a`-prefixed), never sync `subprocess.run()` in production
- Commands return `{"success": bool, ...}` dicts
- All state changes go through `CommandHandler` (single entry point for Discord + MCP + CLI)

## Database Migrations (Alembic)

Schema is defined in `src/database/tables.py` using SQLAlchemy Core `Table` objects.
Migrations are managed by Alembic in `migrations/`.

**After ANY change to `tables.py`** (add/remove/rename columns, add tables, change constraints):

```bash
alembic revision --autogenerate -m "description of change"
# Review the generated file in migrations/versions/
alembic upgrade head  # apply locally
```

- **Never** edit `tables.py` without generating a migration
- **Always** review autogenerated migrations — Alembic can miss renames (it sees drop+add instead)
- Migrations must work for both SQLite and PostgreSQL
- `src/database/hierarchy_migration.py` is the swarm-work-model hierarchy migration's canonicalisation logic (snapshot → canonicalise → validate → apply), driven by Alembic revisions `a1b2c3d4e5f6` (DDL) and `b2c3d4e5f6a7` (data + partial unique index) and exercised standalone via `aq system db-preflight-hierarchy`
- Test with: `pytest tests/test_database.py -v`

## Key Subsystems

- **Self-Improvement Loop:** Reflection engine → knowledge extraction → memory consolidation → prompt builder delivery. Autonomous — no manual intervention needed. See `docs/specs/design/self-improvement.md`.
- **Playbooks:** Markdown-authored DAG workflows compiled to JSON. Replace hooks/rules. Scoped (system/project/agent-type), event-triggered, human-in-the-loop. See `docs/specs/design/playbooks.md`.
- **Workflow Coordination:** Multi-agent pipelines with stage gates, agent affinity, workspace strategies, orphan recovery. See `docs/specs/design/agent-coordination.md`.
- **Workspaces v2:** Typed, normalized workspace model. `workspace_kinds` defines types (e.g. `project-repo`, `package-foo`, `vault`); `workspaces.kind_id` binds instances; `task_workspace_requirements` declares per-task needs. Multi-kind acquisition is all-or-nothing in canonical lock order for deadlock-freedom. Vault is just another auto-attached kind. See `docs/specs/design/workspaces-v2.md`.
- **Memory V2:** Milvus-backed 4-tier knowledge (L0 Identity → L1 Facts → L2 Topic → L3 Search). Semantic search + KV + temporal facts. Multi-scope weighted queries. See `docs/specs/design/memory-plugin.md`.
- **Smart Cascade:** Deterministic promotion cascade each 5s cycle: approvals → resume paused → promote DEFINED → stuck monitoring. Zero LLM overhead.
- **Reflection:** Post-task review with deep/standard/light tiers. Circuit breaker protection. Extracts insights for future agents. See `docs/specs/reflection.md`.
- **Plugins:** 4 internal plugins (files, git, notes, vibecop) + external aq-memory + third-party support. See `docs/specs/plugin-system.md`.
- **MCP Server:** Auto-exposes ~150 CommandHandler commands. See `docs/specs/mcp-server.md`.
- **Vault:** `~/.agent-queue/vault/` — Obsidian-compatible markdown for playbooks, profiles, memory, facts, knowledge bases.
- **Prompt Builder:** 5-layer context assembly pipeline: L0 role → override → L1 facts → L2 context → identity → tools. Budget-aware per tier.

## Detailed Context

See **[profile.md](profile.md)** for full architecture, codebase map, design decisions, and conventions.
