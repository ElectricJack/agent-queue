# HTTP API

The AQ daemon serves one local HTTP API. Everything that is not the daemon
itself — the `aq` command line, the web dashboard and an agent running inside
a worker session — reaches AQ through it. MCP shares the HTTP server but
dispatches tools to `CommandHandler` in-process; it does not call the REST routes.

This page is the entry point for that surface: what it is, how to reach it,
what the three families of routes are, and where the schema everything else is
generated from comes from. The neighbouring pages go deeper:

| Page | Covers |
|---|---|
| [Requests, authentication, scope and errors](conventions.md) | Bearer tokens, what a session token may call, the two error envelopes, limits and paging. |
| [Streaming: WebSocket events, SSE and terminals](events.md) | `/ws/events`, the SSE session/pane/stream endpoints, and the terminal WebSocket. |
| [Response models](models.md) | The Pydantic models the routes answer with, and the schema names they become. |
| [Python client](python-client.md) | The generated, typed `agent_queue_api_client` package. |
| [TypeScript client](typescript-client.md) | The generated `@aq/ts-client` package and how the dashboard uses it. |
| [Module catalog: API](../modules/api.md) | Every module in `src/api/`, the two client packages, and what covers them. |

## Why it exists

AQ keeps all of its state in one PostgreSQL database owned by one daemon
process. Rather than let four surfaces write that state four ways, every
state change goes through a single in-process entry point — the
[`CommandHandler`](../../../src/commands/handler.py) — and the HTTP API is the
only way to reach it from outside the daemon. The CLI is an HTTP client. The
dashboard is an HTTP client. An agent's `aq task close` is an HTTP request.
That is why the API's shape mirrors the command surface so directly: most
endpoints *are* commands.

## Vocabulary

* **Command** — a named operation with a JSON input schema and a JSON result,
  such as `task_show` or `create_task`. See the
  [glossary](../glossary.md) and the CLI reference for the full surface.
* **Typed route** — the auto-generated `POST /api/{category}/{command}`
  endpoint for one command, with a declared request and response schema.
* **Scope** — the identity the daemon attaches to a request: trusted local
  (no token) or a session token bound to one session, task and project. See
  [conventions](conventions.md).
* **Session** — a running agent (a coding-agent CLI inside a terminal). Its
  token is what lets it call back into AQ.

## Where it listens

The API is served by the same uvicorn process that serves the embedded MCP
server, on the `mcp_server` host and port — `127.0.0.1:8081` by default
([`src/config.py`](../../../src/config.py), `McpServerConfig`). It binds
loopback by default and has no TLS: it is a local service, not a public one.

The `aq` CLI resolves the base URL in this order (an explicit `--api-url`
overrides it)
([`src/cli/client.py`](../../../src/cli/client.py)):

1. `AQ_API_URL` — set automatically inside every agent session.
2. `AGENT_QUEUE_API_URL` — legacy alias, still honoured.
3. `mcp_server.host` / `mcp_server.port` from `~/.agent-queue/config.yaml`.
4. `http://127.0.0.1:8081`.

Three things share that port: this API under `/api` (plus `/health`, `/ready`,
`/plans/{task_id}` and `/openapi.json`), the WebSocket
endpoints under `/ws`, and the MCP streamable-HTTP server mounted beneath
`/mcp` ([`src/embedded_mcp.py`](../../../src/embedded_mcp.py)).

## A realistic example

> Assumes a running daemon on `127.0.0.1:8081`. The transcripts below were
> captured from the daemon's application surface running against a throwaway
> database with a throwaway project called `demo`; ids like `quick-harbor` are
> generated per task, so yours will differ.

Create a project, create a task in it, and read the task back:

```bash
curl -s http://127.0.0.1:8081/api/project/create \
  -H 'Content-Type: application/json' \
  -d '{"project_id": "demo", "name": "demo", "repo_path": "/tmp/demo-repo"}'
```

```text
{"created":"demo","name":"demo","assignment_playbook_id":"default-assignment-routing"}
```

```bash
curl -s http://127.0.0.1:8081/api/task/create \
  -H 'Content-Type: application/json' \
  -d '{"project_id": "demo", "title": "Add a health endpoint",
       "description": "Return 200 from /healthz."}'
```

```text
{
  "created": "quick-harbor",
  "title": "Add a health endpoint",
  "project_id": "demo",
  "integration_mode": null,
  "task_type": null,
  "profile_id": null,
  "profile_source": null,
  "intelligence_class": null,
  "route_source": "unrouted",
  "class_hint": null,
  "provider_intent": null,
  "preferred_workspace_id": null,
  "attachments": null,
  "skip_verification": false,
  "warning": null,
  "success": true,
  "task_id": "quick-harbor",
  "gate_id": null,
  "status": "READY",
  "reason": null,
  "parent_id": null,
  "depends_on": []
}
```

The task is born `unrouted`: filing carries hints (`intelligence_class`,
`task_type`), never a profile, and the project's router writes the route on
the next cascade. Until it does, no worker can claim the task.

```bash
curl -s http://127.0.0.1:8081/api/task/show \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "quick-harbor"}'
```

```text
{
  "id": "quick-harbor",
  "project_id": "demo",
  "title": "Add a health endpoint",
  "description": "Return 200 from /healthz.",
  "status": "READY",
  "priority": 100,
  "assigned_agent": null,
  …
  "effective_integration_mode": "pull_request",
  "integration_mode_source": "default",
  "is_blocked": false,
  "depends_on": [],
  "blocks": [],
  "created_at": 1788984712.3304589,
  …
}
```

Clean up by deleting the throwaway project (`POST /api/project/delete`), or
just drop the throwaway database.

Checking that a daemon is up and serving needs no body and no token:

```bash
curl -s http://127.0.0.1:8081/api/health
curl -s http://127.0.0.1:8081/ready
```

```text
{"status":"ok"}
{"ready":true,"timestamp":"2026-09-09T20:08:44.920291+00:00","checks":{"messaging":{"ok":true,"platform":"discord","connected":true},"database":{"ok":true},"required_playbooks":{"ok":true,"required":{"default-assignment-routing":{"ok":true,"artifact_sha256":"sha256:c016bd…"}}}}}
```

`/api/health` is a bare liveness answer. `/ready` reads the background health
snapshot and checks messaging, the database, required playbooks, the playbook
private-value key and snapshot freshness. It returns `503` when any fails.
`/health` (no `/api`) returns all collected checks, also with a 200/503 rule;
its status is `busy` while the snapshot is initializing, stale or timed out.
[`HealthMonitor`](../../../src/api/health_monitor.py) collects every five seconds
with a two-second timeout and marks snapshots stale after ten seconds, so
production probes do not wait for database work on the request path.

## The three families of routes

```mermaid
flowchart LR
  CLI[aq CLI] --> EX["POST /api/execute"]
  SDKPY["Python client"] --> TYPED
  DASH[Dashboard] --> TYPED["POST /api/{category}/{command}"]
  DASH --> RES["Resource + stream routes"]
  DASH --> WS["/ws/events"]
  EX --> CH[CommandHandler]
  TYPED --> CH
  RES --> DB[(PostgreSQL / filesystem)]
  CH --> DB
  BUS[EventBus] --> WS
```

### 1. Generated command routes

Every command that has a category and is not excluded becomes
`POST /api/{category}/{command-name}`, with underscores in the command name
becoming hyphens in the path: `task_show` → `POST /api/task/show`,
`graph_layout_rebuild` → `POST /api/graph/layout-rebuild`. The route's
`operationId` is the command name itself, which is what makes the generated
clients readable. Generation happens at startup in
[`src/api/codegen.py`](../../../src/api/codegen.py); the request model is built
from an explicit request model or the command descriptor's JSON input schema;
the response model is looked up in
[`src/api/models/`](../../../src/api/models/) (see [models](models.md)).

`API_EXCLUDED` in `src/api/codegen.py` excludes internal commands such as
wait/job/cron reconcilers, authenticated ask intake, `run_command`, and
`message_send`. Both generated routes and `/api/execute` enforce that set;
message sending uses the dedicated routes below. The MCP exclusion list is
separate. Route generation reads the shared command descriptor snapshot in
[`src/commands/command_inventory.py`](../../../src/commands/command_inventory.py),
including internal plugin definitions; a dedicated request model takes precedence
over a model generated from the descriptor's JSON schema.

Read current category counts from the committed schema:

```bash
python3 - <<'PYAPI'
import collections, json
schema = json.load(open("openapi.json"))
counts = collections.Counter(
    tag
    for path in schema["paths"].values()
    for method, operation in path.items()
    if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    for tag in operation.get("tags", ["(untagged)"])
)
print(sorted(counts.items()))
PYAPI
```

### 2. Hand-written resource and stream routes

Routes whose shape is not a generated command: a path parameter, a file body,
an aggregate read, or a stream. Most have no category tag and appear in the
Python client under `api/default/`; the named-session messaging routes use
`api/sessions/`. These resource handlers apply their own authorization checks.

| Route | Purpose | Module |
|---|---|---|
| `POST /api/execute` | Run any non-excluded command in a stable envelope — the CLI's transport. | [`execute.py`](../../../src/api/execute.py) |
| `GET /api/tools` | The tool definitions this caller could actually dispatch. | [`execute.py`](../../../src/api/execute.py) |
| `GET /api/health`, `GET /health`, `GET /ready` | Liveness and readiness. | [`health.py`](../../../src/api/health.py) |
| `GET /plans/{task_id}` | Render a task's stored plan as HTML. | [`health.py`](../../../src/api/health.py) |
| `POST /api/messages/send`, `POST /api/sessions/{name}/message`, `GET /api/sessions/{name}/messages` | Send to and poll a named session's conversation. | [`messages.py`](../../../src/api/messages.py) |
| `GET /api/projects/{project_id}/graph` | The whole task graph of one project in a single read. | [`graph.py`](../../../src/api/graph.py) |
| `GET/POST /api/projects/{project_id}/graph/{extent,tiles,list,node,locate,tidy,jobs}` | Viewport-bounded spatial layout. | [`graph_layout.py`](../../../src/api/graph_layout.py) |
| `GET /api/metrics/series`, `GET /api/metrics/delivery-latency` | Fleet metrics history and computed delivery latency reports. | [`metrics.py`](../../../src/api/metrics.py) |
| `GET /api/providers/usage` | Each provider's own quota, as the provider reports it. | [`providers.py`](../../../src/api/providers.py) |
| `GET /api/proposals/{proposal_id}`, `GET /api/projects/{project_id}/batches/{batch_id}` | A proposed task graph and a held-ingestion batch detail. | [`routers/proposals.py`](../../../src/api/routers/proposals.py) |
| `GET /api/tasks/{task_id}/files`, `GET /api/tasks/{task_id}/file` | Browse and read files in a task's worktree. | [`task_files.py`](../../../src/api/task_files.py) |
| `POST/GET/DELETE /api/tasks/{task_id}/attachments…` | Image attachments on a task. | [`task_attachments.py`](../../../src/api/task_attachments.py) |
| `GET /api/reviews/{review_id}/revisions/{revision}/attachments/{attachment_id}` | Revision-owned screenshot evidence. | [`review_attachments.py`](../../../src/api/review_attachments.py) |
| `GET /api/reviews/pull-requests`, `/summary`, `POST /api/reviews/pull-requests/{task_id}/approve` | Cached PR inbox and per-project counts; approval requires the operator viewer and the current head SHA. | [`pull_requests.py`](../../../src/api/pull_requests.py) |
| `GET /ws/terminal/{session_id}` | HTTP access probe for the terminal; returns a status and close-code information without attaching. | [`terminal_stream.py`](../../../src/api/terminal_stream.py) |
| `GET/POST /api/host-shell`, `POST /api/host-shell/{name}/close` | Operator host-shell list/open/close, when enabled. | [`host_shell.py`](../../../src/api/host_shell.py) |
| `GET /api/tasks/{task_id}/sessions` | Execution history of a task, archived tasks included. | [`task_sessions.py`](../../../src/api/task_sessions.py) |
| `GET /api/workspaces/{workspace_id}/browse`, `GET /api/workspaces/{workspace_id}/file` | Browse and read files in a workspace. | [`workspace_files.py`](../../../src/api/workspace_files.py) |
| `POST /api/streams`, `GET /api/streams/{id}`, `/subscribe`, `/tail`, `POST /api/streams/{id}/kill` | Submit a finite managed job and view or cancel it; the stream id is the job id. | [`streams.py`](../../../src/api/streams.py) |

Both file-reading routes go through one helper,
[`file_serving.py`](../../../src/api/file_serving.py), which caps reads at
512 KB (413 above that), refuses absolute paths, `..` traversal and symlink
escapes (403), and answers 404 for anything that is not a readable regular
file.

### 3. Streaming

WebSockets at `/ws/events`, `/ws/terminal/{session_id}` and
`/ws/terminal/{session_id}/input` are outside OpenAPI. The HTTP terminal probe
and the SSE routes (`/api/sessions/{id}/stream`, `/api/sessions/{id}/pane`,
`/api/streams/{id}/subscribe`, `/api/jobs/{job_id}/output`) do appear in the
schema. Generated SSE operations do not implement incremental event parsing;
consumers handle the stream frames themselves. See [streaming](events.md).

## Reading the schema

The daemon serves the full schema as JSON at `/openapi.json` and no HTML API
reference. Neither the schema URL nor `/health`, `/ready` or `/api/health`
require a token.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/openapi.json
```

```text
200
```

## Source of truth, and how it is generated

[`openapi.json`](../../../openapi.json) at the repository root is a **committed
build artifact**, not a hand-written contract. Both generated clients are a
function of it, and it is a function of the checkout:

```mermaid
flowchart LR
  CMD["Command registry<br/>src/tools + src/commands"] --> APP["create_app()<br/>src/api/app.py"]
  MODELS["src/api/models/"] --> APP
  ROUTERS["hand-written routers"] --> APP
  APP --> SPEC["openapi.json"]
  SPEC --> PY["packages/aq-client/<br/>(committed)"]
  SPEC --> TS["packages/aq-ts-client/src/<br/>(generated on demand)"]
```

[`src/api/spec.py`](../../../src/api/spec.py) builds the spec offline from a
throwaway app — no daemon, no database — so regenerating needs only a
checkout:

```bash
./scripts/regenerate-api-client.sh --offline   # openapi.json + Python client
./scripts/regenerate-ts-client.sh --from-file  # TypeScript client from that file
```

Run both after **any** change to `src/api/models/` or a codegen router, and
commit `openapi.json` and `packages/aq-client/` with the change.
`tests/test_api_client_contract.py` fails when the committed spec drifts from
what `create_app()` serves, and when the generated client no longer matches the
spec in either direction. Version pinning and compatibility are covered on the
[Python client](python-client.md) page.

## State ownership

The API owns almost no state of its own. It is a transport in front of state
other components own:

| State | Written by | Lives in |
|---|---|---|
| Tasks, projects, sessions, gates, messages | `CommandHandler` via the command's implementation | PostgreSQL |
| Session API tokens | [`src/api/auth.py`](../../../src/api/auth.py) (`SessionTokenStore`) — minted at session start, revoked at session close | `api_session_tokens` table, plus a per-process cache |
| Per-request identity (`request.state.scope`) | [`src/api/middleware.py`](../../../src/api/middleware.py) | Memory, for one request |
| Console streams started via `POST /api/streams` | [`src/api/streams.py`](../../../src/api/streams.py) | Durable managed jobs and retained output; in-memory viewers are evicted after `streams.retention_seconds` |
| WebSocket client registry and per-client queues | [`src/api/websocket.py`](../../../src/api/websocket.py) | Memory, dropped on disconnect |
| The published schema | `create_app()` | `openapi.json`, committed |

Command-backed mutations dispatch through `CommandHandler`. Transport services
also maintain token records, stream audit events and the cached PR inbox;
these are not separate command routes.

## Common failures and recovery

| Symptom | What it means | What to do |
|---|---|---|
| `curl: (7) Failed to connect to 127.0.0.1 port 8081` | The daemon is not running, or `mcp_server.enabled` is false. | Check with `aq status`; ask the operator to start the daemon. Workers must not start it themselves. |
| `403 {"ok": false, "error": "out of scope: list_tasks"}` | A session token called a command outside the agent surface. | Expected. See [conventions](conventions.md#what-a-session-token-may-call); use a surface the token has, or ask an operator to run it. |
| `401 {"ok": false, "error": "invalid or expired token"}` | The token was revoked (session closed) or has passed `api_auth.token_ttl_hours`. | A live session gets a fresh token at launch; a dead session's token is not renewable. |
| `403 Command 'run_command' is not available over the API` | An `API_EXCLUDED` command was attempted through `/api/execute`. | There is no HTTP path to these; this is deliberate. |
| `422 {"error": "…"}` from a typed route | The command ran and returned an error. | The message is the command's own; the same call over `/api/execute` returns it as `{"ok": false, "error": …}` with a `details` object. |
| `422 {"detail": [{"loc": ["body", "title"], …}]}` | Request-body validation failed before the command ran. | Fix the body; `loc` names the field. |

## Related pages

* [Requests, authentication, scope and errors](conventions.md) — read this
  before writing any client.
* [Streaming](events.md) — the part of the surface OpenAPI cannot describe.
* [Module catalog: API](../modules/api.md) — every module behind these routes.
* [Glossary](../glossary.md) — the vocabulary used above.
* [Documentation style](../../contributing/documentation-style.md) — the rules
  these pages are written to.

## Source and tests

Implementation: [`src/api/`](../../../src/api/). Focused tests:

```bash
aq test tests/test_api_client_contract.py tests/test_api_execute_contract.py \
        tests/test_api_scope.py tests/test_api_auth.py
```
