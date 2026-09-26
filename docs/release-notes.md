# Release notes

What changed in AQ that an existing installation will notice, newest first.
Each entry says what is different, what you have to do, and how to undo it.

An installation from a source checkout — which is what the one-command
bootstrap makes — follows `main`, and `aq update` moves it to the tip, so these
entries are dated by the day the change reached `main` rather than numbered.
A wheel installation gets the same change when it upgrades to a release built
after that date.

## 2026-09-25 — Discord dashboard links name `dashboard.server.public_url`

Digest, escalation and document-review posts, and the digest preview, now link
to one origin: `dashboard.server.public_url` (alias `dashboard.public_url`).
Before, digests and escalations named the daemon's health port (`:8081`, which
serves no dashboard pages) rewritten to the machine's Tailscale address, and
review posts named `http://127.0.0.1:8082`. Neither opened from a phone. The
Tailscale rewrite is gone: a loopback dashboard never becomes a tailnet link,
and `health_check.base_url` is no longer consulted for links.

**What to do:** to get links, put an authenticated tailnet reverse proxy in
front of the loopback dashboard server, set `dashboard.server.public_url` to its
exact origin, add that origin to `api_auth.trusted_dashboard_origins`, and run
`aq dashboard restart`. Until then posts say *Remote dashboard link unavailable*
with the reason. `aq dashboard link` and `aq doctor --check dashboard.remote_link`
show the chosen origin and why ([links in Discord posts](guides/dashboard.md#links-in-discord-posts)).
If both `public_url` keys are set and differ, links stay off until they agree.

**Undo:** clear `dashboard.server.public_url`; new posts carry the notice again.
Posts already sent are not edited. No migration.

## 2026-09-22 — GitHub credentials share AQ's `gh` path (staged)

AQ now selects one GitHub credential mode at daemon startup for its
repository-bound operations. GitHub CLI (`gh`) is required on the daemon host
for **both** modes; the installer does not install it. Choose either setup:

| Setup | Operator action | AQ behavior |
|---|---|---|
| Existing login only | Leave `integration.github_app` unset. Keep the daemon OS user's stored `gh` login/PAT or `GH_TOKEN` / `GITHUB_TOKEN` as before. | `gh` uses `GH_TOKEN`, then `GITHUB_TOKEN`, then its stored login. AQ does not alter stored authentication. |
| GitHub App only | Install the App on the repository and configure the existing `integration.github_app` fields and readable private key; restart the daemon. | AQ supplies a repository-scoped installation token as `GH_TOKEN` for each operation. A personal login or SSH key is not required for supported AQ operations on an already registered repository. |

If both credential sources are present, the configured App wins for AQ's
operations. Binding, token or permission failures are reported; AQ never
retries with a PAT or SSH key. The token is not put in `gh`'s stored login,
the daemon's parent environment or a worker shell. Task authority, immutable
revisions, lease checks, CI policy and integration trust remain in force.

**Upgrade:** no setting rename or database migration is needed. Install `gh`
before using GitHub features, then restart the daemon after changing the App
mode, installation, identity or private-key reference. Token expiry is handled
in memory. [Configuration](reference/configuration.md#github-credentials),
[onboarding](guides/project-onboarding.md#github-on-the-daemon-host) and
[integration](concepts/integration.md#github-access-during-delivery) give the
current setup and source boundaries.

**Current limits:** App mode can validate an explicit repository URL but the
onboarding clone step still returns `github_operation_unsupported`. Existing
local repositories may be linked and already registered repositories can use
App-backed AQ delivery. User-wide search, owner selection, repository
creation, user identity and profile gists remain existing-login features;
App mode reports them unavailable instead of consulting a personal login.
The compatibility `GitHubAppClient` / `GitHubCLIClient` constructors and any
superseded launch paths are scheduled for the final removal task. A live
disposable private-repository App-only acceptance run has not been recorded,
so this entry does not certify that end-to-end gate.

**Deployment-guide follow-up:** `deploy/README.md` belongs to
`feature/cloud-deploy` and is absent from this checkout. When that branch is
integrated, update its deployment prerequisites to install `gh` in both modes,
describe the startup restart boundary, and state that an App failure does not
fall back to the deployment host's PAT or SSH credentials. No parallel deploy
tree is added here.

## 2026-09-21 — The daemon is API only; the dashboard has its own server

**In one line:** open the dashboard at **`http://127.0.0.1:8082/`**. The daemon
on port 8081 no longer serves it.

### What changed

* **The daemon no longer serves the dashboard or HTML API reference pages.**
  Since 2026-09-16 it had mounted the built dashboard at
  `http://127.0.0.1:8081/dashboard/`; that mount is gone
  ([src/api/app.py](../src/api/app.py)). It also no longer serves FastAPI's
  interactive API reference at `/docs` (Swagger UI) and `/redoc` (ReDoc), both
  of which loaded scripts from a CDN. The daemon answers `/api`, `/health`,
  `/ready`, the `/ws` sockets and `/mcp`; the only HTML it returns is the plan
  viewer at `/plans/<task_id>`, and `/openapi.json` still serves the schema as
  JSON for generated clients. A request
  for `/dashboard` or anything under it is redirected (`307`) to the same route
  on the dashboard server — `/dashboard/tasks/abc` to
  `http://127.0.0.1:8082/tasks/abc` — with a pointer as the body instead of a
  page:

  ```json
  {"ok": false, "error": "dashboard_not_served_here",
   "dashboard_url": "http://127.0.0.1:8082/",
   "hint": "The daemon is API-only. Run `aq dashboard status`."}
  ```

  With the dashboard server disabled the answer is a `404` and `dashboard_url`
  is `null`. The redirect keeps an old bookmark working and lets an `aq update`
  begun on older code finish cleanly (below). For a few hours after this change
  reached `main` the daemon answered `404` in both cases (`smart-meadow.9`).
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

Which case you are in depends on how AQ was installed and, for a source
checkout, on whether its updater already finishes on the new code — which is
true when this file exists:

```bash
test -f ~/.local/share/agent-queue/src/install/update_finish.py && echo "new updater" || echo "older updater"
```

(Use your own checkout path if you set `AQ_CHECKOUT_DIR`.) Both kinds update
with exit 0. The `aq update` rows were run end to end in disposable Ubuntu 24.04
containers on 2026-09-21, from three older commits to `7a0956cb0` — before the
daemon redirected `/dashboard`; the [transcript](validation/api-only-daemon-e2e.md)
has the full output.

| Your install | Run | What happens |
|---|---|---|
| Source checkout with the **new updater** (installed or updated since 2026-09-20 ~21:00) | `aq update` | Stops the dashboard server (if one runs) and the daemon, moves the code, rebuilds the dashboard for `/`, starts the daemon and the dashboard server, and checks both. Ends with `OK Start the dashboard server — http://127.0.0.1:8082/`, exit 0. |
| Source checkout with the **older updater** (installed 2026-09-16 to 2026-09-20 ~21:00 and not updated since) | `aq update` | The old updater still running in memory moves the code, rebuilds the dashboard and runs `aq start --no-dashboard`, which still starts the dashboard server as well as the daemon. It then asks the daemon for `/dashboard/`, follows the redirect to the dashboard server, gets its page, and ends with `OK Start the daemon — agent sessions are re-adopted` and `AQ is updated to …`, exit 0. It prints no *Start the dashboard server* line because it does not know that step: confirm with `aq status` (`Dashboard: running at http://127.0.0.1:8082/`), then optionally rerun `aq install`, which records the dashboard server step and prints the new URL without opening a second browser window. Later updates use the new updater. *Proved by driving the old updater's checks against the real new daemon and dashboard server (`tests/test_update.py`); the container run predates the redirect.* |
| Source checkout with the older updater that **already updated and got exit 20** — `XX Start the daemon — the daemon is up but not serving the dashboard`, then "The update failed … AQ was not rolled back" | nothing to repair | That was the daemon's short-lived `404` (`smart-meadow.9`). **Nothing is wrong:** the checkout is on the new code, the schema is at head, the daemon is healthy and the dashboard server is serving. Confirm with `aq status`, then optionally rerun `aq install` as above. Later updates are clean. |
| Source checkout, updated by rerunning the one-command bootstrap instead | the same bootstrap command | The bootstrap moves the checkout to the tip and `aq install` rebuilds the bundle, starts the dashboard server and reports its URL. No second browser window. *The bootstrap rerun itself was not exercised; an `aq install` rerun on the moved checkout was, with this result.* |
| Contributor checkout with no built bundle | nothing | Unchanged: `aq start` offers the Vite dev server on `http://localhost:5173`. |
| Wheel installation | upgrade to a wheel built after 2026-09-21, then `aq restart` | The wheel carries the rebuilt bundle; `aq restart` restarts the daemon and starts the dashboard server. *Not exercised in the end-to-end run — no release wheel has been built since this change.* |

If an installer printed `http://127.0.0.1:8081/dashboard/` and that page was
blank, you installed in the few hours before this change landed, when the
bundle was already built for the dashboard server: the dashboard was already at
`http://127.0.0.1:8082/`, and `aq update` finishes the move.

### Where the dashboard is now

| | Before | Now |
|---|---|---|
| Dashboard URL | `http://127.0.0.1:8081/dashboard/` | `http://127.0.0.1:8082/` (the `dashboard_url` your daemon's pointer names) |
| Served by | the daemon | the dashboard server |
| `http://127.0.0.1:8081/dashboard/` | the dashboard | a `307` redirect to the dashboard server |
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
`/mcp`, `/openapi.json` and `/plans`; port 8081 itself, which
stays on `mcp_server.host`; PostgreSQL; and any file outside the bundle
manifest. `aq doctor --check dashboard.server.exposure` warns for as long as the
bind is not loopback. The full statement is in the
[dashboard guide](guides/dashboard.md#reaching-it-from-another-machine).

### Turning it off, and rolling back

* **Turn the dashboard server off:** `dashboard.server.enabled: false`.
  `aq start` then starts only the daemon, the daemon's `/dashboard` answers
  `404` with `"dashboard_url": null` instead of redirecting, and you can serve
  the bundle with anything that proxies `/api`, `/health`, `/ready` and `/ws` to
  the daemon the way the dashboard server does. There is deliberately no
  setting that makes the daemon serve the dashboard again.
* **A failed `aq update`** rolls the code back as before. The dashboard server
  was stopped before the code moved, so none is left running code the checkout
  no longer has; rolled back to a version from before this change, the bundle is
  rebuilt for that version and its daemon serves the old `/dashboard/` mount
  again.

### Known issues

* **After a daemon crash, `aq start` does not restart the daemon while the
  dashboard server is running** (`smart-meadow.7`). It answers `Daemon is
  already running (PID …)` with the *dashboard server's* PID, because its
  fallback process search matches the dashboard server's command line. Run
  `aq restart` instead: it stops the dashboard server first, then starts both.
  For the same reason, do not run `aq stop --no-dashboard-server` while the
  daemon is down — it stops the dashboard server.
* **The daemon still serves one HTML page:** the plan viewer at
  `/plans/<task_id>` ([src/api/health.py](../src/api/health.py)), which loads
  its markdown renderer from a CDN. It is not the dashboard, and the dashboard
  server does not relay it. FastAPI's Swagger UI at `/docs` and ReDoc at
  `/redoc` are no longer served.
* **With the daemon down, the dashboard does not say so.** The page loads, but
  shows *Preferences unavailable* and *Loading…* until the daemon answers
  again; `aq dashboard status` reports `the daemon is not answering it`.
* **Discord escalation and digest links point at the daemon.** Their dashboard
  link is built from `health_check.base_url`, else the daemon's own
  `health_check.port` ([src/main.py](../src/main.py)), which serves no dashboard.
  Until that follows the dashboard server, set `health_check.base_url` to the
  URL your Discord readers can open the dashboard at.
