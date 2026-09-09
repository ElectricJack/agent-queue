# Streaming: WebSocket events, SSE and terminals

The [HTTP API](README.md) answers questions. This page covers the other half:
the connections that stay open and push. None of them appear in
[`openapi.json`](../../../openapi.json) — OpenAPI describes request/response
operations, not sockets — so no client is generated for them and every
consumer, including the dashboard, speaks them by hand.

| Surface | Path | Transport | What it carries |
|---|---|---|---|
| [Fleet events](#fleet-events-wsevents) | `/ws/events` | WebSocket | Every task, session, gate, pool, playbook, message and metrics event in the fleet. |
| [Session transcript](#session-transcript-sse) | `/api/sessions/{session_id}/stream` | SSE | An agent's transcript, replayed then tailed. |
| [Session pane](#session-pane-sse) | `/api/sessions/{session_id}/pane` | SSE | Rendered terminal screens for a session. |
| [Console stream](#console-stream-sse) | `/api/streams/{stream_id}/subscribe` | SSE | Output of a command started through `POST /api/streams`. |
| [Terminal](#terminal-websocket) | `/ws/terminal/{session_id}` | WebSocket | Raw bytes to and from a tmux attach client. |

## Fleet events (`/ws/events`)

One connection gives you everything the daemon publishes on its internal event
bus that is safe for your scope. The dashboard keeps exactly one such
connection open for the whole tab and uses it to invalidate its caches.

### Connect

Authentication matches REST: no header on loopback is the trusted local
identity; `Authorization: Bearer aqs_…` is a session scope. An invalid token
closes the socket with code `4401` rather than answering a status.

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
`message.`, `gate.`, `session.`, `task.`, `pool.`, `metrics.` and `playbook.`.
Anything else stays internal to the daemon.

### Replay after a disconnect

Reconnect with `?after_seq=N` and the daemon replays persisted events with
`id > N` — in ascending order, 500 at a time — before live streaming resumes.
Frames delivered during the replay window that would duplicate the history are
dropped, so a client that stores the highest `seq` it has seen gets an
at-most-once stream across a reconnect. Two rules make that safe:

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
| `agent.question`, `agent.question.updated` | Projected down to identifiers only (no question content), and only to a connection entitled to that session or project. |

Everything else is delivered as published, so a session-scoped client should
filter on `project_id` itself rather than assume the server has.

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

Three routes work together
([`src/api/streams.py`](../../../src/api/streams.py)):

1. `POST /api/streams` starts a command — `{"command": ["…"], "cwd": …}` —
   and returns a `stream_id`. Requires the local or an elevated scope; the
   `cwd` must resolve inside an accessible workspace (`403` otherwise); more
   than `streams.max_concurrent_per_session` live streams for one session is
   `429`.
2. `GET /api/streams/{stream_id}/subscribe?after_seq=N` streams the output,
   and `GET /api/streams/{stream_id}/tail` returns what has accumulated.
3. `POST /api/streams/{stream_id}/kill` stops it.

Buffers are memory-only and bounded by `streams.buffer_max_lines` /
`streams.buffer_max_bytes`; finished streams are evicted after
`streams.retention_seconds` by a sweep. Nothing survives a daemon restart.

## Terminal WebSocket

`GET /ws/terminal/{session_id}` attaches to a live session's tmux pane and
carries raw bytes both ways. It is the only surface that types into an agent,
and it is guarded accordingly
([`src/api/terminal_stream.py`](../../../src/api/terminal_stream.py)):

* The subprotocol is `aq-terminal-v1`. Credentials travel either as an
  `Authorization` header or as an `aq-bearer.<token>` subprotocol — never in
  the URL, which would leak into access logs (`4401` if you try).
* Only a global-admin token may attach, and only from loopback (`4403`).
* The `Origin` must be the same local origin, or listed in
  `api_auth.trusted_dashboard_origins` (`4403`).
* Input frames are capped at 64 KB, the input queue at 128 KB, and terminal
  dimensions are validated (`4400`).
* Errors close the socket with a constant, operator-safe reason. The route
  never launches an agent; it owns a disposable attach client and nothing
  else, and input is ephemeral — no transcript, no replay, no input log.

## State ownership

Streams own no durable state. The event stream reads the `events` table for
replay but writes nothing; console-stream buffers and the WebSocket client
registry are memory-only and die with the daemon. The durable record of what
happened is the `events` table and each session's transcript on disk, which is
what the replay and SSE endpoints read from.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Socket closes immediately with `4401` | Invalid, expired or revoked token; or `require_session_token` is on and none was sent. | Reconnect with a live session's token. |
| Terminal closes with `4403` | Not a global-admin token, not loopback, or an untrusted `Origin`. | Attach from the local dashboard, or add the origin to `api_auth.trusted_dashboard_origins`. |
| Reconnect delivers nothing new | A stale `after_seq` from a previous daemon epoch: the server's replay cursor is ahead of every live frame. | Compare the `hello` epoch with the stored one; on a mismatch drop the cursor and reconnect without `after_seq`. |
| Gaps in a busy stream | The per-client queue overflowed and dropped the oldest frames. | Treat frames as invalidation hints, not as a ledger; re-read state over REST. |
| `429 too many concurrent streams` | More than `streams.max_concurrent_per_session` console streams for one session. | Kill a finished stream or wait for the retention sweep. |
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
aq test tests/test_events_replay.py tests/test_agent_question_websocket.py \
        tests/test_session_stream_api.py tests/test_pane_stream_api.py \
        tests/test_streams_api.py tests/test_terminal_stream.py
```
