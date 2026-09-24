# Mobile dashboard — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md) ·
[dashboard performance and separation](2026-09-24-dashboard-performance-and-separation.md) ·
[morning report playbook](2026-09-24-morning-report-playbook.md) ·
[dashboard server design record](../../specs/dashboard-server.md) ·
[live pane streaming design](2026-08-25-live-pane-streaming-design.md) ·
[task graph spatial layout](2026-09-01-task-graph-spatial-layout-design.md) ·
[dashboard state contract](2026-09-10-dashboard-state-contract-design.md)

## 1. The ask

Operator: *"Current layout is unusable on a phone: left column too wide,
everything oversized. Must-haves: agent terminals (including rotated
full-screen terminal), usage, task list. Task detail takes the full view, no
sidebar. Graph optional. Dumbed-down is fine. Constraint: avoid building and
testing two layouts."*

Suggested direction (roadmap): a design spike toward **one responsive
codebase** — collapsible chrome and full-screen routes for the terminal and task
detail — rather than a second layout. That answers the two-layout worry and
gives desktop a full-screen terminal for free.

## 2. What exists today

All paths under `dashboard/src/` unless noted. Stack: Vite + React 19 +
TanStack Query + Tailwind v4 + react-router v7 (`dashboard/package.json`).

### 2.1 Shell

- `shell/AppShellV2.tsx` `ShellBody` is a fixed CSS grid:
  `grid h-screen w-screen grid-cols-[auto_1fr_auto] grid-rows-[auto_1fr]` —
  top bar across three columns, then **LeftRail | main | RightSurface**. No
  breakpoint changes this grid.
- `shell/LeftRail.tsx`: `<aside className="… w-64 shrink-0 lg:w-72 …">` —
  **256 px at every width below `lg`** and never collapsible. On a 390 px phone
  that leaves ~134 px for `<main>`. It carries Command Center, the project tree
  (`ProjectTree.tsx`), Metrics, Reviews, Settings and the agent list
  (`AgentFlock.tsx`, the main route to terminals).
- `shell/TopBar.tsx`: `h-12`, brand, `ProviderUsageBars` in the centre slot,
  back/forward, palette and activity-drawer buttons. Only responsive class:
  `sm:gap-3`.
- `shell/RightSurface.tsx` + `useRightSurface.tsx`: the task/pane/drawer side
  sheet, width clamped `MIN = 280`, `MAX = 800`, persisted as the **roaming**
  `shell_preferences` document on the daemon — so the width, the last open
  surface and the last pane (`panes/store.tsx` restore, lines ~71–114) follow
  the operator **from desktop to phone**.
- Keyboard-first chrome: `shell/hotkeys/` (react-hotkeys-hook), two-key `g h` /
  `g a` jumps, Cmd-K palette (`shell/palette/`), cheat sheet. None of it is
  reachable by touch except via the top-bar buttons.
- `index.html` has `<meta name="viewport" content="width=device-width,
  initial-scale=1.0">`; no web-app manifest, no service worker, no icons beyond
  `public/favicon.svg`.

### 2.2 Existing responsiveness (sparse)

- `grep @media` finds only `prefers-reduced-motion` in `index.css`.
- `hooks/useMediaQuery.ts` exists but **nothing imports it**.
- `pages/command-center/Graph.tsx` has its own `usePortraitMobile()`
  (`(max-width: 768px) and (orientation: portrait)`) and swaps the canvas for
  `layout-v2/MobileLayoutList.tsx` — the "phone view" `CLAUDE.md` mentions: the
  same server ordering as the canvas, **paged** via `fetchList` (the tiles API's
  `list` endpoint, `ListRequest` with `root=` for an entered container),
  `PAGE_SIZE = 50`, breadcrumbs, containers entered not expanded. This is the
  only phone-specific view in the app, and it is a graph substitute, not the
  Tasks tab.
- Tailwind breakpoint prefixes appear in ~30 files, mostly grid tweaks
  (`pages/project/Overview.tsx`, `pages/agents/AgentWorkspace.tsx` `lg:grid-cols-2`,
  settings forms). Nothing hides chrome.

### 2.3 The must-have surfaces

**Agent terminals.** Route `/agents` (`pages/agents/AgentWorkspace.tsx`),
selection in the URL (`?agent=<id>` repeatable, `useAgentSelection.ts`, pool
instances as `agent=pool:<key>`), up to four tiled `AgentWindow`s
(`lg:grid-cols-2`, `auto-rows-[minmax(20rem,1fr)]`). Two terminal renderers:

1. **Interactive** — `components/InteractiveTerminal.tsx`: xterm.js 6 +
   `FitAddon`, `fontSize: 12` fixed, one control row (Type / Enter / Ctrl+C),
   `ResizeObserver` → `fit()` → `terminal.onResize` → `connection.resize(cols,
   rows)` over `/ws/terminal/{session}` (`ws/terminalSocket.ts`). Server side
   `src/api/terminal_stream.py` → `src/sessions/terminal_pty.py`
   `PtyTmuxClient.attach` runs a real `tmux attach-session` on a pty and
   `resize()` sets the pty size + `SIGWINCH`. The agent's tmux window is
   created with **`window-size latest`** (`src/sessions/tmux.py:328`), so — by
   code reading, **not verified on a device** — the most recently active
   client sizes the window: a phone at ~45 portrait columns would **reflow the
   agent's actual TUI** for every viewer and for the daemon's own
   `capture-pane` readers until another client becomes active.
2. **Read-only live pane** — `components/LivePaneConsole.tsx`: SSE
   `GET /api/sessions/{id}/pane` (`src/api/pane_stream.py`), `capture-pane -e`
   snapshots rendered as `<pre>` via `ansiToSpans`, `text-xs`, `overflow-auto`.
   No resize side effect. Used in `panes/session-peek/` and
   `pages/command-center/AgentConsoleTile.tsx`, and as `SessionDetail`'s "pane"
   mode.

No full-screen affordance exists (`requestFullscreen` / "maximise" appear
nowhere). The dashboard server's edge (`src/dashboard_server/edge.py`, spec §3.3)
**refuses `/ws/terminal/*` from any non-loopback peer** (`403 loopback_only`),
so the interactive terminal does not work from a phone today unless the phone's
connection reaches the dashboard server from loopback (e.g. an SSH tunnel, or a
local reverse proxy such as `tailscale serve` — whether that satisfies both
the peer gate and the Host/Origin gates is for the
[Tailscale spec](2026-09-24-tailscale-dashboard-link.md) to settle).

**Usage.** Two places: `shell/ProviderUsageBars.tsx` (compact weekly quota
bars in the top bar, every page) and `pages/metrics/ProviderUsage.tsx`
(per-provider quota cards with staleness, under the Metrics route with the
uPlot charts).

**Task list.** `/projects/:projectId/tasks` →
`pages/command-center/Tasks.tsx`: a virtualised `<table>` with
`min-w-[620px]`, columns Task / Project / Status / Priority / Agent (+ Models /
Last activity in a time window) / actions — horizontal scroll on a phone.

**Task detail.** Clicking a row opens the `task-detail` pane in the right
surface (`useTaskSelection.ts` → `useShellPaneStore().open("task-detail", …)`,
`panes/task-detail/`). A full-page route also exists: `/tasks/:taskId` →
`pages/TaskDetail.tsx` (`p-6`, `text-2xl` title, tabs details/explain/graph) —
but it still renders inside `AppShellV2`, i.e. beside the 256 px rail.

**Graph.** `pages/command-center/Graph.tsx` → `layout-v2/LayoutCanvas` (React
Flow) or `MobileLayoutLists` in portrait ≤768 px.

### 2.4 Reaching it from a phone

Default bind is `127.0.0.1:8082` (`dashboard.server.host/port`). Design record
§3.4: a LAN/`0.0.0.0` bind exposes every `/api/**` route with local-operator
scope and no login; the recommended remote path is a loopback bind plus a port
forward. `src/remote_links.py` already resolves the machine's Tailscale
identity for links sent to Discord. Access is the sibling
[Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md) spec's
problem; this spec assumes the phone can load the page.

### 2.5 Test infrastructure

Vitest + Testing Library in jsdom (`dashboard/vitest.config.ts`,
`src/setupTests.ts` stubs `matchMedia`). jsdom has no layout, so it cannot
verify a breakpoint. **Playwright is not a dependency** — it appears in
`package-lock.json` only as `@vitest/browser-playwright`, an optional peer.
`scripts/dashboard-perf/` drives headless Chrome with `puppeteer-core`
installed ad hoc. `tests/test_dashboard_browser_storage.py` forbids new
`localStorage` keys, so a "remember mobile layout" toggle must go through
`useDashboardDocument` or the URL.

## 3. Gaps

1. **Fixed three-column grid and a non-collapsible 256 px rail** — the core
   of "unusable".
2. **Right surface is a side sheet with a 280 px minimum** — on a phone it
   cannot be "full view".
3. **Roaming shell preferences leak desktop layout onto the phone** (last pane,
   drawer, widths).
4. **No full-screen terminal anywhere**; fixed 12 px font; no touch keyboard
   aids (Esc, Tab, arrows, Ctrl combos).
5. **Interactive terminal resizes the agent's real tmux window**
   (`window-size latest`) and is refused from non-loopback peers.
6. **Task table needs 620 px**; no card/list form.
7. **Navigation is keyboard/hover-first** (palette, `g` jumps, hover titles).
8. **No viewport-level test** of any layout.
9. `h-screen` (100vh) is unreliable under mobile browser toolbars (`dvh` is
   the usual fix) — unverified on the operator's device.

## 4. Implementation options

### Option A — One responsive shell with breakpoints

*Sketch.* Below `md` (768 px): the rail becomes an off-canvas drawer behind a
hamburger in the top bar (same `LeftRail` component, `fixed inset-y-0` +
backdrop, closed on navigation); `grid-cols-[auto_1fr_auto]` collapses to a
single column; the right surface renders as a full-screen sheet (`fixed
inset-0`) instead of a column; top bar hides back/forward/palette and shows
usage as a single compact chip. Tasks table gets a card-row rendering under
`md` (title, status, agent) inside the existing virtualiser. Replace
`usePortraitMobile` with the shared `useMediaQuery` and one breakpoint
constant. `h-screen` → `h-dvh`.

*Touches.* `shell/AppShellV2.tsx`, `LeftRail.tsx`, `TopBar.tsx`,
`RightSurface.tsx`, `useRightSurface.tsx`, `pages/command-center/Tasks.tsx`,
`Graph.tsx`, a handful of page paddings.

*Pros.* One codebase; every page benefits at once; tablet and narrow desktop
windows improve too. *Cons.* Breakpoint logic spreads across many components;
every page needs at least a visual check at phone width; right-surface-as-sheet
semantics (Esc, back button) need thought. *Size:* **M**.

### Option B — Route-level "focus" layouts (full-screen routes, all widths)

*Sketch.* Add chromeless routes that render one thing edge to edge, usable on
desktop too: `/focus/agent/:id` (one terminal, full viewport, big controls,
landscape-friendly), `/focus/task/:taskId` (task detail, no rail/pane), and a
lightweight `/focus` home (usage chips + running agents + task list). Either
routes outside `<Route element={<AppShellV2 />}>` in `App.tsx`, or a
`?focus=1`/layout flag the shell honours by hiding rail, surface and top bar.
Desktop gets "pop this terminal full-screen" (plus the Fullscreen API) for free;
phone users live in these routes.

*Touches.* `App.tsx` routing, a thin `FocusShell`, `AgentTerminal` /
`InteractiveTerminal` (size/zoom props), `pages/TaskDetail.tsx` (reused as-is
minus the shell), a compact task list component (could reuse
`MobileLayoutList` paging or `Tasks.tsx` rows).

*Pros.* Directly delivers the three must-haves and "task detail takes the full
view" without re-flowing the whole app; small surface to test; useful on
desktop, so it is not a phone-only layout. *Cons.* Pages outside the focus set
stay unusable on a phone (acceptable per "dumbed-down is fine"); risk of a
parallel navigation model if the focus home grows. *Size:* **S–M**.

### Option C — Separate mobile PWA shell reusing components

*Sketch.* A second entry (`mobile.html`, like the existing `scenarios.html`)
with its own shell, bottom tab bar (Agents / Tasks / Usage), web-app manifest
and install-to-home-screen, reusing the page components and API hooks.

*Pros.* Best phone ergonomics; independent iteration. *Cons.* **Is the two-
layout outcome the operator asked to avoid** — two shells, two navigation
models, two test matrices; the bundle build and manifest verification in
`src/dashboard_server/bundle.py` would need a second entry. *Size:* **L**.

### Option D — Testing approach (applies to A/B)

Add a small Playwright (Chromium) suite — or reuse `puppeteer-core` like
`scripts/dashboard-perf/` — that loads the built bundle against a stubbed or
recorded API at three viewports (390×844 portrait, 844×390 landscape,
1440×900), asserts no horizontal overflow on `document.body`, asserts the
must-have elements are visible, and stores screenshots for review. Keep it out
of the default vitest run (it needs a browser) and the Python CI arm
(`tests/test_dashboard_browser_storage.py` is the only dashboard check there
today). *Size:* **S–M** (dependency and API stubbing are most of it).

## 5. Initial take

*Provisional.* **B first, then the minimal slice of A, tested with D.**

1. Build the focus routes (B): a full-screen agent terminal, full-view task
   detail and a phone home of usage + running agents + task list. These are
   the operator's must-haves and give desktop a full-screen terminal.
2. Then the minimum of A that makes the regular shell *navigable* on a phone:
   collapsible rail drawer, right surface as a full-screen sheet under `md`,
   card rows for Tasks. Not every settings page.
3. D from day one, so "one layout" stays true: one component tree, verified at
   three viewports.

For the terminal specifically, default the phone to the **read-only live pane**
(`LivePaneConsole`, no resize side effect, works through any proxy that relays
SSE), with an explicit "Interact" switch to xterm that (a) warns or is
designed around the `window-size latest` reflow, and (b) only appears when the
peer is allowed terminals. Rotated full-screen = the focus route in landscape
plus the Fullscreen API where the browser supports it.

C is not recommended: it is the two-layout outcome.

## 6. Open questions

1. **Must the phone be able to *type* into terminals, or is watching enough?**
   Decides whether the loopback-only terminal rule, the tmux resize behaviour
   and a touch key bar are in scope at all.
2. **Resize policy for a small viewer.** Options: never send `resize` from a
   phone and letterbox/scale the xterm to the agent's current size; switch the
   window to `window-size largest`/`manual`; or accept the reflow. Changes agent
   behaviour for everyone — needs the operator's call and a check of what
   `capture-pane`-based readiness/exit classifiers assume about width.
3. **Terminal font size and zoom.** Fixed 12 px today. Pinch-zoom, a per-view
   font control, or fit-to-width at the agent's cols? Where is the choice
   stored, given the browser-storage rule (roaming document vs. URL vs. not at
   all)?
4. **Touch keyboard aids.** Which keys need buttons — Esc, Tab, arrows, Ctrl+C,
   Enter, `/`-commands? Does the soft keyboard covering half the screen need
   `visualViewport` handling?
5. **Exposure and auth.** From a phone over the tailnet, `/ws/terminal/*` is
   refused by `edge.py`'s peer gate, and every `/api/**` route is local-operator
   scope with no login. Does mobile access require a login/token story first,
   or is `tailscale serve` to the loopback bind acceptable? Owned by the
   [Tailscale spec](2026-09-24-tailscale-dashboard-link.md); blocks the
   interactive terminal.
6. **Which pages are in scope for the phone?** Proposed: agents/terminals,
   usage, task list, task detail, reviews(?), escalation inbox(?). Explicitly
   out: settings editors, graph canvas, playbook editors. Decides the test
   matrix.
7. **Should roaming `shell_preferences` be per device class?** Today the phone
   restores the desktop's last pane and drawer. Options: ignore restore under
   the phone breakpoint, or key the document by device class. Decides a change
   to `useShellPreferences`/`panes/store.tsx` and possibly the state contract.
8. **Focus routes: separate routes or a shell mode?** Separate routes are
   clean deep links (shareable from Discord / the digest); a shell mode keeps
   the palette and toasts. Decides `App.tsx` structure.
9. **Is "usage" the provider quota bars only, or also token/cost charts from
   Metrics?** Decides whether uPlot loads on the phone at all.
10. **PWA install (manifest + icon, no offline)?** Cheap, gives a home-screen
    icon and a chromeless window; a service worker is not wanted (stale
    bundles vs. the manifest-verified server).
11. **Which browser/device is the target?** iOS Safari vs. Android Chrome
    differ on Fullscreen API support (iOS Safari historically lacks it for
    non-video elements — unverified for current versions) and on `dvh`.

## 7. Dependencies and sequencing

- **Access:** [Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md)
  decides how the phone reaches `:8082` and whether terminals are allowed;
  read-only pages and the live pane can ship before it.
- **Performance:** a phone adds a second concurrent client; see
  [dashboard performance and separation](2026-09-24-dashboard-performance-and-separation.md).
  The phone home should prefer event-driven queries already in place after the
  2026-09-23 work.
- **Links in:** [morning report](2026-09-24-morning-report-playbook.md),
  [supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md)
  and [Discord mention routing](2026-09-24-discord-mention-routing.md) will
  want to deep-link into focus routes (`/focus/task/:id`,
  `/focus/agent/:id`) — agree the URL shape early.
- Order: D harness → B focus routes → A minimal shell collapse → terminal
  interactivity once 1/2/5 are answered.

## 8. Non-goals

- A second, separately maintained mobile app or layout tree.
- Graph canvas parity on a phone (the existing `MobileLayoutList` stays as is).
- Offline support or a service worker.
- Native apps / push notifications (Discord already covers alerts).
- Editing settings, profiles, playbooks or config from a phone.
