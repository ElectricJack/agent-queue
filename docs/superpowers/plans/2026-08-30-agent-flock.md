# Agent flock Implementation Plan

> **For agentic workers:** Use the scoped requirements below. Execute independently owned changes in parallel, then review the integrated result.

**Goal:** Make AQ agents durable global workers and provide the requested sidebar roster and up to four live tmux/settings tiles.
**Architecture:** Reuse Agent and SessionRecord, existing scheduling and tmux SSE, the command/OpenAPI client layer, and a dedicated React route.
**Tech Stack:** Python/FastAPI/SQLAlchemy/Alembic, React19/TypeScript/TanStack Query/Vite.
**Spec:** docs/superpowers/specs/2026-08-30-agent-flock-design.md

## Global constraints

Preserve existing data and unrelated dirty files. No user messages, model launches, or session termination just to test views. Close only unsubscribes. Keep worker project/token scopes. Four tiles maximum. Do not add a second agent identity table. Unsupported telemetry is explicitly unknown.

## Task 1: Durable worker lifecycle
Owner: backend worker.
Files: src/models.py, src/database/tables.py, src/database/queries/agent_queries.py, session_queries.py as needed, migration, src/orchestrator/agent_reconciler.py/core.py/execution.py/pools.py, src/scheduler.py, src/messages/session_lens.py, src/sessions/reconciler.py, assignment adapters if required; focused tests.
Interfaces: Agent adds role='worker', enabled=True, harness/model/intelligence_class=None. SessionRecord adds llm_provider/model/intelligence_class=None and uses existing agent_id. src/agents/configuration.py (root-owned) exposes apply_agent_overrides(profile, agent) and resolve_launch_settings(profile,harness,builder,task_class=None), returning llm_provider/model/intelligence_class dict; ensure_supervisor_agent(db) creates/returns canonical definition.
- [ ] Write failing preservation, reuse, linkage, launch configuration, and single-assignment tests.
- [ ] Run targeted tests to confirm missing behavior.
- [ ] Generate additive Alembic migration with autogenerate, review drift, preserve legacy links/history.
- [ ] Implement persistent global identities, safe lifecycle/reuse, explicit session links, snapshots, worker eligibility.
- [ ] Run relevant scheduler/reconciler/session/pool/migration tests and report changed behavior.

## Task 2: Global command/read model and configuration
Owner: root.
Files: src/agents/__init__.py/configuration.py/service.py, src/commands/agent_commands.py, src/tools/definitions.py, src/api/models/agent.py, src/api/scope.py, src/cli/formatter_registry.py, tests/test_agent_flock.py, generated clients.
Interfaces: exact flat row contract in spec; commands list_agents/get_agent/create_agent/edit_agent. Settings contains configured optional overrides. resolve_launch_settings uses the existing class-to-provider resolution and respects explicit model overrides.
- [ ] Test list without active project, metadata/session identity, next-run settings, validation, scope rejection.
- [ ] Implement aggregate roster and command handlers through shared configuration helpers.
- [ ] Regenerate OpenAPI and TypeScript client from isolated code, without pointing at stale live daemon.
- [ ] Verify API/CLI compatibility and settings error responses.

## Task 3: Subagent accounting
Owner: telemetry worker.
Files: src/agents/subagents.py, src/commands/task_commands.py (creator attribution only), focused tests; transcript helper extension only when needed.
Interfaces: async subagent_counts(db, agent_id, sessions, tasks) -> dict with active_subagent_count, subagent_count_complete, aq_subagent_count, native_subagent_count. Sessions/tasks are prefetched model sequences; no mutations in count reads. Native count uses supported authoritative lifecycle telemetry and does file I/O off the event loop.
- [ ] Test active direct children, completion/removal, deduplication, elevated supervisor attribution, native lifecycle and missing data.
- [ ] Implement accurate counts without guessing from task-tree size or activity timeouts.
- [ ] Run focused tests; report telemetry coverage.

## Task 4: Sidebar and tiled agent workspace
Owner: frontend worker.
Files: dashboard/src/api/agents.ts, shell/AgentFlock.tsx/LeftRail.tsx, pages/agents/*, App.tsx, pages/command-center/Agents.tsx, ws/useEventStream.ts, corresponding tests.
Interfaces: consume global listAgents/getAgent/createAgent/editAgent via generated client and shared row contract. URL /agents?agent=<id>&agent=<id>; max4.
- [ ] Write failing tests for exact requested modifier/close behavior, metadata and settings.
- [ ] Implement collapsible roster, stable URL selection, responsive real tmux tiles, per-agent settings.
- [ ] Refresh queries on agent/session/task/message events and short polling; never start workers from rendering.
- [ ] Run tests/typecheck; preserve existing console-not-inside-button regression.

## Task 5: Integration and live verification
Owner: root with independent review.
- [ ] Review each owned diff for spec compliance and scope/data safety.
- [ ] Run combined backend/UI tests, build, lint touched files, and inspect migration on copied data.
- [ ] Back up live DB, integrate local feature preserving unrelated changes, restart AQ with --keep-sessions.
- [ ] Verify live global roster, metadata, selection/collapse/close/settings, and no browser errors. Report any telemetry/runtime limits honestly.

