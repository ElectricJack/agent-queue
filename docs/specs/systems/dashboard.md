# System spec: dashboard

Status: describes the system as of 2026-10-20 (base `22cffd1b7`). This is system 15 of the
eighteen in the agent-queue system inventory (vault
`projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`, review
`rev-bright-rapids`, approved at revision 1 when this was written). It explains what the
system is for, how it works today, where it stops, and what is planned. It links to the
existing guide, reference and design documents rather than repeating them.

## 1. Purpose

Agent Queue runs dozens of agents across many projects. The CLI can answer any single
question, but nobody can watch a task graph of hundreds of nodes, a fleet of live terminals
and a review queue through `aq task show`. The dashboard is the operator's browser
workspace for that: one place to see the work graph, follow and steer agents, read and
approve reviews, inspect playbook runs and change settings, on a desktop or a phone.

It solves three problems the rest of the system does not:

- **Seeing a large graph.** The command center draws a project's whole task hierarchy as a
  spatial map that stays fast at thousands of nodes, because the layout is computed on the
  server and fetched in tiles.
- **Watching live work.** Agent terminals, transcripts, playbook runs and task state update
  as events arrive, without the operator refreshing anything.
- **Staying up when the daemon does not.** The static app is served by its own small
  process, so a daemon restart or a busy orchestrator cycle never takes the page away; the
  page shows that the daemon is unreachable and reconnects.

The dashboard computes no policy and owns no work state. Every change it makes goes through
the API to `CommandHandler`, like the CLI, MCP and Discord.

## 2. How it works today

### 2.1 Vocabulary

- **Dashboard server**: the separate process (`src/dashboard_server/`) that serves the
  built React app and proxies API traffic to the daemon. Default `127.0.0.1:8082`.
- **Daemon API**: the daemon's HTTP and WebSocket surface on port 8081. Since the
  API-only split it serves no HTML.
- **Bundle**: the built frontend in `src/dashboard_assets/dist`, with a manifest of
  SHA-256 digests the server verifies before serving anything.
- **Shell**: the frame every page sits in (`dashboard/src/shell/AppShellV2.tsx`): navigation
  rail, top bar, command palette and the right-hand pane host.
- **Pane**: a self-contained view that opens on the right-hand side of the shell (task
  detail, session peek, diff review and so on). Panes are frontend modules only.
- **Command center**: the per-project graph view at `/projects/:id/graph`, drawn from
  server-computed layout tiles.
- **Dashboard state**: per-user and per-workspace UI documents (navigation tree, shell
  preferences, graph view) stored by the daemon in typed namespaces.
- **Focus mode**: the phone UI under `/focus/*`.

### 2.2 Two processes

The [dashboard server spec](../dashboard-server.md) is the design record; the
[dashboard guide](../../guides/dashboard.md) is the operator manual. In short:

- The **daemon** on 8081 answers `/api`, `/health`, `/ready`, `/ws`, `/mcp` and
  `/plans/<task_id>`. A request for `/dashboard` gets a 307 redirect to the configured
  dashboard server, or a 404 `dashboard_not_served_here` when none is configured
  (`src/api/app.py:249-286`).
- The **dashboard server** is a raw ASGI app (`src/dashboard_server/app.py`,
  `DashboardServerApp`) run by uvicorn. It uses Starlette only for `FileResponse`, and one
  shared aiohttp session (256 connections, 2 s connect timeout, 120 s to response headers,
  no read timeout, so streams stay open) for the upstream.
  - `bundle.py` serves the verified bundle. It checks `aq-dashboard-manifest.json` and
    fails closed on a digest mismatch, falls back to `index.html` for client routes, marks
    `assets/` immutable, sends ETags and a CSP of `frame-ancestors 'self'`.
  - `proxy.py` forwards `/api`, `/health`, `/ready` and `/ws`. For a WebSocket it opens
    the upstream handshake first and passes the client's subprotocols through (the
    terminal uses `aq-terminal-v1`).
  - `edge.py` is the gate in front of both: a Host check (421), an Origin check (403), and
    a bearer-token requirement for any client off loopback. Tailnet ranges
    (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) count as private. Pull-request approval is
    `operator_only`. The server stamps `x-aq-dashboard-viewer` on proxied requests; the
    daemon trusts that header only from loopback (`src/api/auth.py:94-119`).
  - `/__aq/health` reports the server's identity and `upstream_ok`; `/__aq/metrics`
    reports relay statistics (`relay_stats.py`).
  - Upstream failures map to 503 `daemon_unreachable` (with `Retry-After: 2`), 504
    `daemon_timeout` and 502 `daemon_bad_response`. The frontend keys its offline banner
    on these.
- The server **imports almost nothing from the daemon**: `src.config` for settings, plus
  `src.metrics.histogram` for relay stats, a `src.cli.__version__` fallback and a lazy
  `src.remote_links`. It never opens the database.

**Process lifecycle.** `python -m src.dashboard_server` is the entry point
(`__main__.py`; exit 0, 1 or 2; it never tries the next port). `process.py` starts it with
an environment allowlist (`_ENV_ALLOWED`), so the child receives no database URL and no API
keys, and writes `~/.agent-queue/dashboard-server.pid` and `.log`. `aq start` calls
`ensure_dashboard_server()` (`src/cli/dashboard.py:100`) and only warns if it fails. `aq
dashboard serve|start|stop|restart|status|link` manages it directly. `aq restart` stops
the dashboard server and the daemon and starts both; `--no-dashboard-server` leaves the
server alone. Nothing supervises the server itself. The optional `aq service` watchdog
(`src/install/watchdog.py`) runs `aq start --no-dashboard` when the daemon dies, which
also ensures the dashboard server; a dashboard server that dies while the daemon is healthy
stays down until someone runs `aq start` or `aq dashboard start`.

**Configuration.** `dashboard.server` in `config.yaml` (`src/config.py:3650-3769`):
`enabled`, `host` (default `127.0.0.1`), `port` (8082, or 8083 when the MCP port takes
8082), `public_url` and `tailscale_path`. The server's `settings.py` reads `config.yaml`
directly and never loads `.env` secrets. Exposure on a tailnet is covered in
[dashboard server spec §3.4](../dashboard-server.md) and the guide.

**Doctor.** `src/doctor/dashboard_server_checks.py` checks that the server runs, answers,
serves a verified bundle and reaches the daemon.

### 2.3 The frontend

`dashboard/` is a Vite 6 + React 19 app: react-router 7, TanStack Query 5 and TanStack
Virtual, Tailwind v4, `@xyflow/react` 12 for graphs, dagre for the playbook, proposal and
run graphs only, xterm 6 for terminals, cmdk for the palette, uplot for charts and zod for
runtime validation. Tests are Vitest 4. `dashboard/src` holds about 640 `.ts`/`.tsx` files,
roughly 420 source and 220 test. [`dashboard/AGENTS.md`](../../../dashboard/AGENTS.md) holds
the rules for working in it.

The start-up chain is `main.tsx` → a `QueryClient` (30 s stale time) → the router →
`EventStreamProvider` → `DashboardStateProvider` → `App.tsx` (routes at lines 240-322) →
`shell/AppShellV2.tsx`. Below 768 px the shell switches to its compact layout.

Routes, by area:

| Area | Routes |
|---|---|
| Phone | `/focus/*` |
| Fleet | `/agents`, `/conversations`, `/metrics`, `/host-shell`, `/reviews` |
| Project | `/projects/:id/graph`, `tasks-knowledge`, `overview`, `sessions`, `workspaces`, `playbooks`, `config` |
| Objects | `/tasks/:id`, `/files`, `/sessions/:id`, `/playbooks/:id` |
| Settings | `/settings/*` |
| Old URLs | `LegacyRedirects` |

The [module reference](../../reference/modules/dashboard.md) catalogues the directories.

### 2.4 Reading data

All reads go through the generated TypeScript client `@aq/ts-client`
(`packages/aq-ts-client/src`, gitignored, regenerated from `openapi.json` by the
`predev`, `prebuild` and `pretypecheck` scripts). `dashboard/src/api/client.ts` wraps it
and `api/hooks.ts` turns calls into TanStack Query hooks. `legacy-fetch.ts` exists for the
three non-API paths, `/health`, `/ready` and `/plans`. The
[codegen guide](../../contributing/codegen.md) covers the pipeline from `src/api/spec.py`
to both clients.

Most endpoints come from command contracts and reach `CommandHandler`. A few routers are
hand-written and read the database or services directly: `src/api/graph.py`,
`graph_layout.py`, `metrics.py`, `providers.py`, `pull_requests.py`, the stream endpoints
and the file endpoints. Each is read-only except `graph_layout`'s tidy action, which
dispatches the `graph_tidy` command.

**Authentication.** `src/api/middleware.py:43-118` gives a tokenless request the local
scope unless `api_auth.require_session_token` is set. The dashboard has no login; behind
the dashboard server's edge gate every viewer acts as the single local operator.

### 2.5 Live updates

The page keeps one WebSocket to `/ws/events` (`src/api/websocket.py`). The daemon forwards
events whose names start with `notify.`, `agent.`, `message.`, `gate.`, `session.`,
`task.`, `pool.`, `provider.`, `metrics.`, `dashboard_state.`, `review.`, `ask.` and
`playbook.`, plus `project.paused` and `project.resumed`, filtered by the client's scope.
Each event is serialised once. Each client has a 1,000-event queue that drops the oldest
when full. A `hello` frame carries an epoch, a reconnect may pass `?after_seq=` to replay
missed events in 500-row pages, and an auth failure closes with code 4401.

On the client, `ws/useEventStream.ts` is a singleton with 1–30 s reconnect backoff. It
coalesces bursts before invalidating queries: roster 1,000 ms, playbooks 400 ms, dashboard
state 100 ms, graph 500 ms, layout tiles 150 ms. `metrics.tick` arrives at 1 Hz through a
raw subscription. Some views still poll every 10–60 s as a backstop.

Three other streams exist: SSE for session transcripts and for live pane streaming
(`/api/sessions/{id}/pane`, `src/api/pane_stream.py`), and the terminal WebSocket
`/ws/terminal/{id}` with the `aq-terminal-v1` subprotocol.

### 2.6 Dashboard state

UI state that should follow the operator between browsers lives in the daemon, not in the
browser. The contract is
[the dashboard state design](../../superpowers/specs/2026-09-10-dashboard-state-contract-design.md).
Namespaces are declared in `src/dashboard_state/namespaces.py`:

| Namespace | Scope | Write mode |
|---|---|---|
| `nav_organization` | workspace | compare-and-swap |
| `shell_preferences` | user | last write wins |
| `command_center_preferences` | user | last write wins |
| `command_center_project_view` | user, per project | compare-and-swap |
| `playbook_graph_view` | user | compare-and-swap |

Values are pydantic models in `src/api/models/dashboard.py` with `extra=forbid`. The
commands in `src/commands/dashboard_state_commands.py` are list, get, put and reset. They
accept only the human local-operator principal, cap a value at 64 KiB, and emit
`dashboard_state.changed.v1` without the value so other tabs refetch. Documents live in
`dashboard_state_documents` (`src/database/tables.py:136-163`, migration `a0000000000e`);
deleting a project deletes its documents, and `aq doctor --check dashboard_state.orphans`
finds any left behind.

On the client, `api/dashboardState.ts` and `dashboardStateStore.ts` keep a write queue per
document address, send `base_revision`, show the change optimistically and retry on
`revision_conflict`. There is deliberately no browser-storage fallback. The only
device-local keys (`src/deviceLocal.ts`) are the event-stream cursor `aq:ws:last_seq`, its
`aq:ws:epoch` and a tab session id `aq:session:id`.

### 2.7 Panes

A pane is a directory `dashboard/src/panes/<id>/` with a `manifest.ts` and a lazily loaded
component; `panes/registry.ts` collects them by glob. Thirteen ship today: `task-detail`,
`session-peek`, `diff-review-changes`, `playbook-run-inspector`, `playbook-detail`,
`file-browser`, `spec-doc-reader`, `console-stream`, `proposal-preview`,
`contextual-settings`, `review`, `knowledge`, and the test pane `__stub-smoke`.
`panes/store.tsx` holds the one open pane and persists it in
`shell_preferences.right_surface.pane`, so it survives a reload. The pane designs are the
`docs/superpowers/specs/2026-08-22-pane-*` documents and
[live pane streaming](../../superpowers/specs/2026-08-25-live-pane-streaming-design.md).

The server keeps a mirror of the pane ids and their arguments in `src/panes/registry.py`
(`SERVER_PANE_REGISTRY`); `tests/test_pane_registry_parity.py` keeps the two in step. Its
one use is validating a `pane_open` action on `message send`
(`src/commands/message_commands.py:506-563`), which lets an agent's message carry a button
that opens a pane. There is no server-side pane plugin interface.

### 2.8 Command center

Graph layout is computed by the daemon, not the browser
([design](../../superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md);
[command center unification](../../superpowers/specs/2026-08-30-command-center-unification-design.md)).
`src/task_graph/layout/` places nodes; `src/orchestrator/layout_step.py` recomputes stale
projects off the critical path of the cycle, up to ten a cycle. `src/api/graph_layout.py`
serves extent (202 `layout_pending` until a layout exists), tiles, list, node, locate, the
tidy action, jobs and the running target, capped at 2,000 expanded nodes and 200 list or
locate rows, with a 32-entry geometry cache.

The client in `pages/command-center/layout-v2/` (`LayoutCanvas.tsx`, `useLayoutTiles`,
`layoutStore`, `flowNodes`, `MobileLayoutList` for phones) fetches only the visible tiles.
A container node is entered by re-rooting the view on it, never expanded in place. The
older whole-graph snapshot (`src/api/graph.py`, `dashboard/src/api/graph.ts`) still feeds
the Tasks list view.

### 2.9 Links from outside

Discord messages, digests, review notifications and hourly reports link into the dashboard.
`src/remote_links.py` resolves the public base URL (from `dashboard.server.public_url` or
the Tailscale path) and `src/dashboard_paths.py` builds the paths, so a route rename
changes one file. Callers are escalations, the digest, `reviews/notifier.py`,
`reports/hourly.py`, `conversation_commands.py` and `discord/render_budget.py`;
`tests/test_dashboard_links.py` pins the paths.

### 2.10 Performance work

Two rounds of performance work shaped the current design. The 2026-09-04
[investigation](../../superpowers/specs/2026-09-04-dashboard-performance-investigation.md)
and [plan](../../superpowers/specs/2026-09-04-dashboard-performance-implementation.md),
measured in [2026-09-23](../../superpowers/specs/2026-09-23-dashboard-performance.md), cut
the initial bundle from 1,097 KB to 413 KB and the agent roster from 365 ms to 118 ms. The
second round, the vault spec on performance under load and separation, set an envelope:
cold pages under 2 s, `agent/list` p95 under 500 ms, event-loop lag p95 under 50 ms. It
added request timing (`src/api/perf_middleware.py`), loop-lag sampling
(`src/metrics/perf.py`, `loop_watchdog.py`), the doctor check `perf.sustained_loop_lag` and
the relay statistics. Epic `wise-ember` implemented its plan. See §5 for where the envelope
stands.

### 2.11 Testing

- **Vitest**: about 220 test files under `dashboard/src`, run locally with
  `npm --prefix dashboard test`. Flakiness rules are in
  [the Vitest flakiness note](../../superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md).
- **Browser checks**: `dashboard/layout-checks/` runs 21 checks against a stub daemon,
  including axe accessibility checks.
- **Python**: `tests/test_dashboard_server_{app,bundle,edge,proxy}.py`,
  `test_cli_dashboard_server.py`, `test_doctor_dashboard_server.py`,
  `test_dashboard_state.py`, `test_dashboard_browser_storage.py`,
  `test_dashboard_secure_context.py`, `test_api_dashboard_pointer.py`,
  `test_pane_registry_parity.py`, `test_dashboard_links.py`, `test_api_client_contract.py`.
- **Performance**: `scripts/dashboard-perf/` and `tests/perf/test_layout_api_statements.py`.
- **CI**: `.github/workflows/tests.yml` runs `npm ci`, the typecheck and the production
  build for `dashboard/`. It does not run Vitest or the browser checks (§5).

## 3. Boundaries

| System | What crosses the boundary |
|---|---|
| daemon-core | Every dashboard write ends in `CommandHandler`. The orchestrator cycle runs the off-cycle layout step; the event bus feeds `/ws/events`. |
| task-graph | The layout engine and its tiles belong there; the dashboard draws them. Task status, readiness reasons and hierarchy are read, never computed, in the browser. |
| scheduling-and-claims | Pool status, claims and resource gates are shown on the agents and metrics pages; pool changes go through commands. |
| routing-and-profiles | Profile and intelligence-class settings pages; task routes shown in task detail. |
| sessions-and-harnesses | Session lists, transcripts, the terminal WebSocket and pane streaming read sessions; sending input goes through session commands. |
| projects-workspaces-git | Project pages, workspace lists, the file browser and diff review read through the API. |
| integration-and-delivery | Pull-request views and delivery state; approval is `operator_only` at the edge. |
| playbooks | Playbook list, detail, graph and run inspector panes; gate approvals go through playbook commands. |
| messaging-and-human-asks | Conversations, the inbox, asks and escalations render here; Discord and digests link back through `remote_links`. Agent messages can carry `pane_open` actions. |
| supervisors-and-policy | Supervisor sessions appear as agents; policy decisions are made through commands, not dashboard logic. |
| reviews-and-knowledge | The review queue, review pane, knowledge pane and spec reader. Held-batch fidelity review adds a proposal-preview mode (§6). |
| providers | Availability, usage and token endpoints; the dashboard renders them and computes nothing. |
| configuration-and-vault | `dashboard.server` config; settings pages edit config through config commands. The vault is never read directly by the browser. |
| api-and-clients | The dashboard is the main consumer of `openapi.json` and the generated TypeScript client; dashboard state models and endpoints live in the API. |
| test-service-and-jobs | No runtime link. Vitest runs outside `aq test`; CI runs only the typecheck and build. |
| operations-and-recovery | `aq start`/`restart` start the dashboard server; doctor checks; fleet metrics and loop-lag measurements feed the metrics page. |
| plugins-mcp-and-tools | Plugin-provided views have no dashboard surface; the dashboard calls no MCP tools. |

What this system does not own: any work state or policy, the graph layout algorithm, the
API contract (api-and-clients), or the daemon's process supervision. `src/editor/` is not
part of it: that package is a standalone voxel level editor imported only by
`tests/test_editor_*`.

## 4. Principles and invariants

- **The browser holds no truth.** Work state is fetched; UI state that should travel
  lives in daemon-side dashboard state. Only the event cursor and a tab id are
  device-local.
- **Writes go through commands.** A button in the dashboard is a command call, so the
  CLI, MCP and Discord can do the same thing and the same validation applies.
- **The page outlives the daemon.** The dashboard server has no database access and
  almost no daemon imports, so it starts, serves and reports `daemon_unreachable` while
  the daemon restarts.
- **The server serves only what it verified.** A bundle whose digests do not match the
  manifest is not served.
- **Off loopback, a token or a private network.** The edge gate checks Host and Origin on
  every request and requires a bearer token from any non-loopback, non-tailnet client.
- **Generated client only.** Pages and panes call the API through `@aq/ts-client`, never
  `fetch` by hand, so a contract change breaks the typecheck instead of the page.
- **Expensive work happens on the server, once.** Graph layout is computed off-cycle and
  served in tiles; events are serialised once per broadcast and coalesced on the client.
- **Pane ids are a shared vocabulary.** The frontend registry and the server mirror must
  agree, so agents can open panes by name.

## 5. Known gaps

**Documentation drift.**

- The inventory's entry for this system says the dashboard server "owns typed
  server-backed state namespaces and the editor" and renders panes "that the pane plugin
  interface defines". The state namespaces are daemon-side (§2.6), `src/editor/` is an
  unrelated voxel level editor (§3), and panes are frontend modules with no server plugin
  interface (§2.7). Its "258 modules" undercounts `dashboard/src` (§2.3).
- The inventory names two vault files that do not exist. The feedback spec is
  `specs/2026-10-03-aq-dashboard-feedback-ideas-and-versioned-releases.md`, and the
  performance plan is
  `plans/2026-09-25-dashboard-performance-under-load-and-separation-plan.md`.
  `2026-09-26-mobile-dashboard.md` is a plan; the spec it implements is
  `specs/2026-09-24-mobile-dashboard-2.md`.
- The [guide's troubleshooting table](../../guides/dashboard.md) and the
  [release notes' known issues](../../release-notes.md#known-issues) still list
  `smart-meadow.7` (`aq start` mistaking the dashboard server for the daemon). It is fixed:
  `find_daemon_pid` in `src/daemon_state.py:235` now excludes the dashboard server, and the
  task is completed.
- Stale comments: `AppShellV2.tsx:255`, `pages/command-center/Graph.tsx:26` and
  `api/graph.ts:47` describe arrangements that have since changed. `IntelligenceClassesStub`
  is a working settings page despite its name.
- `aq restart --no-dashboard` sounds like it leaves the dashboard server alone; it only
  skips the Vite dev-server prompt. `--no-dashboard-server` is the flag that does.

**Behaviour.**

- **Vitest does not run in CI.** About 220 test files are checked only when someone runs
  them locally, and the browser checks never run in CI either.
- **Direct `fetch` calls** break the generated-client rule:
  `pages/settings/ProjectRoots.tsx:63`, `panes/console-stream/index.tsx:56`,
  `panes/console-stream/hooks.ts:112`, `panes/spec-doc-reader/hooks.ts:85`, and
  `api/activity.ts:72` for `task_recent_activity`.
- **A test pane ships in production.** `__stub-smoke` is in the production registry and
  the `[` shortcut opens it (`AppShellV2.tsx:169`).
- **Unwired manifest fields.** Pane manifests declare `open_shortcut`, `palette_*` and
  `route_scope`, but only tests read them; `setShortcuts` in `ShellPaneHost.tsx:30-33` is a
  no-op.
- **Dead pages.** `pages/work/*`, `pages/system/{Events,Gates,Sessions}` and
  `pages/knowledge/*` are not routed.
- **Hand-mirrored pane registry.** `src/panes/registry.py` is kept in step by a parity
  test, not generated.
- **No supervision of the dashboard server.** A crash while the daemon stays healthy
  leaves the page down until someone runs `aq start` or `aq dashboard start`; the
  `aq service` watchdog brings it back only as a side effect of restarting a dead daemon.
- **Daemon down means a blank workspace.** With the daemon unreachable the shell shows
  "Preferences unavailable" or "Loading…" rather than an explanation and a way to recover.
- **Single operator identity.** There is no login and no per-person state: every viewer
  allowed through the edge gate is the one local operator.
- **The performance envelope was not met and has not been re-measured.** The evidence
  review of 2026-09-26 (vault `notes/dashboard-perf-evidence-review-2026-09-26.md`) found
  cold Tasks at 2,860 ms and cold Metrics at 6,342 ms against 2,000 ms, `agent/list` p95
  near 1,018 ms against 500 ms, loop lag p95 123.7 ms against 50 ms with a 2,870 ms worst
  stall; task detail passed at 159 ms, and it found no case for a separate read process.
  The remediation tasks (`wise-ember.15`–`.19`: route labels, blocking spans, cold pages)
  are completed, but no later measurement was found. `pool/status` still takes about
  0.65 s.

## 6. Planned work

Open tasks were re-queried on 2026-10-20 against the operator database, and vault spec
states were read from their frontmatter. What is open or approved but unbuilt:

- **`calm-orbit-69` (IN_PROGRESS): held ingestion P6, API, dashboard and CLI.** From the
  vault spec `specs/2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`
  (approved, `rev-agile-beacon` r1), which refines
  `specs/2026-10-13-spec-ingestion-with-task-structure-review-before-release.md`
  (`rev-bright-meadow` r2). `.1` (completed) added `GET /projects/{id}/batches/{batch_id}`
  and `TaskDetail.held_batch`. `.2` (completed 2026-10-19) is the dashboard part: a "Held
  for fidelity review" status pill, a held-batch section in task detail, a held-batch mode
  for the `proposal-preview` pane and four `batch.*` events. Its commits are on
  `origin/aq/calm-orbit-69.2` and are not yet on dev at `22cffd1b7`. `.3` (defined) is CLI,
  doctor and MCP only.
- **`stark-willow-95.5` (DEFINED): delivery latency panel.** Part of fast smooth
  integration (vault `specs/2026-10-12-fast-smooth-integration.md` rev 2, §10). A panel in
  `pages/metrics` showing delivery median and p90, a time breakdown, backlog and shared
  blockers, over 24 h or 7 days, with a project filter and drill-down. It waits on
  `stark-willow-95.4` (READY: `src/metrics/delivery_latency.py` and the metrics endpoint)
  and `bold-zenith-11.8`.
- **`bold-zenith-11` (IN_PROGRESS).** `.8` (wait explain and the wait graph in the
  dashboard) and `.20` (wait metrics on the dashboard) are completed but not yet on dev;
  `.22` (READY) flips the defaults.
- **Task graph UI redesign.** Vault `specs/2026-10-06-task-graph-ui-redesign-2.md` is
  approved (`rev-bold-current` r1) with five open questions for the operator. Phase A
  landed (`3f5b620a6` graph tokens; `3af00ea10` card and frame anatomy, status and reason
  models, stripes and the edge legend). Phases B (edges, zoom, hover, minimap), C (chrome,
  right rail, phase rail) and D (engine tuning, which bumps `ENGINE_RULES_VERSION`) have no
  tasks filed.
- **Feedback, ideas and versioned releases.** Vault
  `specs/2026-10-03-aq-dashboard-feedback-ideas-and-versioned-releases.md` is approved
  (`rev-bright-ember` r1): a Feedback and What's-new pane, an installation-wide ideas list
  with a review lifecycle, approval that files a design task, and a version policy with
  seen-state. Its plan
  `plans/2026-10-03-implementation-plan-aq-dashboard-feedback-and-release-histor.md` is in
  review (`rev-amber-delta` r1). No implementation tasks exist.
- **Performance under load.** Vault
  `specs/2026-09-24-dashboard-performance-under-load-and-separation.md` is approved
  (`rev-eager-falcon` r1) and its plan
  `plans/2026-09-25-dashboard-performance-under-load-and-separation-plan.md` is approved
  (`rev-bold-quest` r1) and implemented as `wise-ember`. Static separation is done; read
  latency isolation is not, and the envelope needs re-measuring (§5). No task is open for
  it.

Settled and not planned: the mobile dashboard (vault `specs/2026-09-24-mobile-dashboard-2.md`,
implemented, `rev-brisk-delta` r1; plan `plans/2026-09-26-mobile-dashboard.md`, approved,
`rev-crisp-delta` r1) and the Tailscale link fix
(`specs/2026-09-24-tailscale-dashboard-link-not-posting.md`, implemented,
`rev-solid-harbor` r1). The earlier drafts `2026-09-24-dashboard-performance-and-separation.md`,
`2026-09-24-mobile-dashboard.md` and `2026-09-24-tailscale-dashboard-link.md` are
superseded by those. The system-map vision spec that would have framed this work
(`2026-10-20-agent-queue-the-vision-and-the-system-map.md`, `rev-smart-nexus` r1) was
withdrawn.

Nothing open addresses the gaps in §5 other than the drift this document records.

## 7. Related documents and source

Documents:

- [Dashboard guide](../../guides/dashboard.md)
- [Dashboard server design record](../dashboard-server.md)
- [Dashboard module reference](../../reference/modules/dashboard.md)
- [`dashboard/AGENTS.md`](../../../dashboard/AGENTS.md)
- [Codegen](../../contributing/codegen.md)
- [Dashboard v2 and work pipeline](../../superpowers/specs/2026-08-21-dashboard-v2-and-work-pipeline-design.md)
- [Dashboard shell v2](../../superpowers/specs/2026-08-22-dashboard-shell-v2-design.md)
- [Command center unification](../../superpowers/specs/2026-08-30-command-center-unification-design.md)
- [Task graph spatial layout](../../superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md)
- [Live pane streaming](../../superpowers/specs/2026-08-25-live-pane-streaming-design.md)
  and the `2026-08-22-pane-*` designs
- [Dashboard state contract](../../superpowers/specs/2026-09-10-dashboard-state-contract-design.md)
- [Performance investigation](../../superpowers/specs/2026-09-04-dashboard-performance-investigation.md),
  [implementation](../../superpowers/specs/2026-09-04-dashboard-performance-implementation.md)
  and [measurements](../../superpowers/specs/2026-09-23-dashboard-performance.md)
- [Project folders and drag and drop](../../superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md)
- [Recent work and model attribution](../../superpowers/specs/2026-09-07-recent-work-and-model-attribution-design.md)
- [Tasks and knowledge detail pane](../../superpowers/specs/2026-10-05-tasks-knowledge-detail-pane.md)
- Vault: the specs and plans named in §6, and
  `notes/dashboard-perf-evidence-review-2026-09-26.md`

Source:

- `src/dashboard_server/`: `app.py`, `bundle.py`, `proxy.py`, `edge.py`, `settings.py`,
  `process.py`, `relay_stats.py`, `__main__.py`
- `src/cli/dashboard.py`, `src/doctor/dashboard_server_checks.py`
- `src/dashboard_state/`, `src/commands/dashboard_state_commands.py`,
  `src/database/queries/dashboard_state_queries.py`, `src/api/models/dashboard.py`
- `src/api/app.py`, `src/api/websocket.py`, `src/api/pane_stream.py`,
  `src/api/graph_layout.py`, `src/api/graph.py`, `src/api/middleware.py`, `src/api/auth.py`
- `src/panes/registry.py`, `src/remote_links.py`, `src/dashboard_paths.py`
- `dashboard/src/`: `main.tsx`, `App.tsx`, `shell/`, `api/`, `ws/`, `panes/`,
  `pages/command-center/layout-v2/`, `deviceLocal.ts`
