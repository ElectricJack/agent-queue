# System spec: dashboard

Status: audited on 2026-10-22 against `origin/dev` at `52d2efe4c`. This is system 15
of the eighteen in the agent-queue system inventory (vault
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
  API requests fail until the daemon returns. The shell currently shows “Preferences
  unavailable” or “Loading…” rather than a dedicated offline explanation.

The dashboard computes no policy and owns no work state. Product mutations normally go through
the API to `CommandHandler`, like the CLI, MCP and Discord; direct service operations
are noted in §2.4 and §5.

## 2. How it works today

### 2.1 Vocabulary

- **Dashboard server**: the separate process (`src/dashboard_server/`) that serves the
  built React app and proxies API traffic to the daemon. Default `127.0.0.1:8082`.
- **Daemon API**: the daemon's HTTP and WebSocket surface on port 8081. Since the
  API-only split it serves no dashboard. The HTML plan viewer at `/plans/<task_id>`
  remains, as does `/openapi.json`; Swagger and ReDoc are disabled.
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
- **Responsive shell**: the same dashboard at every width; below 768 px navigation is a menu and detail panes are full-screen sheets.

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
  - `edge.py` gates proxied daemon paths: Host check (421), Origin check (403), and
    `403 loopback_only` for any non-loopback request carrying `Authorization` or an
    `aq-bearer` WebSocket subprotocol. It **refuses remote bearer credentials**; it does
    not require them. Tokenless traffic reaches the daemon's local-operator scope unless
    `api_auth.require_session_token` is enabled. A wildcard bind does not allow arbitrary
    Host names: loopback names, the configured concrete host and trusted-origin hostnames
    are allowed. A supplied Origin must match the request origin or a trusted origin.
    Remote browser terminals additionally require an exact trusted origin at the daemon.
  - Pull-request approval is `operator_only`: loopback, or a tailnet peer
    (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) using a trusted browser origin. The server
    replaces `x-aq-dashboard-viewer` and `x-aq-dashboard-peer` on forwarded requests;
    the daemon trusts this verdict only from its loopback proxy.
  - Static bundle files and `/__aq/health` (identity and `upstream_ok`) and
    `/__aq/metrics` (relay statistics) bypass those proxy edge checks.
  - Upstream failures map to 503 `daemon_unreachable` (with `Retry-After: 2`), 504
    `daemon_timeout` and 502 `daemon_bad_response`. There is no dedicated frontend
    offline banner yet.
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
runtime validation. Tests are Vitest 4. [`dashboard/AGENTS.md`](../../../dashboard/AGENTS.md) holds
the rules for working in it.

The start-up chain is `main.tsx` → a `QueryClient` (30 s stale time) → the router →
`EventStreamProvider` → `DashboardStateProvider` → `App.tsx` →
`shell/AppShellV2.tsx`. Below 768 px the shell switches to its compact layout.

Routes, by area:

| Area | Routes |
|---|---|
| Requests | `/inbox`, `/asks/:id`, `/escalations/:id` |
| Fleet | `/agents` (including `?view=providers`), `/conversations`, `/waiting`, `/metrics`, `/host-shell`, `/reviews`, `/reviews/:id` |
| Project | `/projects/:id/graph`, `tasks-knowledge`, `overview`, `sessions`, `workspaces`, `playbooks`, `config` |
| Objects | `/tasks/:id`, `/tasks/:id/files`, `/sessions/:id`, `/playbooks/:id`, `/reports/:id`, `/batches/:id`, `/projects/:id/knowledge/:recordId` |
| Settings | `/settings/*` |
| Old URLs | `/focus/*` redirects to the corresponding full-dashboard route, preserving query and fragment |

The [module reference](../../reference/modules/dashboard.md) catalogues the directories.

### 2.4 Reading data

The supported API boundary is the generated TypeScript client `@aq/ts-client`
(`packages/aq-ts-client/src`, gitignored, regenerated from `openapi.json` by the
`predev`, `prebuild` and `pretypecheck` scripts). `dashboard/src/api/client.ts` wraps it
and `api/hooks.ts` turns calls into TanStack Query hooks. `legacy-fetch.ts` exists for the
non-API paths such as `/health`, `/ready` and `/plans`. Several older direct
`fetch` calls remain (§5). The
[codegen guide](../../contributing/codegen.md) covers the pipeline from `src/api/spec.py`
to both clients.

Most endpoints come from command contracts and reach `CommandHandler`. A few routers are
hand-written and read the database or services directly: `src/api/graph.py`,
`graph_layout.py`, `metrics.py`, `providers.py`, `pull_requests.py`, the stream endpoints
and the file endpoints. Provider overrides and pull-request approval dispatch commands;
`graph_layout` also exposes layout jobs and dispatches `graph_tidy` for tidy. Host-shell
open/close (`src/api/host_shell.py`) call the shell service directly, an exception to the
command boundary (§5).

**Authentication.** `src/api/middleware.py:43-118` gives a tokenless request the local
scope unless `api_auth.require_session_token` is set. The dashboard has no login; behind
the dashboard server's edge gate tokenless API calls use the single local-operator
identity. Sensitive operations can additionally require the proxy's operator verdict,
and terminals enforce origin/peer rules; this is not multi-user login.

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
finds any left behind. List synthesizes defaults for the four global namespaces;
project-view documents are listed only after materialization.

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
`panes/store.tsx` holds one open pane. Desktop user-opened panes persist in
`shell_preferences.right_surface.pane`; widths and right-surface kind/activity tab also
roam. Compact views do not write their surface changes or restore the desktop surface
on initial load. Agent-pushed panes are
ephemeral. Tasks & Knowledge uses route-owned detail alongside its list. The pane designs are the
`docs/superpowers/specs/2026-08-22-pane-*` documents and
[live pane streaming](../../superpowers/specs/2026-08-25-live-pane-streaming-design.md).

The server keeps a mirror of pane ids and `agent_pushable` flags in `src/panes/registry.py`
(`SERVER_PANE_REGISTRY`); `tests/test_pane_registry_parity.py` keeps the two in step. Its
one use is validating a `pane_open` action on `message send`
(`src/commands/message_commands.py:506-563`), which lets a user-directed agent message open a pane automatically through
`panes/agentPush.ts`. Argument schemas are validated in the browser, not by the server
mirror. There is no server-side pane plugin interface.

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
the task list view. Tasks & Knowledge (`pages/records/RecordsRoute.tsx`) is the
canonical list/detail route; old task/knowledge/records links preserve query parameters
when redirecting. Global command-center URLs currently select the remembered/first
project. Scroll/pinch zoom is enabled. Task nodes are fixed by layout; only playbook
cards can be dragged. Container activity and delivery progress are displayed separately.

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

- **Vitest**: co-located suites under `dashboard/src`, run locally with
  `npm --prefix dashboard test`. Flakiness rules are in
  [the Vitest flakiness note](../../superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md).
- **Browser checks**: `dashboard/layout-checks/` runs checks against a stub daemon,
  including axe accessibility checks.
- **Python**: `tests/test_dashboard_server_{app,bundle,edge,proxy}.py`,
  `test_cli_dashboard_server.py`, `test_doctor_dashboard_server.py`,
  `test_dashboard_state.py`, `test_dashboard_browser_storage.py`,
  `test_dashboard_secure_context.py`, `test_api_dashboard_pointer.py`,
  `test_pane_registry_parity.py`, `test_dashboard_links.py`, `test_api_client_contract.py`.
- **Performance**: `scripts/dashboard-perf/` and `tests/perf/test_layout_api_statements.py`.
- **CI**: `.github/workflows/tests.yml` runs `npm ci`, the typecheck and the production
  build for `dashboard/`. It does not run Vitest or the browser checks (§5).

### 2.12 Waits and delivery latency

Wait explanations and metrics are shipped at this baseline (`4406ff7b5`, `4168e6312`,
`8d800678a`), as is delivery latency (`754bc111c`); these are not planned-only features.
`components/TaskWaitPanel.tsx` shows verified blockers separately from claimed context,
priority inheritance and evidence. `/waiting?project=:id` shows wait chains, deadlocks,
escalations, branch blockers and held-message backlog, with truncation labelled.
Task filters include `WAITING`, `HIBERNATED`, `RESUME_PENDING` and derived delivery/withheld
filters. Fleet metrics show wait counts, slot held/released time, wake outcomes, resume
latency and counterfactual token savings. Wait pages poll as well as use event invalidation;
`wait.`, `continuation.` and `reservation.` are absent from the WebSocket forward-prefix
list at this baseline (§5).

`pages/metrics/DeliveryLatency.tsx` renders `/metrics#delivery`: a 24-hour/7-day cohort,
project and task filters, delivered median/p90 and coverage, pending/unknown work, slow
live waits versus stuck generations, and CI/train/repair/author timing segments. It
refreshes every minute and labels stale/error/missing data. Drill-down links timing
anchors and retained/re-written delivery state to CI, wait evidence, task Explain and
project waits. Segments can overlap; deployment-after-dev is explicitly time-based, not
proof of commit containment. See the [guide](../../guides/dashboard.md#waiting-and-delivery-measurements).

## 3. Boundaries

| System | What crosses the boundary |
|---|---|
| daemon-core | Product writes normally reach `CommandHandler`; direct host-shell service calls are an exception (§5). The orchestrator cycle runs the off-cycle layout step; the event bus feeds `/ws/events`. |
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
| test-service-and-jobs | The console-stream pane can show managed job output. Vitest runs outside `aq test`; CI runs the typecheck and build. |
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
- **Command-backed product writes.** This is the intended boundary, so the CLI, MCP
  and Discord can share validation. Direct service exceptions are recorded below.
- **The page outlives the daemon.** The dashboard server has no database access and
  almost no daemon imports, so it starts, serves and reports `daemon_unreachable` while
  the daemon restarts.
- **The server serves only what it verified.** A bundle whose digests do not match the
  manifest is not served.
- **Remote bearer credentials are refused.** Host and Origin gates protect proxied
  daemon paths. Static files and server diagnostics bypass this gate. Tokenless remote
  access has local-operator API scope unless daemon token enforcement is enabled;
  terminal origins and operator-only approval have additional checks.
- **Generated client boundary.** New endpoint calls belong in `@aq/ts-client`; older
  raw-fetch exceptions remain below.
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
  interface (§2.7). Its module count is a dated snapshot rather than a current source count.
- The inventory names two vault files that do not exist. The feedback spec is
  `specs/2026-10-03-aq-dashboard-feedback-ideas-and-versioned-releases.md`, and the
  performance plan is
  `plans/2026-09-25-dashboard-performance-under-load-and-separation-plan.md`.
  `2026-09-26-mobile-dashboard.md` is a plan; the spec it implements is
  `specs/2026-09-24-mobile-dashboard-2.md`.
- Stale comments: `AppShellV2.tsx:255`, `pages/command-center/Graph.tsx:26` and
  `api/graph.ts:47` describe arrangements that have since changed. `IntelligenceClassesStub`
  is a working settings page despite its name.
- `aq restart --no-dashboard` sounds like it leaves the dashboard server alone; it only
  skips the Vite dev-server prompt. `--no-dashboard-server` is the flag that does.

**Behaviour.**

- **Vitest does not run in CI.** The suites are checked only when someone runs
  them locally, and the browser checks never run in CI either.
- **Direct `fetch` calls** break the generated-client rule:
  `pages/settings/ProjectRoots.tsx:63`, `panes/console-stream/index.tsx:56`,
  `panes/console-stream/hooks.ts:112`. Hosted-doc fetching in
  `panes/spec-doc-reader/hooks.ts` is an intentional non-command exception;
  `api/activity.ts` already uses the client and is not a raw-fetch gap.
- **The command boundary has exceptions.** `src/api/host_shell.py` opens and closes
  shells through the service directly rather than `CommandHandler`.
- **Wait events are not forwarded.** Client invalidation handles `wait.`, `continuation.`
  and `reservation.`, but `src/api/websocket.py` does not forward those prefixes. Polling
  is the backstop; immediate wait updates can be missed.
- **All-projects list navigation redirects to a project.** `workspaceHref` can build
  `/command-center/tasks-knowledge`, but `CommandCenterRedirect` selects a project even
  for that tab. The current guide describes that observed behavior.
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
- **Single operator identity.** There is no per-person login: tokenless viewers share
  the local-operator document owner. Sensitive operations impose additional gates.
- **The last located performance evidence missed the envelope.** The evidence
  review of 2026-09-26 (vault `notes/dashboard-perf-evidence-review-2026-09-26.md`) found
  cold Tasks at 2,860 ms and cold Metrics at 6,342 ms against 2,000 ms, `agent/list` p95
  near 1,018 ms against 500 ms, loop lag p95 123.7 ms against 50 ms with a 2,870 ms worst
  stall; task detail passed at 159 ms, and it found no case for a separate read process.
  The remediation tasks (`wise-ember.15`–`.19`: route labels, blocking spans, cold pages)
  were recorded as completed in the previous system audit, but no later
  measurement was found. The 0.65 s `pool/status` figure is also historical evidence,
  not a current measurement.

## 6. Design work beyond this baseline

This is a repository audit, not a live task-queue report. The 2026-10-20 inventory's
branch/task statuses are historical. Wait explanations, wait metrics and delivery
latency have landed and are described in §2.12. The following design material is useful
context but must not be read as evidence that its entire proposed UI has shipped:

- **Held-ingestion fidelity review.** Vault
  `specs/2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`
  refines `specs/2026-10-13-spec-ingestion-with-task-structure-review-before-release.md`.
  The held-batch task-detail section and proposal-preview fidelity mode described in
  the previous audit are absent from the audited frontend. Backend batch APIs do not
  by themselves establish that the UI exists.
- **Task graph redesign.** Vault `specs/2026-10-06-task-graph-ui-redesign-2.md` describes
  later phases beyond the current tokens, card/frame anatomy, status/reason stripes and
  edge legend. Scroll/pinch zoom and theme support are already present. Treat later
  edge/chrome/engine proposals as design context, not current behavior.
- **Feedback, ideas and versioned releases.** Vault
  `specs/2026-10-03-aq-dashboard-feedback-ideas-and-versioned-releases.md` and
  `plans/2026-10-03-implementation-plan-aq-dashboard-feedback-and-release-histor.md`
  propose Feedback/What's-new panes, an ideas lifecycle and seen-state. Those panes
  are absent from the current pane registry.
- **Performance under load.** Vault
  `specs/2026-09-24-dashboard-performance-under-load-and-separation.md` and
  `plans/2026-09-25-dashboard-performance-under-load-and-separation-plan.md` framed
  static separation and latency targets. Static separation is shipped; the old
  measurements in §5 do not establish current performance or a separate read process.

Mobile behavior and Tailscale link resolution are shipped; use the guide for their
current contract. Their earlier vault drafts are historical, as are the August shell
and pane designs. The September state-boundary inventory is explicitly a pre-migration
snapshot; the implemented state contract and current namespace models take precedence.

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

### 2026-10-25: One dashboard at every width

Focus is retired. Previously posted `/focus` links remain client redirects:
home → Command Center (honoring `project`), inbox → `/inbox`, objects → their
same paths without `/focus`, and conversations → `/conversations?conversation=:id`.
Unknown Focus paths return to Command Center. Redirects replace the history entry.
Backend notifications use full-dashboard paths and doctor probes `/`.

Pre-removal feature inventory and destinations:

| Former Focus feature | Shared destination |
|---|---|
| Tasks, filters, pagination and related tasks | Tasks & Knowledge and `/tasks/:id` |
| Live agents and pool workers | `/agents` and project sessions |
| Provider quota and age | Shell usage bars and `/metrics` |
| Needs-you, waiting, history and pagination | `/inbox`; shared AskInbox in Activity |
| Quick answers, review discussion, decision preview/receipt | `/asks/:id`; review/escalation aliases resolve the canonical ask |
| Conversation history, author/state and quick replies | `/conversations` picker and shared chat composer |
| Session terminal, process pin and explicit restart acknowledgement | `/sessions/:id?started=:epoch`; ended sessions keep transcript and current-agent link |
| Morning report and task evidence | `/reports/:id` |
| Batch delivery state, PR and member tasks | `/batches/:id` |
| Back, menu, compact detail sheets | Shared shell navigation/history |

Phone acceptance uses the built bundle at 390 CSS pixels: navigation collapses,
page content stays within the viewport, primary controls have 44px touch targets,
and task, ask/review, session and conversation content remains readable. There is
no alternate phone mode or terminal link mode.
