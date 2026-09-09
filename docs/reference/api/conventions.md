# Requests, authentication, scope and errors

How to talk to the [HTTP API](README.md): what a request looks like, who the
daemon thinks you are, which of the two error envelopes you get, and how
listing endpoints limit their results.

## Requests

Almost every endpoint is `POST` with a JSON body and no query parameters. That
is a consequence of the surface being generated from commands: a command takes
an arguments object, so the route takes it as the body.

```bash
curl -s http://127.0.0.1:8081/api/task/show \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "solid-grove.13"}'
```

* Path parameters appear only on the hand-written resource routes
  (`/api/tasks/{task_id}/files`, `/api/projects/{project_id}/graph`, …).
* Query parameters appear only on `GET` routes
  (`/api/metrics/series?from=…&to=…&step=…`,
  `/api/sessions/{name}/messages?limit=…`).
* Every response is JSON except `/plans/{task_id}` (HTML), the file-read
  routes (plain text or a JSON envelope), the attachment read (an image) and
  the SSE streams (`text/event-stream`).

Two headers are worth knowing:

| Header | Direction | Meaning |
|---|---|---|
| `Authorization: Bearer aqs_…` | request | A session token. Omit it on loopback to get the trusted local identity. |
| `X-Request-ID` | both | Correlates a request with the daemon's structured logs. Supply your own or read the one the daemon generated; [`RequestContextMiddleware`](../../../src/api/middleware.py) echoes it back and binds it into every log line for the request. |

## Identity: the two scopes

Every request is resolved to a `RequestScope`
([`src/api/auth.py`](../../../src/api/auth.py)) by
[`TokenAuthMiddleware`](../../../src/api/middleware.py) before it reaches a
route:

| Scope | How you get it | What it can do |
|---|---|---|
| **local** | No `Authorization` header. | Everything. This is the operator path: the `aq` CLI on your own machine, and the dashboard. |
| **session** | `Authorization: Bearer aqs_…` | A narrow, server-defined set of commands, pinned to the token's own session, task and project. |

Session tokens are minted by the daemon when it launches a session and are
revoked when the session closes; `api_auth.token_ttl_hours` (default 72) is a
backstop expiry. An agent finds its own token in `AQ_API_TOKEN`, which the
session runtime sets alongside `AQ_API_URL`.

> **Note.** Ships with `api_auth.require_session_token: false`: a request
> without a token is trusted, which is what makes the local CLI work with no
> setup. Turning it on makes every non-exempt path require a token;
> `/api/health`, `/health`, `/ready`, `/docs`, `/redoc` and `/openapi.json`
> stay open either way.

Two identity fields are never taken from the client. `profile_id` and
`policy_fingerprint` are derived *after* the token validates, from the live
`sessions` row, so editing a profile takes effect without re-minting a token.
And every server-owned key (`_scope` and the rest of `SERVER_OWNED_ARG_KEYS`)
is stripped from the request body before the daemon injects its own — a client
cannot claim to be someone else by putting it in the body.

### What a session token may call

The allowlist is [`AGENT_COMMAND_SET`](../../../src/api/scope.py) — the
commands a worker needs to do its job and close out: `prime`, `task_show`,
`task_set`, `task_comment`, `task_comments`, `task_close`, `task_children`,
`task_progress`, `task_heartbeat`, `task_handoff`, `task_claim`,
`session_drain_ack`, `create_task`, `reparent_task`, `project_ready`,
`message_send`, `message_inbox`, `message_reply`, `memory_save`,
`memory_search`, `formula_list`, `formula_show`, `get_schema`,
`subagent_event`, `integration_status`,
`integration_resolve_candidate_member`. Anything else is refused:

```bash
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8081/api/execute \
  -H "Authorization: Bearer $AQ_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"command": "list_tasks", "args": {}}'
```

```text
{"ok":false,"error":"out of scope: list_tasks"}
HTTP 403
```

The same token on a command it does hold, over the typed route:

```bash
curl -s http://127.0.0.1:8081/api/task/show \
  -H "Authorization: Bearer $AQ_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task_id": "solid-grove.13"}'
```

```text
{"id":"solid-grove.13","project_id":"agent-queue","title":"Document REST, WebSocket and generated Python/TypeScript clients", … }
```

On top of the allowlist, `check_command_scope` **pins and injects** identity:
if the body omits `task_id`, `project_id` or `session_id`, the token's own
values are filled in; if the body names a different one, the request is
refused with `out of scope: <field> mismatch`. A worker therefore cannot act
on another task by naming it.

### Narrower and wider than the allowlist

Four things modify that picture, all of them in
[`src/api/scope.py`](../../../src/api/scope.py) and all decided from persisted
state — a live session row, the task it holds, the agent that holds it — never
from anything in the request:

* **Elevated tokens.** A per-project supervisor session skips the allowlist
  but stays pinned to its project. A *global* admin token (elevated with no
  project) may call anything, and is refused from any client address other
  than loopback.
* **Operator-only commands.** Some commands are refused even to an elevated
  token: the integration controls, integration configuration fields on
  `edit_project`, intelligence-class edits, `session_input`, global agent
  settings and the seven project-onboarding commands, which require the global
  admin or the local CLI.
* **Capability carve-outs.** A live worker session may run a small set of git
  commands (`git_push`, `git_create_pr`, and the read-only ones) restricted to
  its own task's branch — `git_merge` and `pr_merge` are not among them.
  Sessions running the `reviewer`, `final-reviewer`, `triage` or
  `playbook-compiler` profiles get similarly narrow grants, each resolved from
  graph provenance and each reaching exactly the task it was spawned for.
  These are capabilities that exist *if* a project runs such a session; the
  shipped default pipeline does not create reviewer or triage tasks.
* **Capability policy.** Scope answers "which project and task may this token
  touch". A separate policy answers "which commands may this profile run", and
  a denial there is `403` with `"error_code": "capability_denied"`.

## The two error envelopes

The same command error looks different depending on which surface you called,
and both shapes are pinned by
[`tests/test_api_execute_contract.py`](../../../tests/test_api_execute_contract.py).

### Typed routes — `POST /api/{category}/{command}`

Success is the command's result object, at the top level, with `200`:

```text
{"created":"demo","name":"demo","default_profile_id":null}
```

Failures map onto HTTP status codes:

| Status | Body | When |
|---|---|---|
| `401` | `{"ok": false, "error": "invalid or expired token"}` | Bearer token invalid, expired or revoked. |
| `403` | `{"error": "out of scope: …"}` or `{"error": "…", "error_code": "capability_denied"}` | Scope or capability refusal. |
| `409` | `{"error": …, "error_code": "revision_conflict", "current_revision": N}` | `edit_intelligence_class` only. |
| `422` | `{"error": "…"}` | The command ran and returned an error. |
| `422` | `{"detail": [{"loc": ["body", "title"], "msg": "Field required", …}]}` | The body failed validation before the command ran. |

Two families keep their whole structured payload on a `422` rather than being
reduced to one string, because their clients render it: the project-onboarding
commands (with `error_code`, `phase`, `field_errors`) and the
`escalation_*` / `digest_*` commands.

Real examples, same missing task:

```text
POST /api/task/show   {"task_id": "demo.999"}   → 422  {"error": "Task 'demo.999' not found"}
POST /api/task/create {"project_id": "demo"}    → 422  {"detail": [{"type": "missing", "loc": ["body", "title"], "msg": "Field required", …}]}
```

### `POST /api/execute` — the CLI envelope

One shape for everything, almost always `200`:

```text
{"ok": true,  "result": { … }}
{"ok": false, "error": "Task 'demo.999' not found"}
```

When a command returns extra structure alongside its message — the per-node
errors and warnings of `create_task_graph`, for instance — it is preserved
under `details`, which is why the CLI can print every finding rather than just
a count. Two cases are not `200`: a scope or capability refusal is `403`, and
an unhandled exception is `500` with `{"ok": false, "error": "Internal server
error"}`.

An excluded command is refused before it reaches the handler:

```text
POST /api/execute {"command": "run_command", "args": {}}
→ 403 {"ok": false, "error": "Command 'run_command' is not available over the API"}
```

## Limits and paging

There is **no global pagination convention** — each listing endpoint caps
itself in the way that suits it, and the schema is the authority:

| Shape | Where | Example |
|---|---|---|
| `limit` only (a cap, newest first) | most listings | `message_inbox`, `escalation_list`, `list_archived`, `read_logs`, `get_recent_events`, `session_logs`, `task_recent_activity`, `playbook_*` listings |
| `limit` + `offset` + `total` | comments and children | `task_comments` returns `{"comments": […], "total": N, "limit": L, "offset": O}` |
| `cursor` | two endpoints | the graph-layout `list` endpoint and `search_github_repositories` |
| Server-chosen resolution | `GET /api/metrics/series` | Never more than 12,000 points: the step is coarsened instead, and `truncated: true` says so. |
| Sequence replay | `/ws/events` | `?after_seq=N`, delivered in pages of 500 — see [streaming](events.md). |

Byte caps rather than row caps apply to the file routes: 512 KB per file read,
10 MB per task attachment.

To find the exact limits of one call, read the schema instead of guessing:

```bash
python3 -c "import json; s=json.load(open('openapi.json')); \
print(json.dumps(s['components']['schemas']['TaskCommentsRequest']['properties'], indent=2))"
```

## State ownership

Nothing on this page is configuration you edit except two keys, both under
`api_auth` in `~/.agent-queue/config.yaml`: `token_ttl_hours` and
`require_session_token` (plus `trusted_dashboard_origins`, which only the
[terminal WebSocket](events.md#terminal-websocket) reads). Tokens themselves
live in the database and in a per-process cache; scope is derived per request
and never stored.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `401 invalid or expired token` | Session closed (tokens are revoked on close) or past its TTL. | Use the token from a live session; there is no refresh endpoint. |
| `403 token restricted to loopback` | A global-admin token was used from another host. | By design. Run the call locally. |
| `403 out of scope: project_id mismatch` | The body named a different project than the token's. | Omit the field and let the daemon inject it. |
| `403 out of scope: this interactive agent has no assigned project` | A manually opened terminal session has no project; only `prime`, `get_schema` and `subagent_event` work there. | Run the command from a task session, or as the local operator. |
| `422 Field required` with `loc: ["body", …]` | The body is missing a required field, or a field has the wrong type. | Check the request schema in `openapi.json` or `/docs`. |
| A `null` you sent was ignored | Typed routes drop `null` fields before dispatch, so "unset" and "explicitly null" normally collapse. | A few commands preserve an explicit null deliberately (`task_set`, `pool_scale`'s bounds, `edit_task`'s routing fields). For anything else, use `/api/execute`, which forwards `args` verbatim. |

## Related pages

* [HTTP API overview](README.md) — the route families these rules apply to.
* [Streaming](events.md) — the socket surfaces, which authenticate the same
  way but close with a code instead of answering a status.
* [Python client](python-client.md) / [TypeScript client](typescript-client.md)
  — both of which encode these conventions for you.

## Source and tests

[`src/api/auth.py`](../../../src/api/auth.py),
[`src/api/middleware.py`](../../../src/api/middleware.py),
[`src/api/scope.py`](../../../src/api/scope.py),
[`src/api/execute.py`](../../../src/api/execute.py),
[`src/api/codegen.py`](../../../src/api/codegen.py).

```bash
aq test tests/test_api_auth.py tests/test_api_scope.py \
        tests/test_api_execute_contract.py tests/test_command_scope_isolation.py
```
