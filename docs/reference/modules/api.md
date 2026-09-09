# Module catalog: API

Every module behind the daemon's [HTTP API](../api/README.md), plus the two
generated client packages and the committed schema they come from. This is the
`api` shard of the [module catalog](README.md); the pages in the Component
column explain these modules in prose.

Sources of truth this shard covers: [`src/api/`](../../../src/api/) (51
production modules), [`openapi.json`](../../../openapi.json),
[`packages/aq-client/`](../../../packages/aq-client/) and
[`packages/aq-ts-client/`](../../../packages/aq-ts-client/) (1,442 generated
files). The dashboard's transport code is documented in prose on the
[TypeScript client](../api/typescript-client.md) page but belongs to the
`dashboard` shard.

## Application, transport and identity

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/api/__init__.py`](../../../src/api/__init__.py) | Marks the FastAPI application package; holds no logic. | [API overview](../api/README.md) | Docstring only. |
| [`src/api/app.py`](../../../src/api/app.py) | Builds the FastAPI app: wires shared dependencies, registers every router in precedence order, patches the upload schema, and starts the WebSocket manager. | [API overview](../api/README.md) | `tests/test_api_client_contract.py` builds a real app from it. Middleware registration order is deliberate — Starlette applies middleware LIFO. |
| [`src/api/codegen.py`](../../../src/api/codegen.py) | Turns each categorized command into a typed `POST /api/{category}/{command}` route, with a request model built from its JSON input schema and a registered response model. | [API overview](../api/README.md) | `tests/test_api_codegen_input_models.py`. Owns `API_EXCLUDED`, the four commands with no HTTP route. |
| [`src/api/routers/__init__.py`](../../../src/api/routers/__init__.py) | Mounts every generated category router onto the app and reports how many routes were registered. | [API overview](../api/README.md) | Thin wrapper over `build_category_routers()`. |
| [`src/api/execute.py`](../../../src/api/execute.py) | Serves `POST /api/execute` (the CLI's transport, `{ok, result|error, details}`), `GET /api/tools` and the compatibility `GET /api/health`. | [Conventions](../api/conventions.md) | `tests/test_api_execute_contract.py` pins the envelope against the typed routes'. |
| [`src/api/dependencies.py`](../../../src/api/dependencies.py) | Module-level holder for the shared orchestrator, command handler, token store and health/plan providers, injected via FastAPI `Depends`. | [API overview](../api/README.md) | Prefers the orchestrator's live command handler over the startup snapshot, so a runtime swap is followed. |
| [`src/api/middleware.py`](../../../src/api/middleware.py) | Two middlewares: request-id/structlog binding, and bearer-token resolution into `request.state.scope` with the loopback restriction on global-admin tokens. | [Conventions](../api/conventions.md) | `tests/test_api_auth.py`. Derives `profile_id` / `policy_fingerprint` from the live session row, never from the token. |
| [`src/api/auth.py`](../../../src/api/auth.py) | `RequestScope` and `SessionTokenStore`: mint, validate, revoke session bearer tokens (`aqs_` prefix, sha256-hashed at rest, cached per process). | [Conventions](../api/conventions.md) | `tests/test_api_auth.py`. TTL from `api_auth.token_ttl_hours`. |
| [`src/api/scope.py`](../../../src/api/scope.py) | The pure scope gate: the agent command allowlist, identity pinning and injection, operator-only commands, and the worker/reviewer/final-reviewer/triage/compiler carve-outs resolved from persisted state. | [Conventions](../api/conventions.md) | `tests/test_api_scope.py`, `tests/test_reviewer_api_scope.py`, `tests/test_triage_api_scope.py`, `tests/test_command_scope_isolation.py`. |
| [`src/api/errors.py`](../../../src/api/errors.py) | Catch-all exception handler returning an opaque `500`. | [Conventions](../api/conventions.md) | Deliberately says nothing about the exception. |
| [`src/api/spec.py`](../../../src/api/spec.py) | Builds the OpenAPI document offline from a throwaway app and writes it in the committed on-disk format; also the `python -m src.api.spec` entry point the regeneration scripts use. | [Python client](../api/python-client.md) | `tests/test_api_client_contract.py`. Restores every dependency global it touches. |

## Resource and aggregate routes

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/api/health.py`](../../../src/api/health.py) | `/health` (full check, 503 when degraded), `/ready` (messaging + database + required playbooks) and `/plans/{task_id}` (plan markdown as HTML). | [API overview](../api/README.md) | `tests/test_health_platform.py`. The plan route's id pattern rejects the `.` in hierarchical task ids. |
| [`src/api/messages.py`](../../../src/api/messages.py) | The chat relay: send to any recipient, send to a named session, and poll a session's conversation. Resolves `<role>-<project>` names against the known-role list. | [API overview](../api/README.md) | `tests/test_api_messages.py`. This is why `message_send` has no generated route. |
| [`src/api/graph.py`](../../../src/api/graph.py) | `GET /api/projects/{id}/graph` — every task and edge of one project in a single, order-stable read. | [API overview](../api/README.md) | `tests/test_api_graph.py`. One statement for all edges, not one per task. |
| [`src/api/graph_layout.py`](../../../src/api/graph_layout.py) | The viewport-bounded spatial layout surface: `extent`, `tiles`, `list`, `node`, `locate`, `tidy`, `jobs`. | [API overview](../api/README.md) | `tests/test_api_graph_layout.py`. `202` means the layout is still being built. |
| [`src/api/routers/proposals.py`](../../../src/api/routers/proposals.py) | `GET /api/proposals/{id}` — a proposed task graph for the dashboard's ghost overlay. | [API overview](../api/README.md) | `tests/test_proposal_api.py`. |
| [`src/api/metrics.py`](../../../src/api/metrics.py) | `GET /api/metrics/series` — fleet metrics history, choosing the finest resolution that fits 12,000 points and flagging a downgrade as `truncated`. | [API overview](../api/README.md) | `tests/test_api_metrics.py`. Live ticks arrive over the WebSocket instead. |
| [`src/api/providers.py`](../../../src/api/providers.py) | `GET /api/providers/usage` — each provider's own quota as the provider reports it, distinct from the token ledger's estimate. | [API overview](../api/README.md) | `tests/test_api_provider_usage.py`. |
| [`src/api/task_files.py`](../../../src/api/task_files.py) | Lists and reads files in the worktree attached to a task; answers a marker rather than 404 when no worktree is attached. | [API overview](../api/README.md) | `tests/test_api_task_files.py`. |
| [`src/api/task_attachments.py`](../../../src/api/task_attachments.py) | Upload, list, read and delete image attachments on a task (10 MB cap, content sniffed against the declared type). | [API overview](../api/README.md) | `tests/test_task_attachments_api.py`. The only multipart route; `src/api/app.py` patches its schema so the Python generator emits a file upload. |
| [`src/api/task_sessions.py`](../../../src/api/task_sessions.py) | `GET /api/tasks/{id}/sessions` — a task's execution history, archived tasks included. | [API overview](../api/README.md) | `tests/test_task_session_attempts.py`. |
| [`src/api/workspace_files.py`](../../../src/api/workspace_files.py) | Browses and reads files under a workspace root for the file-browser pane. | [API overview](../api/README.md) | `tests/test_api_workspace_files.py`. |
| [`src/api/file_serving.py`](../../../src/api/file_serving.py) | The one path-safe, size-capped, binary-aware file read both file routes call: 403 on traversal or symlink escape, 404 on missing, 413 above 512 KB. | [API overview](../api/README.md) | Covered through its two callers' suites. |

## Streaming

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/api/websocket.py`](../../../src/api/websocket.py) | `/ws/events`: subscribes to the event bus, forwards the allowed prefixes to every client, replays persisted events after `after_seq`, and filters metrics, pool and question frames per scope. | [Streaming](../api/events.md) | `tests/test_events_replay.py`, `tests/test_agent_question_websocket.py`. Per-client queue of 1,000 frames, oldest dropped. |
| [`src/api/sessions.py`](../../../src/api/sessions.py) | `GET /api/sessions/{id}/stream` — SSE transcript replay and live tail, optionally pinned to one recorded attempt. | [Streaming](../api/events.md) | `tests/test_session_stream_api.py`. |
| [`src/api/pane_stream.py`](../../../src/api/pane_stream.py) | `GET /api/sessions/{id}/pane` — SSE terminal screens, one shared poll loop per watched session. | [Streaming](../api/events.md) | `tests/test_pane_stream_api.py`, `tests/test_pane_broadcaster.py`. |
| [`src/api/streams.py`](../../../src/api/streams.py) | The console-stream registry: start a command, read its metadata, subscribe or tail its output, kill it; bounded buffers, per-session concurrency cap, retention sweep. | [Streaming](../api/events.md) | `tests/test_streams_api.py`, `tests/test_streams_registry.py`, `tests/test_cli_streams.py`. Memory-only. |
| [`src/api/terminal_stream.py`](../../../src/api/terminal_stream.py) | `/ws/terminal/{session_id}` — authorized raw tmux attach with bounded input, origin checks and constant close reasons. Never launches an agent. | [Streaming](../api/events.md) | `tests/test_terminal_stream.py`, `tests/test_agent_terminals.py`. Global admin, loopback only. |

## Response models

Each module below exports a `RESPONSE_MODELS` mapping from command name to
model, aggregated by `get_all_response_models()`; the four with no mapping are
consumed directly by a hand-written route. All are explained on the
[Response models](../api/models.md) page, and covered as a set by
`tests/test_response_model_registry.py`.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/api/models/__init__.py`](../../../src/api/models/__init__.py) | Shared `ErrorResponse` / `TaskRef` / `TaskBrief` plus the registry that merges every category module. | [Response models](../api/models.md) | `tests/test_response_model_registry.py` is the guard that a categorized command has a model. |
| [`src/api/models/task.py`](../../../src/api/models/task.py) | Tasks: detail, listing, comments, gates, dependencies, claims, batches, deliverables. | [Response models](../api/models.md) | The largest family; `TaskShowResponse` extends `TaskDetail`. |
| [`src/api/models/project.py`](../../../src/api/models/project.py) | Projects, workspaces, workspace kinds, constraints, default branch. | [Response models](../api/models.md) | — |
| [`src/api/models/project_onboarding.py`](../../../src/api/models/project_onboarding.py) | Root browsing, GitHub discovery and the onboarding saga, including its recovery contract. | [Response models](../api/models.md) | `tests/test_project_onboarding_contract.py`. These commands keep their structured payload on a 422. |
| [`src/api/models/agent.py`](../../../src/api/models/agent.py) | Agents, profiles, agent-side session views, questions and pool status. | [Response models](../api/models.md) | — |
| [`src/api/models/session.py`](../../../src/api/models/session.py) | Session listing, logs, input and lifecycle. | [Response models](../api/models.md) | `tests/test_response_model_registry.py` uses `SessionSummary` as its fixture. |
| [`src/api/models/git.py`](../../../src/api/models/git.py) | Status, diff, log, branch, changed files, push, PR, merge. | [Response models](../api/models.md) | Merge commands exist here but are refused to worker tokens by `src/api/scope.py`. |
| [`src/api/models/gate.py`](../../../src/api/models/gate.py) | Human and routing gates: create, list, show, resolve. | [Response models](../api/models.md) | — |
| [`src/api/models/message.py`](../../../src/api/models/message.py) | Inbox, send, reply and thread listing. | [Response models](../api/models.md) | — |
| [`src/api/models/memory.py`](../../../src/api/models/memory.py) | Semantic memory search and save, project profiles, compaction. | [Response models](../api/models.md) | Backed by the external `aq-memory` plugin. |
| [`src/api/models/files.py`](../../../src/api/models/files.py) | Read, write, edit, glob and grep results, and the notes commands. | [Response models](../api/models.md) | — |
| [`src/api/models/system.py`](../../../src/api/models/system.py) | Config, schema, logs, events, costs, doctor, workflows and integration control. | [Response models](../api/models.md) | Includes `EditIntelligenceClassConflictResponse`, the only 409 body. |
| [`src/api/models/plugin.py`](../../../src/api/models/plugin.py) | Plugin listing, install, enable/disable and configuration. | [Response models](../api/models.md) | — |
| [`src/api/models/mcp.py`](../../../src/api/models/mcp.py) | MCP server registry entries and the tool catalog. | [Response models](../api/models.md) | — |
| [`src/api/models/playbook.py`](../../../src/api/models/playbook.py) | Playbook listing, health, artefacts and run history. | [Response models](../api/models.md) | — |
| [`src/api/models/playbook_v2.py`](../../../src/api/models/playbook_v2.py) | The V2 semantic-graph surface: definitions, proposals, validation, activation, run state. | [Response models](../api/models.md) | `tests/test_playbook_v2_api_dtos.py`. `playbook_graph_view` is serialized with `exclude_none`. |
| [`src/api/models/graph.py`](../../../src/api/models/graph.py) | Nodes and edges of the aggregate project-graph read. | [Response models](../api/models.md) | Used by `src/api/graph.py` directly. |
| [`src/api/models/graph_layout.py`](../../../src/api/models/graph_layout.py) | Requests *and* responses for the layout routes: extent, tiles, list, node, locate, tidy, jobs. | [Response models](../api/models.md) | Registers no command; `src/api/graph_layout.py` uses it directly. |
| [`src/api/models/metrics.py`](../../../src/api/models/metrics.py) | One fleet metrics sample — agents, tasks, tokens, sub-agents, slots, machine, daemon — and a series of them. | [Response models](../api/models.md) | Every field is optional with a neutral default so a partial sample is not a 500; `agents.total` is currently typed `int`, which a rolled-up average violates. |
| [`src/api/models/provider.py`](../../../src/api/models/provider.py) | Provider quota as each provider reports it. | [Response models](../api/models.md) | `tests/test_api_provider_usage.py`. |
| [`src/api/models/escalation.py`](../../../src/api/models/escalation.py) | Durable human escalations, replies and the shared error body for that family. | [Response models](../api/models.md) | `EscalationErrorResponse` is declared as the 422 model for every `escalation_*` and `digest_*` route. |
| [`src/api/models/digest.py`](../../../src/api/models/digest.py) | Digest preview (dry) and schedule health. | [Response models](../api/models.md) | — |
| [`src/api/models/discord.py`](../../../src/api/models/discord.py) | Explicit Discord channel housekeeping. | [Response models](../api/models.md) | Notification-only surface; there are no Discord control commands. |

## Generated artifacts

Covered per resource family, never file by file, and never hand-edited.

| Family | Files | Generated from | Regenerate with | Notes |
|---|---|---|---|---|
| [`openapi.json`](../../../openapi.json) | 1 | `create_app()` — every route, request model and response model in `src/api/` | `./scripts/regenerate-api-client.sh --offline` (or `python -m src.api.spec`) | The committed contract both clients are built from. `tests/test_api_client_contract.py` fails on drift. |
| [`packages/aq-client/`](../../../packages/aq-client/) — operation modules, `api/<category>/*.py` | 288 (+21 package `__init__.py`) | `openapi.json`, one module per operation | `./scripts/regenerate-api-client.sh --from-file` | Grouped by category; `default/` holds the uncategorized routes. |
| [`packages/aq-client/`](../../../packages/aq-client/) — schema models, `models/*.py` | 1,121 (+1 package `__init__.py`) | `openapi.json`, one module per schema | same | Includes the nested `…ResponseItem` / `…Type0` classes the generator derives. |
| [`packages/aq-client/`](../../../packages/aq-client/) — runtime and packaging (`client.py`, `errors.py`, `types.py`, `__init__.py`, `py.typed`, `pyproject.toml`, `README.md`) | 8 | The pinned generator's templates, not the spec | same | Digests recorded in [`scripts/aq-client-boilerplate.sha256`](../../../scripts/aq-client-boilerplate.sha256) and checked by `tests/test_api_client_contract.py`. |
| [`packages/aq-ts-client/`](../../../packages/aq-ts-client/) — `package.json`, `tsconfig.json` | 2 | Hand-written workspace manifest for the generated tree | — | The generated `src/` is gitignored and rebuilt on demand by `./scripts/regenerate-ts-client.sh --from-file`. |

Counts are as of this page's commit; `git ls-files packages/aq-client | wc -l`
is the current number.

## Related pages

* [HTTP API](../api/README.md) — the prose for everything above.
* [Module catalog index](README.md) — the other shards.
* [Documentation map](../../documentation-map.md) — who owns which page.
