# Dashboard performance measurement

The scripts behind the numbers in
[`docs/superpowers/specs/2026-09-23-dashboard-performance.md`](../../docs/superpowers/specs/2026-09-23-dashboard-performance.md).
They drive headless Chrome over the DevTools protocol against a built
dashboard, so a change can be measured the same way before and after.

They need `puppeteer-core` (not a repo dependency) and a Chrome binary
(`CHROME`, default `/usr/bin/google-chrome`):

```bash
work=$(mktemp -d) && (cd "$work" && npm init -y >/dev/null && npm i puppeteer-core@24)
cp scripts/dashboard-perf/*.mjs "$work"/

# Build the revision to measure and serve it; vite preview proxies /api and
# /ws to the daemon on :8081 (AQ_API_TARGET overrides).
(cd dashboard && npx vite build --outDir "$work/build" --emptyOutDir)
(cd dashboard && npx vite preview --outDir "$work/build" --port 4173 --strictPort --host 127.0.0.1) &

cd "$work"
node harness.mjs http://127.0.0.1:4173 result.json --runs 3 --idle-ms 60000
node focus.mjs http://127.0.0.1:4173 /projects/agent-queue/tasks 20000
node chunks.mjs build
```

## What `harness.mjs` records

Per surface (graph, tasks, reviews, metrics, agents, sessions, overview):

- **cold** — a fresh page: time from navigation to the surface's own
  "ready" predicate (its rows / canvas / controls rendered, no "Loading…"),
  long tasks and total blocking time until 2.5s after ready, API requests,
  React commits, and main-thread script / task time (`Performance.getMetrics`).
- **warm** — client-side navigation between surfaces in one session, as a
  rail link click: time to ready, blocking time, commits and component renders.
- **interactions** — open a task from the list (time until the pane shows the
  clicked task, and until its sections have loaded), open another, type in and
  clear the search, switch graph/tasks tabs, open a task from the graph, change
  the reviews filter, change the metrics range. Input latency comes from the
  Event Timing API (`PerformanceObserver` type `event`).
- **idle** — 60s on the surface after it settles: API requests per minute (by
  path), WebSocket frames, long tasks, React commits and component renders.

React commits are counted through a stand-in `__REACT_DEVTOOLS_GLOBAL_HOOK__`
installed before the bundle runs; "component renders" are fibers whose render
function ran in the commit (`PerformedWork`), skipping subtrees React bailed
out of.

The harness answers `dashboard/state-put` and `state-reset` itself instead of
sending them: the dashboard persists roaming preferences (last pane, widths,
last project) as the operator's user, and a measurement run must not rewrite
them. The latencies it reports are measured against a live daemon and move
with its load, so compare runs taken back to back and read medians.
