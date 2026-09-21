# Release notes

What changed in AQ that an existing installation will notice, newest first.
Each entry says what is different, what you have to do, and how to undo it.

An installation from a source checkout — which is what the one-command
bootstrap makes — follows `main`, and `aq update` moves it to the tip, so these
entries are dated by the day the change reached `main` rather than numbered.
A wheel installation gets the same change when it upgrades to a release built
after that date.

## 2026-09-21 — The daemon is API only; the dashboard has its own server

**In one line:** open the dashboard at **`http://127.0.0.1:8082/`**. The daemon
on port 8081 no longer serves it.

### What changed

* **The daemon no longer serves the dashboard.** Since 2026-09-16 it had
  mounted the built dashboard at `http://127.0.0.1:8081/dashboard/`. That mount
  is gone ([src/api/app.py](../src/api/app.py)). The daemon answers `/api`,
  `/health`, `/ready`, the `/ws` sockets and `/mcp`; the only HTML it still
  returns is FastAPI's interactive API reference at `/docs` and `/redoc` (see
  [known issues](#known-issues)). A request
  for `/dashboard` or anything under it gets `404` with a pointer instead of a
  page:

  ```json
  {"ok": false, "error": "dashboard_not_served_here",
   "dashboard_url": "http://127.0.0.1:8082/",
   "hint": "The daemon is API-only. Run `aq dashboard status`."}
  ```

  `dashboard_url` is `null` when the dashboard server is disabled. The answer is
  deliberately a `404`, not a redirect: an old bookmark shows the pointer rather
  than silently moving.
* **A new process, the dashboard server, serves the dashboard.** It serves the
  verified bundle at `/` and relays `/api`, `/health`, `/ready` and `/ws` to the
  daemon, so the browser still talks to a single origin and nothing needed CORS
  ([src/dashboard_server/](../src/dashboard_server/)). It holds no state and no
  secret. See [architecture](concepts/architecture.md#two-processes-the-daemon-and-the-dashboard-server).
* **`aq start`, `aq stop` and `aq restart` manage both processes**, and
  `aq status` has a *Dashboard* line (`dashboard_server` under `--json`), which
  works with the daemon down. New commands manage it on its own:
  `aq dashboard start | stop | restart | status`, plus `aq dashboard serve` in
  the foreground. `aq start --no-dashboard` still means "skip the Vite prompt"
  and does **not** skip the dashboard server; `--no-dashboard-server` does
  ([command reference](reference/cli/commands.md)).
* **New settings:** `dashboard.server.enabled` (default `true`), `host`
  (default `127.0.0.1`) and `port` (default `8082`, or `8083` when your daemon
  already uses 8082 and you set no port). See the
  [configuration reference](reference/configuration.md#dashboard-server-settings).
* **New doctor checks:** `dashboard.server.running`, `dashboard.server.bundle`,
  `dashboard.server.port` and `dashboard.server.exposure`.
* **Installer:** `aq install` gains a `dashboard.serve` step; `dashboard.build`
  no longer restarts the daemon, and `dashboard.open` opens the dashboard
  server's URL — once, and never from an unattended run
  ([install reference](reference/cli/install.md)).
* **`aq update`** stops the dashboard server before the daemon, before it moves
  the code in either direction, then starts both on the new code and checks the
  daemon's `/health` and that the dashboard server answers with this install's
  bundle. It never asks the daemon for a page.
* **The bundle is now built for `/`.** Its manifest records `"base": "/"`, and
  the dashboard server refuses a bundle built for the old `/dashboard/` mount,
  so an existing install rebuilds it once.
* **Unchanged:** the API and `openapi.json`, the generated clients, the daemon's
  `/health` payload, the meaning of `api_auth.trusted_dashboard_origins`, and the
  contributor path — `npm run dev` still serves the dashboard on
  `http://localhost:5173` from a checkout, and `aq start` still offers it when no
  bundle is built.

### What you do on an existing install

<!-- e2e:upgrade-matrix -->

### Where the dashboard is now

| | Before | Now |
|---|---|---|
| Dashboard URL | `http://127.0.0.1:8081/dashboard/` | `http://127.0.0.1:8082/` (the `dashboard_url` your daemon's pointer names) |
| Served by | the daemon | the dashboard server |
| `http://127.0.0.1:8081/dashboard/` | the dashboard | `404` JSON pointer |
| Started by | `aq start` (the daemon) | `aq start` (the daemon, then the dashboard server) |
| Log | `~/.agent-queue/daemon.log` | `~/.agent-queue/dashboard-server.log` |

Update your bookmark. If something else already listens on 8082, the
dashboard server does not pick another port — `aq dashboard status` says
`port conflict`; set `dashboard.server.port` and run `aq dashboard restart`.

### Exposing the dashboard on a LAN, and what that exposes

By default the dashboard server listens on `127.0.0.1`, so nothing is reachable
from another machine. The recommended way in from elsewhere is to leave it that
way and forward the port:

```bash
ssh -L 8082:127.0.0.1:8082 <aq-host>
```

and open `http://localhost:8082/` on your own machine. Everything works that
way, interactive terminals included, with no configuration.

If you do want it on your network, set the bind address, list the origin your
browsers will use, and restart only the dashboard server:

```yaml
dashboard:
  server:
    host: 0.0.0.0          # or one LAN address
api_auth:
  trusted_dashboard_origins:
    - http://192.168.1.20:8082
```

```bash
aq dashboard restart
```

**Read this before you do.** A LAN bind hands the operator console to that
network. There is no login: a request without a bearer token runs with
local-operator scope (unless `api_auth.require_session_token` is on), so anyone
who can reach the port can create and delete tasks, type into agent sessions,
read transcripts, panes and workspace files, and read (redacted) and write
configuration. The Host and Origin checks stop *other websites' scripts* from
driving it; they do not stop a person on that network with `curl`.

What stays unreachable from another machine even then: interactive terminals
and any request carrying a bearer token (`403 loopback_only`); the daemon's
`/mcp`, `/docs`, `/redoc`, `/openapi.json` and `/plans`; port 8081 itself, which
stays on `mcp_server.host`; PostgreSQL; and any file outside the bundle
manifest. `aq doctor --check dashboard.server.exposure` warns for as long as the
bind is not loopback. The full statement is in the
[dashboard guide](guides/dashboard.md#reaching-it-from-another-machine).

### Turning it off, and rolling back

* **Turn the dashboard server off:** `dashboard.server.enabled: false`.
  `aq start` then starts only the daemon, the daemon's pointer says
  `"dashboard_url": null`, and you can serve the bundle with anything that
  proxies `/api`, `/health`, `/ready` and `/ws` to the daemon the way the
  dashboard server does. There is deliberately no setting that makes the daemon
  serve the dashboard again.
* **A failed `aq update`** rolls the code back as before. The dashboard server
  was stopped before the code moved, so none is left running code the checkout
  no longer has; rolled back to a version from before this change, the bundle is
  rebuilt for that version and its daemon serves the old `/dashboard/` mount
  again.

### Known issues

<!-- e2e:known-issues -->
