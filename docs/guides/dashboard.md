# Dashboard guide

The AQ dashboard is the browser workspace for seeing work move through a project and for operating the durable settings that shape that work. It is a React application served by the **dashboard server**, a small local process beside the daemon: it shows data and invokes the daemon's API; it does not run workers or own your project data.

## Why it exists

AQ can run many tasks and workers without a browser, but a terminal is a poor place to compare a project graph, watch a worker, inspect a worktree change, and respond to a decision. The dashboard makes those connections visible while retaining the command line for automation. Its current navigation and route redirects live in [dashboard/src/App.tsx](../../dashboard/src/App.tsx); the persistent chrome is [dashboard/src/shell/AppShellV2.tsx](../../dashboard/src/shell/AppShellV2.tsx).

> **Current scope.** Ships enabled: an installed AQ serves the built dashboard from the dashboard server at `http://127.0.0.1:8082/` ([src/dashboard_server/](../../src/dashboard_server/)). The daemon on port 8081 is **API only** — it serves no dashboard and no HTML except the plan viewer at `/plans/<task_id>`; `/openapi.json` remains available for API clients. Its old `/dashboard` path answers a `307` redirect to the same route on the dashboard server, with a JSON pointer body — or a `404` pointer when the dashboard server is disabled ([src/api/app.py](../../src/api/app.py)). A source checkout with no built bundle runs the Vite dev server on `http://localhost:5173` instead, which proxies the same paths. Neither is a hosted control plane: both listen on loopback by default. `VITE_API_URL` and `VITE_WS_URL` remain an optional compatibility escape hatch for a development build that must reach another daemon directly ([dashboard/src/api/client.ts](../../dashboard/src/api/client.ts), [dashboard/src/ws/useEventStream.ts](../../dashboard/src/ws/useEventStream.ts)); the release bundle is built with both unset, so it only ever talks to its own origin. The exact available projects, profiles, playbooks, access control, and policy are configured local state, not dashboard defaults.

## Vocabulary

* The **daemon** is the long-running `agent-queue` process that owns tasks, sessions and the database. It exposes an HTTP API (`/api`, `/health`, `/ready`, `/ws`, `/mcp`) on `mcp_server.port`, 8081 by default, and serves no dashboard. See [architecture](../concepts/architecture.md#two-processes-the-daemon-and-the-dashboard-server).
* The **dashboard server** is the separate process that serves the verified dashboard bundle and forwards the daemon's API paths to it, so the browser talks to a single origin. `aq start` and `aq stop` manage it alongside the daemon; `aq dashboard status` reports it.
* A **project** is AQ's record of a repository and its related work; the left rail can create one and select its workspace.
* A **task** is a tracked piece of work. A **graph** shows task relationships; the **Tasks** view is the filterable list form. See the [glossary](../reference/glossary.md) for the durable meanings of these terms.
* A **worker** is an agent process. **Agent flock** is the dashboard's name for the global workers and pools shared across projects, not a per-project task list.
* A **worktree** is the isolated checkout a task uses. The dashboard can preview its files and diffs; it does not make that checkout the browser's state.
* A **playbook** is an event-driven workflow definition. AQ's current playbook UI is in Settings; historic review, triage, and Discord-control screens are not current default navigation.
* A **pane** is a contextual right-hand surface, separate from the **Activity drawer**. `[` toggles a pane, `]` toggles the drawer, and `Esc` closes the open right surface.

## Opening the dashboard

`aq install` builds the dashboard, starts the dashboard server and opens `http://127.0.0.1:8082/` in your browser once, at the end of an interactive install. After that, `aq start` starts the daemon and then the dashboard server, `aq stop` stops both, and `aq restart` restarts both ([src/cli/daemon.py](../../src/cli/daemon.py)). To see where it is and whether it is up — this works with the daemon stopped:

```bash
aq dashboard status
```

```text
Dashboard server: running at http://127.0.0.1:8082/ (PID 7132)
  bundle 0.1.0 (42 files); proxying http://127.0.0.1:8081 (daemon answering)
  log: ~/.agent-queue/dashboard-server.log
```

`aq status` prints the same fact as its *Dashboard* line, and `aq --json status` carries it as a `dashboard_server` object whose `state` is one of `running`, `stopped`, `disabled`, `no_bundle`, `stale_pid`, `port_conflict`, `unresponsive` or `misconfigured` ([src/dashboard_server/process.py](../../src/dashboard_server/process.py)). `aq dashboard start`, `stop` and `restart` manage it on its own; `aq dashboard serve` runs it in the foreground with logs on stderr. The managed process writes `~/.agent-queue/dashboard-server.pid` and `~/.agent-queue/dashboard-server.log`.

Nothing restarts a crashed dashboard server by itself. `aq status` and `aq doctor` (`dashboard.server.running`, `dashboard.server.bundle`, `dashboard.server.port`, `dashboard.server.exposure`) report it, and `aq start` or `aq dashboard start` brings it back ([src/doctor/dashboard_server_checks.py](../../src/doctor/dashboard_server_checks.py)). It stays up when only the daemon is down, so the page still loads — but today it only shows *Preferences unavailable* and *Loading…* until the daemon answers again, rather than saying the daemon is unreachable.

Its host and port are the `dashboard.server` settings in `~/.agent-queue/config.yaml`; a changed value takes effect at `aq dashboard restart`, and the daemon needs no restart ([configuration reference](../reference/configuration.md#dashboard-server-settings)).

### Reaching it from another machine

The dashboard server listens on `127.0.0.1` by default, so nothing is reachable from another machine. The recommended way in from elsewhere is to keep that and forward the port over SSH:

```bash
ssh -L 8082:127.0.0.1:8082 <aq-host>
```

Then open `http://localhost:8082/` on your own machine. The browser's origin is then a loopback one, so everything — interactive terminals included — works with no configuration.

For direct access on a trusted LAN, bind the serve-mode dashboard to `0.0.0.0`
and list the exact browser origin in `api_auth.trusted_dashboard_origins`:

```yaml
dashboard:
  server:
    host: 0.0.0.0
    port: 5173
api_auth:
  trusted_dashboard_origins:
    - http://192.168.1.69:5173
```

Use your AQ host's LAN address in place of the example and keep any existing
trusted origins. Run `aq restart --no-dashboard` when changing origins, because
both the daemon and dashboard read them at startup. If only the dashboard bind
or port changes, `aq dashboard restart` is enough. Open the listed origin from
the other laptop; interactive terminals work through the same serve-mode proxy.
Behind WSL NAT, Windows must also forward that port to WSL and permit it through
the firewall. A trusted origin is required for remote terminal WebSockets even
when it matches the dashboard's bind address.

Windows `localhost` may also arrive through WSL's port forward as a non-loopback
peer. To use both Windows-local and LAN terminals, also list the exact local
origins you open, such as `http://localhost:5173` and `http://127.0.0.1:5173`.
This trust covers both the terminal WebSocket and its read-only reconnect access
check. A direct loopback connection within WSL needs no additional trust entry.

> **Warning.** Setting `dashboard.server.host` to a LAN address or `0.0.0.0` hands the operator console to that network. There is no login: a request without a bearer token runs with local-operator scope unless `api_auth.require_session_token` is on, so anyone who can connect to the port can create and delete tasks, type into agent sessions, read transcripts, panes and workspace files, and read (redacted) and write configuration. The Host and Origin checks stop *other websites' scripts*; they do not stop a person on that network with `curl`.

When the server is bound to a non-loopback address:

| | From another machine |
|---|---|
| **Reachable** | The static dashboard, `/health`, `/ready`, `/ws/events` and every `/api/**` route with local-operator scope. From a browser, only under an allowed `Host` and `Origin`: the address itself, the configured `dashboard.server.host`, or a name listed in `api_auth.trusted_dashboard_origins`. Any other `Host` gets `421 misdirected_host`; any other `Origin` gets `403 origin_not_allowed`. |
| **Interactive terminals** | Available when the browser's exact origin is listed in `api_auth.trusted_dashboard_origins`. Missing or unlisted origins are refused. |
| **Not reachable** | Any request carrying a bearer token (`403 loopback_only`); `/mcp`, the retired `/docs` and `/redoc` paths, `/openapi.json` and `/plans/*`; port 8081 itself, which stays on `mcp_server.host`; PostgreSQL; any file not listed in the bundle manifest. |

With `api_auth.require_session_token: true`, a LAN browser gets `401` on everything except the health paths, since it holds no token and the dashboard server refuses remote bearer tokens. `aq doctor --check dashboard.server.exposure` warns while a non-loopback bind is configured. The gates are in [src/dashboard_server/edge.py](../../src/dashboard_server/edge.py); the reasoning is in the [design record](../specs/dashboard-server.md) §3.

### Dashboard links in Discord posts

Escalation posts, the hourly digest and document-review announcements link into the dashboard. Every one of them names the same origin, chosen by [src/remote_links.py](../../src/remote_links.py):

1. **`dashboard.server.public_url`** (also accepted as `dashboard.public_url`; two different values are a configuration error). It must be an `http(s)` origin with no path, query, fragment or credentials, and never a loopback or wildcard address. It is used exactly as configured, with no Tailscale CLI involved.
2. Otherwise **`dashboard.server.host`**, but only when it is this machine's Tailscale address, as confirmed by `tailscale ip` within 2 seconds. The CLI comes from `PATH`, or from `dashboard.server.tailscale_path` when that is set.
3. Otherwise **no link**. The post reads `Remote dashboard link unavailable (<reason>; open it on the daemon host).`, and the reason names the setting to fix.

The daemon's own port (`health_check.base_url`, 8081) is never used, because it serves no dashboard pages. A loopback bind is never rewritten to a tailnet address either: being on a tailnet does not make a port bound to `127.0.0.1` reachable from your phone. A configured origin is also not a tested one, so AQ reports it as *remote reachability unverified* until you open it from the other device.

```bash
aq dashboard link                        # the origin, the key it came from, and the edge's verdict
aq dashboard link --json
aq doctor --check dashboard.remote_link
```

The daemon re-resolves the link at most every five minutes (thirty seconds after a failure), and at once when `dashboard.server` is edited; `public_url` needs no restart. A post that has already been sent keeps the link it was rendered with.

#### Which page a post links

The origin is only half of it. The path comes from
[src/dashboard_paths.py](../../src/dashboard_paths.py), which holds the whole
scheme: a task links `/focus/tasks/:id`, an incident links
`/focus/escalations/:id`, a review `/focus/reviews/:id`, the digest the
needs-you inbox `/focus/inbox`. Focus routes are phone-first and redirect to a
desktop page where one exists. A link is how the reader *acts* on a post, so no
post links a `/settings/` page: that asks a phone to configure the machine that
sent it.

Two rules hold across every producer, and
`tests/test_dashboard_links.py` pins them:

* **at most one link per post**, always the last line, bare and wrapped in
  `<…>` so Discord renders no preview card. The two rows that carry none are
  the collapsed escalation root and the replies inside an incident's thread,
  where the page is already linked above them;
* **the link is never cut.** If the envelope does not fit the post's budget, the
  text gives way first, because a link truncated mid-path points somewhere
  else.

Without an origin the last line is the resolver's notice naming the
configuration gap, never a guess.

#### Supported remote setup: an authenticated tailnet proxy you run

Keep the dashboard server on `127.0.0.1` and put an HTTPS reverse proxy that you operate in front of it, reachable only over your tailnet:

1. Point the proxy at `http://127.0.0.1:8082` on the AQ host.
2. Limit it with a tailnet ACL to identities you trust. The proxied console grants local-operator power with no login, and because the proxy connects from loopback it also passes the terminal peer gate. Anyone who reaches the proxy can type into agent sessions.
3. Name the proxy's exact origin in both settings:

   ```yaml
   dashboard:
     server:
       public_url: https://aq.your-tailnet.ts.net
   api_auth:
     trusted_dashboard_origins:
       - https://aq.your-tailnet.ts.net
   ```

4. Run `aq restart --no-dashboard`. `api_auth` is read at startup by both processes.
5. From another tailnet device, check through the proxy that the page loads, that `/health` answers, that the live event stream connects, and that a request with a different `Host` or `Origin` is refused (`421` or `403`). Until that check has passed, the setup is configured but unverified.

AQ never runs `tailscale serve`, never widens a bind and never changes network policy. Do not use Funnel or a LAN bind for this.

### From a phone

Open `/focus`, or **Focus view** in the rail. It is the same app laid out for a small screen, at every width ([FocusHome.tsx](../../dashboard/src/pages/focus/FocusHome.tsx)):

- **Live sessions**: each card opens the same terminal component as **Host shell**
  ([InteractiveTerminal.tsx](../../dashboard/src/components/InteractiveTerminal.tsx)).
  It has the same colors, fixed 12 px font, Type focus control and details menu
  on phones and desktops. There is no watch/type mode or extra phone toolbar.
  Columns and rows follow the terminal area, rotation and the on-screen keyboard;
  the agent's tmux window follows them. A compact attach restores the earlier
  window size when the phone leaves ([terminals reference](../reference/terminals-and-claims.md#from-a-phone)).
  Drag down to reach earlier output; a flick continues with momentum. Drag up
  to return to current output. Dropped connections retry automatically.
- **Typing from a phone**: tap the terminal or Type. xterm's own textarea takes
  keyboard input and paste, just as on the host shell. Enter and Ctrl+C are in
  the terminal's details menu. The visible grid shrinks above the keyboard.
  A refused attach reports its error and disables input. An ended session links
  to its transcript.
- **Providers**: the quota cards with their age; a stale reading says so ([ProviderUsage.tsx](../../dashboard/src/pages/metrics/ProviderUsage.tsx)).
- **Tasks**: the Tasks tab's list and filters as cards, 50 per page. A task opens as a full page; Back returns to the same page and scroll ([FocusTaskList.tsx](../../dashboard/src/pages/focus/FocusTaskList.tsx)).

Posted task and morning-report links open these pages ([src/dashboard_paths.py](../../src/dashboard_paths.py)); **Full** at the top opens the full dashboard page ([FocusShell.tsx](../../dashboard/src/pages/focus/FocusShell.tsx)). Below 768 px the rest of the dashboard is one column: the menu opens the rail as a drawer ([TopBar.tsx](../../dashboard/src/shell/TopBar.tsx)), panes and the activity list fill the screen, and the agents and session pages show the same shared terminal ([AgentTerminal.tsx](../../dashboard/src/pages/agents/AgentTerminal.tsx), [SessionDetail.tsx](../../dashboard/src/pages/SessionDetail.tsx)). Every viewport uses the host shell's terminal component, including focus view. Reaching the dashboard from a phone is the same question as from any other machine ([above](#reaching-it-from-another-machine)). Nothing the phone opens is saved to your roaming preferences.

#### Checking a release on a real phone

On iPhone Safari and Android Chrome, after an update that touched the dashboard. First note an agent's window size on the AQ host: `tmux display -p -t <session> '#{window_width}x#{window_height}'`.

Before the checklist: the browser is served a **built** SPA from `src/dashboard_assets/dist/`, never the dashboard source, so a dashboard source update also needs the build/staging step in `aq update` (or `aq install --restart-from dashboard.build`). `aq doctor --check dashboard.server.bundle` checks the bundle's manifest and base path; its OK result alone does not prove it matches the updated sources. Check the served assets after rebuilding.

1. `/focus` loads with no sideways scroll; sessions, providers and tasks show.
2. Open a live session, then open **Host shell** on the same phone. They are the same terminal, so the two screens are the same colours, the same font and the same column count; the terminal controls match as well; page headers can leave different amounts of space for the grid. The text fills the width without sideways scrolling, and on the host the window size now matches the phone.
3. Drag down: earlier output scrolls into view, a flick keeps going, and the page itself does not move. Keep going back past the first screen into older history. Drag up to return.
4. Rotate and rotate back: the columns and rows follow each rotation, the font stays the host shell's size, and the page stays.
5. Tap the screen or Type: the keyboard opens and all terminal rows fit above it, including on a full session page or docked pane. Type a short line and Enter; the agent receives both. Check Enter and Ctrl+C in the details menu. Paste two lines; xterm forwards the paste.
6. Airplane mode for ten seconds, then back online: the terminal reconnects by itself (or tap **Reconnect now**) and the history is there again, once.
7. Leave the session page. On the host the window is back to the size you noted.
8. Tasks page 2, scroll, open a task, Back: same page, same scroll; a long title wraps and nothing runs off the screen.
9. If posts carry links (`aq dashboard link`), a task link opens the focus task page.

## A realistic tour

Assume the daemon and dashboard server are running (`aq start`), the dashboard is open at `http://127.0.0.1:8082/`, a project named `demo` exists, and a task has been created for it.

1. Open **Command Center** in the left rail. AQ redirects this to the remembered project (or its first project), then opens **Graph**. Select a task node to read its task details in the contextual surface. Use the **Tasks** tab when you need search, state, or ownership filters rather than relationship geometry.
2. In **Projects**, choose `demo`; its row gives you **Graph** and **Tasks**. The same project route also has **Overview**, **Sessions**, **Workspaces**, **Playbooks**, and **Config** deep links. Project state is loaded from the daemon, so a refresh is safe when another operator changes it.
3. Open **Agent flock** from the rail. Select a worker to see its terminal and session information. Shift-click up to four agents to tile their windows. Pools show their global/project allocation rather than pretending each pool belongs only to the selected project.
4. From a task, open **Task files** / **Worktree preview** to inspect file content and a diff associated with that task. Treat it as an inspection surface: make and commit repository changes in the worker's worktree, not in the browser.
5. Open **Settings** for **Playbooks**, **Profiles**, **Intelligence classes**, **Project roots**, **Messaging**, and **Config**. These are curation surfaces: changing one writes daemon-managed configuration, vault content, or database records, so use the validation/error feedback before retrying.
6. Open **Metrics** for live and historical fleet measurements. A temporarily disconnected browser does not erase those durable samples; it reconnects and refetches. The live provider-usage display can label unavailable data rather than inventing a value.

For the tour, the inputs are a running daemon plus an existing project/task; the outputs are a project-scoped graph/list, task and worktree detail, agent/pool status, configuration forms, and metric charts. Navigation is intentionally URL-backed: opening `/projects/demo/graph` or `/projects/demo/tasks` reaches the same project workspace, while old URLs redirect to the current surfaces in [dashboard/src/App.tsx](../../dashboard/src/App.tsx).

```mermaid
flowchart LR
  Browser[Browser dashboard] -->|"/ and assets"| Server[Dashboard server :8082]
  Browser -->|"/api, /health, /ready, /ws"| Server
  Server -->|"same paths, byte for byte"| Daemon[AQ daemon :8081 API only]
  Daemon --> DB[(PostgreSQL)]
  Daemon --> Vault[Vault markdown]
  Daemon --> Workers[Worker sessions and worktrees]
  Workers --> Daemon
```

## Pages and what they are for

| Current label / route | Use it for | Main implementation |
|---|---|---|
| **Command Center** — `/projects/:projectId/graph`, `/tasks` | Visual task relationships or a filtered work list. Graph layout is server-produced; task selection opens detail. | [Graph.tsx](../../dashboard/src/pages/command-center/Graph.tsx), [Tasks.tsx](../../dashboard/src/pages/command-center/Tasks.tsx) |
| **Projects** in the left rail | Choose, organize, or add a project. Project-specific deep links retain their project scope. | [LeftRail.tsx](../../dashboard/src/shell/LeftRail.tsx), [ProjectTree.tsx](../../dashboard/src/shell/ProjectTree.tsx) |
| **Agent flock** — `/agents` | Inspect worker terminals, global agents, and worker pools; tile selected views. | [AgentWorkspace.tsx](../../dashboard/src/pages/agents/AgentWorkspace.tsx) |
| **Focus view** — `/focus` | A phone-first page: live sessions in the phone terminal, provider quota and the task list; task, session and report pages under `/focus/`. See [from a phone](#from-a-phone). | [FocusShell.tsx](../../dashboard/src/pages/focus/FocusShell.tsx), [FocusHome.tsx](../../dashboard/src/pages/focus/FocusHome.tsx) |
| **Task files** — `/tasks/:taskId/files` | Preview a task's worktree files and changes. | [TaskFiles.tsx](../../dashboard/src/pages/TaskFiles.tsx), [TaskFilesPanel.tsx](../../dashboard/src/components/TaskFilesPanel.tsx) |
| **Playbooks** — `/settings/playbooks` | Inspect and curate the active V2 playbook definitions; open a playbook detail or graph view when linked from the list. | [Playbooks.tsx](../../dashboard/src/pages/system/Playbooks.tsx), [PlaybookDetail.tsx](../../dashboard/src/pages/PlaybookDetail.tsx) |
| **Host shell** — `/host-shell` | An interactive login shell on the AQ machine for remote management, not an agent. Off unless enabled; see [host shell](#host-shell). | [HostShell.tsx](../../dashboard/src/pages/host-shell/HostShell.tsx) |
| **Metrics** — `/metrics` | Read fleet rate, capacity, and provider-usage charts. Each provider's card starts with its availability: a state pill, the reason, since when, the expected recovery, any operator override, the held and re-routed counts, and *Disable for…* / *Recheck* / *Clear override*. | [Metrics.tsx](../../dashboard/src/pages/metrics/Metrics.tsx), [ProviderAvailabilityHeader.tsx](../../dashboard/src/pages/metrics/ProviderAvailabilityHeader.tsx) |
| **Provider banner** — every page | Shown while any provider is unavailable (out of usage, logged out, failing or disabled), with what the outage moved and held and a link to that provider's card. | [ProviderAvailabilityBanner.tsx](../../dashboard/src/shell/ProviderAvailabilityBanner.tsx) |
| **Settings** — `/settings/*` | Configure profiles, intelligence classes, project roots, messaging, and system config. | [SettingsLayout.tsx](../../dashboard/src/pages/settings/SettingsLayout.tsx), [SettingsSidebar.tsx](../../dashboard/src/components/nav/SettingsSidebar.tsx) |
| **Activity drawer** and contextual panes | See recent dashboard events/gates or task-, session-, file-, and playbook-specific tools without leaving the current route. | [ActivityDrawer.tsx](../../dashboard/src/shell/ActivityDrawer.tsx), [panes/registry.ts](../../dashboard/src/panes/registry.ts) |

In Settings → Intelligence Classes, **Delete** confirms the class by name. The daemon refuses deletion while an active agent, an agent-type profile, or a non-terminal task references it and reports each blocker with repointing guidance. Successful deletion renames the vault file to `.md.retired` and updates the live registry. Global administrators can use `aq system delete-intelligence-class --class-id <id>` for the same operation; `--expected-revision` rejects a stale selection.

## Host shell

**Host shell** in the left rail opens a plain login shell (`$SHELL -l` in `$HOME`) on
the machine running AQ. It is not an agent: no task, no `sessions` row. Each shell is a
tmux session `aq-host-shell-<n>` on the daemon's tmux socket, so it survives page reloads
and is reattached from the page's tabs; **Close shell** kills it. The terminal is the
same `/ws/terminal/` stream agent terminals use.

It is remote code execution by design, so it is **off by default**:

```yaml
dashboard:
  host_shell:
    enabled: true
    max_shells: 4        # 1-32
    allow_remote: false  # also admit dashboards opened from another machine
```

The flags are read per request; no restart is needed.

- **Who may use it.** The local operator: a loopback peer with no bearer token, on a
  loopback Host or through the dashboard server's operator verdict (a loopback browser,
  or a tailnet browser on an `api_auth.trusted_dashboard_origins` origin). With
  `allow_remote: true`, also any browser the dashboard server proxied from another
  machine, such as a LAN browser on a trusted origin, which the server stamps as a
  non-operator viewer. The dashboard server's Host and Origin gates still apply, and a
  peer that reaches the daemon port directly is never a dashboard viewer.
- **Never a bearer token.** Any bearer token is refused whatever `allow_remote` says, so
  workers and the supervisor can never open, attach to or type into a host shell, and
  the dashboard server refuses a bearer token from a non-loopback peer outright. A
  DNS-rebound Host is refused, and a daemon with `api_auth.require_session_token: true`
  refuses host shells too.
- **Audit.** Every open and close is logged (`aq.audit.host_shell`) and recorded as a
  `host_shell.opened` / `host_shell.closed` event. Accepted terminal sockets also record
  `host_shell.attached` / `host_shell.detached`; input-only sockets record
  `host_shell.input_connected` / `host_shell.input_disconnected`. Each log line and event
  has a timestamp and the caller's identity:
  `local-operator (…)`, `local-operator via dashboard (peer …)` or
  `remote-dashboard-viewer (peer …)`, the peer being the browser's real address as the
  dashboard server saw it. Disconnects include backend errors and shutdowns. Terminal
  input is never audit-logged.
- **Environment.** The shell starts from `env -i` with only `HOME`, `USER`, `LOGNAME`,
  `PATH`, locale and `TZ`, so no `AQ_*` agent or session token, DSN or daemon secret
  reaches it; tmux global variables are also removed for the session's later windows.
- **Not counted anywhere.** The `aq-host-shell-` prefix is outside the `s-`/`n-`/`p-`
  prefixes session adoption, the reaper, the stall sweep, pool counts and the agent and
  session lists read, so a host shell never shows up in them.

API: `GET /api/host-shell` (list), `POST /api/host-shell` (open),
`POST /api/host-shell/{name}/close` ([src/api/host_shell.py](../../src/api/host_shell.py),
[src/sessions/host_shell.py](../../src/sessions/host_shell.py)).

## State ownership and live updates

The daemon is authoritative for all dashboard feature state that should survive a reload or appear on another browser. The dashboard uses `DashboardStateProvider` / `useDashboardDocumentState` to load a typed document, write it through the API, and replace cached values only with newer server revisions. There is no browser-storage fallback or import: stale keys from older dashboard versions are ignored.

| Ownership | Use it for | Current examples | Rule |
|---|---|---|---|
| Shared workspace | An operator's organization that every user should see | `nav_organization`: folders, assignments, project order | One workspace document; events refresh every dashboard. |
| Per-user roaming | A preference that follows the authenticated person across browsers and machines | `shell_preferences`, `command_center_preferences`, per-project command-center view, playbook graph view | The server derives the owner from authentication; only that user's dashboards receive the document. |
| Device-local transport | Recoverable connection bookkeeping that has no product meaning elsewhere | WebSocket replay cursor and epoch; console-stream identity guard | `src/deviceLocal.ts` is the only browser-persistence boundary. Absence means reconnect/refetch, never a preference reset. |
| Ephemeral | Current interaction, route, draft, cache, or mounted component state | URL navigation, selection, open drawer, React Query cache, drag preview | Keep it in the URL or memory; discard it on reload and do not synchronize it. |

API mutations and queries go through the generated TypeScript client configured by [dashboard/src/api/client.ts](../../dashboard/src/api/client.ts), usually wrapped by [dashboard/src/api/hooks.ts](../../dashboard/src/api/hooks.ts). PostgreSQL owns task, session, project, metric, audit, and dashboard-state data; vault markdown owns authored configuration such as playbooks and profiles; worker sessions and worktrees own terminal processes and checked-out files. The dashboard never substitutes local UI state for a successful daemon write.

[dashboard/src/ws/EventStreamProvider.tsx](../../dashboard/src/ws/EventStreamProvider.tsx) keeps a bounded activity buffer while [dashboard/src/ws/useEventStream.ts](../../dashboard/src/ws/useEventStream.ts) maintains one reconnecting `/ws/events` connection and invalidates relevant cached queries. It stores a replay sequence and server epoch: after a daemon epoch changes, it clears an unusable cursor and reconnects. Metrics deliberately use a raw subscription so their one-second ticks do not churn every query cache.

### Adding dashboard state

Classify a field before writing code. If it must roam, add it to the dashboard-state registry and its typed server/client document; choose workspace versus user ownership, and project subject versus global scope. Keep independent concurrency units in separate namespaces. Add the default, validation, generated API union, event handling, and a two-context test that proves load, live propagation, reload/reconnect, reset, and the relevant user/project isolation. If it is only transport recovery, add a narrowly justified `DEVICE_LOCAL_KEYS` entry and its storage-guard documentation; ordinary remembered UI choices do not qualify. URL-addressable navigation belongs in the route, and drafts, selections, animations, and caches remain ephemeral. The detailed registry and add-a-namespace checklist are in the [dashboard-state contract](../superpowers/specs/2026-09-10-dashboard-state-contract-design.md).

## Common failures and recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| `http://127.0.0.1:8082/` refuses connections | The dashboard server is not running: it crashed, `aq start --no-dashboard-server` skipped it, or it could not start. | `aq dashboard status` names the state and the fix; `aq dashboard start` starts it. Startup problems are in `~/.agent-queue/dashboard-server.log`. |
| `aq dashboard status` says `port conflict` | Another program answers on the dashboard server's port. The port is never auto-incremented, so the URL stays predictable. | Free the port, or set `dashboard.server.port` and run `aq dashboard restart`. |
| `aq dashboard status` says `no bundle` | No built dashboard is installed — typically a contributor checkout. | `aq install --restart-from dashboard.build` builds one; in a checkout you develop in, run `npm -w dashboard run dev` instead. |
| The page loads but stays on *Loading…* and the header says *Preferences unavailable* | The dashboard server is up and the daemon is not: proxied requests answer `503` `daemon_unreachable` (`aq dashboard status` says `the daemon is not answering it`). | `aq status`, then start the daemon. The page recovers by itself once the daemon answers. |
| After a daemon crash, `aq start` says `Daemon is already running (PID …)` but port 8081 does not answer | A [known issue](../release-notes.md#known-issues) (`smart-meadow.7`): with no daemon PID file, `aq start` mistakes the running dashboard server for the daemon. | Run `aq restart`, which stops the dashboard server first and then starts both. Do not use `aq stop --no-dashboard-server` while the daemon is down — it stops the dashboard server instead. |
| An old `http://127.0.0.1:8081/dashboard/` bookmark lands on `http://127.0.0.1:8082/` | Expected: the daemon used to serve the dashboard there and is API only now, so it redirects (`307`) to the dashboard server. | Update the bookmark. If the redirect's target refuses connections, the dashboard server is not running — see the first row of this table. |
| Opening `http://127.0.0.1:8081/dashboard/` shows JSON with `dashboard_not_served_here` and `"dashboard_url": null` | `dashboard.server.enabled` is `false`, so there is no dashboard server to redirect to. | Set it to `true` and run `aq start`, or serve the dashboard yourself (Vite or any server that honours the proxy contract). A client that does not follow redirects, such as `curl` without `-L`, sees the same JSON with the URL filled in. |
| A LAN or DNS name gets `421 misdirected_host` or `403 origin_not_allowed` | The dashboard server answers only for loopback names, its configured host, and the origins in `api_auth.trusted_dashboard_origins`. | Prefer `ssh -L` ([reaching it from another machine](#reaching-it-from-another-machine)); otherwise add the exact origin to `api_auth.trusted_dashboard_origins` and run `aq dashboard restart`. |
| Page says it cannot load data in a source checkout | Vite's proxy target is not the daemon you intended. | Start/check the daemon, then check `AQ_API_TARGET` for development or `VITE_API_URL` for the optional remote target. [dashboard/vite.config.ts](../../dashboard/vite.config.ts) is the source of the proxy default. |
| `localhost:5173` refuses connections in a source checkout | The Vite dev server is not running, dependencies are absent, or its port is occupied. | From the repository root run `npm install`, then `npm -w dashboard run dev`; inspect `~/.agent-queue/dashboard.log` when `aq start` launched it. [src/cli/daemon.py](../../src/cli/daemon.py) records the launcher behavior. |
| A newly changed screen looks stale | Browser cache, a dashboard server still serving the previous build, or a stale development server. | Reload first. `aq dashboard status` says `serving an older build` after a rebuild the server has not picked up; run `aq dashboard restart` (`aq update` does this for you). In a source checkout, restart `npm -w dashboard run dev`. |
| Activity stops updating | The `/ws/events` connection is disconnected, often because the daemon was restarted or a proxy does not forward WebSockets. | Check browser network errors and daemon health; refresh after restoring the endpoint. The client reconnects with backoff and discards a stale replay cursor after an epoch change. |
| Terminal does not accept input or shows an error | The session is gone, the current actor lacks permission, the terminal WebSocket cannot reach the daemon, or a remote browser's origin is not trusted. | Re-open the agent/task session, confirm the daemon is reachable, and inspect the task/session state in AQ. For direct LAN access, list the exact browser origin in `api_auth.trusted_dashboard_origins` and restart AQ; an SSH tunnel also works. Do not treat terminal output as a successful task close; the daemon's task record is authoritative. |
| A banner says a provider is unavailable, or a task shows *Held by provider* | AQ stopped launching against that provider. Queued work on it is being moved to the same class elsewhere, or held (a pin, a single-provider class such as `astra-*`, or no capacity yet). A task's detail shows its provider intent and any "re-routed from … · undo". The Tasks tab's *Held by provider* filter lists what is waiting. | Follow [a provider ran out of usage](provider-outage.md). Every state shown is the daemon's own; refreshing will not change it, but `aq provider recheck --provider <p>` or the card's *Recheck* will. |
| A form save fails | Server validation or an optimistic assumption was rejected. | Read the displayed API error, correct the input, and retry. Refresh the route if another user changed the same record. Do not edit browser storage to repair server state. |

## For contributors

### Architecture and API boundary

The entry chain is [dashboard/src/main.tsx](../../dashboard/src/main.tsx) → [dashboard/src/App.tsx](../../dashboard/src/App.tsx) → [dashboard/src/shell/AppShellV2.tsx](../../dashboard/src/shell/AppShellV2.tsx). React Router owns routes; TanStack Query owns cached API data; the pane store and right-surface provider own transient contextual UI. Pages compose components and hooks, but backend calls belong in `dashboard/src/api/` through the generated `@aq/ts-client` export. The older [legacy-fetch.ts](../../dashboard/src/api/legacy-fetch.ts) exists only for endpoints absent from the generated client; new typed endpoint work should extend the API/code-generation boundary rather than add direct `fetch`.

The browser always talks to one origin, and two processes can be that origin:

* **Development.** Vite serves the app on port 5173 and proxies `/api`, `/health`, `/ready`, and `/ws` to `AQ_API_TARGET`, which defaults to `http://127.0.0.1:8081` ([dashboard/vite.config.ts](../../dashboard/vite.config.ts)). `npm -w dashboard run dev` runs it. In a source checkout with no built bundle, `aq start` offers to launch it (`--no-dashboard` skips the offer); that launcher is a convenience, not an asset server.
* **Installed.** [`scripts/build_release_artifact.py`](../../scripts/build_release_artifact.py) builds the app with Vite `base: "/"` and every `VITE_*_URL` unset, and stages it with a SHA-256 manifest (`"base": "/"`) in `src/dashboard_assets/dist/`. The dashboard server ([src/dashboard_server/](../../src/dashboard_server/)) verifies that manifest at startup and fails closed — a missing, altered or daemon-mount-era bundle exits instead of serving — then serves only manifest-listed files with SPA fallback and relays the same four path prefixes to the daemon: request and response bytes unchanged, SSE streamed chunk by chunk, WebSocket frames and subprotocols (the terminal's `aq-terminal-v1`) relayed one to one. The daemon's non-dashboard paths and retired docs paths (`/mcp`, `/docs`, `/redoc`, `/openapi.json`, `/plans`, `/dashboard`) answer `404` there, never the SPA's `index.html`.

The daemon registers no static files at all; [tests/test_api_dashboard_pointer.py](../../tests/test_api_dashboard_pointer.py) pins that. The dashboard server imports only `src.config` from AQ — never the API, orchestrator, database or command layers ([tests/test_dashboard_server_app.py](../../tests/test_dashboard_server_app.py) checks it from a subprocess) — and needs no secret, so `aq dashboard start` launches it with database URLs and provider keys stripped from its environment.

The dashboard package's `predev`, `prebuild`, and `pretypecheck` hooks regenerate the TypeScript API client. After changing a codegen API surface, use the repository's offline generation workflow documented in [Code generation](../contributing/codegen.md), then typecheck. Do not hand-edit generated client output.

### Page and component families

* `pages/command-center/` renders project graph/list workspaces, including the server-backed layout-v2 canvas and live graph refresh.
* `pages/agents/` renders the global flock, workers, terminals, and pool configuration. `components/InteractiveTerminal.tsx` is the same shared terminal used by Host shell and every agent terminal route.
* `pages/project/` provides project overview/configuration/workspace/onboarding surfaces; `pages/settings/` and `pages/system/` are settings curation routes and legacy-compatible page implementations.
* `pages/focus/` is the phone-first focus view (`/focus`): its own shell, home page, and task, session and report pages. It reuses the Tasks tab's rows and `components/InteractiveTerminal.tsx`, including the host shell's attach socket, palette, options, fit logic, input handling and touch scrolling.
* `pages/metrics/` turns durable metric queries plus a raw event feed into chart data; `pages/playbook-graph-v2/` renders the semantic playbook graph and run overlays.
* `components/` contains shared task, modal, markdown, terminal, profile, navigation, and workspace controls. `shell/` is global navigation, keyboard shortcuts, palette, activity drawer, and right surface.
* `panes/` registers contextual, lazy-compatible views; every pane has a manifest and arguments/state owned by the pane store. `ws/` owns the shared event and terminal sockets, not individual pages.
* [src/editor/](../../src/editor/) is an assigned backend utility for a separate voxel-level editor model and brush operations. It has no current import from `dashboard/src`; the catalog keeps that boundary explicit instead of implying it ships in the Vite bundle.

The exhaustive source-to-family map, including nested helpers and focused Vitest locations, is the [dashboard module catalog](../reference/modules/dashboard.md).

### Focused checks

The following commands are the dashboard's local checks; run them from the repository root after dependencies and the generated client are available.

```bash
npm -w dashboard run lint
npm -w dashboard run typecheck
npm -w dashboard run test
npm -w dashboard run build && npm -w dashboard run check:layout
```

The last line checks the layout at phone and desktop sizes in headless Chrome against a built bundle ([dashboard/layout-checks/README.md](../../dashboard/layout-checks/README.md)). Use a focused Vitest file while iterating, for example `npm -w dashboard run test -- src/pages/metrics/__tests__/Metrics.test.tsx`, then run the relevant family suite once before delivery. The catalog names co-located tests for each family. See [Local checks](../contributing/checks.md) for the maintained check matrix and [dashboard/AGENTS.md](../../dashboard/AGENTS.md) for the React/Vitest isolation and worker-cap conventions.

## Related pages

* [First task](../tutorials/first-task.md) — create the project and task that make the tour concrete.
* [Project onboarding](project-onboarding.md) — creates and recovers the durable project/workspace state behind the project screens.
* [Task state machine](task-state-machine.md) — explains task state before using Command Center filters.
* [Playbooks V2](../concepts/playbooks.md) — explains what the Settings Playbooks surface configures.
* [Escalations and the hourly digest](escalations.md) — distinguishes the current Messaging/Inbox flow from retired Discord controls.

## Source and tests

Primary sources: [dashboard/src/App.tsx](../../dashboard/src/App.tsx), [dashboard/src/api/client.ts](../../dashboard/src/api/client.ts), [dashboard/src/ws/useEventStream.ts](../../dashboard/src/ws/useEventStream.ts), [dashboard/vite.config.ts](../../dashboard/vite.config.ts), [dashboard/package.json](../../dashboard/package.json), [src/dashboard_server/](../../src/dashboard_server/), [src/cli/dashboard.py](../../src/cli/dashboard.py), [src/cli/daemon.py](../../src/cli/daemon.py), the focus view in [dashboard/src/pages/focus/](../../dashboard/src/pages/focus/), and the browser layout checks in [dashboard/layout-checks/](../../dashboard/layout-checks/).

The dashboard server and the daemon's pointer are covered by:

```bash
aq test tests/test_dashboard_server_app.py tests/test_dashboard_server_bundle.py \
        tests/test_dashboard_server_edge.py tests/test_dashboard_server_proxy.py \
        tests/test_cli_dashboard_server.py tests/test_doctor_dashboard_server.py \
        tests/test_api_dashboard_pointer.py
```

Focused frontend tests include [dashboard/src/App.navigation.test.tsx](../../dashboard/src/App.navigation.test.tsx), [dashboard/src/shell/LeftRail.addProject.test.tsx](../../dashboard/src/shell/LeftRail.addProject.test.tsx), [dashboard/src/ws/__tests__/useEventStream.wire.test.tsx](../../dashboard/src/ws/__tests__/useEventStream.wire.test.tsx), [dashboard/src/pages/metrics/__tests__/Metrics.test.tsx](../../dashboard/src/pages/metrics/__tests__/Metrics.test.tsx), and the co-located suites listed in the [module catalog](../reference/modules/dashboard.md).
