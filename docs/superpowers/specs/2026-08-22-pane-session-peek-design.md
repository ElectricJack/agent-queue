# Pane View: `session-peek` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:**
- `2026-08-22-dashboard-shell-v2-design.md` (shell primitives, right
  surface contract, agent-push message frame).
- `2026-08-22-pane-plugin-interface-design.md` (the pane view contract
  every view — including this one — implements).

## 1. Goal

Ship `session-peek`: a live tmux peek-frame stream for one session,
viewable from the shell's right surface without navigating away from
whatever the user is doing (a task's graph node, Command Center's
Agents tab, a supervisor chat chip). This is the v2 ship-priority view.
Content substantially exists today as
`dashboard/src/components/PaneView.tsx` (terminal-styled scrollback
renderer) and the pane-view-mode branch of
`dashboard/src/pages/SessionDetail.tsx` (Phase 5). This spec reframes
that code as a shell-pane-scoped view: same rendering, same data hook,
new host (`<ShellPane>` instead of a full page), new host frame
(manifest, toolbar, shortcuts) per the pane plugin contract.

The point: "peek any session's terminal without leaving whatever you're
doing." Click an agent row in Command Center → Agents, or click through
a supervisor chat chip, and the session's live tmux output streams into
the pane while the rest of the shell stays put.

## 2. Non-goals

- Not replacing `SessionDetail` (`/sessions/:id`) — that page keeps its
  Attach / Nudge sections and Transcript/Pane toggle. This view is a
  lighter, read-mostly companion (see `[Open full session detail]`).
- Not adding a transcript-mode toggle inside the pane. Only the
  peek-frame branch renders here; the structured transcript stream
  stays on the full page.
- Not building a new SSE endpoint — reuses
  `GET /api/sessions/{session_id}/stream` verbatim (`src/api/sessions.py`).
- Not adding nudge/message-sending UI — nudge stays on `SessionDetail`.
- Not multi-session tiling. One `session-peek` instance shows one
  session; opening a second session replaces the pane's args (same
  slot, per the interface spec's "nested panes" note, §11).

## 3. Manifest

```ts
// dashboard/src/panes/session-peek/manifest.ts

import { z } from "zod";
import { CommandLineIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const sessionPeekArgsSchema = z.object({
  sessionId: z.string().min(1),
  tail: z.boolean().optional(),
});

export type SessionPeekArgs = z.infer<typeof sessionPeekArgsSchema>;

export const manifest: PaneManifest<SessionPeekArgs> = {
  id: "session-peek",
  name: "Session Peek",
  description: "Live tmux peek stream for one session.",
  icon: CommandLineIcon,
  args_schema: sessionPeekArgsSchema,
  // open_shortcut omitted per interface spec (no literal null; undefined = no shortcut)
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Peek session",
  palette_section: "Sessions",
};
```

- `open_shortcut: null` — reached by click-through (Agents tab, a
  task's active-session chip, an agent-push chip) or the palette
  (`> Peek session`), never a dedicated keybinding. If no session is in
  focus when the palette action is invoked, it does not register for
  that invocation (interface spec §6.3).
- `tail` is `.optional()` rather than `.default(true)` so an explicit
  `tail: false` round-trips through `setArgs` without the schema
  re-adding a default; the component treats `args.tail ?? true` as
  effective default (§5.3).
- `route_scope: "cross-route"` — matches "peek without leaving what
  you're doing"; the pane survives navigating around Command Center.

## 4. Args + validation

| field       | type      | required | notes |
|-------------|-----------|----------|-------|
| `sessionId` | `string`  | yes      | non-empty; the session bearer id. |
| `tail`      | `boolean` | no       | follow-tail mode; effective default `true`. |

Two validation points per the interface contract:
- `open("session-peek", args)` — validated against `args_schema` before
  mount (interface spec §6.1); empty/missing `sessionId` → `open` no-ops
  with `console.error`.
- `setArgs(next)` — used when the toolbar toggles follow-tail without a
  full reopen. Re-validated against the same schema; a view-internal
  bug producing invalid args throws (interface spec §5.3 — programming
  error, not user error).

No `resolveDefaultArgs` helper — every open call supplies a concrete
`sessionId` from its caller's context (row click, chip click,
agent-push payload).

## 5. Component

### 5.1 File layout

```
dashboard/src/panes/session-peek/
├── manifest.ts
├── index.tsx
└── __tests__/
    └── index.test.tsx
```

No `hooks.ts` — reuses `useTranscriptStream`
(`dashboard/src/ws/useTranscriptStream.ts`) directly.

### 5.2 Composition

```tsx
// dashboard/src/panes/session-peek/index.tsx

import { useEffect, useRef, useState } from "react";
import {
  PlayIcon, StopIcon, ClipboardIcon,
  ArrowTopRightOnSquareIcon, XCircleIcon,
} from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useTranscriptStream } from "../../ws/useTranscriptStream";
import { useSession, useSessionKill } from "../../api/hooks";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args, close, setArgs, setToolbar, setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const tail = args.tail ?? true;
  const navigate = useNavigate();

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();
  const { entries, status, error } = useTranscriptStream(sessionId, { enabled: true });

  const peekFrames = entries.filter((e) => e.source === "peek");
  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";

  const boxRef = useRef<HTMLDivElement>(null);
  const [confirmingKill, setConfirmingKill] = useState(false);

  // Follow-tail: snap to bottom on new frames when tail is on.
  useEffect(() => {
    if (!tail) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [peekFrames.length, tail]);

  // Sticky-when-scrolled-up: manual scroll away from bottom turns tail
  // off. Re-enabling is explicit (toolbar / space / End).
  const onScroll = () => {
    const el = boxRef.current;
    if (!el || !tail) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!nearBottom) setArgs({ ...args, tail: false });
  };

  const copyScrollback = () =>
    navigator.clipboard.writeText(peekFrames.map((f) => f.text).join("\n"));
  const openFullSession = () => navigate(`/sessions/${sessionId}`);
  const doKill = () => {
    if (!confirmingKill) return setConfirmingKill(true);
    kill.mutate({ session_id: sessionId });
    setConfirmingKill(false);
  };

  useEffect(() => {
    setToolbar([
      { id: "toggle-tail", label: tail ? "Pause tail" : "Follow tail",
        icon: tail ? StopIcon : PlayIcon, onClick: () => setArgs({ ...args, tail: !tail }) },
      { id: "copy-scrollback", label: "Copy scrollback", icon: ClipboardIcon,
        onClick: copyScrollback, disabled: peekFrames.length === 0 },
      { id: "open-full", label: "Open full session detail",
        icon: ArrowTopRightOnSquareIcon, onClick: openFullSession },
      { id: "kill-session", label: confirmingKill ? "Confirm kill?" : "Kill session",
        icon: XCircleIcon, onClick: doKill, disabled: exited },
    ]);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, peekFrames.length, confirmingKill, exited]);

  useEffect(() => {
    setShortcuts([
      { key: "space", label: "Toggle follow tail", onFire: () => setArgs({ ...args, tail: !tail }) },
      { key: "k", label: "Kill session", onFire: doKill },
      { key: "o", label: "Open full session detail", onFire: openFullSession },
      { key: "c", label: "Copy scrollback", onFire: copyScrollback },
      { key: "Home", label: "Scroll to top", onFire: () => {
          setArgs({ ...args, tail: false });
          if (boxRef.current) boxRef.current.scrollTop = 0;
        } },
      { key: "End", label: "Scroll to bottom", onFire: () => {
          setArgs({ ...args, tail: true });
          if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
        } },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, confirmingKill, exited]);

  return (
    <div className="flex h-full flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — showing last scrollback.
        </div>
      )}
      {error && <p className="border-b border-gray-800 px-3 py-1 text-xs text-amber-400">{error}</p>}
      {status === "connecting" && peekFrames.length === 0 && (
        <p className="px-3 py-2 text-xs text-gray-500">Connecting…</p>
      )}
      <div ref={boxRef} onScroll={onScroll}
        className="flex-1 overflow-y-auto bg-black p-3 font-mono text-xs leading-tight text-green-200">
        {peekFrames.length === 0 && status !== "connecting" ? (
          <p className="text-gray-500">
            Waiting for pane snapshot… (peek frames arrive whenever the
            harness has no readable transcript, or on fallback)
          </p>
        ) : (
          peekFrames.map((f) => (
            <pre key={f._idx} className="whitespace-pre-wrap border-b border-gray-900/40 py-1">
              {f.text}
            </pre>
          ))
        )}
      </div>
    </div>
  );
}
```

### 5.3 Follow-tail model

Same semantics as `PaneView.tsx` today, moved from a component-local
ref into pane `args.tail` so the toolbar button, `space`, and route
persistence can all observe/flip it:

- `args.tail` (default `true`) is the single source of truth — no
  local `followRef`.
- New frame + `tail === true` → scroll snaps to bottom.
- Scroll away from bottom (>24px slack, same threshold as
  `PaneView.tsx`) while `tail === true` → `setArgs({ tail: false })`.
  This is the sticky-when-scrolled-up behavior from the brief.
- Re-enable is explicit: toolbar button, `space`, or `End` (which also
  scrolls immediately).
- `Home` turns tail off and scrolls to the buffer's top.

### 5.4 Monospace rendering

Identical DOM shape to `PaneView.tsx`: a scroll container (`bg-black`,
`font-mono text-xs leading-tight text-green-200`) holding one
`<pre className="whitespace-pre-wrap">` per peek frame, keyed by
`TranscriptFrame._idx` (peek frames have no `uuid`). No ANSI stripping
needed — peek frames come from `tmux capture-pane -p`
(`src/sessions/tmux.py:445`), plain rendered text. Filtering is exactly
`entries.filter((e) => e.source === "peek")` (`PaneView.tsx:23`);
transcript-source frames are ignored here — that's `SessionDetail`'s job.

## 6. Toolbar + shortcuts

Toolbar (left to right, after the shell's fixed `[icon] Session Peek ⋯ ×`
header per interface spec §5.1):

| id | label (dynamic) | icon | behavior |
|----|------------------|------|----------|
| `toggle-tail` | "Pause tail" / "Follow tail" | `StopIcon`/`PlayIcon` | flips `args.tail`. |
| `copy-scrollback` | "Copy scrollback" | `ClipboardIcon` | joins buffered peek text with `\n`, writes to clipboard. Disabled with no frames. |
| `open-full` | "Open full session detail" | `ArrowTopRightOnSquareIcon` | `navigate("/sessions/:id")`. Pane stays open (cross-route). |
| `kill-session` | "Kill session" / "Confirm kill?" | `XCircleIcon` | two-tap confirm (first click arms, second commits). Disabled once `exited`. |

Shortcuts (fire only while pane has focus; shown in `?` cheat sheet
under Session Peek):

| key | action |
|-----|--------|
| `space` | toggle follow-tail. |
| `k` | kill session (shares the toolbar's arm/confirm state). |
| `o` | open full session detail. |
| `c` | copy scrollback. |
| `Home` | scroll to top, turn tail off. |
| `End` | scroll to bottom, turn tail on. |

`confirmingKill` is plain component state, not pane args — transient UI
state that shouldn't round-trip through `setArgs` or be visible to an
agent-push producer.

## 7. Data + queries

- **Session metadata:** `useSession(sessionId)`
  (`dashboard/src/api/hooks.ts:953`) — read only for `session.lifecycle`
  (exited banner, §8) and to gate kill. Existing 15s poll, unchanged.
- **Peek stream:** `useTranscriptStream(sessionId, { enabled: true })`
  (`dashboard/src/ws/useTranscriptStream.ts:41`), unchanged. Opens
  `EventSource` against `GET /api/sessions/{session_id}/stream`
  (`src/api/sessions.py`), parses `data:` JSON frames
  (`{source, uuid?, parent_uuid?, type?, text, model?, usage?, ts}`),
  buffers up to `bufferSize` (default 2000), ignores `: heartbeat`.
- **Unsubscribe:** handled entirely by the hook's existing cleanup
  (`useTranscriptStream.ts:94-98`) — `es.close()` fires on unmount or
  when `sessionId`/`enabled` change. Because the component only mounts
  while the pane is open for this view, closing the pane or switching
  views tears the `EventSource` down naturally; no extra teardown code
  needed in the view.
- **No new endpoints, no new hooks file.** Pure composition of two
  existing hooks plus the existing kill mutation.

## 8. Loading + error + session-exited states

- **Loading:** `status === "connecting"` with no peek frames yet →
  "Connecting…" line, distinct from the "waiting for pane snapshot"
  empty state (shown once `open` but no frame has arrived — same copy
  as today's `PaneView.tsx`).
- **Stream error:** `useTranscriptStream`'s `error` string renders as
  an amber banner (same treatment as `SessionDetail.tsx:168`).
  `EventSource` retries natively; no view-level reconnect logic.
- **Session exited:** driven off `useSession().lifecycle` (`"exited"`
  or `"terminated"`) — not off SSE `status`, since a stream `error`/
  `closed` can also mean a transient blip, not an actual exit. When
  exited: an amber "Session exited — showing last scrollback." banner
  renders; buffered peek frames stay visible (nothing cleared);
  `kill-session` disables; `toggle-tail` and `open-full` remain enabled.

## 9. Agent-push examples

Per interface spec §6.5, the supervisor emits `message.sent` with
`body_kind: "pane_open"`.

**"Watch this session run" (headline scenario) — supervisor just
dispatched a task:**

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "watch this session run →" \
    --pane-open '{"view": "session-peek", "args": {"sessionId": "sess-8f21", "tail": true}}'
```

Client renders a `pane_open` chip in the transcript reading "watch this
session run →"; since `manifest.agent_pushable !== false`, the client
also auto-dispatches `pane.open("session-peek", {...})`, sliding the
pane in with follow-tail already on.

**Supervisor flags a stall, doesn't want to force the live tail:**

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "this session looks stalled — here's its output →" \
    --pane-open '{"view": "session-peek", "args": {"sessionId": "sess-8f21", "tail": false}}'
```

Same auto-open path; `tail: false` avoids snapping to bottom on open.

Both examples assume the `--pane-open` CLI flag and the server-side
`agent_pushable` mirror entry (interface spec §6.5, §7) are wired for
`session-peek` — required by this view's implementation checklist
(§11), not deferred.

## 10. Tests

`dashboard/src/panes/session-peek/__tests__/index.test.tsx`:

**Manifest (interface spec §9.1):**
- `manifest.id === "session-peek"` matches directory name.
- `sessionPeekArgsSchema` accepts `{sessionId: "x"}` and
  `{sessionId: "x", tail: false}`; rejects `{}` and `{sessionId: ""}`.
- `manifest.open_shortcut` is `null`.

**Component:**
- Renders without crashing given valid args; peek frames from a mocked
  `useTranscriptStream` render as `<pre>` blocks in order.
- Non-peek frames (mocked `source: "transcript"`) are filtered out.
- Follow-tail: appending a frame sets `scrollTop = scrollHeight` when
  `args.tail !== false`.
- Scrolling away from bottom while `tail` is true calls `setArgs` with
  `tail: false`.
- `space` flips `tail` via `setArgs`.
- `k` / kill button: first call arms (no `mutate` call, label updates);
  second call invokes `useSessionKill().mutate({session_id})`.
- `o` / open-full button calls `navigate("/sessions/:id")`.
- `c` / copy button calls `navigator.clipboard.writeText` with joined
  peek text (mock `navigator.clipboard`).
- `Home` scrolls to top + sets `tail: false`; `End` scrolls to bottom +
  sets `tail: true`.
- Exited state: mock `useSession` → `lifecycle: "exited"`; banner
  renders, frames still render, kill disables.
- Error state: mock `useTranscriptStream` error → banner renders.
- Loading state: `status: "connecting"`, empty entries → "Connecting…".
- Unmount calls `setToolbar([])` and `setShortcuts([])`.

**Registry parity (shared, interface spec §9.2):** `session-peek`
appears in both `dashboard/src/panes/registry.ts` and
`src/panes/registry.py`; covered by the existing
`dashboard/src/panes/__tests__/registry.test.ts` and
`tests/test_pane_registry_parity.py` — no new test file needed here.

## 11. Implementation checklist

- [ ] Create `dashboard/src/panes/session-peek/` directory.
- [ ] `manifest.ts` per §3.
- [ ] `index.tsx` per §5, wiring `useTranscriptStream`, `useSession`,
      `useSessionKill`, `useNavigate`. No new hooks.
- [ ] Confirm `session-peek` appears in `PANE_REGISTRY`
      (`dashboard/src/panes/registry.ts`, auto-picked-up via
      `import.meta.glob`).
- [ ] Add `session-peek` to `src/panes/registry.py`
      (`SERVER_PANE_REGISTRY["session-peek"] = {"agent_pushable": True}`).
- [ ] Wire `--pane-open` support into `_cmd_message_send` /
      `aq message send` if not already landed by an earlier pane view
      (interface spec §6.5 — shared plumbing, flagged here since this
      view is first to need a worked example).
- [ ] Write `__tests__/index.test.tsx` per §10.
- [ ] Run the frontend/backend registry parity test.
- [ ] Click-through call sites (Command Center Agents tab, per shell
      spec §7.6) are that tab's responsibility, landing with Phase D —
      this view's PR only needs `open("session-peek", {sessionId})` to
      work when called.

## 12. Open questions

- **`setArgs`-driven `sessionId` swap.** This view assumes a session
  switch always comes through a fresh `open()` (new mount), never a
  `setArgs` that changes `sessionId` in place. `useTranscriptStream`'s
  effect dependency array already includes `sessionId`, so an in-place
  swap would work if a future caller wanted "no visible pane
  close/reopen" session cycling — unexercised, not unsupported.
- **Copy-scrollback size.** `copyScrollback` joins the entire buffered
  array (up to 2000 frames), no truncation. Could be multi-MB for large
  peek captures. Not addressed; follow-up only if it proves a real
  problem.
- **Confirm-kill UX consistency.** The two-tap arm/confirm pattern here
  (label change, no modal) is one interpretation of the shell spec's
  "(with confirm)" note (§8.7). Other views/rows might use a modal
  instead. Not reconciled here — flagged for a shell-wide confirm-action
  pass once more panes exist to compare against.
