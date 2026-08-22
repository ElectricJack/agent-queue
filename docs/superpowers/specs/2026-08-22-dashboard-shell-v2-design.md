# Dashboard Shell v2 — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Companion specs:**
- Pane plugin interface: `2026-08-22-pane-plugin-interface-design.md`
- Per-pane view specs: `2026-08-22-pane-<view>-design.md` (nine files)

## 1. Goal

Rebuild the Agent Q dashboard shell around a global-supervisor front door,
a keyboard-first navigation model, and two shared right-side surfaces (a
demand-driven multi-purpose pane and an ambient activity/gates drawer).
Consolidate the current `Work` and `Command Center` routes into a single
tabbed hub. Retain per-project supervisors as specialists reached from
inside each project page.

The dashboard we're targeting takes visible inspiration from the Claude
Code and Codex native apps: chat as the always-present entry surface, a
dominant center pane holding whatever the user is doing, and a right-side
surface for anything ephemeral or contextual (diffs, files, session
peek, notification streams).

## 2. Non-goals

- Not touching the per-project supervisor session model beyond removing
  the landing-page routing that pointed at it. `supervisor-<pid>`
  sessions keep their scope, memory, lifecycle, and everything else they
  already do.
- Not building runtime module federation for pane plugins. The pane
  plugin architecture ships as a bundled build-time registry with a
  future-extensible manifest shape (see companion spec).
- Not redesigning `Settings`, project overview, or task-detail pages
  beyond swapping their inline sidebars for the shell pane and adopting
  the keyboard vocab.
- Not adding mobile-first features. `<768px` gets a degraded but
  functional shell; the design target is desktop keyboard-first.

## 3. Shell layout

Every route renders inside a shell with four regions.

```
┌──────────┬──────────────────────────────────────────┬──────────────┐
│          │                                          │              │
│  Left    │              Center                      │  Right       │
│  rail    │              (route content)             │  surface     │
│          │                                          │  (pane OR    │
│          │                                          │   drawer)    │
│          │                                          │              │
├──────────┴──────────────────────────────────────────┴──────────────┤
│  Top bar (palette trigger + activity/gates bell + status)          │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 Left rail

Persistent navigation. Sections:
- Home (`/`)
- Command Center (`/command-center`) — hub for project work
- Settings (`/settings`)

Below the sections: collapsible project list. Each entry links to the
project's overview (`/projects/:id`). This is the entry point to each
project's per-project supervisor (via that page's Chat tab).

Rail width: ~240px desktop, hidden below 768px (replaced with a bottom
nav from Phase 3).

### 3.2 Center

Whatever the current route renders. On `/`, it's the global supervisor
chat. On `/command-center`, it's the tabbed hub. On `/settings/*`, it's
the settings sub-nav + content. On detail routes (`/tasks/:id`,
`/sessions/:id`, `/playbooks/:id`), it's the entity detail.

### 3.3 Right surface — two mutually exclusive primitives

- **`<ShellPane>`** — multi-purpose contextual surface. Content is one of
  the registered pane views (see §5).
- **`<ActivityDrawer>`** — ambient notification/gate surface (see §6).

They share the interaction contract (open, close, resize width, keyboard
shortcuts, focus trap while open) but hold different content, and they
render in the same visual slot on the right edge. Opening one closes the
other. This is enforced by a single `useRightSurface` hook — the shell
tracks `surface: { kind: "pane" | "drawer" | null, ... }` state; setting
`kind` from one to the other transparently closes the previous.

Default width when open: 480px. User can drag the divider to resize
(200px min, 800px max). Width persists per-surface in `localStorage`.

### 3.4 Top bar

Left: workspace / build indicator (small).
Center: current-page breadcrumb (subtle).
Right: palette trigger button (`⌘K`), activity bell with badge, account
status.

Palette trigger is redundant with `Cmd-K` — kept for discoverability.
Bell trigger is redundant with `]` — kept for discoverability.

## 4. Global supervisor session

### 4.1 Identity

- Session id (bearer scope): `supervisor-global`.
- Runtime session name (harness): `n-supervisor--global`. Mirrors the
  per-project convention (`n-supervisor--<pid>`).
- Address in the messaging layer: `supervisor-global`.
- Displayed name in the chat UI header: "Agent Q".

### 4.2 Scope

Extend `check_command_scope` in `src/api/scope.py` with a new
sub-condition of the existing elevated flag: when `scope.elevated` is
true AND `scope.project_id is None`, treat the token as **global
admin** — allow any command name AND skip the project_id match check
entirely.

Concretely, the elevated path today:

```python
if scope.elevated:
    expected_pid = scope.project_id
    if expected_pid is not None:
        # enforce project_id match on args
    return None
```

becomes:

```python
if scope.elevated:
    if scope.project_id is None:
        # global admin — no project filter
        return None
    # existing per-project elevated path (unchanged)
    ...
```

No new `kind` value in `RequestScope`; no schema migration. The existing
`elevated: bool` column already carries the bit; `project_id` being NULL
is the discriminator.

### 4.3 Auth — loopback restriction

The global admin token is more powerful than any current session token.
Restrict its validation to loopback:

- In `src/api/middleware.py::TokenAuthMiddleware`, after resolving the
  bearer token to a `RequestScope`, inspect the request's client address.
  If `scope.elevated` AND `scope.project_id is None` AND the client
  address is not in `{127.0.0.1, ::1}`, reject with 403 `token restricted
  to loopback`.
- The daemon binds to `127.0.0.1` today, so in practice the check is
  belt-and-braces — but the check is what makes it safe to bind to a
  broader interface in future without exposing global-admin auth.
- Loopback check is per-request; the DB row is not restricted. This
  means a global admin token exfiltrated to a remote host wouldn't
  validate at the daemon.

Also: audit-log every command executed with a global-admin scope with
`scope=global_admin` in the audit-log payload so all admin actions are
traceable.

### 4.4 Memory

The global supervisor gets its own memory scope: `supervisor:global`.

- Isolated from every per-project supervisor's `supervisor:<pid>` scope.
- No auto-cross-pollination. What the global supervisor learns doesn't
  leak into per-project scope; what per-project supervisors learn doesn't
  leak up.
- Cross-scope reads are technically possible via the memory system's
  multi-scope weighted query — we do NOT wire the global supervisor to
  query per-project scopes automatically. If a follow-up shows the
  isolation causes context loss, we can add a `search-all-supervisor-
  memory` affordance later.
- Memory storage: same `memory_v2` plugin, same schema; only the
  `scope_id` value differs.

### 4.5 Session lifecycle

On-demand, mirroring the per-project supervisor model:

- Daemon startup does NOT spawn `supervisor-global`.
- First user message to `supervisor-global` (via the chat wire on `/`)
  wakes the session — `SessionLens.ensure_started` handles this using
  the existing supervisor cold-start path with `project_id=None` /
  runtime name `n-supervisor--global`.
- Idle timeout: 45 minutes of no user activity (longer than per-project
  supervisors' default because the global supervisor is high-touch and
  we want fewer visible cold starts). Configurable via
  `supervisor.global.idle_timeout_seconds` in config.
- Cold start UI: the existing `ThinkingBubble` covers wake-up cost —
  first `notify.text` or `session.started` event surfaces "Session woke
  up" as an activity chip.

### 4.6 Coordination model

Direct action. When the user asks the global supervisor to do something
that scopes to project X, the supervisor invokes the aq CLI directly
under its own bearer token (which validates as global admin). It does
NOT delegate to project X's per-project supervisor.

Practical consequence: everything the global supervisor does is logged
in its own memory + audit trail, not in project X's supervisor memory.
This matches the "used less" role we picked for per-project supervisors
— they exist for humans who want isolated focus in one project, not as
middlemen the global supervisor punts to.

### 4.7 What lives on `/`

The center pane at `/` is the global supervisor chat. It reuses
`ChatConversation` with two differences:

- `projectId` is not derived from a URL param; the session is fixed at
  `supervisor-global`.
- The chat thread id is `dashboard:global` (mirrors the per-project
  `dashboard:<pid>`).
- The header reads "Agent Q" instead of `supervisor-<pid>`.

The existing `ThinkingBubble` behavior, `useChatTranscript` hook,
`InlineEventCard`, activity chips, and command.invoked chip integration
all carry over unchanged. No new chat components.

### 4.8 What retires

- The current `ChatLanding` component (project-picker cards at `/`) is
  retired. Users who need a project picker use the palette (`Cmd-K` +
  `@`) or the left rail's project list.
- `/chat/:projectId` is retained (unchanged) — that's still how a human
  reaches a per-project supervisor. It's linked from the project
  overview page's Chat tab, not from `/`.

## 5. Right pane (`<ShellPane>`)

The pane primitive lives in `dashboard/src/shell/ShellPane.tsx` and is
mounted once in the app shell root. Content is dispatched from a pane
view registry.

The full contract — manifest schema, component props, agent-push message
frame, registry mechanics, view lifecycle — lives in the companion spec
`2026-08-22-pane-plugin-interface-design.md`. Below is the shell's side
of that contract.

### 5.1 State

Tracked in a single `useShellPaneStore` (Zustand or React context — TBD
by implementation, likely context since we already lean on React Query
elsewhere).

```ts
type PaneState =
  | { kind: "closed" }
  | { kind: "open", view: string /* view id */, args: unknown, width: number };
```

`open(view, args, opts?)` — sets kind=open, seeds width from
localStorage. Closes the drawer if it's open.
`close()` — sets kind=closed.
`setWidth(n)` — mutates + persists.

### 5.2 Opening channels

Two ways the pane opens:

**User channel:**
- Palette action (e.g. `> open diff for current task`)
- Keyboard shortcut (`[` toggle last-used view; per-view shortcut declared
  in the manifest)
- Click-through from a list row (e.g. Command Center Tasks tab → click
  row → task-detail view opens)

**Agent-push channel:**
- Supervisor emits a special message with `body_kind: "pane_open"` and
  payload `{ view: <id>, args: {...} }`. The messaging layer's WS
  bridge receives it; the chat surface dispatches `open(view, args)` in
  addition to rendering an inline chip in the chat transcript ("opened
  the diff on the right →" with an icon).
- User-facing behavior: the pane slides in; the chat chip is a marker
  of what was opened. Clicking the chip re-opens the pane if the user
  had closed it.

### 5.3 Closing

- `Esc` when the pane has focus.
- Close button in the pane header.
- Opening the activity drawer closes the pane.
- Navigating to a different route does NOT close the pane by default;
  pane content persists across route changes so the user can chat about
  what's in the pane while navigating. Per-view manifests can opt-out
  (some views are truly route-scoped).

### 5.4 Resize

- Drag handle on the pane's left edge.
- `Cmd-\` toggles between two width presets (compact 320px, wide 640px).
- Width persists per view id in `localStorage` under
  `aq:shellpane:width:<viewId>`.

### 5.5 Header

Every view renders inside a shared frame:

```
┌ [icon] View Name ─────────────────── × ┐
│                                        │
│    view content                        │
│                                        │
└────────────────────────────────────────┘
```

Header is provided by the shell; view content owns the body. Views can
optionally emit toolbar actions (right side of header) via a hook.

## 6. Activity drawer (`<ActivityDrawer>`)

Ambient stream of notifications (gates + events). Mounted once in the
shell root; shares the right-edge slot with `<ShellPane>`.

### 6.1 Content

Two tabs at the top of the drawer:

- **Gates** — every open HITL gate across every project. Each row:
  gate id, gate type, project, associated task title, age. Inline
  approve/reject buttons for `human` gates. Bell badge counts open
  human gates specifically (the ones needing action).
- **Events** — recent WS events. Filter chips at top:
  `All / Tasks / Playbooks / Sessions / Gates`. Rows show event type,
  ts, project, and a compact summary (reuses `InlineEventCard`'s
  formatter with a smaller rendering). Max 200 rows kept in memory;
  older events scroll off.

### 6.2 Data sources

- Gates: `useProjectGates` — a new React Query hook that lists gates
  across all projects (`aq gate list --json`). Invalidated on
  `gate.created`, `gate.resolved`, `gate.expired`.
- Events: subscribes to `useEventStream` (existing WS bridge). Keeps a
  rolling window of the last N events in local state.
- Bell badge: derived — count of open `human` gates.

### 6.3 Interactions

- `]` toggle. Bell click toggles.
- `↑↓` moves focus within the current tab list.
- On focused gate row: `a` approve, `r` reject, `Enter` opens the pane
  view most useful for THIS gate's type. Dispatch by `gate_type`:
  - `human` (routing / approval on a task) → `task-detail` view for
    the associated task
  - `routing` (spec-ingest proposal awaiting approval) →
    `proposal-preview` view for the associated proposal id
  - anything else → falls back to `task-detail` when a task id is
    present, otherwise no-op with a small toast
  The drawer's row model carries `gate_type` (from
  `aq gate list --json`), so the dispatch is a switch statement.
  Per-view spec authors: don't assume Enter always opens task-detail —
  the drawer is dispatch-aware.
- On focused event row: `Enter` opens the associated entity in the pane
  (task-detail if a task event, session-peek if a session event, etc.).
- `1` / `2` switch between Gates / Events tabs.

### 6.4 Persistence

- Open/close state per-session (not persisted — always closed on load,
  users toggle when they want).
- Width persists in `localStorage`.
- Selected tab persists in `localStorage`.

## 7. Command Center consolidation

`/command-center` becomes a tabbed hub with three tabs.

### 7.1 Route shape

- `/command-center` → redirects to `/command-center/graph`
- `/command-center/graph` — Graph tab (canvas)
- `/command-center/tasks` — Tasks tab (table)
- `/command-center/agents` — Agents tab (table)

### 7.2 Legacy redirects

- `/work` → `/command-center/tasks`
- `/work/tasks` → `/command-center/tasks`
- `/work/agents` → `/command-center/agents`
- `/work/sessions` → `/command-center/agents` (sessions fold into
  agents; the drawer covers "events" needs)
- `/work/events` → `/command-center/tasks?openDrawer=events`
- `/work/gates` → `/command-center/tasks?openDrawer=gates`
- The `?openDrawer=...` query param is intercepted by the shell on
  route entry and opens the drawer with that tab, then removes itself
  from the URL.

### 7.3 Layout

```
┌────────────────────────────────────────────────────────────────────┐
│  Project strip: [ demo ] [ foo ] [ +new ]     ⟳ auto-refresh (60s) │
├────────────────────────────────────────────────────────────────────┤
│  [ Graph ] [ Tasks ] [ Agents ]                                    │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  <tab content>                                                     │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

- `ProjectStrip` (existing) persists above the tab strip. Changing
  project selection re-filters all three tabs simultaneously.
- Tab strip below. Clicking a tab pushes route; keyboard `1`/`2`/`3`
  when focus is in the tab strip.
- Tab content occupies the remainder of the center pane, sharing width
  with the right surface if open.

### 7.4 Graph tab

Unchanged from what ships today (React Flow canvas, dagre layout, agent
avatar overlay, ghost overlay stub). Two changes:

- Remove inline `TaskSidebar` — click on a node now dispatches
  `pane.open("task-detail", { taskId })` (see companion pane spec).
- Node click also focuses the shell pane so `Esc` after clicking
  closes the pane (not the graph selection).

### 7.5 Tasks tab

Content is the current `WorkTasks` sortable table, migrated in-place.
Row semantics:

- Click / `Enter` → opens task-detail pane view.
- `↑↓` moves focus.
- Per-row context actions (see §8 keyboard) — `o` open, `c` close,
  `r` reopen, etc.
- Filter bar at top: status pills + text search. `/` focuses the search
  input.

### 7.6 Agents tab

Content is the current `WorkAgents` table with sessions folded in:

- Each row shows agent id, current profile, current task, current
  session id, state (idle / busy / paused), last activity.
- Click / `Enter` on an agent with an active session → opens the
  shell pane's session-peek view for that session.
- Click on the task cell → opens task-detail pane view.
- Per-row actions (see §8).

### 7.7 Mobile

`<768px`:

- Graph tab is landscape-only. Portrait shows a "rotate device or use
  Tasks tab" placeholder.
- Tasks + Agents tabs render their `MobileCardList` projections (already
  built).
- Tab strip becomes a segmented control.
- Right pane and drawer render as bottom sheets (max-h-[80vh]) via the
  same primitive with a media-query-driven rendering variant.

## 8. Keyboard system

### 8.1 Modifier detection

- Detect Cmd vs Ctrl via `navigator.userAgent` + platform hint at app
  boot. Store on a `usePlatform` context (`{ modifier: "cmd" | "ctrl" }`).
- All shortcut declarations use a normalized `"$mod-K"` form; the
  hotkey layer expands `$mod` to the detected modifier.
- Custom shortcut edits (future) store the normalized form so they
  don't need re-editing per OS.

### 8.2 Command palette

- Library: `cmdk` (Vercel).
- Trigger: `$mod-K`, or clicking the palette button in the top bar.
- Style: Linear-prefix palette:
  - Default (no prefix) — fuzzy across pages + entities + actions.
  - `>` — actions only ("open diff", "create project", "toggle pane").
  - `#` — tasks (by id or title, current project scope by default;
    `##` for all projects).
  - `@` — projects.
- Ranking: weighted mix of (a) fuzzy score, (b) recency (last-used
  action within 24h boosted), (c) type (actions rank above entities for
  short queries).
- Actions are registered via a `useRegisterAction({id, name, run,
  keywords, section})` hook. The palette renders sections grouped by
  `section` (Chat, Tasks, Agents, Panes, Nav, etc.).
- Enter runs the action / navigates. `Escape` closes.

### 8.3 Section jumps

Two-key sequences (Gmail/Linear style). First key `g` puts the shell
in "goto mode" (a visual affordance shows the cheat sheet for 2s), then
the second key routes:

- `g h` — home (`/`, global chat)
- `g c` — Command Center (last-used tab)
- `g s` — Settings
- `g p` — project picker (palette pre-focused on `@`)

Escape or any non-matching key exits goto mode.

### 8.4 List motion (context-sensitive)

Any focused list surface (task table, agent table, drawer list, palette)
consumes:

- `↑↓` — move focus. `j/k` also, for Vim users.
- `Enter` — open the focused entity (usually into the pane).
- `/` — focus the list's filter/search input; `Esc` clears + un-focuses.
- `Home / End` — jump to first / last.
- `PgUp / PgDn` — move by page.

### 8.5 Right-side surfaces

- `[` — toggle pane (opens to last-used view, or task-detail if the
  current focus is on a task row).
- `]` — toggle activity drawer.
- `$mod-\` — cycle pane width preset.
- `Esc` — cascade: if a modal is open close it; else if the drawer or
  pane is open close it; else clear list-search focus; else return focus
  to the main content.

### 8.6 Chat composer (on `/` and `/projects/:id/chat`)

- `Enter` — submit.
- `Shift-Enter` — newline.
- `$mod-Enter` — also submits (fallback for muscle-memory).
- `Esc` — blur composer.
- `$mod-↑` — recall previous user message (terminal-style history).
- `t` (from any non-input focus on `/`) — focus the composer.

### 8.7 Per-entity single-letter actions

Context-aware — only bound when a specific entity type is in focus.
The shell renders a small hint footer showing which are live.

- Task row / task-detail pane:
  - `o` — open (task-detail pane, or full-page detail if held with
    `Shift`)
  - `r` — reopen with feedback (opens modal)
  - `d` — delete (opens confirm modal — this is the destructive action
    `TaskActions` already exposes; there is no separate "close" action
    on a task, and a completed task auto-transitions via `task_close`
    from the agent side, not by a UI button on this row)
  - `y` — duplicate (`d` was originally proposed for duplicate but
    conflicts with delete; delete wins because it's more common and
    keeps the same key across agent/gate rows)
  - `.` — more (opens dropdown of every registered action for the entity)
- Agent row:
  - `o` — open session peek
  - `p` — pause project (with confirm)
  - `k` — kill session (with confirm)
- Gate in drawer:
  - `a` — approve
  - `r` — reject
- Project (from left rail or palette):
  - `o` — open project overview
  - `.` — dropdown

Bindings are registered via `useEntityShortcuts({entityKind, actions})`
scoped to a component's focus.

### 8.8 Cheat sheet

`?` — opens a modal cheat sheet listing every currently-bound shortcut,
grouped by category. Auto-scoped to current context (chat composer
shortcuts don't show when focus is in a list, etc.). `?` again closes.
`Esc` closes.

### 8.9 Implementation

- `react-hotkeys-hook` for the low-level binding.
- `cmdk` for the palette.
- Central `useShortcuts` hook to declare context-scoped bindings + feed
  the cheat sheet. Bindings declared here are what the cheat sheet
  enumerates.
- `usePlatform` for modifier detection.
- Custom-shortcut edits: a `Settings > Keyboard` page (v2 follow-up, not
  in this initial ship) reads/writes to `localStorage` under
  `aq:keyboard:overrides`.

## 9. Testing shape

### 9.1 Backend

- `tests/test_supervisor_global_scope.py` — `check_command_scope`'s new
  global-admin path: elevated + project_id=None allows any command AND
  skips project match; elevated + project_id=demo enforces per-project
  match (unchanged); non-elevated + project_id=None rejects everything.
- `tests/test_supervisor_global_token_loopback.py` — token validation
  path: global-admin token from loopback address → 200; same token
  presented from a non-loopback address → 403.
- `tests/test_supervisor_global_lifecycle.py` — first message to
  `supervisor-global` mints an elevated + project_id=None token, spawns
  the session; idle timeout expires the session and revokes the token.
- Extend `tests/test_session_lens.py` — `supervisor-global` address
  resolves to runtime session name `n-supervisor--global`.

### 9.2 Frontend

Vitest + React Testing Library. No E2E infra in the repo — manual
verification for cross-page flows.

- `dashboard/src/shell/__tests__/ShellPane.test.tsx` — open/close state
  machine; opening pane closes drawer; width persistence.
- `dashboard/src/shell/__tests__/ActivityDrawer.test.tsx` — same
  primitive tests as pane; bell badge derived count.
- `dashboard/src/shell/__tests__/useShortcuts.test.tsx` — palette
  registration + firing; per-entity binding scoping.
- `dashboard/src/pages/__tests__/CommandCenter.test.tsx` — tab
  switching, ProjectStrip filter propagation, task-row click dispatches
  pane.open.
- Each pane view directory ships its own component tests (see per-pane
  specs).

### 9.3 Manual verification

- Chat on `/` sends messages to `supervisor-global`; supervisor replies
  arrive; command.invoked chips appear.
- `Cmd-K` opens palette; `>` scopes to actions; `#` scopes to tasks;
  `@` scopes to projects.
- `g h` / `g c` / `g s` / `g p` all jump correctly.
- Task row click on Command Center Tasks tab opens pane with
  task-detail view.
- Activity drawer badge counts match `aq gate list --status open`.
- Legacy `/work/*` URLs redirect.
- `<768px` mobile — bottom sheets, segmented tabs, portrait Graph
  placeholder.

## 10. Rollout — phased

Big enough to ship in ordered phases so no partial shell lands in a
release. Each phase is one PR family.

**Phase A — global supervisor session (backend + tests).**
- `check_command_scope` global-admin path.
- Loopback restriction in `TokenAuthMiddleware`.
- `SessionLens` cold-start path for `supervisor-global`.
- `_cmd_ensure_global_supervisor` command (behind `aq supervisor
  ensure-global`).
- Audit-log tagging.
- No frontend changes. Full test coverage.

**Phase B — shell foundation (frontend, feature-flagged).**
- New `AppShellV2` component behind a `?v2=1` query flag.
- Left rail, top bar, center outlet.
- `<ShellPane>` primitive + registry stub.
- `<ActivityDrawer>` primitive + gates/events tabs.
- Palette + hotkey vocab + cheat sheet.
- Home route (`/`) renders `ChatConversation` bound to
  `supervisor-global`.
- Existing routes still work via the old shell when `v2=1` is absent.

**Phase C — pane views (parallel).**
- Fan out to sub-agents. Nine per-view specs → nine parallel
  implementations. Each view is a directory under
  `dashboard/src/panes/<view>/` with manifest + component + tests.
- v1 subset (task-detail, diff-review-changes, file-browser) ship first
  so the pane is useful even before v2/v3 land.
- v2 subset (session-peek, console, playbook-run-inspector) ship next.
- v3 subset (spec-doc-reader, proposal-preview, contextual-settings)
  ship last.
- Each pane view is a self-contained PR.

**Phase D — Command Center consolidation.**
- Add tab strip to `/command-center`.
- Migrate `WorkTasks` → Tasks tab.
- Migrate `WorkAgents` → Agents tab; fold sessions into rows.
- Retire inline `TaskSidebar` (nodes dispatch to shell pane).
- Add legacy redirects for `/work/*`.
- Route existing `/system/*` alias unchanged.

**Phase E — remove v2 flag.**
- Delete the old shell path.
- `v2` becomes the only shell.
- Delete the retired `ChatLanding` component.

## 11. Open questions

- **Custom keyboard editor.** Not in initial ship. Deferred to a
  post-launch follow-up. Storage shape is defined here so it's
  forward-compatible when built.
- **Multi-select actions.** The keyboard vocab defines `x` as
  toggle-select. Bulk-action UX (multi-select then bulk-close, bulk-
  reopen) is out of scope for this shell rework. Design deferred.
- **Palette permission awareness.** Palette actions surface every
  registered action. Some may be gated by scope (e.g. `delete
  project` on a non-elevated session). Current behavior: attempt the
  action, surface the daemon's `out of scope` error inline. A cleaner
  path would filter the palette by scope; not required for v1.
