# Dashboard process boundary audit

Task: `bright-cascade-28`, audited 2026-10-23.

The checked-out implementation already serves the dashboard in a separate OS
process. No daemon-side dashboard mount, build, in-process server, or dashboard
module import was found. This change adds a regression guard and clarifies the
boundary; it does not relocate an existing host or claim to eliminate API load.

## Hosting and lifecycle inventory

Line references identify the audited implementation, not historical design prose.

| Path | Finding |
| --- | --- |
| `src/main.py:543`, `src/embedded_mcp.py:124` | Daemon startup creates its API/MCP app. The only sub-app mount in the embedded server is MCP at `/` (`embedded_mcp.py:159`). No dashboard startup or asset build. |
| `src/api/app.py:122`, `src/api/app.py:269` | FastAPI disables Swagger/ReDoc HTML. `/dashboard` and descendants return JSON plus a 307 to the configured dashboard origin, or 404 when disabled; no asset reads, proxying or static mount. |
| `src/api/health.py:114`, `src/api/health.py:136` | The legacy `/plans/{task_id}` endpoint still renders a standalone HTML plan page. This is an explicit exception in `docs/release-notes.md:108`, not the dashboard host; the standalone server intentionally rejects `/plans`. No runtime caller of `get_plan_url` or dashboard plan route was found. It prevents claiming that the daemon serves no HTML at all. |
| `src/dashboard_server/app.py:127`, `src/dashboard_server/app.py:265` | The separate server classifies requests, serves the verified bundle and SPA fallback, and proxies the API/streams. `src/dashboard_server/__main__.py:101` owns its uvicorn process. |
| `src/dashboard_server/process.py:645` | Managed start launches `python -m src.dashboard_server` through `Popen` with `start_new_session=True`, a separate PID/log, and a stripped environment. It verifies the child PID through HTTP. |
| `src/cli/dashboard.py:157`, `src/cli/dashboard.py:206` | `serve` runs in the foreground CLI process; `start`, `stop`, `restart` manage the separate server. These commands do not dispatch hosting into the daemon. |
| `src/cli/daemon.py:722`, `src/cli/daemon.py:1004`, `src/cli/daemon.py:1094`, `src/cli/daemon.py:1249` | The CLI launches the daemon, Vite (when selected), and the installed dashboard as separate subprocesses. Start/restart coordination happens in the CLI; `--no-dashboard` skips the Vite prompt, and `--no-dashboard-server` skips installed-server management. |
| `src/install/dashboard.py:206`, `src/install/dashboard.py:551` | The installer executes npm and the release builder outside the daemon, then calls `aq --json dashboard start`/`restart`. Asset builds do not run in the daemon event loop. |
| `src/install/onboarding.py:1315`, `src/install/update.py:858`, `src/install/update.py:881`, `src/install/update.py:912` | Onboarding resolves the separate server URL. Update builds in installer subprocesses and validates the API and dashboard listeners separately. Its dashboard imports are in installer/CLI processes. |
| `setup.sh:128`, `setup.sh:231` | Bootstrap installs npm dependencies and delegates runtime setup to `aq install`; it does not embed a UI server. |
| `src/install/service.py:149`, `src/install/service.py:208`, `src/install/service.py:245`, `src/install/service.py:285`, `src/install/watchdog.py:752` | systemd, launchd and cron entries execute the watchdog. It invokes `aq start --no-dashboard --unless-stopped`; the CLI launches separate processes. No service unit embeds dashboard code in the daemon. |
| `src/doctor/dashboard_server_checks.py:85`, `src/doctor/dashboard_server_checks.py:249` | Explicit diagnostics read bundle metadata and probe HTTP. An explicit fix launches `aq dashboard` in a subprocess; it imports no dashboard implementation into the daemon. Metadata inspection is not asset hosting. |
| `src/dashboard_assets/runtime.py:14` | Compatibility shim imports the standalone bundle verifier for old updaters. It is not on the daemon import path. |

## Background work and aggregation

The task permits heavy aggregation to remain when justified as a general API.
These are the retained services and their specific justification:

| Work | Owner and justification |
| --- | --- |
| Rendering, query cache and refresh timers | Browser: `dashboard/src/main.tsx:17`, `dashboard/src/api/hooks.ts:230`, `dashboard/src/pages/command-center/layout-v2/useLayoutTiles.ts:153`, `dashboard/src/pages/metrics/useServerNow.ts:21`. No React query loop is installed in the daemon. |
| HTTP/SSE/WebSocket relay and counters | Standalone process: `src/dashboard_server/proxy.py`, `src/dashboard_server/relay_stats.py:21`. The daemon receives ordinary API/stream requests. |
| Durable graph coordinates, tidy/rebuild/reflow | General graph service: `src/commands/graph_commands.py:10`/`:29`/`:52` expose CLI/MCP operations; `src/api/graph_layout.py:228` exposes versioned REST reads. `src/orchestrator/core.py:3754` schedules `src/orchestrator/layout_step.py:42`, consuming durable dirty marks, serializing jobs and recovering stale work even with no browser/server open. Moving it into the stateless dashboard would make the shared graph API depend on a UI process and grant that process database write authority. It remains an explicit API cost. |
| Per-request collapsed geometry | `src/api/graph_layout.py:398` computes the viewport API representation. Its bounded cache (`:232`, `:568`) is keyed by project, variant, published version and request parameters; it is not a browser session cache or polling loop. All API clients receive the same representation. |
| Fleet aggregation, history and relay polling | `src/main.py:366` starts `MetricsSampler`; `src/metrics/sampler.py:322` records operational performance and `:352` schedules bounded HTTP counter reads through `src/metrics/relay.py:95`. `/api/metrics/series` (`src/api/metrics.py:91`) and `metrics.tick` expose that durable history for diagnosis with or without a browser. Stopping the server produces an unavailable observation; the sampler cannot start it. |
| Health and event/session streams | `src/api/app.py:151`, `src/api/pane_stream.py:52`, `src/api/websocket.py:233` support general health/CLI/REST clients. The shared pane broadcaster is subscription-driven and cancels capture when unused. These are API responsibilities, not a UI host. |
| Roaming dashboard state | `src/commands/dashboard_state_commands.py:34` persists validated client documents through commands. No UI rendering, layout engine or polling task runs in these handlers. |

There is therefore **no promise of zero daemon CPU or database work** when a
dashboard is used. The enforced separation is zero dashboard hosting/rendering
in the daemon, with the shared API responsibilities above retained explicitly.
No required refactoring was split into follow-up tasks.
The supervisor explicitly confirmed preserving the legacy plan viewer on
`bright-cascade-28-plan-scope`; the guard allows exactly `/plans/{task_id}`.

## Regression coverage

`tests/test_api_dashboard_pointer.py::test_daemon_startup_cannot_import_or_host_the_dashboard`
runs in a fresh interpreter. It imports the real daemon entrypoint, constructs
the real API/MCP application, and enters its lifespans, replacing only uvicorn's
socket-serving method. It records forbidden import attempts even if caught,
rejects static/dashboard mounts, forbids hosting subprocess launches during API
startup, verifies dashboard/static and interactive API documentation routes are absent,
and verifies compatibility
redirects without following them. The database object remains uninitialized.
It also verifies that the sole HTML response declaration is the plan route,
exercises its real HTML response with a stub content provider, and checks
that the route exception does not extend to neighboring or malformed paths.

Existing dashboard-server and CLI tests exercise a real standalone process,
identity/PID checks, static serving, API relay, stop/restart and operation while
the upstream daemon is unavailable. The task result records exact commands and
outcomes; this audit does not assert an operator deployment or a live benchmark.

## Validation

Both test runs used `aq test` with its default worker cap/marker exclusions and
the documented disposable PostgreSQL test cluster on port 5534.

* Related area checks: **606 passed** in 145.46 seconds:
  `aq test tests/test_api_dashboard_pointer.py tests/test_embedded_mcp.py tests/test_dashboard_server_app.py tests/test_dashboard_server_bundle.py tests/test_dashboard_server_edge.py tests/test_dashboard_server_proxy.py tests/test_cli_dashboard_server.py tests/test_doctor_dashboard_server.py tests/test_install_dashboard.py tests/test_install_onboarding.py tests/test_install_service.py tests/test_update.py tests/test_update_rollback.py tests/test_metrics_relay.py --tb=short -q`.
* After adding the exact plan-viewer exception: **52 passed** in 129.99 seconds:
  `aq test tests/test_api_dashboard_pointer.py tests/test_health_platform.py tests/test_embedded_mcp.py --tb=short -q`.
* `ruff check tests/test_api_dashboard_pointer.py src/embedded_mcp.py` and
  `git diff --check` passed. `python scripts/generate-selection-catalogue.py`
  regenerated the catalogue without a change.

No claim, pool, hierarchy, formula or provider-failover implementation changed;
the swarm end-to-end suite was not required. No full-suite run was performed.
