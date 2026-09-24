# Dashboard performance — measured causes and fixes

Task `steady-bridge` (2026-09-23): "the dashboard feels laggy; make the
interface snappy, terminals excepted." Measure first, then fix the biggest
causes. This page records the method, the baseline, what was found, what was
changed and what was deliberately left.

## Method

Everything was measured against the live daemon (`:8081`, the operator's
real data: 13 projects, 218 tasks in `agent-queue`, 109 agents, 3,349
sessions) with headless Chrome driven over the DevTools protocol. The two
builds — `bc248153b` (before) and the branch head (after) — were each served by
`vite preview` (which proxies `/api` and `/ws` to the daemon) and measured back
to back by the same harness. The scripts and what each metric means are in
[`scripts/dashboard-perf/`](../../../scripts/dashboard-perf/README.md).

The daemon was busy throughout (other agents working, load average 18-29 on
the box), so API-bound latencies move by seconds between runs. Read the
tables as medians of three runs taken minutes apart; the request counts,
render counts, long tasks and bundle sizes are the stable signals.

The harness never writes the operator's roaming dashboard state: it answers
`dashboard/state-put` itself. (The first exploratory runs did write it — the
last open pane and project — and the pane was reset to closed afterwards.)

## Results

"Before" is `bc248153b`, "after" the branch head, measured back to back on
2026-09-23 (three runs each; medians). Times are milliseconds.

### Idle page (60 s after the surface settles)

The request rate was the headline problem: every route mounts the same shell
widgets, and they polled the daemon about once a second between them.

| Surface | API req/min | Long tasks/min | Blocking ms/min | React commits/min | Components rendered/min |
|---|---|---|---|---|---|
| Graph | 211 → **16** | 0 → 0 | 0 → 0 | 88 → 95 | 574 → 254 |
| Tasks | 92 → **20** | 0 → 0 | 0 → 0 | 48 → 39 | 1,183 → 1,447 |
| Reviews | 68 → **14** | 0 → 0 | 0 → 0 | 59 → 37 | 529 → 150 |
| Metrics | 77 → **13** | 2 → 0 | 5 → 0 | 148 → 96 | 4,830 → 2,244 |
| Agents | 77 → **18** | 0 → 0 | 0 → 0 | 31 → 35 | 2,901 → 1,040 |
| Sessions | 85 → **15** | **8 → 0** | **430 → 0** | 40 → 15 | **12,318 → 239** |
| Overview | 79 → **15** | 0 → 0 | 0 → 0 | 44 → 30 | 751 → 310 |

Before, 39-130 of those requests a minute were one `gate-list` per project
(13 projects), re-run on every `gate.*`/`task.*` frame; `agent/list` (the most
expensive read the daemon serves, see below) ran 10-25 times a minute. After,
the busiest path on any surface is `agent/list` at 2-5 a minute. WebSocket
traffic (~1 metrics frame a second plus events) is unchanged.

Returning to the tab after 20 s hidden fired **24 requests** at once before
(13 of them the gate fan-out; the slowest took 2.9 s) and **2** after (the
slowest 0.3 s).

### Interactions (input → the change on screen)

| Interaction | Visible before → after | Content loaded before → after | Input delay (Event Timing) |
|---|---|---|---|
| Open a task from the list | 985 → **115** | 985 → 299 | 64 → 48 |
| Open another task, pane already open | 1,012 → **64** | 1,012 → 261 | 40 → 48 |
| Open a task from the graph | 1,081 → **47** | 1,081 → 205 | 56 → 48 |
| Type in task search | 49 → 45 | — | 24 → 16 |
| Clear task search | 27 → 45 | — | 24 → 16 |
| Tab: tasks → graph | 517 → **220** | — | — |
| Tab: graph → tasks | 41 → 49 | — | — |
| Reviews: change state filter | 28 → 24 | — | — |
| Metrics: change time range | 45 → 37 | — | 24 → 24 |

"Visible" for a task is the pane showing the clicked task's title, status and
priority; "loaded" is its sections (attachments, sessions, comments) rendered.
Before, both waited on `/api/task/get`, which queued behind the roster read on
a busy daemon (75 ms alone, 1.2 s alongside `agent/list`).

### Route navigation

Warm (client-side, rail link) navigation was already fast except where a page
rendered too much; cold loads are bound by the daemon's latency.

| Surface | Warm ready | Warm blocking | Cold ready | Cold blocking | API requests during load | Main-thread script (cold) |
|---|---|---|---|---|---|---|
| Graph | 63 → 40 | 0 → 0 | 2,409 → 1,070 | 28 → 4 | 31 → 18 | 291 → 200 |
| Tasks | 57 → 56 | 0 → 0 | 2,138 → 1,967 ¹ | 27 → 0 | 30 → 15 | 281 → 210 |
| Reviews | 22 → 23 | 0 → 0 | 540 → 431 | 22 → 0 | 25 → 12 | 165 → 117 |
| Metrics | 211 → 321 | 1 → 39 | 3,691 → 1,888 | 127 → 44 | 26 → 12 | 428 → 290 |
| Agents | 56 → 85 | 0 → 0 | 10,221 → 3,079 | 16 → 0 | 32 → 13 | 380 → 182 |
| Sessions | **668 → 45** | **364 → 0** | 3,241 → 1,048 | 654 → 0 | 28 → 13 | 547 → 195 |
| Overview | 49 → 20 | 0 → 0 | 532 → 447 | 18 → 0 | 30 → 15 | 181 → 140 |

Cold "ready" is the surface's own content on screen (rows, canvas, pools)
from a reload with a warm HTTP cache. It varied by seconds between runs with
the daemon's load — the agents page waits on `agent/list` — so treat those
columns as indicative; the blocking time, request count and script time are
the stable ones. A first visit with an empty cache paints the shell in
~0.85-1.1 s in both builds: that path is the HTML → entry → shell chunk →
daemon waterfall, not JavaScript.

¹ Pooled median of nine runs per build (the full run plus two interleaved
focused runs): single-run medians ranged 1.1-2.8 s for both builds.

### Bundle

| | Before | After |
|---|---|---|
| Entry chunk | 1,097 KB (332 KB gz) | **413 KB (123 KB gz)** |
| Initial JS, shell only | 1,251 KB (382 KB gz) | 583 KB (181 KB gz) |
| Initial JS, graph route | 1,315 KB (404 KB gz) | 832 KB (264 KB gz) |
| Initial JS, tasks route | 1,305 KB (400 KB gz) | 642 KB (202 KB gz) |
| Initial JS, reviews / sessions / overview | 1,252-1,264 KB | 590-609 KB |
| Initial JS, agents route (terminal) | 1,621 KB (477 KB gz) | 953 KB (276 KB gz) |

### Roster read (`/api/agent/list`)

Against the operator database in an isolated read-only process
(`default_transaction_read_only=on`, 109 agents, 3,349 sessions):
**365 → 118 ms** median, and **89 → 0** agent reads per call; the old and new
implementations return identical rows on one frozen snapshot of the data (81
agents, with and without a project filter). In the daemon it took 0.7 s idle
and 1.1-3 s under load, blocking other requests while it ran. This takes
effect when the daemon runs the new code.

## Causes, ranked by user-visible impact, and what changed

1. **The shell's polling storm kept the daemon busy, so every click queued.**
   Always-mounted shell widgets polled `agent/list`, `pool/status` and pool
   sessions every 5 s and fanned `gate-list` out to every project every 20 s
   (80-210 requests a minute per open dashboard). `agent/list` held the
   daemon's event loop for 0.7-3 s per call: `task/get` went from 75 ms to
   1.19 s alongside it. *Fixed:* the roster, pool status and pool sessions are
   event-driven (`agent.*`, `session.*`, `task.*`, `message.*`, `pool.*` frames
   invalidate them) with a 30 s reconciliation poll; open gates are one
   unscoped `gate-list` limited to the listed projects, polled every 60 s and
   refreshed by `gate.*` frames; the palette's task list only loads while the
   palette is open (`c28d8a215`).
2. **Every stream frame was applied 2-5 times.** Each mounted
   `useEventStream` (root provider, agent-push bridge, project graph, drawer,
   open panes) ran the whole invalidation switch; React Query restarted the
   in-flight refetch each time and, since the fetchers ignore the abort
   signal, each restart was another request. *Fixed:* one cache pass per frame
   per QueryClient; the roster refresh is coalesced into one per 1 s window,
   always after the burst's last frame; session start/exit also refreshes pool
   supply (`94a7d8d8a`).
3. **The roster read itself was quadratic.** For each agent it rebuilt the
   owner of every session, rescanned them twice and read the agent back from the
   database. *Fixed:* the agent-independent part is built once per read
   (`5aa107f3e`, `src/agents/subagents.py`, `src/agents/service.py`).
4. **Opening a task waited on two round trips.** The pane showed only the id
   until `task/get` returned, and its sections then fetched their own data.
   *Fixed:* the header renders from the clicked row or card at once
   (`4ad06696f`), and the sections' reads start in parallel with the task read
   (`ed55cb014`). Interleaved re-runs: visible in 119-175 ms from the list and
   50-57 ms from the graph (before: 285-317 and 142-274).
5. **The sessions table rendered thousands of rows.** 2,947 rows (1.8 MB),
   all mounted and all re-rendered by every 15 s refresh: a 300-650 ms blocking
   task on each navigation and 8 long tasks a minute while idle. *Fixed:*
   virtualized (`749ff61bc`).
6. **The entry chunk carried every pane.** `panes/registry.ts` globbed the
   pane components eagerly, pulling React Flow, dagre, react-markdown, the
   unified/remark pipeline and yaml into the chunk every load parses, undoing the
   lazy routes. *Fixed:* panes are lazy chunks (`dd31250e4`); the task pane and
   the workspace's graph and task views are preloaded a moment after load, so
   the first click on them does not pay for the split (`53365eb6f`, `aff08fce9`).
7. **Refocusing the tab refetched everything.** The default 10 s `staleTime`
   is shorter than every poll, so each return to the tab and each page revisit
   refetched every mounted query. *Fixed:* 30 s default; live data comes from
   the stream, which keeps invalidating while the tab is hidden (`f9ef01b31`).
8. **Status readers re-rendered on every frame.** The event-stream context was
   one fresh object per frame; the Metrics page re-rendered per agent-output
   line. *Fixed:* separate status / buffer / subscription contexts
   (`65db20d4d`).
9. **The task pane polled its project's whole gate history** (190 KB) every
   20 s. *Reduced:* 60 s, since `gate.*` frames refresh it (`f34f8a758`); the
   payload itself is a follow-up (below).

## Against the targets

- **Common interactions within 100 ms:** opening a task (list or graph),
  searching, filtering reviews and changing the metrics range now answer in
  24-115 ms. The task's sections follow in 200-300 ms, bound by the daemon.
  Switching to the graph tab takes ~220 ms: React Flow mounting and laying
  out the canvas, the one interaction over 100 ms; getting it under would mean
  reworking the graph canvas's first render, which was out of scope here.
- **Route navigation usable within ~300 ms warm:** every warm navigation is
  20-85 ms except Metrics (~200-320 ms), which waits on its series read and
  draws the uPlot charts.
- **Idle: no recurring long tasks, much lower request rate:** no surface has a
  recurring long task (Sessions had 8 a minute); requests are down 4-13x.
- **Cold loads** are 0.4-3 s and bound by daemon latency under load, most of
  all the roster read, which the backend change addresses once deployed.

## Measured and deliberately left alone

- **`pool/status` costs ~0.65 s per call** (the scheduler's `_measure_pools`
  plus a `get_task` per live session). The shell now calls it 2-4 times a
  minute instead of 12; the per-call cost is follow-up `bold-horizon`, since it
  shares code with the scheduler.
- **The task pane downloads its project's whole gate history** to show one
  task's gates, and **the Sessions page downloads every session the project
  ever ran** (1.8 MB) on each refresh. Both need an API change (a task filter
  on `gate_list`, paging on `session_list`) and are filed as follow-ups.
- **zod in the entry chunk** (~15 KB gz of the 123 KB): only the pane
  manifests' argument schemas use it; moving them to `zod/mini` changes the
  schema types and error formatting in twelve files for about a tenth of the
  entry.
- **React commits on the idle Graph and Metrics pages** (~95 a minute): the
  Metrics charts are live at 1 Hz by design, and the graph's running-playbook
  timers tick each second; each commit renders a handful of components and none
  is a long task.
- **Two preference writes per pane open** (`shell_preferences`: the pane, then
  the surface kind), whose echo can trigger a re-read under load. They are off
  the render path.
- **Terminals:** xterm.js loads only with the agent and session views; writes
  go through xterm's own batching with flow control, the terminal and its
  socket are disposed on unmount, and xterm pauses rendering while offscreen.
  Nothing to change.

## Tests

- `npx vitest run` in `dashboard/`: 1,461 of 1,462 pass. The failure,
  `TaskActions` "explains an integration-history refusal", fails the same way
  on `bc248153b`. Two back/forward tests in `shell/__tests__/navigationHistory`
  are load-sensitive on both trees: under a load average of ~28 they failed
  in 3-5 of 5 runs of `bc248153b` as well, and they pass on a quieter box.
- New tests: one cache pass per frame and roster coalescing
  (`ws/__tests__/useEventStream.agents.test.tsx`), split stream contexts
  (`ws/__tests__/EventStreamProvider.test.tsx`), the single gate read
  (`api/__tests__/useAllOpenGates.test.tsx`), lazy pane registry, sessions
  virtualization, task-pane preview and prefetch, and the shared subagent index
  (`tests/test_agent_subagents.py`).
