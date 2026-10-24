# Streaming: WebSocket events, SSE and terminals

The [HTTP API](README.md) answers questions. This page covers the other half:
the connections that stay open and push. WebSockets are outside
[`openapi.json`](../../../openapi.json). SSE endpoints appear as HTTP operations,
but their frame protocols and incremental consumption are handled by callers,
including the dashboard.

| Surface | Path | Transport | What it carries |
|---|---|---|---|
| [Fleet events](#fleet-events-wsevents) | `/ws/events` | WebSocket | Every task, session, gate, pool, playbook, message and metrics event in the fleet. |
| [Session transcript](#session-transcript-sse) | `/api/sessions/{session_id}/stream` | SSE | An agent's transcript, replayed then tailed. |
| [Session pane](#session-pane-sse) | `/api/sessions/{session_id}/pane` | SSE | Rendered terminal screens for a session. |
| [Console stream](#console-stream-sse) | `/api/streams/{stream_id}/subscribe` | SSE | Retained output of a managed job submitted through `POST /api/streams`. |
| [Job output](#console-stream-sse) | `/api/jobs/{job_id}/output` | SSE | The same retained job output, with an `after` byte cursor. |
| [Terminal](#terminal-websocket) | `/ws/terminal/{session_id}` | WebSocket | Raw bytes to and from a tmux attach client; `/input` accepts input without attaching. |

## Fleet events (`/ws/events`)

One connection carries the forwarded event families, with the specific scope
filters listed below. Other families have a known isolation gap. The dashboard keeps exactly one such
connection open for the whole tab and uses it to invalidate its caches.

### Connect

The handshake resolves no bearer header to local scope (unless
`require_session_token` is on), and `Authorization: Bearer aqs_…` to a session
scope. An invalid token closes with `4401`. Unlike HTTP token middleware,
this handler does not enforce the global-admin token loopback restriction;
it also lacks the terminal service's origin checks. See the
[system gaps](../../specs/systems/api-and-clients.md#behaviour).

```bash
python3 - <<'PY'
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8081/ws/events") as ws:
        for _ in range(2):
            print(json.dumps(json.loads(await ws.recv()))[:120])

asyncio.run(main())
PY
```

```text
{"type": "hello", "epoch": "50790184-303d-4776-8c5a-503fe59df557"}
{"event_type": "notify.task_message", "severity": "info", "category": "task_stream", "project_id": "agent-queue", …
```

### Frames

The first frame is always `{"type": "hello", "epoch": "<uuid>"}`. The epoch is
regenerated every time the daemon starts. Store it with your cursor: when the
epoch changes, a stored sequence number is meaningless and must be discarded.

Every other frame is an event. The shape is the event's own payload plus two
fields the transport adds:

| Field | Meaning |
|---|---|
| `_event_type` | The bus event type — `task.completed`, `notify.task_message`, `metrics.tick`, `playbook.v2.run.started`, … |
| `seq` | The `events` row id for a replayed frame; `null` for a live frame that no row backs. |

A real live frame:

```text
{
  "event_type": "notify.task_message",
  "severity": "info",
  "category": "task_stream",
  "project_id": "agent-queue",
  "task_id": "solid-grove.13",
  "message": "[tool_use: Bash]",
  "message_type": "agent_output",
  "role": "assistant",
  "stream_id": "e49cbc70-…:52c4d223-…",
  "stream_done": false,
  "event_id": "fdf8fb6a6d91",
  "_event_type": "notify.task_message",
  "seq": null
}
```

Only these event-type prefixes are forwarded
([`_FORWARDED_PREFIXES`](../../../src/api/websocket.py)): `notify.`, `agent.`,
`message.`, `gate.`, `session.`, `task.`, `pool.`, `metrics.`, `provider.`,
`dashboard_state.`, `review.`, `ask.`, `playbook.`, `project.paused` and
`project.resumed`. Anything else stays internal to the daemon.

### Replay after a disconnect

Reconnect with `?after_seq=N` and the daemon replays persisted events with
`id > N` — in ascending order, 500 at a time — before live streaming resumes.
Frames delivered during the replay window that would duplicate the history are
dropped, so a client that stores the highest `seq` it has seen can suppress
persisted duplicates across a reconnect. Live frames with
`seq: null` cannot be deduplicated this way, and bounded queues can lose frames;
treat events as invalidation hints and refetch authorized state. On reconnect:

* Only forwarded event types are replayed, so a reconnect does not flood you
  with traffic the live path never sends.
* If the `hello` epoch differs from the one you stored, throw your cursor away
  and reconnect without `after_seq`.

### Scope filtering

Some frames are filtered per connection, based on the scope resolved at
handshake:

| Event | Who sees it |
|---|---|
| `metrics.*` | The local surface and elevated sessions only — a metrics sample is fleet-wide by construction and has no per-project projection. |
| `pool.*` | Local; a project-elevated session for its project; a worker only for its own session's events. |
| `provider.*` | Local and global elevated scopes see the full event; project supervisors receive an invalidation projection with `redacted: true`; workers receive none. |
| `dashboard_state.*` | Local scope only, for workspace documents or the local operator's user documents. |
| `agent.question`, `agent.question.updated` | Projected down to identifiers only (no question content), and only to a connection entitled to that session or project. |

There is no general project/task filter for the other forwarded families in
[`websocket.py`](../../../src/api/websocket.py). They are delivered as published.
Client-side filtering does not provide authorization; this is a known server
isolation gap, and callers must not treat this socket as a project-confined
feed. HTTP resource reads enforce their own scope checks.

### Backpressure

Each client has a 1,000-frame queue. When it overflows, the **oldest** frame
is dropped to make room — a slow consumer loses history, never the connection.
On a busy fleet `metrics.tick` alone is one frame per second, which is why the
dashboard subscribes to raw frames for its Metrics tab and routes everything
else through a separate invalidation path
([`dashboard/src/ws/useEventStream.ts`](../../../dashboard/src/ws/useEventStream.ts)).

## Session transcript (SSE)

`GET /api/sessions/{session_id}/stream` replays a session's transcript and
then tails it. The `session_id` may be a session id or a session name. Query
parameters: `attempt_id` (pin the read to one recorded attempt rather than the
live session), `replay_only=1` (send the history and stop) and `max_seconds`.

Because a pool session is reused across tasks, "the transcript of a session"
and "the transcript of an attempt" are different reads: passing `attempt_id`
bounds the output to that attempt's window, which is what makes a finished
task's transcript still readable after the session moved on. A missing session
or a mismatched attempt is `404`; a session in another project is `403`.

## Session pane (SSE)

`GET /api/sessions/{session_id}/pane` streams rendered terminal screens
(`tmux capture-pane` output) for a session. One poll loop is shared by every
watcher of the same session
([`PaneBroadcaster`](../../../src/api/pane_stream.py)). Use it to *watch* a
session; use the terminal WebSocket to type into one.

## Console stream (SSE)

The routes in [`src/api/streams.py`](../../../src/api/streams.py) are bounded
viewers over durable managed jobs. They do not execute arbitrary commands.

1. `POST /api/streams` requires a local or elevated scope and a body containing
   `command` (a nonempty argv list), `cwd`, `session_id`, and `idempotency_key`;
   `project_id` and `title` are optional. The session must hold a task, and
   `cwd` must equal that task's workspace root. The finite command adapter
   accepts server-owned test/lint/build/install presets; arbitrary shell
   commands are refused with `400`. Admission runs through `job_submit`, so
   `resources.jobs.enabled: false` returns `503 jobs.disabled`. The returned
   `stream_id` is the canonical job id; retries use the same idempotency key.
2. `GET /api/streams/{id}` reads metadata. `/subscribe?after_seq=N` streams
   retained output, and `/tail?after_seq=N` returns frames as JSON.
   `GET /api/jobs/{job_id}/output?after=N` exposes the same SSE output.
3. `POST /api/streams/{id}/kill` requests `job_cancel`. Viewer disconnect,
   eviction or failure never cancels execution.

Cursors count original output **bytes**, rather than line numbers. Frames
include `line`, `gap`, `exit` and `killed`; gap frames carry `after` and `next`
when bounded retention omitted a range. Reconnect resumes from the retained
output store even after the in-memory viewer was discarded. Metadata and
cancellation remain available after output expires; log reads answer `410`
with `logs_expired`. Job scope is checked on each route, including cached viewers.

Viewer rings obey `streams.buffer_max_lines` / `buffer_max_bytes`, with finished
viewers evicted after `streams.retention_seconds`. Each principal gets at most
two SSE attachments to a job; excess attachments or excess concurrent starts
(`streams.max_concurrent_per_session`) answer `429`. A slow subscriber receives
a gap and is disconnected so it can resume from durable output. Job records,
execution recovery, output retention and workspace pins belong to the
[managed job service](../../specs/systems/test-service-and-jobs.md).

## Terminal WebSocket

`/ws/terminal/{session_id}` attaches a disposable tmux client to a live session.
`/ws/terminal/{session_id}/input` sends input without changing the watched pane's
size, useful alongside the pane SSE on a phone. An HTTP `GET` on the first path
is an access probe (`terminal_access` in OpenAPI): it returns `ready`, `exited`
or `error` with close-code information and never attaches.

The shared checks live in
[`src/api/terminal_stream.py`](../../../src/api/terminal_stream.py):

* The subprotocol is `aq-terminal-v1`. Credentials use an `Authorization`
  header or an `aq-bearer.<token>` subprotocol. URL credentials are refused
  (`4401`). No token uses local scope unless `require_session_token` is on;
  a bearer caller needs an elevated, projectless, taskless global token.
* The daemon-facing peer must be loopback (`4403`). A browser `Origin` must
  match a literal-loopback origin or `api_auth.trusted_dashboard_origins`.
  The HTTP probe applies the same origin policy.
* The target must still be a live tmux session with the same launch instance,
  task assignment and claim generation. Disappearance or a changed assignment
  closes with `4409`; the connection cap uses `4429`.
* Input frames are capped at 64 KB and the queued input at 128 KB. Invalid
  controls or dimensions close with `4400`. Desktop/phone attach options
  (`cols`, `rows`, `phone`, `scrollback`) are validated by the service.
* Attach owns a disposable client, never the agent. Agent terminal input is
  ephemeral; the socket does not create an input transcript or replay log.

Operator host shells use this same terminal transport, but have a separate
lifecycle: `GET/POST /api/host-shell` and `POST /api/host-shell/{name}/close`.
They require `dashboard.host_shell.enabled` and reject **all** bearer tokens.
The local operator can open them; a remote dashboard viewer requires
`dashboard.host_shell.allow_remote`. The dashboard edge supplies the viewer
verdict. Open, close and socket connect/disconnect events are audit-logged.
The list route returns `enabled: false` with no shells when disabled. See
[`src/api/host_shell.py`](../../../src/api/host_shell.py).

## State ownership

The event stream reads the `events` table for replay. Session transcript SSE
reads transcripts and retained attempt windows; pane SSE reads live screens.
WebSocket queues and console viewers are in memory. Managed-job records and
retained output outlive those viewers, while host-shell lifecycle auditing
also writes events. Viewer retention and durable output retention are separate.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Socket closes immediately with `4401` | Invalid, expired or revoked token; or `require_session_token` is on and none was sent. | Reconnect with a live session's token. |
| Terminal closes with `4403` | Caller lacks global operator scope, is not loopback, or has an untrusted `Origin`. | Attach from the local dashboard, or add the origin to `api_auth.trusted_dashboard_origins`. |
| Reconnect delivers nothing new | A stale `after_seq` from a previous daemon epoch: the server's replay cursor is ahead of every live frame. | Compare the `hello` epoch with the stored one; on a mismatch drop the cursor and reconnect without `after_seq`. |
| Gaps in a busy stream | The per-client queue overflowed and dropped the oldest frames. | Treat frames as invalidation hints, not as a ledger; re-read state over REST. |
| `429 too many concurrent streams` | The session has reached its concurrent viewer limit. | Wait for a job to finish or cancel an active job. |
| `503 jobs.disabled` / `400 jobs.preset_denied` | Managed jobs are disabled, or the command is outside the finite presets. | Ask the operator about job enablement; submit only an accepted preset. |
| `410 logs_expired` | The durable output retention period ended. | Read job metadata/result; reconnecting cannot restore expired bytes. |
| SSE ends with no data | The session ended, or the attempt window closed. | Read the recorded attempt with `attempt_id`, or `aq session logs`. |

## Related pages

* [HTTP API overview](README.md) — the request/response half.
* [Requests, authentication, scope and errors](conventions.md) — the same
  identity model these sockets use.
* [TypeScript client](typescript-client.md) — how the dashboard consumes the
  event stream alongside the generated REST client.

## Source and tests

[`src/api/websocket.py`](../../../src/api/websocket.py),
[`src/api/sessions.py`](../../../src/api/sessions.py),
[`src/api/pane_stream.py`](../../../src/api/pane_stream.py),
[`src/api/streams.py`](../../../src/api/streams.py),
[`src/api/terminal_stream.py`](../../../src/api/terminal_stream.py).

```bash
aq test tests/test_events_replay.py tests/test_websocket_forwarding.py tests/test_agent_question_websocket.py \
        tests/test_session_stream_api.py tests/test_pane_stream_api.py \
        tests/test_streams_api.py tests/test_terminal_stream.py
```
