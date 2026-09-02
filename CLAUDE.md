# CLAUDE.md

Agent Queue — self-improving orchestration platform for AI coding agents. Manages task queues, coordinates multi-agent workflows via playbooks, accumulates knowledge through a 4-tier memory system, and continuously improves through automated reflection. The core value proposition: the system gets better with use — every task feeds the reflection engine, insights accumulate in scoped memory, and future agents benefit automatically. Discord + MCP + CLI controlled, SQLAlchemy-backed (SQLite default, PostgreSQL supported), fully async Python.

## Quick Reference

- **Entry point:** `src/main.py` → orchestrator + Discord bot + embedded MCP server
- **Core files:** `orchestrator.py`, `src/commands/` (handler + mixin modules), `supervisor.py`, `database/` (including `src/database/queries/hierarchy_queries.py` — task hierarchy: `set_parent`, children/progress reads, close/delete/archive subtree semantics), `models.py`
- **Playbooks:** `src/playbooks/` (compiler, runner, manager, models, store, handler, state_machine, health, graph, graph_view, resume_handler)
- **Memory:** External `aq-memory` plugin (install via `aq plugin install`), plus in-tree `facts_parser.py`, `profile_parser.py`
- **Profiles:** `src/profiles/` — `parser.py`, `sync.py`, `migration.py`. Markdown source of truth in `vault/agent-types/<id>/profile.md` (system) and `vault/projects/<pid>/agent-types/<id>/profile.md` (project override). The profile's `## Config.harness` field selects which CLI runs the agent (`claude`, `codex`, `gemini` — see `src/sessions/default_harnesses/`); **every** agent runs as a tmux session and `harness` is the only selector. The `runtime` and `agent_name` config keys were **removed** (with the in-process Supervisor and the `acpx` runtime respectively); `parser.py` rejects both with a pointer to `harness`.
- **Runtimes:** `src/runtimes/` — `base.py` (Runtime ABC, Capability enum, `requires_workspace` ClassVar) and the `RuntimeRegistry` in `__init__.py`. **There are no in-tree implementations**: every agent runs as a session (a CLI wrapped in tmux, chosen by `harness`), and the in-process Supervisor was deleted. `default_registry(config=...)` registers nothing; the registry remains the injection seam for tests and `sync_workflow`.
- **MCP registry:** `src/profiles/mcp_registry.py` (in-memory registry + vault watcher), `mcp_probe.py` (parallel probes, 10s timeout), `mcp_catalog.py` (cache), `mcp_inline_migration.py` (legacy extractor). Source of truth: `vault/[projects/<pid>/]mcp-servers/*.md`. Profiles reference servers by name.
- **Workspaces:** `src/orchestrator/workspace.py` + `src/orchestrator/workspace_attachments.py` (multi-kind acquisition), `src/database/queries/workspace_queries.py` + `workspace_kinds_queries.py` + `task_requirements_queries.py`. Tasks declare `requires_kinds` at creation; orchestrator atomically acquires one workspace per declared kind in canonical lock order. Kinds are markdown in `vault/[projects/<pid>/]workspace-kinds/<id>.md` (system + project override) — built-ins seeded by migration: `project-repo` (writable, exclusive lock), `vault` (auto-attached, no lock), `readonly-dir`. See `docs/specs/design/workspaces-v2.md`.
- **Formulas:** `src/task_graph/formulas.py` (registry, `extends` merge, vars), `src/commands/formula_commands.py` (`formula_list|show|cook`), provenance in `creator.write_plan`. Files: `vault/[projects/<pid>/]formulas/<name>.md` (frontmatter + one `aq-graph` block). Spec: design spec Part III.
- **Swarm (claims/pools):** `src/database/queries/claim_queries.py` (claim transaction, epoch fence), `src/commands/claim_commands.py` (`task_claim`), `src/orchestrator/pools.py` (`_reconcile_pools`; pure `size_pools` in `scheduler.py`), pool carve-outs in `src/sessions/reconciler.py`, checks in `src/doctor/pool_checks.py`. Profiles with `lifecycle: pool` pull work via `aq task claim`; `lifecycle: task` keeps push. Off by default (`swarm.enabled`). Spec: `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part II.
- **Resource gating:** `src/resources/` — `limits.py` (per-session env caps + `nice` + optional cgroup scopes, applied in `src/sessions/spec.py:_build`), `semaphore.py` (the box-wide `flock` slot semaphore behind `aq test`), `procs.py` (attribute load back to sessions). Config section `resources:`; checks in `src/doctor/resource_checks.py`; CLI in `src/cli/test_runner.py`. Guide: `docs/guides/resource-gating.md`.
- **Config editor:** `src/config_editor.py` — ruamel round-trip writer behind `get_config` / `update_config` / `get_config_schema`. Validates via temp-file `load_config()` before swap.
- **Intelligence:** `prompt_builder.py`, `tools/registry.py`, `llm_logger.py`. Reflection is a playbook, not a module.
- **LLM direct path:** `src/llm/` — `LLMClient.complete`/`run_tools`, `LLMCallSpec`, config `llm:`, intelligence classes shared with sessions. Consumers: playbook nodes/transitions, plugin `invoke_llm`, the reference-stub enricher, and `aq vault rebuild-index --with-summaries`.
- **Workflows:** `workflow_stage_resume_handler.py`, `orphan_workflow_recovery.py`, `workflow_pipeline_view.py`
- **Plugins:** `src/plugins/` (base, registry, loader, internal/)
- **Internal plugins:** `src/plugins/internal/` (aq-files, aq-git, aq-notes, aq-vibecop)
- **Messages:** `src/messages/` — `session_lens.py` (`SessionManagerProto`/`SessionLens`; supervisor messaging address `supervisor-<pid>` → runtime name `n-supervisor--<pid>`), `delivery.py` (`MessageDeliveryEngine`; per-`to_kind` delivery policy; parking for stale session rows; transcript-tail fallback). Cascade step in `run_one_cycle` behind `messages.enabled`; `aq inbox --inject` hook in claude harness; prime surfaces pending messages via `via="prime"`.
- **Subsystems:** `src/runtimes/`, `src/discord/`, `src/git/`, `src/tokens/`, `src/messaging/`
- **Specs:** `docs/specs/` (source of truth — specs first, then code)
- **Design specs:** `docs/specs/design/` (principles, playbooks, memory, self-improvement, coordination, vault, profiles, roadmap)
- **Config:** `~/.agent-queue/config.yaml`
- **Vault:** `~/.agent-queue/vault/` (playbooks, profiles, memory, facts — all markdown)
- **Packages:** `packages/aq-client/` (typed API client)
- **OpenAPI spec:** `openapi.json` is a committed artifact both clients are generated from. `src/api/spec.py` builds it offline (no daemon) — regenerate with `./scripts/regenerate-api-client.sh --offline` and `./scripts/regenerate-ts-client.sh --offline` after **any** change to `src/api/models` or a codegen router. `tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface` fails when the committed spec drifts from what `create_app()` serves.

## Development

```bash
pip install -e ".[dev,cli]"
pip install -e packages/aq-client      # typed API client (generated)
aq test tests/test_orchestrator.py                # focused tests for what you changed (see Testing below)
./run.sh start                         # start daemon
```

- **Swarm end-to-end:** `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` drives a real daemon on real PostgreSQL through the pool/claim/formula/hierarchy scenarios via the real CLI, in ~2½ min and with no LLM. See **[docs/guides/e2e-swarm.md](docs/guides/e2e-swarm.md)**; run it after any change to claims, pools, formulas or the task hierarchy.
- Python 3.12+, ruff (line-length 100, py312), pytest-asyncio (auto mode)
- Async-first: use `GitManager` async API (`a`-prefixed), never sync `subprocess.run()` in production
- Commands return `{"success": bool, ...}` dicts
- All state changes go through `CommandHandler` (single entry point for Discord + MCP + CLI)

## Testing — read this before running anything

The suite is **11,330 tests** and, until the schema-cache work lands, every fresh test database replays 58 alembic migrations (~8 s each, ~2,700 tests pay it). A full run takes **~14 minutes on 24 cores and effectively never finishes serially**. Running it casually stalls every agent on the machine.

Rules:
- **Use `aq test`, not bare `pytest`, for anything past a single file.** It takes one of the box's global test slots first, so eight agents testing at once cannot become 200 test processes, and it applies the worker cap and the default marker deselects for you. Everything that is not an `--aq-*` option goes to pytest untouched:
  ```bash
  aq test tests/test_playbook_runner.py          # the file for the module you changed
  aq test tests/test_claim_queries.py tests/test_pools.py
  aq test tests/ -k "schema_setup or run_schema"
  aq test --aq-status                            # who is holding the slots
  aq test --aq-help                              # -h belongs to pytest
  ```
  A `waiting for 1 of 2 test slot(s)` line means the box is busy, not that you are stuck. Exit code 75 means no slot came free — retry, it is not a test failure. Plain `pytest` still works for a single quick file.
- **Never run a bare `pytest` / `pytest tests/` mid-task.** Run only the tests for the code you touch.
- **Never override the worker count upward.** `-n auto` inside a session already resolves to this box's per-session share (`PYTEST_XDIST_AUTO_NUM_WORKERS`, derived from cores ÷ concurrent agents); passing a bigger `-n` bypasses the gating and is what took the box down on 2026-09-01. See [resource gating](docs/guides/resource-gating.md).
- **Find focused tests** (the layout is one file per area, `tests/test_<area>.py`, plus `tests/perf/`, `tests/llm/`, `tests/fixtures/`):
  ```bash
  aq test tests/test_playbook_runner.py            # the file for the module you changed
  aq test tests/test_claim_queries.py tests/test_pools.py    # a few related files
  aq test tests/ -k "schema_setup or run_schema"   # by name, across files
  aq test tests/test_x.py -x                       # stop at first failure while iterating
  aq test --lf                                     # re-run only what failed last time
  pytest --co -q -k <term> | tail -20              # collection only — no slot needed
  ```
- **Skip the slow-by-nature markers** unless the change is about them (real tmux, Milvus, latency budgets). `aq test` applies `-m "not perf and not migration and not slow and not tmux and not integration"` by default; pass your own `-m` (or `--aq-all-markers`) when the change *is* about them.
- **One broader run at the end of a task, not during:** the area suite for what you changed (e.g. `aq test tests/test_playbook*.py tests/test_pipeline*.py`). The whole-repo run is for CI and explicit review gates only.
- Ruff on changed files only: `ruff check <paths>`.

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
- **Prompt Builder:** 5-layer context assembly pipeline: L0 role → override → L1 facts → L1 guidance → L2 context → identity → tools. Budget-aware per tier (advisory, warn at 2×).

## Detailed Context

See **[profile.md](profile.md)** for full architecture, codebase map, design decisions, and conventions.
