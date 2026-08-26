# Live Pane Streaming + Agents Console Grid — Design

**Status:** design (approved by brainstorming pass 2026-08-25).
**Depends on:**
- `2026-08-22-pane-session-peek-design.md` (the `session-peek` pane view
  this spec makes actually live).
- `docs/specs/design/session-runtime.md` (`SessionProvider` contract,
  `Cap` gating, the tmux provider).

## 1. Goal

Make the dashboard a real window onto running agents: a live terminal
screen per session, and an Agents-tab grid that shows several at once.

Today the pieces look present but the live path is transcript-only.
`GET /api/sessions/{id}/stream` (`src/api/sessions.py`) emits a pane
peek frame **once, at connect** (`:130-146`) and never again — the tail
loop reads only the harness JSONL (`:184-196`). So the "pane view"
toggle on `SessionDetail` and the whole `session-peek` pane show a
single stale snapshot. What the transcript can never show is exactly
what you need when debugging: startup dialogs, harness crash output,
and TUI state.

This spec adds a live pane screen stream and a multi-session grid,
without touching the transcript path.

## 2. Non-goals

- **Not** post-mortem scrollback. The stream is for live sessions; a
  dead session's last screen is whatever `remain-on-exit` preserved.
- **Not** byte-faithful capture. `tmux pipe-pane` was considered and
  rejected: the harnesses are full-screen TUIs, so raw pane bytes are
  mostly cursor-addressing escapes, and rendering them faithfully needs
  a real emulator (xterm.js, ~250KB) plus mandatory log rotation. tmux
  is already a correct terminal emulator; we read its rendered screen
  instead.
- **Not** an input path. Typing into an agent stays
  `tmux -u -L aq attach -t =<name>` (already surfaced by
  `session_attach`). Web `session_nudge` is a message, not a keyboard.
- **Not** a change to the transcript stream. `/api/sessions/{id}/stream`
  is untouched.
- **Not** visible desktop terminal windows per agent. That option was
  investigated and superseded by this one; hands-on rescue still uses
  the attach command above.

## 3. Architecture

Three units, each independently testable:

```
tmux capture-pane -p -e        PaneBroadcaster           SSE                 React
  (one per watched session) ->  poll + dedupe + fanout ->  /pane  ->  usePaneStream
```

### 3.1 `PaneBroadcaster` (new — `src/sessions/pane_broadcaster.py`)

One poll loop **per watched session**, fanned out to every subscriber.
Cost is `O(watched sessions)`, never `O(sessions x viewers)`. This is
the load-bearing decision: the reconciler already collapses per-session
tmux calls behind `TmuxStateCache` (`state_cache_ttl_seconds: 2`)
precisely because every `_tmux()` call is a fresh
`asyncio.create_subprocess_exec` (`src/sessions/tmux.py:106`). A naive
per-viewer poll would reintroduce what that cache exists to prevent.

Interface:

- `subscribe(session) -> asyncio.Queue[Frame]` — registers a subscriber
  and starts the session's loop if it is the first. Immediately enqueues
  the last known screen, or a fresh peek when there is none.
- `unsubscribe(session, queue)` — drops the subscriber; the loop stops
  after a **5s linger** when the last one leaves, so a page refresh or
  React StrictMode's double-mount does not thrash the loop.
- `shutdown()` — cancels every loop; called from daemon teardown.

Loop behaviour:

- Poll `provider.peek(handle, lines, ansi=True)` every
  `pane_stream_interval_seconds`.
- **Push only when the screen changed.** An idle agent costs one
  `capture-pane` per tick and zero bytes on the wire.
- Each frame carries a monotonically increasing `seq` per session.
- On a provider error: keep the last good screen, do not push. After 3
  consecutive failures, push one `{type:"error"}` frame and stop the
  loop (subscribers stay connected and see the reason).
- When the session is no longer running, push `{type:"stopped"}` and
  stop the loop.

Capacity: `pane_stream_max_sessions` (default 12) bounds concurrently
watched sessions. Over the cap, `subscribe` refuses with an error frame
rather than degrading the daemon. Refusal is explicit and visible in
the UI — never a silent empty tile.

### 3.2 Provider contract — one additive change

```python
async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str
```

`TmuxProvider` appends `-e` to `capture-pane` when `ansi` is set
(`src/sessions/tmux.py:450`), preserving SGR colour. `SubprocessProvider`
and `FakeProvider` ignore the flag — their output is already raw log
text.

`ansi=False` is the default, so every existing caller
(`_cmd_session_peek`, the reconciler, the SSE peek fallback) is
byte-identical to today. Dialog scraping does not go through `peek` at
all — it uses the provider-private `_capture` (`src/sessions/tmux.py:256`)
— so the startup-dialog and readiness matchers are untouched by this
change.

### 3.3 API — `GET /api/sessions/{session_id}/pane`

Server-Sent Events, one frame per changed screen:

```json
{"source": "pane", "type": "screen", "screen": "...", "seq": 12, "ts": 1.0}
{"source": "pane", "type": "stopped", "seq": 13, "ts": 2.0}
{"source": "pane", "type": "error", "message": "...", "seq": 14, "ts": 3.0}
```

- First frame is immediate (last known screen, else a fresh peek).
- 15s heartbeat comments, matching `/stream`'s intermediary handling.
- `max_seconds` query param for tests, mirroring the transcript endpoint.
- Unknown session -> 404. Provider without `Cap.PEEK` -> 409 naming the
  provider, never a plausible-looking empty stream. Both are *request*
  errors, so both stay HTTP status codes.
- Over the cap -> **HTTP 200** whose first and only frame is
  `{"type":"error","message":"pane stream cap reached ..."}`. A refusal
  is a *stream* condition, and `EventSource` fails permanently on a
  non-200 while exposing neither status nor body to JS — a 429 would
  render as exactly the silent dead tile §3.1 forbids.
- Built by a `build_pane_router(...)` factory alongside
  `build_sessions_router`, so tests wire a lightweight db and a
  `FakeProvider` without the daemon.

A separate endpoint rather than a `?source=pane` flag on `/stream`: the
lifecycles differ (broadcaster-backed fan-out vs per-connection file
tail), and keeping `/stream` untouched means the transcript path cannot
regress.

### 3.4 Frontend

- `dashboard/src/ws/usePaneStream.ts` — `usePaneStream(sessionId, {enabled})`,
  shaped like `useTranscriptStream` (EventSource, status surface,
  native reconnect) but holding **one current screen** plus status,
  not a bounded buffer. Screens replace; they do not accumulate.
- `dashboard/src/components/LivePaneConsole.tsx` — renders that screen
  through the existing `ansiToSpans`
  (`dashboard/src/panes/console-stream/ansi.tsx`), reusing
  `PeekFrameConsole`'s terminal styling. No new dependency.
- `dashboard/src/panes/session-peek/index.tsx` and `SessionDetail`'s
  pane-view branch switch from `useTranscriptStream` peek frames to
  `usePaneStream`, so both become live.
- `dashboard/src/pages/command-center/Agents.tsx` gains a `Table|Grid`
  segmented toggle. Table stays the default view, unchanged. Grid
  renders one `AgentConsoleTile` per **running** session (a tile is a
  header button plus a `LivePaneConsole`), capped at `MAX_TILES = 8`
  with a visible "+N more" note. The tile cap sits *below*
  `pane_stream_max_sessions` (12) on purpose: a full grid must not
  saturate the backend cap, or navigating from the grid to a session
  detail page inside the broadcaster's linger window gets refused.
  Clicking a tile's header opens the full `session-peek` pane,
  preserving today's row behaviour.

Polling only happens while something is subscribed, so the Table view
costs nothing.

## 4. Config

New fields on `SessionsConfig` (`src/config.py:867`), all validated
`> 0` in `validate()`:

| Field | Default | Meaning |
|---|---|---|
| `pane_stream_interval_seconds` | `1.0` | Poll cadence per watched session. |
| `pane_stream_max_sessions` | `12` | Concurrently watched session cap. |
| `pane_stream_lines` | `60` | Lines requested per `peek`. |

## 5. Error handling

Every failure is explicit rather than a plausible blank:

- Provider lacks `Cap.PEEK` -> 409 naming the provider.
- Session unknown -> 404.
- tmux command failure -> last good screen retained; error frame after 3
  consecutive failures.
- Over the session cap -> refusal frame, rendered as a visible tile
  message.
- Session stopped -> `stopped` frame, loop ends, last screen stays on
  screen.

## 6. Testing

**Backend** (`FakeProvider`, no tmux required):
- Two subscribers to one session cause one poll loop, not two.
- Unsubscribing the last subscriber stops the loop after the linger;
  re-subscribing within the linger reuses the running loop.
- An unchanged screen emits no frame; a changed screen emits one.
- The cap refuses subscription 13 with an error frame over HTTP 200.
- Consecutive provider errors emit one error frame and stop the loop.
- Endpoint: 404 unknown, 409 no-PEEK, frame shape, heartbeat, bounded
  by `max_seconds` (the pattern in `tests/` for `/stream`).

**tmux** (`tests/test_tmux_integration.py`): `ansi=True` puts `-e` on
the `capture-pane` argv; `ansi=False` does not.

**Frontend** (vitest, following `dashboard/src/panes/__tests__`):
`usePaneStream` replaces rather than appends screens and surfaces
status; `AgentConsoleTile` renders a screen and its header; the
Table/Grid toggle switches views and only the grid subscribes.

## 7. Files touched

New:
- `src/sessions/pane_broadcaster.py`
- `src/api/pane_stream.py`
- `dashboard/src/ws/usePaneStream.ts`
- `dashboard/src/components/LivePaneConsole.tsx`
- `dashboard/src/pages/command-center/AgentConsoleTile.tsx`

Modified:
- `src/sessions/provider.py` (peek signature), `tmux.py`,
  `subprocess.py`, `fake.py`
- `src/config.py` (`SessionsConfig`)
- `src/api/app.py` (router registration), daemon teardown for
  `shutdown()`
- `dashboard/src/pages/command-center/Agents.tsx`,
  `dashboard/src/panes/session-peek/index.tsx`,
  `dashboard/src/pages/SessionDetail.tsx`
