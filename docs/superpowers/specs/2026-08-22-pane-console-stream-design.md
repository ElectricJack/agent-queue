# Pane View: `console-stream` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell primitives),
`2026-08-22-pane-plugin-interface-design.md` (pane view contract — every
section below implements that contract).
**Ship wave:** v2 (Phase C, second subset — session-peek, console,
playbook-run-inspector).

## 1. Goal

Give the supervisor a way to surface **live stdout/stderr of a single
long-running command** in the shell pane without cluttering the chat
transcript with scrollback. Typical trigger: the user asks the
supervisor to run `pytest`, a build, or a migration; the supervisor
starts the command, gets back a `streamId`, and pushes
`pane.open("console-stream", { streamId })` so output streams live on
the right while chat stays a clean narrative ("Started pytest — watch
it on the right →").

Deliberately narrow: **one command, one stream, one view instance.**
Not a terminal, not a REPL, not a way to send input to the process.

## 2. Non-goals

- Not an interactive terminal — no stdin, no PTY, no resize signal.
- Not a persistent log browser. Once the pane closes and the command
  exits, there's no "reopen the last stream" affordance in the view
  itself (the daemon may still hold it briefly, §8.5).
- Not a job-queue/background-task manager UI — scoped to one command's
  lifetime.
- Not multiplexed — one pane instance shows one `streamId`. The shell's
  single-pane-slot rule means watching a second command replaces the
  view.
- Not responsible for **starting** commands from the UI — only the
  supervisor (or another trusted server-side actor) starts streams;
  there's no "run a command" button anywhere in this view (§10).

## 3. Manifest

```ts
// dashboard/src/panes/console-stream/manifest.ts
import { z } from "zod";
import { TerminalSquare } from "lucide-react";
import type { PaneManifest } from "../types";

export const consoleStreamArgsSchema = z.object({
  streamId: z.string().min(1),
  title: z.string().optional(),
  sessionId: z.string().optional(),
});
export type ConsoleStreamArgs = z.infer<typeof consoleStreamArgsSchema>;

export const manifest: PaneManifest<ConsoleStreamArgs> = {
  id: "console-stream",
  name: "Console",
  description: "Live stdout/stderr for a running command.",
  icon: TerminalSquare,
  args_schema: consoleStreamArgsSchema,
  open_shortcut: null,          // agent-push is the primary opener
  route_scope: "cross-route",   // a running stream survives navigation
  agent_pushable: true,
  palette_label: null,          // ephemeral, per-command — not searchable
};
```

Directory layout (per plugin interface spec §3):

```
dashboard/src/panes/console-stream/
├── manifest.ts
├── index.tsx
├── hooks.ts             # useConsoleStream(streamId) — SSE subscription
└── __tests__/
    ├── manifest.test.ts
    └── index.test.tsx
```

## 4. Args + validation

| field       | type   | required | notes |
|-------------|--------|----------|-------|
| `streamId`  | string | yes      | Id the daemon assigned when it started the streamable command. |
| `title`     | string | no       | Header label, e.g. `"Running pytest…"`. Falls back to `"Console"`. |
| `sessionId` | string | no       | Owning session, for a client-side scope sanity check (§4.1). Not an auth credential — the daemon enforces the real check server-side (§8.4). |

Validated by `consoleStreamArgsSchema` on every `open()` / `setArgs()`
per the plugin interface contract. Invalid args → console error + no-op,
same as every other view.

### 4.1 Client-side scope sanity check

If `args.sessionId` doesn't match the dashboard's authenticated session
identity, the component renders the scope-mismatch state (§9.3) instead
of subscribing. UX nicety only — the server-side check (§8.4) is the
real enforcement point and must not be skipped.

## 5. Component

### 5.1 Layout

```
┌ [terminal] Running pytest… ─── running ⟳  [Pause tail][Copy][Kill] × ┐
│ $ pytest tests/ -x                                                  │
│ ============================= test session starts =================│
│ tests/test_orchestrator.py::test_promote_defined PASSED       [12%] │
│ tests/test_orchestrator.py::test_stuck_monitor PASSED          [25%]│
│ ▊ (cursor, tail-following)                                          │
└───────────────────────────────────────────────────────────────────┘
```

- Monospace scrolling console, dark terminal-style background
  regardless of app theme (matches existing code-block treatment).
- Each line is its own row (not one giant `<pre>`) for line-level ANSI
  coloring and predictable copy-selection; virtualized past ~500 lines
  (same virtualizer the Tasks table already uses for large lists).
- stdout/stderr interleaved in arrival order (server tags each frame
  `stream: "stdout"|"stderr"`); stderr lines get a subtle red left
  border rather than a separate region — order matters more than
  stream separation for a one-shot run.
- ANSI SGR codes (pytest/build output) parsed client-side with a small
  local converter — reuse one if the dashboard already has it, else a
  ~100-line util; no new heavy dependency for this alone.

### 5.2 Header status

Driven by the SSE stream's terminal frame (§7):

| state | rendering |
|-------|-----------|
| `running` | spinner + `"running"` + live elapsed time (`12s`, `1m 04s`) |
| `exited(rc=N)` | `rc===0` → green check `"exited (0)"`; else red X `"exited (N)"` |
| `killed` | gray stop icon + `"killed"` |

Header freezes at whichever terminal state arrives — no further
transitions (§9.5).

### 5.3 Auto-follow ("tail")

- Default **on**; new lines append and view auto-scrolls to bottom.
- Any manual scroll away from bottom disables it (standard sticky-tail
  UX, e.g. `docker logs -f`).
- Re-enable: scroll to bottom, click `[Pause tail]`/`[Resume tail]`, or
  `space`.
- On exit (terminal frame received): auto-follow is force-disabled and
  the exit banner (§5.4) renders as the last row.

### 5.4 Exit banner

Appended as the final row on a terminal frame:

```
──────────────────────────────────────
  exited with code 1 after 34.2s
──────────────────────────────────────
```

(or `killed after 12.8s`). Scrollback above stays intact and scrollable.

## 6. Toolbar + shortcuts

### 6.1 Toolbar (`setToolbar`, re-registered on status change)

| button | behavior | visible when |
|---|---|---|
| `[Pause tail]` | toggles auto-follow; label flips to `[Resume tail]` when paused | always |
| `[Copy output]` | copies full scrollback (plain text, ANSI stripped); brief "Copied" toast | always |
| `[Kill]` | inline confirm popover ("Kill this process?"); on confirm calls the kill endpoint (§8.2) | only while `status === "running"`, per requirement — hidden entirely after exit, not just disabled |

### 6.2 Shortcuts (`setShortcuts`, pane-focus-scoped)

| key | action |
|---|---|
| `space` | toggle follow-tail |
| `k` | trigger kill confirm (no-op if not running) |
| `c` | copy output |

## 7. Data + queries — SSE subscription

### 7.1 Hook

```ts
interface ConsoleLine { seq: number; stream: "stdout" | "stderr"; text: string; ts: number }
interface ConsoleStreamState {
  status: "connecting" | "running" | "exited" | "killed" | "error";
  exitCode: number | null;
  lines: ConsoleLine[];
  startedAt: number | null;
  endedAt: number | null;
  errorMessage: string | null;
}
function useConsoleStream(streamId: string): ConsoleStreamState;
```

Opens an `EventSource` against `GET /api/streams/{streamId}/subscribe`
(§8.2). Appends `line` frames; `exit`/`killed` frames flip `status` +
`exitCode` + `endedAt`. An `onerror` after a prior open transitions to
`status: "error"` with reconnect-with-backoff and replay-from-`after_seq`
so no lines are lost (§9.4).

### 7.2 Replay on (re)connect

Every connection replays buffered lines (from `after_seq` if the client
already has some) before switching to live tail — mirrors
`src/api/sessions.py`'s replay-then-tail shape exactly.

### 7.3 Client-side buffer cap

`lines` capped at 5,000 entries (oldest dropped, one-time "earlier
output truncated" marker inserted) to bound render memory. The server
buffer (§8.1) is the real retention source of truth.

### 7.4 First-paint metadata

No React Query for the line stream itself (manual `useReducer`-backed
stream, same rationale as `useChatTranscript`). Optional best-effort
`GET /api/streams/{streamId}` fetch on mount so the header can render
before the `EventSource` connects.

## 8. Backend requirements — the streams API

**NEW.** No streamable-command primitive exists today (confirmed: no
`aq exec`/`aq bash`; closest analogues are `_run_subprocess`/
`_run_subprocess_shell` in `src/commands/helpers.py`, none exposed as a
general streaming primitive). This spec adds one, modeled on
`src/api/sessions.py`'s SSE shape.

### 8.1 Data model — in-memory, not a DB table

A stream is short-lived and its output can be large — not a `tables.py`
row. New module `src/api/streams.py`:

- `StreamRegistry` — `dict[str, StreamHandle]` keyed by uuid4 `stream_id`,
  hung off the orchestrator instance like `orch.session_providers`.
- `StreamHandle`: `stream_id`, `title`, `session_id` (owner),
  `project_id`, `command` (argv list — never a shell string, §8.6),
  `cwd`, `status: running|exited|killed`, `exit_code`, `started_at`,
  `ended_at`, a bounded `deque[ConsoleFrame]` ring buffer (default cap
  5,000 frames / 2MB, configurable via `streams.buffer_max_lines` /
  `streams.buffer_max_bytes`), the live `asyncio.subprocess.Process`
  handle, and a per-subscriber `asyncio.Queue` fan-out list (same shape
  `WebSocketManager` in `src/api/websocket.py` already uses).
- **Reaping**: a finished handle stays for `streams.retention_seconds`
  (default 300s) so a late `subscribe`/`GET` still finds it, then is
  evicted by a periodic sweep.

### 8.2 Endpoints

**`POST /api/streams`** — start a command.

```json
{"command": ["pytest", "tests/", "-x"], "cwd": "/repo",
 "title": "Running pytest…", "session_id": "supervisor-demo", "project_id": "demo"}
```

- `command` is an **argv list**, never a shell string (§8.6).
- `cwd` must resolve inside a workspace the caller's scope can reach
  (reuses the `project-repo` workspace-membership check,
  `src/orchestrator/workspace.py`).
- Spawns via `asyncio.create_subprocess_exec(*command, cwd=cwd,
  stdout=PIPE, stderr=PIPE, stdin=DEVNULL)` — the same async-subprocess
  pattern `src/git/manager.py`'s `a*` methods already use, never
  `subprocess.run()` / `create_subprocess_shell`.
- Two reader tasks pump stdout/stderr into the ring buffer and fan out
  to live subscribers as lines arrive; a third `await process.wait()`s
  and writes the terminal frame.
- Returns `{"stream_id": "...", "status": "running"}` immediately.

**`GET /api/streams/{stream_id}`** — metadata snapshot: `{stream_id,
title, status, exit_code, started_at, ended_at, session_id,
project_id}`. 404 once reaped.

**`GET /api/streams/{stream_id}/subscribe`** — SSE (chosen over WS: the
task is one-directional server→client, exactly like session streaming,
so the frontend reuses the same `EventSource` shape). Mirrors
`sessions.py`'s `stream()` handler:

1. **Replay** buffered frames from `after_seq` (or oldest retained;
   `truncated: true` on the first frame if the buffer already dropped
   earlier ones).
2. **Live tail** — event-driven via the per-connection queue, not
   polled (the one place this diverges from `sessions.py`, since the
   source here is a live pipe rather than a file needing re-stat).
3. **Terminal frame** — `{"type": "exit", "rc": N}` or
   `{"type": "killed"}`, then the SSE stream closes (unlike the
   indefinite session-stream tail).
4. **Heartbeat** — `: heartbeat\n\n` every 15s, same constant as
   `_DEFAULT_HEARTBEAT_SECONDS`.
5. **Disconnect** — checked via `request.is_disconnected()` each loop;
   a dropped subscriber does **not** kill the process — it keeps
   running/buffering with zero subscribers.

Frame shapes:
```json
{"type": "line", "seq": 42, "stream": "stdout", "text": "PASSED [12%]", "ts": 1234567890.1}
{"type": "exit", "seq": 43, "rc": 1, "ts": 1234567891.4}
{"type": "killed", "seq": 43, "ts": 1234567891.4}
```

**`POST /api/streams/{stream_id}/kill`** — SIGTERM, wait up to
`streams.kill_grace_seconds` (default 5s), then SIGKILL. Sets
`status="killed"`, fans out the terminal frame, closes subscriber
connections. Idempotent 200 no-op on an already-exited stream (avoids
an error toast on the toolbar-button race).

### 8.3 Lifecycle summary

```
POST /api/streams  → stream_id, spawn subprocess, status=running
  ↓ reader tasks pump frames into ring buffer + live fan-out
GET .../subscribe   → replay buffer, then live tail
  ↓ process exits, or POST .../kill fires
terminal frame fanned out → status=exited|killed, SSE closes
  ↓ retention_seconds elapses, no new subscribe/GET
StreamHandle evicted
```

### 8.4 Security model

- **Start** (`POST /api/streams`): caller's `RequestScope` must be
  `elevated` (supervisor) or `local` (trusted loopback). A plain
  session-scoped (`AGENT_COMMAND_SET`) token cannot start a stream —
  this spec does not add it to that set. If `elevated` with a set
  `project_id`, the request's `project_id` must match (same posture as
  `check_command_scope` for every other elevated command); global-admin
  scope (`project_id is None`) may start a stream for any project.
- **Read/kill** (`GET .../{id}`, `.../subscribe`, `POST .../kill`):
  caller must own the stream — `scope.session_id == handle.session_id`
  OR `scope.elevated and scope.project_id in (None, handle.project_id)`.
  Else 403 `out of scope: stream ownership` — the server-side backing
  for the client check in §4.1.
- **Audit log** every `POST /api/streams` and `POST .../kill` with the
  resolved command argv and scope — same posture as the shell spec's
  global-admin audit requirement; arbitrary command execution needs a
  trail.
- **No shell string, ever** (§8.6) — `command` must be a non-empty list
  of strings; a raw string is 400, never silently wrapped in `sh -c`.
- **`cwd` confinement** — rejected 403 if outside any workspace the
  caller's scope can reach.
- **No stdin** (`stdin=DEVNULL`) — a command that blocks on input just
  looks "stuck"; accepted per the non-interactive-terminal non-goal.
- **Concurrency cap** — `streams.max_concurrent_per_session` (default
  3); `POST /api/streams` above the cap → 429 `too many concurrent
  streams`.

### 8.5 Optional: `GET /api/streams/{stream_id}/tail?after_seq=N`

Non-SSE polling fallback returning JSON frames since `after_seq`, for
clients that can't hold an `EventSource` (e.g. `aq stream tail <id>`,
§10). Not required by the pane itself; cheap to add since it reads the
same ring buffer.

### 8.6 Why argv list, not shell string

Worth restating: every existing shell-out in the codebase
(`src/git/manager.py`'s `a*` methods) uses `create_subprocess_exec`
with an argv list, never `create_subprocess_shell`. This spec holds
that line — the single most security-relevant decision here. A caller
needing shell features wraps it explicitly as `["bash", "-c", "..."]`,
which is still an argv list from the daemon's point of view, just an
auditable, intentional choice by the caller rather than an implicit
default the endpoint applies.

### 8.7 Tests

`tests/test_streams_api.py`:
- `POST /api/streams` with `["echo", "hi"]` → `stream_id`; `GET .../{id}`
  shows `exited`/`rc=0` shortly after.
- Non-list `command` → 400. Non-elevated/non-local scope → 403.
  `cwd` outside accessible workspaces → 403.
- `.../subscribe` on `["sleep", "2"]` yields the `exit` frame at ~2s
  then closes; reconnect with `after_seq` doesn't re-yield seen frames.
- `.../kill` on `["sleep", "30"]` — SIGTERM then exit within
  `kill_grace_seconds`, `status="killed"`; on an already-exited stream
  → 200 no-op.
- Ownership check: session A's token reading session B's stream → 403.
- Concurrency cap: 4th concurrent stream for one session while 3 run
  → 429.
- Retention sweep evicts a finished stream past `retention_seconds`;
  subsequent `GET` → 404.
- Audit log row written for every start and kill.

## 9. Loading + error + exit states

- **Connecting** (`status: "connecting"`) — neutral "connecting…" chip;
  thin skeleton body, not a spinner overlay (avoids flash for the
  common sub-100ms case).
- **Stream not found** (404) — `"This console session has ended and its
  output is no longer available."`, toolbar fully hidden. Expected once
  `retention_seconds` elapses on a stale chat chip.
- **Scope mismatch** (§4.1, or server 403) — `"You don't have access to
  this console output."`, toolbar hidden. Distinct copy from not-found.
- **Connection error mid-stream** (`status: "error"`) — auto-reconnect
  with backoff up to `streams.client_reconnect_attempts` (default 5);
  while retrying, header stays `running` with a "reconnecting…"
  sub-label. Exhausted → amber `connection lost` state (distinct from
  `exited`/`killed`, since the process may still be running
  server-side) with `[Retry]` replacing the normal toolbar.
- **Exit states** (§5.2/§5.4) — `exited(rc=0)`, `exited(rc≠0)`,
  `killed`: header frozen, kill button hidden, follow-tail disabled,
  exit banner appended.

## 10. Agent-push examples

Always follows a `POST /api/streams` call the supervisor itself just
made (only elevated/local scope can start a stream, §8.4):

```bash
# 1. Start the streamable command
aq stream start --title "Running pytest…" -- pytest tests/ -x
# → { "stream_id": "a1b2c3d4", "status": "running" }

# 2. Push the pane-open frame
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Started pytest — watch it on the right →" \
    --pane-open '{"view": "console-stream", "args": {"streamId": "a1b2c3d4", "title": "Running pytest…", "sessionId": "supervisor-global"}}'
```

`aq stream start` is a thin new CLI command wrapping `POST
/api/streams` (`--` separates its own flags from the passthrough argv,
per CLAUDE.md's `{"success": bool, ...}` command convention).
`aq stream tail <id>` is the optional CLI-side consumer of §8.5's
polling endpoint.

`_cmd_message_send`'s existing `--pane-open` validation (plugin
interface spec §6.5) additionally rejects the frame if `args.streamId`
isn't a stream the sending session owns — defense in depth alongside
the SSE endpoint's own ownership check.

## 11. Tests

**Manifest** (`__tests__/manifest.test.ts`): id matches directory;
`args_schema` accepts `{streamId:"abc"}`, rejects `{}` and
`{streamId:123}`; `open_shortcut` and `palette_label` are `null`.

**Component** (`__tests__/index.test.tsx`):
- Renders `connecting` immediately, then `running` (spinner + elapsed
  time) once frames arrive; stdout/stderr interleaved with stderr
  border styling.
- Auto-follow stays pinned while appending at bottom; a manual scroll
  up disables it.
- `space` toggles follow-tail; toolbar label flips accordingly.
- `[Copy output]`/`c` copies full plain-text (ANSI-stripped) scrollback.
- `[Kill]`/`k` opens confirm popover; confirm calls kill endpoint,
  cancel doesn't; button absent after a terminal frame.
- Terminal frame freezes header, appends exit banner, force-disables
  follow-tail.
- `sessionId` mismatch renders scope-mismatch state without opening
  the `EventSource`.
- Unmount closes the `EventSource` (no leaked subscription).

**Backend**: see §8.7 — `tests/test_streams_api.py` is the primary
coverage; the pane view is a thin consumer of that API.

## 12. Implementation checklist

**Frontend**
- [ ] `manifest.ts` — manifest + `consoleStreamArgsSchema` (§3).
- [ ] `hooks.ts` — `useConsoleStream` SSE hook, replay-then-tail +
      reconnect-with-backoff (§7).
- [ ] `index.tsx` — console body, header status, toolbar, shortcuts, all
      states from §9.
- [ ] ANSI-to-span util if not already present.
- [ ] `__tests__/manifest.test.ts` + `index.test.tsx` (§11).
- [ ] Register in `dashboard/src/panes/registry.ts` (auto-picked up by
      `import.meta.glob`, per plugin interface spec §4.1).
- [ ] Add `console-stream` to `src/panes/registry.py` with
      `agent_pushable: true`; run the parity test.

**Backend**
- [ ] `src/api/streams.py` — `StreamRegistry`, `StreamHandle`,
      `ConsoleFrame`, spawn/pump/wait tasks (§8.1).
- [ ] `POST /api/streams` — argv validation, `cwd` confinement, scope
      check, concurrency cap, audit log (§8.2, §8.4).
- [ ] `GET /api/streams/{stream_id}` metadata handler.
- [ ] `GET /api/streams/{stream_id}/subscribe` SSE handler mirroring
      `src/api/sessions.py`'s `stream()` structure (§8.2).
- [ ] `POST /api/streams/{stream_id}/kill` — grace-period kill,
      idempotent (§8.2).
- [ ] `GET /api/streams/{stream_id}/tail?after_seq=N` polling fallback
      (§8.5).
- [ ] Retention sweep task.
- [ ] Config: `streams.buffer_max_lines`, `streams.buffer_max_bytes`,
      `streams.retention_seconds`, `streams.kill_grace_seconds`,
      `streams.max_concurrent_per_session`.
- [ ] `aq stream start` / `aq stream tail` CLI commands (§10).
- [ ] `--pane-open` `streamId` ownership check for `console-stream`.
- [ ] `tests/test_streams_api.py` (§8.7).
- [ ] Wire `build_streams_router` into `src/api/app.py`, following the
      factory + default-router pattern `src/api/sessions.py` uses.

## 13. Open questions

- **Buffer persistence across daemon restart.** Streams are purely
  in-memory (§8.1); a restart mid-command orphans the registry entry
  (the subprocess itself may or may not survive depending on
  process-group setup — not resolved here). Acceptable given streams
  are short-lived dev-facing runs, not critical jobs.
- **stdin support.** Out of scope (§2, §8.4) but likely the first
  extension request once someone hits an interactive prompt. Adding it
  changes the security model materially and deserves its own spec.
- **Multiple simultaneous viewers of one stream.** The fan-out design
  already supports N subscribers per `stream_id` for free, but nothing
  in this spec exercises that from the dashboard (one pane instance at
  a time). Relevant if a future "shared session view" wants two tabs
  watching the same stream.
- **CLI wrapper naming.** `aq stream start` / `aq stream tail` are
  proposed names (§10); confirm against existing `aq <group> <cmd>`
  conventions before implementation.
