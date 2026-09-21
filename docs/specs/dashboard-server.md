---
tags: [spec, dashboard, daemon, api, installer, security]
---

# Dashboard server process and API-only daemon

<!-- aq:historical -->
> **Design record — implemented 2026-09-21.** Written on 2026-09-20 against
> `main` `0ad341dfa`, before the code. It is not revised to track the code
> afterwards; where this page and the code disagree, the code is right. The
> daemon first shipped `/dashboard` as a `404`, and a pre-change install's first
> `aq update` paid exactly the cost §5 names for that choice; since
> `smart-meadow.9` it answers the `307` §5 decides — see the
> [release notes](../release-notes.md). What ships is described
> by the [dashboard guide](../guides/dashboard.md) and
> [architecture](../concepts/architecture.md#two-processes-the-daemon-and-the-dashboard-server);
> start at [the documentation home](../README.md) for the rest.

**Decision owner:** Jack, 2026-09-20 — the daemon goes back to exposing an API and
nothing else, and the one-command install keeps ending at an open dashboard.
This spec decides how; the implementation tasks follow it.

## 0. Summary

Since 2026-09-16 the daemon mounts a verified dashboard bundle at `/dashboard`
([`src/dashboard_assets/runtime.py`](../../src/dashboard_assets/runtime.py),
called from [`src/api/app.py`](../../src/api/app.py)) as an install convenience.
A separate **dashboard server** replaces it: a small process that serves the
verified bundle with SPA fallback and reverse-proxies the daemon's API, which is
what the Vite dev server does in a source checkout. The browser stays
same-origin, nothing gains CORS, and the daemon stops serving browser content.

**The proxy shape holds.** JSON, multipart upload, three SSE streams and two
WebSockets — including the binary terminal with its subprotocol and credit-based
flow control — all relay cleanly, on one condition: the WebSocket handshake is
opened upstream *first* (§2.4). Two findings changed the brief: a proxy hides the
browser's address from the daemon, so two loopback-only rules are re-applied at
the dashboard server (§3.3); and a pre-change `aq update` validates new code with
its old in-memory logic, which decides what the daemon may answer on
`/dashboard` (§5, §6.2).

## 1. Process model

| Decision | Value |
|---|---|
| Module | `src/dashboard_server/`: `__main__.py`, `app.py` (`create_app(settings)`), `bundle.py`, `proxy.py`, `edge.py` (Host/Origin/peer gates), `settings.py`, `process.py` (PID, spawn, stop, identity probe) |
| App / server | Plain **Starlette** app under **uvicorn**, one worker. No FastAPI: the process has no schema, and `openapi.json` must not change. `uvicorn` becomes a declared dependency (today it arrives transitively) |
| Upstream client | One `aiohttp.ClientSession` for HTTP and WebSocket, `auto_decompress=False`, connector limit 256 |
| Foreground | `aq dashboard serve [--host] [--port] [--api-url]` — runs in the terminal, logs to stderr |
| Managed | `aq dashboard start\|stop\|restart\|status`; `aq start` / `aq stop` / `aq restart` call them |
| Files | `~/.agent-queue/dashboard-server.pid`, `~/.agent-queue/dashboard-server.log` |
| Identity | `GET /__aq/health` on the dashboard server |

**Import boundary.** `src/dashboard_server/` may import `src.config` and the
standard library plus Starlette, uvicorn and aiohttp; it must not import
`src.api`, `src.orchestrator`, `src.database`, `src.commands` or `fastapi`. The
daemon's import path (`src.main`, `src.api.*`) must not import
`src.dashboard_server` or `src.dashboard_assets.runtime`. Both directions are
tested (§7).

**CLI.** `aq dashboard` already exists as the auto-generated group for the
`state-*` commands; [`src/cli/auto_commands.py`](../../src/cli/auto_commands.py)
merges into a hand-written group of the same name, so the process commands live
in a hand-written `dashboard` group. They are local process commands like
`aq start`, not `CommandHandler` commands: they must work while the daemon is
down.

**Launch.** `aq dashboard start` spawns `[sys.executable, "-m",
"src.dashboard_server"]` with `start_new_session=True`, output appended to the
log, and an environment stripped of database URLs and provider keys — the
process needs no secret. It waits up to 10 s for `/__aq/health` to answer with
the child's PID. Start is idempotent: a running instance with a matching
identity is success.

`aq start` starts the dashboard server once the daemon answers `/health` — also
when the daemon was already running — if `dashboard.server.enabled` is true and
a verified bundle exists. **`--no-dashboard` keeps its historical meaning, "skip
the interactive Vite prompt", and does not suppress the dashboard server**
(§6.2 depends on this); the new `--no-dashboard-server` starts the daemon alone.
With no bundle (a contributor checkout) `aq start` offers the Vite dev server as
today, which keeps `dashboard.pid`, `dashboard.log` and port 5173; separate file
names mean an older release's PID file is never mistaken for the dashboard
server.

**Shutdown.** `SIGTERM` stops accepting, closes proxied WebSockets with `1001`,
cancels SSE relays and exits within 5 s; `aq dashboard stop` sends `SIGKILL`
after 10 s, as `stop_daemon` does. `aq stop` stops the dashboard server, then
the daemon. `aq restart` restarts both, so a changed port or a rebuilt bundle
needs no second command.

**Crash and restart: no supervisor.** Nothing restarts a crashed dashboard
server. It is stateless (static files and byte pumps), so a supervisor would be
more machinery than the thing supervised; and making the *daemon* the supervisor
would give it a browser-facing job again and tie the dashboard's availability to
its own — staying up across a daemon restart is what lets the page say "daemon
unreachable" instead of failing to load. A crash shows in `aq status` and
`aq doctor`, and `aq start` heals it.

**Observers.**

* `/__aq/health` returns `{"service": "aq-dashboard-server", "version", "pid",
  "bundle": {"version", "files", "verified": true}, "api_url", "upstream_ok"}`.
  The `/__aq/` prefix is reserved: never proxied, never an SPA route.
* `aq status` gains a *Dashboard* line and, under `--json`, a `dashboard_server`
  object: `state` ∈ `running | stopped | disabled | no_bundle | stale_pid |
  port_conflict`, plus `url` and `pid`. The CLI computes it from the PID file and
  the identity probe, so it works with the daemon down and the daemon's
  `/health` payload is unchanged. `port_conflict` is a port that answers but is
  not ours, which today's bare TCP connect (`_dashboard_running`) cannot tell.
* `aq doctor` runs inside the daemon, so its checks
  (`src/doctor/dashboard_server_checks.py`) import nothing from
  `src.dashboard_server`; they read `config.dashboard.server` and make one HTTP
  probe: `dashboard.server.running` (fix: `aq dashboard start`),
  `dashboard.server.bundle` (fix: `aq install --restart-from dashboard.build`),
  `dashboard.server.port` and `dashboard.server.exposure` (a non-loopback bind;
  warns with §3.4).
* `aq restart` and `aq update`: above and §6.2.

## 2. Proxy contract

### 2.1 Path set

Enumerated from `dashboard/src` — every `fetch`, `EventSource`, `WebSocket` and
the generated client's base URL.

| Prefix | Callers in `dashboard/src` | Transport |
|---|---|---|
| `/api` | generated client (`api/client.ts`), `api/legacy-fetch.ts`, `api/chat.ts`, `pages/settings/ProjectRoots.tsx`, `panes/console-stream/` | JSON; multipart upload (task attachments, 10 MiB daemon cap); **SSE** on `/api/sessions/{id}/stream`, `/api/sessions/{id}/pane`, `/api/streams/{id}/subscribe` |
| `/health` | `api/hooks.ts` | JSON |
| `/ready` | none today; kept for parity with [`dashboard/vite.config.ts`](../../dashboard/vite.config.ts) and for probes | JSON |
| `/ws` | `/ws/events?after_seq=` (`ws/useEventStream.ts`), `/ws/terminal/{session}?cols=&rows=` (`ws/terminalSocket.ts`) | WebSocket |

A prefix matches on a segment boundary (`/api` and `/api/…`, never `/apix`).
Nothing else is forwarded. The daemon's other prefixes — `/mcp`, `/docs`,
`/redoc`, `/openapi.json`, `/plans` and `/dashboard` — answer `404` at the
dashboard server and never fall back to `index.html`, so a client pointed at the
wrong port gets an error, not HTML; no SPA route uses them. A proxied path whose
percent-decoded form contains a `.` or `..` segment is answered `400`. The
spec-document pane's `useHostedDoc` fetches an arbitrary same-origin URL that is
not a daemon route; it keeps answering `404`, as under the daemon mount.

### 2.2 HTTP, streaming and timeouts

* Method, raw path and query string go byte-for-byte to the daemon's API base,
  resolved as every `aq` command resolves it (`AQ_API_URL`, else
  `mcp_server.host`/`port`, default `http://127.0.0.1:8081`).
* **Request headers pass through end to end**, including `Host`, `Origin`,
  `Cookie`, `Authorization` and `Last-Event-ID`. `Host` is deliberately not
  rewritten (Vite's `changeOrigin: false`): the daemon's terminal origin rule
  compares `Origin` with `Host` (§3.2). Hop-by-hop headers are dropped. Inbound
  `Forwarded`, `X-Forwarded-*` and `X-Real-IP` are **dropped and none are
  added** — the daemon trusts no such header and must not start trusting one a
  client can forge.
* Bodies stream both ways in chunks of at most 64 KiB, never buffered whole and
  never re-encoded. Status and response headers pass through verbatim, including
  the SSE endpoints' `Cache-Control: no-cache` and `X-Accel-Buffering: no`; an
  SSE chunk is sent the moment it arrives.
* Timeouts: connect 2 s; **120 s to response headers** (`504`,
  `error: "daemon_timeout"`); **no body or idle timeout**, because SSE streams
  are legitimately silent and unbounded — and they return headers at once, so
  the header timeout is safe for them.
* A browser disconnect cancels the upstream request at once, so the daemon's
  stream generators release their pane pollers. Upstream EOF ends the response.
* `Authorization`, `Cookie` and `Sec-WebSocket-Protocol` (which can carry
  `aq-bearer.<token>`) are never logged. There is no access log.

### 2.3 Back-pressure

There is no queue in the proxy. An HTTP relay awaits the downstream send before
reading the next chunk; a WebSocket is two pumps, each `receive → await send`. A
slow browser blocks the send, the proxy stops reading, and TCP flow control
reaches the daemon, whose own bounds apply unchanged: `/ws/events` drops the
oldest of 1,000 queued events per client, and the terminal stops reading the PTY
at 128 KiB of unacknowledged output and closes `4408` after 30 s. The terminal's
`ack` credit frames are end-to-end messages and pass through untouched.

### 2.4 WebSockets

1. **Upstream first.** On an upgrade the proxy opens the upstream WebSocket
   before accepting the browser's, offering the browser's
   `Sec-WebSocket-Protocol` list verbatim (`aq-terminal-v1`, optionally
   `aq-bearer.<token>`) and forwarding `Host`, `Origin`, `Cookie`,
   `Authorization` and the query string. It then accepts downstream with exactly
   the subprotocol the daemon selected, or none.
2. **Rejections stay rejections.** The daemon refuses a terminal *before*
   accepting (for example `4401`, `4403`, `4429`), which on the wire is an HTTP
   `403` to the handshake. The proxy denies the browser's handshake the same way,
   so the browser sees what it sees today. Accepting first and closing afterwards
   would turn a refusal into an open-then-closed socket; upstream-first is the
   one ordering that makes the proxy transparent.
3. Frames are relayed one-to-one in order, preserving text versus binary and
   frame boundaries — the terminal is binary output plus JSON text control
   frames. Maximum message size is 16 MiB on both hops (uvicorn's default, so the
   proxy is never the tighter limit). Ping/pong is per hop and not relayed.
4. Close code and reason are relayed in both directions; `4400`–`4429` carry
   meaning the dashboard displays.

### 2.5 When the daemon is down

The bundle keeps loading, so the SPA renders its own disconnected state. Proxied
HTTP answers `503` with `{"ok": false, "error": "daemon_unreachable", "api_url":
…}`, `Retry-After: 2` and `Cache-Control: no-store`; a WebSocket handshake is
denied with `503`, which the browser reports as close `1006` and
`useEventStream`'s existing backoff retries. Every response the dashboard server
generates itself carries `X-AQ-Dashboard-Server: <version>`, which separates its
`503` from the daemon's own degraded `/health` `503`.

## 3. Origin and auth

### 3.1 Configuration

```yaml
dashboard:
  server:
    enabled: true      # `aq start` manages it when a verified bundle exists
    host: 127.0.0.1    # an IP literal or `localhost`
    port: 8082
```

**Default port 8082**: next to the daemon's 8081; outside the range Vite walks
when 5173 is taken (5174, 5175, …), so dev and served coexist; not a contended
developer port (3000, 8000, 8080, or 5000 and 7000, which macOS AirPlay holds);
referenced nowhere else in this repository. A busy port is a startup failure
naming the key, **never an auto-increment**: the daemon's pointer (§5), the
installer's open step and bookmarks need a deterministic URL. Validation: port
1–65535 and different from `mcp_server.port`. When `port` is unset and
`mcp_server.port` is 8082 — a daemon moved there before the dashboard server
existed — the default steps aside to **8083**, so an upgrade never leaves a
config that no longer loads (`src.config.default_dashboard_server_port`, shared
by the daemon's loader and this process). The rule reads the config alone, so
the URL stays deterministic; a port the operator sets is never moved, and
setting it to the daemon's is still an error. Settings are read at process
start, so `aq dashboard restart` applies a change and the daemon needs no
restart. The bundle is built with every `VITE_*_URL` escape hatch unset, so the
page talks only to its own origin.

### 3.2 How `api_auth.trusted_dashboard_origins` interacts

The key keeps one meaning in both processes: *origins, besides literal loopback,
from which a browser may drive this install*. The dashboard server reads it with
the daemon's parser.

* **Default bind.** `Origin` is `http://127.0.0.1:8082` (or `localhost`), `Host`
  reaches the daemon unchanged, and `TerminalStreamService._check_origin` sees a
  same-origin literal-loopback pair: terminals work with an empty list, as they
  do through Vite.
* **Any other name** — a LAN address, a DNS name, a TLS front proxy — must be
  listed, exactly as today.
* The daemon checks `Origin` only on `/ws/terminal`, so the dashboard server
  gates every proxied request. **Host gate:** the `Host` hostname must be literal
  loopback, the configured `host` when that is a concrete address, or the
  hostname of a trusted origin; otherwise `421`. This stops DNS rebinding against
  `/api`. **Origin gate:** an `Origin` header, when present, must equal
  `scheme://Host` or be trusted; otherwise `403` `origin_not_allowed`. Requests
  with no `Origin` (curl, same-origin `GET`) pass.

### 3.3 The address a proxy hides

Behind any proxy the daemon sees every connection arrive from `127.0.0.1`. Two
daemon rules key on the peer address and would silently stop meaning anything:
terminals are loopback-only
([`src/api/terminal_stream.py`](../../src/api/terminal_stream.py)), and
global-admin bearer tokens are loopback-only
([`src/api/middleware.py`](../../src/api/middleware.py)). The same hole exists
today behind `vite --host`. The dashboard server sees the real peer, so it
re-applies both: from a **non-loopback peer**, `/ws/terminal/*` is denied and any
request carrying `Authorization` or an `aq-bearer.*` subprotocol is denied, both
`403` `loopback_only`. The daemon is not changed and no forwarding header is
invented.

### 3.4 Exposure statement

With the default bind, nothing is reachable from another machine. If an operator
sets `host` to a LAN address or `0.0.0.0`:

**Reachable** by anyone who can connect to that port (from a browser, only under
an allowed `Host` and `Origin`): the static bundle; `/health`, `/ready`;
`/ws/events`; and **every `/api/**` route with local-operator scope**, because a
request without a bearer token is `LOCAL_SCOPE` and
`api_auth.require_session_token` defaults to false. That includes creating and
deleting tasks, typing into agent sessions, reading transcripts, panes and
workspace files, and reading (redacted) and writing configuration. There is no
login. The Host and Origin gates stop other websites' scripts; they do not stop
a person on that network with `curl`. **A LAN bind hands the operator console to
that network.**

**Not reachable:** interactive terminals and bearer-token requests (§3.3);
`/mcp`, `/docs`, `/redoc`, `/openapi.json`, `/plans/*`; port 8081 itself, which
stays on `mcp_server.host`; PostgreSQL; any file not listed in the bundle
manifest. With `require_session_token: true` a LAN peer gets `401` on everything
but the health paths, since the browser holds no token and the edge refuses
remote bearers.

**Recommended remote access** is to keep the loopback bind and forward the port
(`ssh -L 8082:127.0.0.1:8082 host`): the origin is then `http://localhost:8082`
and everything, terminals included, works with no configuration.

## 4. Bundle ownership

* `src/dashboard_assets/` stays the data package: `pyproject.toml` keeps
  `"src.dashboard_assets" = ["dist/**"]`, the release script keeps staging there,
  and **the wheel keeps shipping the bundle** with its integrity checks intact.
* `DashboardBundle`, `verify_dashboard_bundle`, `dashboard_directory`,
  `installed_version` and the SPA static-files class move to
  `src/dashboard_server/bundle.py`. `mount_dashboard` is deleted, not moved.
  `src/dashboard_assets/runtime.py` remains as a **shim re-exporting
  `verify_dashboard_bundle`** with no FastAPI import, because a pre-change
  `aq update` lazy-imports that name from the new checkout (§6.2).
  [`src/install/dashboard.py`](../../src/install/dashboard.py) imports from the
  new module.
* **The bundle is served at `/` and built with Vite `base: "/"`**, the same URL
  shape as the dev server. `AQ_DASHBOARD_EMBEDDED` and the `/dashboard/` base are
  removed from `vite.config.ts` and `scripts/build_release_artifact.py`. The
  manifest gains `"base": "/"` and stays `schema_version: 1`, so an old verifier
  still reads it, while the new verifier **rejects a manifest without `base`**
  ("built for the daemon mount; rebuild") instead of serving a blank page whose
  assets 404. `dashboard/` is already a build input, so the fingerprint changes
  and installs rebuild without being told.
* Verification runs at startup and **fails closed**: a digest mismatch exits
  non-zero with the reason, and no directory (a source checkout) exits `2`
  pointing at `npm -w dashboard run dev` and `aq install --restart-from
  dashboard.build`. Only manifest-listed files are served. SPA fallback is
  unchanged — an extensionless path that is not a file gets `index.html`, a path
  with a suffix gets `404` — and never applies under a proxied prefix, `/__aq`
  or the `404` prefixes of §2.1, so an unknown API path returns the daemon's
  JSON, not HTML.
* `index.html` is `Cache-Control: no-cache`; content-hashed `assets/*` are
  `public, max-age=31536000, immutable`; every static response carries
  `X-Content-Type-Options: nosniff` and
  `Content-Security-Policy: frame-ancestors 'self'`.
* Source checkouts keep Vite, its port and its proxy for development.

## 5. Daemon behaviour after removal

`src/api/app.py` drops the `mount_dashboard` import and call. Two routes,
`/dashboard` and `/dashboard/{rest:path}` (`GET`, `HEAD`,
`include_in_schema=False`, registered before the MCP catch-all mount at `/`),
answer with this machine-readable body:

```json
{"ok": false, "error": "dashboard_not_served_here",
 "dashboard_url": "http://127.0.0.1:8082/",
 "hint": "The daemon is API-only. Run `aq dashboard status`."}
```

`dashboard_url` comes from `dashboard.server` alone — never from the request's
`Host` — with a wildcard bind rendered as `127.0.0.1`.

**Status — a deviation from the brief, decided here.** The brief asked for `404`.
This spec decides **`307` with `Location: <dashboard_url><rest>?<query>`** and
the body above when `dashboard.server.enabled` is true, and `404` with
`"dashboard_url": null` when it is false. The reason is §6.2: a pre-change
`aq update` probes `GET /dashboard/` on the *new* daemon and treats `>= 400` as
"the daemon is up but not serving the dashboard". It would roll every existing
install back on every attempt — or, when the update carried migrations, leave it
on new code and report a failed update. `urllib` follows the redirect to the
dashboard server, which answers `200`. The redirect also keeps the bookmark the
installer opened (`http://127.0.0.1:8081/dashboard/`) working, and the daemon
still serves no content. **If `404` is preferred regardless**, change that one
status; the accepted cost is that the first `aq update` from a pre-change install
fails at "Start the daemon" and is recovered by hand with `git pull --ff-only`,
`aq restart`, `aq install`.

Nothing else in the API changes: no CORS, no new headers, the same `/health`
payload, the same `trusted_dashboard_origins` semantics, a byte-identical
`openapi.json`, so neither client is regenerated.

## 6. Installer and upgrade

### 6.1 Steps

`daemon.start` → `dashboard.build` → **`dashboard.serve`** (new) →
`daemon.dashboard` → `dashboard.open`.

* `dashboard.build` builds as today. It **no longer restarts the daemon**; when a
  running dashboard server reports a different bundle version, it restarts *that*.
* `dashboard.serve` runs `aq dashboard start`. Its read-only `verify` is that
  `/__aq/health` answers with our identity and a verified bundle and `GET /` is
  `200`. It depends on `dashboard.build`; a checkout with no bundle reports the
  Vite instructions it reports today.
* `daemon.dashboard` reports the dashboard server's URL and reachability
  (`DASHBOARD_PATH` is retired), and `dashboard.open` opens that URL under the
  unchanged rule: once, interactive only, never from an unattended run.

### 6.2 `aq update` on an install that relies on the daemon mount

`aq update` moves the checkout forward and then keeps running **the old code
already in memory**. Three things follow for the pre-change updater; each is a
compatibility requirement on the new code and each has a test (§7):

1. It lazy-imports `src.dashboard_assets.runtime.verify_dashboard_bundle` *after*
   the pull. If the module were gone the `ImportError` would escape its
   `_StepFailed` handler with the daemon stopped and the code moved. → the shim
   in §4.
2. It rebuilds with the new release script, then verifies through that shim. →
   the manifest stays `schema_version: 1`.
3. It runs `aq start --no-dashboard` and probes `GET <api>/dashboard/` for
   `< 400`. → `--no-dashboard` still starts the dashboard server (§1), and the
   daemon answers `307` to it (§5).

The result: an existing install runs `aq update` once and ends with the daemon
API-only, the dashboard server running, and its old bookmark redirecting.

The new updater stops the dashboard server together with the daemon **before it
moves the code in either direction**, rebuilds when the build inputs changed,
starts both, and validates `/health` on the daemon plus `/__aq/health` and
`GET /` on the dashboard server. It never probes the daemon for browser content.

### 6.3 Rollback

* **A failed update** uses the existing recovery: the code moves back, the bundle
  is rebuilt from the old commit (the fingerprint differs, so the `/dashboard/`
  base returns) and the old daemon mounts it again. The dashboard server was
  stopped before the code moved, so none is orphaned under a CLI that no longer
  knows the command.
* **Turning it off:** `dashboard.server.enabled: false`. `aq start` manages
  nothing, the daemon's pointer becomes a `404`, and the operator runs Vite or
  any static server that honours §2. There is deliberately no flag that
  re-enables the daemon mount.
* **Reverting the change** is a revert of the removal commit followed by
  `aq install --restart-from dashboard.build`.

## 7. Test plan

| Component | File | What it proves |
|---|---|---|
| Bundle | `tests/test_dashboard_server_bundle.py`; the verifier and mount cases leave [`tests/test_release_artifact.py`](../../tests/test_release_artifact.py), whose staging and metadata cases stay | digest mismatch, unsafe path, missing `index.html` and missing `base` fail closed; a file outside the manifest is not served; SPA fallback and the reserved prefixes; cache and security headers |
| HTTP proxy | `tests/test_dashboard_server_proxy.py`, against a fake upstream ASGI app on an ephemeral port | the path table (`/apix`, `/mcp`, dot segments); headers passed and dropped; an SSE chunk delivered before upstream finishes, asserted with events, not wall-clock; disconnect cancels upstream; streamed upload; `503` and `504` bodies |
| WebSocket proxy | same file | the subprotocol selected upstream is the one accepted; an upstream refusal denies the handshake; text/binary and frame boundaries; close code and reason both ways; a stalled consumer blocks the upstream sender with bounded proxy memory; upstream down denies `503` |
| Edge gates | `tests/test_dashboard_server_edge.py` | Host `421`; Origin `403`; trusted origin admitted; a non-loopback peer is refused terminals, `Authorization` and `aq-bearer.*` |
| Daemon | `tests/test_api_dashboard_pointer.py` and the existing [`tests/test_api_client_contract.py`](../../tests/test_api_client_contract.py) | `307` with `Location` and body; `404` when disabled; precedence over the MCP mount; unchanged `openapi.json`; **the import boundary in both directions**, from a subprocess inspecting `sys.modules`; the real `TerminalStreamService` accepts an `Origin`/`Host` pair relayed unchanged and refuses an untrusted LAN host `4403` |
| CLI / process | `tests/test_cli_dashboard_server.py` | idempotent start, stale PID, `port_conflict`, `SIGTERM` then `SIGKILL`, `aq start` / `stop` / `restart` ordering, both flags, `aq status --json` with the daemon down |
| Doctor | `tests/test_doctor_dashboard_server.py` | each check's pass, warn and fix text; no import of `src.dashboard_server` |
| Installer / update | [`tests/test_install_dashboard.py`](../../tests/test_install_dashboard.py), [`tests/test_update.py`](../../tests/test_update.py) | `dashboard.serve` run and verify; the open-step URL; unattended never opens; **the three §6.2 requirements, driven through the pre-change import and probe sequence**; the new updater's validation; rollback stops the dashboard server first |
| Dashboard | vitest and the build | `base` is `/`; the staged manifest carries `"base": "/"` |

Run each with `aq test <files>`; none needs a marker beyond the defaults.

**End-to-end on a fresh clone**, in a disposable `ubuntu:24.04` container as
[installer testing](../contributing/installer-testing.md) describes, and once on
macOS:

1. The one-command install, unattended, reaches *ready* with no hand-run step
   and `dashboard.open` does not fire.
2. `:8082/__aq/health` reports our identity and a verified bundle; `/` and
   `/tasks` return the same HTML; `/nope.js` and `/mcp` are `404`.
3. `:8082/api/health` equals `:8081/api/health`; a Python `websockets` client
   receives a frame on `:8082/ws/events`; `:8081/dashboard/` redirects and lands
   on `200`.
4. With a live session, a browser shows the event stream connected and a
   terminal that accepts typing and resizing through the proxy.
5. `kill -9` the dashboard server: `aq status` says `stopped`, `aq doctor` warns,
   `aq start` heals. Stop the daemon: `:8082/` is still `200` and
   `:8082/api/health` is `503` `daemon_unreachable`. `aq stop` leaves neither
   process.
6. Install from the commit before the removal, then `aq update` to the tip: it
   succeeds without a rollback and ends in the state of steps 2–3.
