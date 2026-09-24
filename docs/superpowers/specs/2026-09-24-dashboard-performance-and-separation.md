# Dashboard performance under load, and what "separated" really means — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [exclusive job queue](2026-09-24-exclusive-job-queue.md) ·
[resource-aware planner](2026-09-24-resource-aware-planner.md) ·
[managed long-running commands](2026-09-24-managed-long-running-commands.md) ·
[mobile dashboard](2026-09-24-mobile-dashboard.md) ·
[dashboard performance — measured causes and fixes (2026-09-23)](2026-09-23-dashboard-performance.md) ·
[dashboard performance investigation (2026-09-04)](2026-09-04-dashboard-performance-investigation.md) ·
[dashboard server design record](../../specs/dashboard-server.md) ·
[resource gating guide](../../guides/resource-gating.md)

## 1. The ask

Operator: *"Frontend must be snappy; may require backend caching or
architecture changes. Confirm the dashboard backend is actually separated from
the daemon (I think a task exists — verify it landed)."*

Context: the lag was noticed while a CI agent was hammering the machine. The
roadmap's working hypothesis is that the exclusive job queue (resource
isolation for heavy commands) removes much of it, so this spec is mostly about
**what to measure, when, and what to do if the job queue is not enough**.

Two deliverables, then:

1. A verified answer to "is the dashboard backend separated?" (§2.1 — short
   answer: *the page is, the API is not*).
2. A plan for staying snappy when the box is saturated.

## 2. What exists today

### 2.1 Separation: verified, and narrower than it sounds

**What landed (2026-09-21).** A separate *dashboard server* process serves the
built bundle and reverse-proxies the daemon:

- Code: `src/dashboard_server/` — `app.py` (Starlette `create_app`, line 242),
  `proxy.py` (`DaemonProxy`, one `aiohttp.ClientSession`, 64 KiB chunked relay,
  WebSocket handshake upstream-first), `bundle.py` (manifest-verified static
  files, `index.html` `no-cache`, hashed assets `immutable`), `edge.py`
  (Host/Origin/peer gates), `process.py` (PID/log/identity lifecycle),
  `settings.py`, `__main__.py`.
- Daemon side: `src/api/app.py` builds FastAPI with `docs_url=None`,
  `redoc_url=None` (lines 91–92), mounts no static files, and answers
  `/dashboard[/…]` with `dashboard_not_served_here` (line 220; `307` to the
  dashboard server, or `404` when it is disabled).
- Design record: `docs/specs/dashboard-server.md` (header: "implemented
  2026-09-21"). Operator summary: `docs/release-notes.md` § "2026-09-21 — The
  daemon is API only; the dashboard has its own server". Architecture page:
  `docs/concepts/architecture.md` § "Two processes".
- Tests present: `tests/test_dashboard_server_{app,bundle,edge,proxy}.py`,
  `tests/test_cli_dashboard_server.py`, `tests/test_doctor_dashboard_server.py`,
  `tests/test_api_dashboard_pointer.py`.
- **Git evidence caveat:** this checkout is a shallow clone (221 commits, oldest
  2026-09-22), so the landing commits are not reachable here —
  `git log -- src/dashboard_server` shows only `22ee291` (2026-09-22), and
  `13c58d2bd` (cited in `CLAUDE.md`) is not a valid object locally. The
  landing is established by the code, the tests and the release notes, not by
  commit archaeology. Anyone needing the commit list should run the same
  `git log` on an unshallowed clone.

**What it does *not* separate.** The dashboard server is a byte relay with no
state and no cache for API traffic (`proxy.py` adds `cache-control: no-store`
only on its own error answers; there is no response cache anywhere in
`src/dashboard_server/`). Every `/api`, `/health`, `/ready` and `/ws` request —
JSON reads, the event WebSocket (`/ws/events`), the terminal WebSocket
(`/ws/terminal/{session}`), the pane SSE (`/api/sessions/{id}/pane`) — is
answered by **the daemon process**. And inside the daemon, the API is not
isolated either:

- `src/main.py` runs the scheduler (`_run_scheduler_cycles`, line 86 →
  `orch.run_one_cycle()` every ~5 s), the Discord bot and the MCP/API server as
  sibling `asyncio` tasks in **one event loop** (`asyncio.run(run(...))`,
  line 610; tasks created at lines 411–414).
- `src/embedded_mcp.py` `run_mcp_server` runs the FastAPI app under a
  `uvicorn.Server` inside that same loop (lines 158–168), single worker.
- One SQLAlchemy engine per daemon: `src/database/engine.py`
  `create_postgres_engine` (`pool_size=pool_max`, `max_overflow=pool_max`,
  `pool_timeout=30`), with `database.pool_max_size` defaulting to 10
  (`src/config.py:1085`) — so ≤20 connections shared by the cycle, the API,
  the metrics sampler (1 sample/s) and everything else.

**Finding.** "Separated" today means *the static bundle and the browser-facing
edge are in their own process*, which buys: the page loads while the daemon is
down or restarting (proxied paths answer `503 daemon_unreachable`), no secrets
in the browser-facing process, and a smaller attack surface. It does **not**
buy latency isolation: any CPU-bound stretch on the daemon's event loop
(orchestrator cycle work, a slow roster read, JSON serialisation, event
fan-out), any PostgreSQL slowness, and any pool exhaustion shows up directly as
dashboard latency. The 2026-09-23 report measured exactly this: `task/get` went
from 75 ms to 1.19 s while `agent/list` held the loop for 0.7–3 s.

### 2.2 Prior performance work

**2026-09-04 investigation** (`2026-09-04-dashboard-performance-investigation.md`)
fixed the N+1 project graph endpoint (11.5 s → 134 ms), the per-cycle legacy
promotion scan (9.1 s → 42 ms) and recommended narrow projections, indexes,
`process_dirty` skipping pure status flips, WebSocket serialise-once (now in
`src/api/websocket.py`, which uses a shared `send_text(frame)` and logs per-event
at DEBUG), and `pg_stat_statements`.

**2026-09-23 fixes** (`2026-09-23-dashboard-performance.md`, task
`steady-bridge`), measured with headless Chrome against the live daemon at load
average 18–29:

| Fixed | Effect |
|---|---|
| Shell polling storm → event-driven roster/pool/gates + 30 s reconciliation (`c28d8a215`) | idle API req/min 68–211 → 13–20 |
| One cache pass per stream frame, coalesced roster refresh (`94a7d8d8a`) | removes 2–5× duplicate refetches |
| Roster read no longer quadratic (`5aa107f3e`) | `agent/list` 365 → 118 ms offline |
| Task pane renders from the clicked row; sections load in parallel | open task 985 → 47–115 ms visible |
| Sessions table virtualised (`749ff61bc`) | 300–650 ms blocking tasks gone |
| Panes code-split (`dd31250e4`) | entry 1,097 KB → 413 KB |
| `staleTime` 10 s → 30 s, split stream contexts | fewer refocus refetches / re-renders |

**Deliberately left** in that report: `pool/status` at ~0.65 s/call; the task
pane downloading the project's whole gate history; the Sessions page
downloading every session; zod in the entry chunk; ~95 idle React commits/min
on Graph/Metrics; the ~220 ms graph-tab switch (React Flow mount). The shallow
history shows three of those follow-ups already landed the same day:
`6eb5038` "perf(pools): reuse measurement for pool status", `927c2c3`
"fix(gates): filter gate list by task for detail pane", `6cbcaab`
"feat(sessions): page dashboard session history". Their measured effect is not
recorded anywhere I found — **unverified**.

**Harness.** `scripts/dashboard-perf/` (`harness.mjs` 576 lines, `focus.mjs`,
`chunks.mjs`, README): puppeteer-core + Chrome over CDP; per-surface cold/warm
ready, long tasks, blocking time, API requests, React commits, interaction
latency (Event Timing API), 60 s idle profile. It serves the build with
`vite preview` proxying to `:8081` — i.e. it **bypasses the dashboard server**,
so the relay hop is not in any published number.

### 2.3 Resource gating that already protects the daemon

`src/resources/limits.py` + `ResourcesConfig` (`src/config.py:2168`):

- Layer 1: every harness launch is wrapped in `nice -n session_nice`
  (default 10, `wrap_session_argv`), plus env caps
  (`PYTEST_XDIST_AUTO_NUM_WORKERS`, BLAS/OpenMP/libuv thread caps). Children —
  including the tests an agent runs — inherit the niceness. The docstring's
  stated goal is literally "so the daemon, dashboard and tmux stay responsive
  under load".
- Layer 2: `aq test` global flock slots (`test_slots`, default 2) and the
  full-suite lock (`src/resources/semaphore.py`).
- Layer 3: optional cgroup v2 scopes via `systemd-run --user --scope`
  (`ResourceCgroupConfig`: `enabled=False` by default, `cpu_quota_percent=600`,
  `memory_max=6G`), only when delegation is available.

What none of these touch: **PostgreSQL** (a separate server process, not
niced, and — depending on `POSTGRES_TEST_DSN` in `tests/pg_dsn.py` — possibly
the same server the daemon uses, so test databases compete for its buffers, IO
and connections), **IO priority** (no `ionice`/`IOWeight`), **memory
pressure** (only with cgroups on), and any heavy process not launched as an
agent session (an operator shell, a GitHub Actions self-hosted runner — which
"CI agent" meant is **unverified**).

### 2.4 Observability gaps

- No event-loop lag metric. `src/metrics/sampler.py` records
  `daemon.uptime_seconds`/`restarts`; `src/main.py`
  `_report_long_scheduler_cycle` only warns when one cycle exceeds 30 s.
- No per-route API latency histogram; the fleet-metrics tab cannot show "the
  API got slow at 14:02".
- `pg_stat_statements` status on the operator's PostgreSQL is unknown.

## 3. Gaps

1. **No latency isolation between the API and the orchestrator.** One loop,
   one pool; the dashboard is exactly as snappy as the daemon's worst await.
2. **No measurement under a controlled load.** Every published number was
   taken at whatever load the box happened to have; nothing says how the
   dashboard behaves at "one full-suite run + N agents".
3. **No loop-lag / request-latency telemetry**, so "the dashboard felt slow"
   cannot be correlated with cause after the fact.
4. **PostgreSQL and IO are outside resource gating.**
5. **Harness bypasses the dashboard server**, so its cost is unmeasured
   (expected small — it is an aiohttp relay — but unverified).

## 4. Implementation options

### Option A — Measure first, under synthetic load (prerequisite for all others)

*Sketch.* Extend `scripts/dashboard-perf/` with a `--load` profile that starts
a reproducible background load before the run: e.g. an `aq test` full-suite
run (or a CPU/IO stressor mimicking one: `stress-ng --cpu N --io M` at
`nice 10`, plus a pgbench-style read load on the same PostgreSQL). Measure
three configurations back to back: idle box, loaded box today, loaded box with
the job queue. Point the harness at the **dashboard server** (`:8082`) as well
as `vite preview` to price the relay. Add two cheap daemon metrics so the
harness (and the operator) can see cause: event-loop lag (a 100 ms `sleep`
drift probe sampled by `MetricsSampler`) and API p50/p95 per route bucket
(middleware in `src/api/middleware.py` alongside `RequestContextMiddleware`).

*Touches.* `scripts/dashboard-perf/*.mjs`, a small load script,
`src/metrics/sampler.py`, `src/api/middleware.py`, metrics tab (optional).

*Pros.* Tells us whether B–D are needed at all; turns "felt laggy" into a
number; loop-lag metric is useful forever. *Cons.* A realistic load profile is
a judgement call; measurements on a shared box stay noisy (the 09-23 report
used medians of three for this reason). *Size:* **S–M**.

### Option B — Read model / caching in the dashboard server

*Sketch.* Give the dashboard server a small read cache for hot, safe, idempotent
reads (`agent/list`, `pool/status`, `provider usage`, `gate-list`, graph
`extent`), invalidated by subscribing once to `/ws/events` and applying the
same invalidation rules the client already uses (`ws/useEventStream.ts`), with
a short TTL fallback. N browser tabs then cost the daemon one read, not N.

*Touches.* `src/dashboard_server/proxy.py`/`app.py` (new cache layer),
possibly a declared allowlist of cacheable routes. Breaks the design record's
"holds no state" property (`docs/specs/dashboard-server.md` §1) and must
respect auth: a cached answer must never cross a principal boundary (today
the browser is `LOCAL_SCOPE`, but `api_auth.require_session_token` exists).

*Pros.* Helps multi-tab / multi-device (phone + desktop) fan-out; keeps a warm
answer when the daemon stalls. *Cons.* Most of the fan-out problem was already
fixed client-side on 09-23; a single operator has 1–3 tabs; stale-read and
cache-invalidation bugs; the POST-based command API (`/api/execute`,
`/api/<category>/<cmd>`) is not naturally cacheable. Does nothing for the first
request after a stall. *Size:* **M**.

### Option C — Move the read API out of the daemon into its own process

*Sketch.* A second API process (could be the dashboard server growing a read
tier, or a separate `aq api-read` worker) with **its own event loop and its
own DB pool**, serving read-only routes directly from PostgreSQL; writes,
commands with side effects, `/ws/terminal`, pane SSE and anything touching
in-memory orchestrator state stay in the daemon. Events reach the read process
via the durable `events` table (already the WebSocket replay source) or
PostgreSQL `LISTEN/NOTIFY`.

*Touches.* A split of `src/api/` routers into read/write; many read handlers
go through `CommandHandler` and read in-memory state (`Orchestrator` pools,
provider availability snapshot, session manager), which would need either a
DB-only reimplementation or an internal RPC back to the daemon; auth
middleware; process management in `src/dashboard_server/process.py`-style
lifecycle; the dashboard server proxy routing table. Relaxes the import
boundary the design record tests (`tests/test_dashboard_server_app.py`).

*Pros.* True latency isolation from the orchestrator loop for reads; can be
niced/prioritised independently; scales reads with processes. *Cons.* Largest
change; the reads that were slow (`agent/list`, `pool/status`) are exactly the
ones built from in-memory daemon state, so they may not move cleanly; still
shares PostgreSQL, which under a CI load may be the actual bottleneck; two
sources of truth risk. *Size:* **L–XL**.

### Option D — OS-level priority for the daemon and PostgreSQL

*Sketch.* Keep the architecture; make the kernel favour it. Options, cheapest
first: (1) enable the existing cgroup layer by default where delegation works
(`resources.cgroups.enabled`) so session trees get `CPUQuota`/`MemoryMax`;
(2) add `ionice -c3` (idle IO class) to `wrap_session_argv` alongside `nice`;
(3) run the daemon and dashboard server with a higher `CPUWeight`/`IOWeight`
(systemd user unit or a scope at `aq start`), and document the same for
PostgreSQL; (4) keep test databases on a **separate** PostgreSQL server from
the daemon's (a `POSTGRES_TEST_DSN` recommendation plus a doctor check that
warns when they share a server).

*Touches.* `src/resources/limits.py`, `src/config.py` `ResourcesConfig`,
`src/config_tuning.py` (defaults + `TuningNote`s), `src/doctor/resource_checks.py`,
`scripts/setup-cgroup-delegation.sh`, `docs/guides/resource-gating.md`.

*Pros.* Small, composes with the job queue, protects everything the daemon
does (not just the dashboard). *Cons.* Priority does not fix loop-internal
stalls (a 1 s Python read is 1 s at any niceness); cgroup delegation is
host-dependent; raising daemon priority above agent sessions needs care not to
starve interactive tmux. *Size:* **S–M**.

## 5. Initial take

*Provisional.* Do **A now**, in two parts: add the loop-lag and per-route
latency metrics immediately (they are cheap and needed regardless), and build
the load profile so that measurement is ready the day the
[exclusive job queue](2026-09-24-exclusive-job-queue.md) lands. Then measure
idle / loaded-today / loaded-with-queue.

- If the job queue brings loaded p95 for the 09-23 interaction set within ~2×
  of idle, stop there plus the cheap parts of **D** (ionice, test PostgreSQL
  separation check).
- If loop lag stays high under load while CPU is available, the problem is
  inside the daemon: attack the offending awaits (the 09-04/09-23 method), and
  only then consider **C** for the specific reads that remain slow.
- **B** is the last resort for a single operator: the fan-out it would absorb
  was mostly removed client-side on 09-23. It becomes more attractive if the
  [mobile dashboard](2026-09-24-mobile-dashboard.md) means two or three
  concurrent devices routinely.

The operator should also be told plainly (release notes / dashboard guide) that
the dashboard server isolates *page availability*, not *API latency*.

## 6. Open questions

1. **What was the "CI agent"?** An AQ agent running `aq test` (niced 10,
   slot-gated), the CI main sentinel's repair task, an operator shell, or a
   self-hosted GitHub Actions runner (not niced, not gated)? Decides whether D
   and the job queue even reach the offending process.
2. **Do test databases share the daemon's PostgreSQL server?** If yes, PG
   buffer/IO contention may dominate and neither nice nor the job queue (unless
   it serialises test runs) helps. Decides priority of D(4).
3. **What is the latency target under load?** "Snappy" needs a number — e.g.
   task open visible ≤150 ms and API p95 ≤500 ms at load average ≈ cores.
   Decides when we stop.
4. **Which load profile is canonical?** Full-suite `aq test` vs. a synthetic
   stressor. A real suite is honest but slow and noisy; a stressor is
   repeatable. Decides the harness design in A.
5. **Is loop lag or PostgreSQL the bottleneck under CI load?** Unknown until A
   runs; decides between "fix awaits / move reads (C)" and "isolate PG (D)".
6. **Should the loop-lag metric feed the digest / doctor?** A `daemon.loop_lag`
   WARN would make future "it felt slow" reports self-diagnosing; decides scope
   of A.
7. **Does the dashboard server relay add measurable latency?** Expected
   negligible; one harness run against `:8082` answers it. If it is not
   negligible, it changes how we think about B.
8. **Is the "holds no state" property of the dashboard server worth keeping?**
   B and C both break it. Decides whether read-side work lives in the dashboard
   server or a third process.
9. **Would raising the daemon's CPU/IO weight starve interactive tmux sessions
   the operator is typing into?** Decides the knobs in D(3).

## 7. Dependencies and sequencing

1. Loop-lag + per-route latency metrics (A, part 1) — independent, do first.
2. Load-profile harness (A, part 2) — independent; ready before the job queue.
3. [Exclusive job queue](2026-09-24-exclusive-job-queue.md) lands → measure.
   [Managed long-running commands](2026-09-24-managed-long-running-commands.md)
   and [resource-aware planner](2026-09-24-resource-aware-planner.md) change
   the load shape too; re-measure after each if they land separately.
4. Decide D / C / B from the numbers.
5. [Mobile dashboard](2026-09-24-mobile-dashboard.md) adds concurrent devices;
   its terminal and task-list views should be in the harness surface list.

## 8. Non-goals

- Terminal rendering performance (xterm.js was reviewed on 09-23; nothing to
  change).
- Re-doing the 09-23 client-side work, or the graph-tab React Flow mount time.
- Horizontal scaling / multi-host daemons.
- Replacing PostgreSQL or the event bus.
- Changing the dashboard server's security edge (`edge.py`) — any read tier
  must sit behind the same gates.
