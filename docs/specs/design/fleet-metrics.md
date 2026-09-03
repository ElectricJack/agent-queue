# Fleet metrics

**Status:** implemented (task `wise-grove-39`)
**Surfaces:** `metrics_samples` table · `MetricsSampler` · `metrics.tick` bus event ·
`GET /api/metrics/series` · dashboard `/metrics`

The operator question this answers is *"what is the fleet doing right now, and
what was it doing five minutes ago?"* — asked most often while something is
already going wrong. Every design decision below follows from two constraints:
the answer has to be **live enough to watch** (about a second), and collecting
it must not become part of the problem it is measuring.

---

## 1. What is sampled

One sample is a JSON object. Its shape is defined by
`src/metrics/sampler.py` and mirrored field-for-field by the Pydantic models
in `src/api/models/metrics.py`, so the OpenAPI document describes it and the
generated TypeScript client types it.

| Group | Fields | Source |
| --- | --- | --- |
| `agents` | `total`, `by_state`, `by_harness`, `by_profile`, `by_lifecycle` | grouped count over `sessions` |
| `tasks` | `READY` / `IN_PROGRESS` / `ASSIGNED` / `PAUSED` / `BLOCKED` / `WAITING_INPUT` / `other` / `total` | grouped count over `tasks` |
| `subagents` | `spawned_per_hour`, `active` (== `total`), `native`, `aq`, `complete`, `by_session` | `subagent_events` (starts over `metrics.subagent_window_seconds`, and the open-child fold) + delegation provenance on `tasks` |
| `tokens` | `input_per_min`, `output_per_min`, `cache_read_per_min`, `cache_write_per_min`, `total_per_min`, `unattributed_per_min`, `*_per_min_1m`, `window_seconds`, `by_model` | `token_ledger` over `metrics.token_window_seconds`, scaled to per minute |
| `slots` | `used`, `total`, `cap` | `workspaces` rows with a `slot_index` |
| `machine` | `load1/5/15`, `cpu_count`, `mem_total_mb`, `mem_free_mb`, `mem_available_mb` | `os.getloadavg()`, `/proc/meminfo` |
| `daemon` | `uptime_seconds`, `restarts` | process clock + a durable counter in `system_config` |
| `stall` | `nudges_per_hour`, `kills_per_hour` | in-memory 1 h window of bus events |
| `throughput` | `completions_per_hour`, `prs_per_hour` | trailing hour of `task_completion_records` |
| `merges_per_hour` | scalar | in-memory 1 h window of `merge.succeeded` |
| `sampler` | `collect_ms` | the sampler timing itself |

### Three deliberate omissions

**Anthropic usage headroom.** `claude_usage` (`src/commands/system_commands.py`)
was evaluated and rejected as a source. It walks every
`~/.claude/projects/*.jsonl` transcript end to end — unbounded I/O that grows
with session history — and the only rate-limit information it returns is the
`subscriptionType` and `rateLimitTier` *strings* from the credentials file.
There is no headroom number to graph, so sampling it would cost real I/O for
no series.

**Pool supply and demand as a separate query.** `pool_status` calls
`Orchestrator._measure_pools`, which is far too heavy for a one-second loop.
The same signal is derived from aggregates the sampler already has:
`agents.by_lifecycle.pool` is supply, `tasks.READY` is demand.

**Per-session rows for idle sessions.** `subagents.by_session` omits sessions
with no children of any kind. Including them would put one entry per idle
session into 86,400 stored samples a day for no information. A session that
*did* start children inside the window keeps its row even after it exits —
keyed by session id rather than name, since the row it was named by is gone.
That set is unbounded on a busy pool fleet, so the drill-down is capped at the
25 busiest sessions per sample. The fleet totals stay exact; only the
breakdown is trimmed.

### Rates are windowed, and the window is not the sample cadence

Two series are counts of events, not gauges, and reading them over one
sample's worth of time turns them into noise:

* **Tokens.** A harness writes a whole turn's usage in one row, so a
  60-second window sampled once a second reads 0 for most seconds and spikes
  on the tick after each flush. The reported rates are measured over
  `metrics.token_window_seconds` (default 300) and scaled to per minute; the
  raw trailing-minute counts are kept beside them as `*_per_min_1m` rather
  than discarded, and `window_seconds` says which window produced the rest.
* **Sub-agents.** The open-child fold answers "how many are running right
  now", which on a pool fleet is ~0 because the sessions that start children
  exit before the children are counted. `spawned_per_hour` counts *starts*
  over `metrics.subagent_window_seconds` (default an hour) straight off
  `subagent_events`, with no join to live sessions at all. Both are reported:
  `active` is the instantaneous reading, `spawned_per_hour` is the one the
  tab plots.

### Cache tokens are tokens

`token_ledger.tokens_used` has always included cache reads and cache
creation, but until the ledger grew `cache_read_tokens` / `cache_write_tokens`
there was nowhere to put them, so `tokens_used - (input + output)` was
reported as `unattributed`. On a warm context that residue is three orders of
magnitude larger than fresh input, which is why a chart plotting
input + output as "total" looked broken. The cache columns stay *outside* the
priced input/output split — each is billed at its own rate, and folding
either into `input_tokens` would overprice every row — but they are inside
`total_per_min`.

### Honesty rules

* A value the platform cannot supply is `null`, never `0`. A zero load average
  is a claim; an absent one is an admission.
* `subagents.complete` is the **conjunction** over live sessions, matching
  `flock_rollup`: one session launched without its harness hooks wired makes
  `native` and `active` a lower bound for the whole fleet, and the tile says
  "at least" rather than showing a confident wrong number.
* Ledger volume no column can account for — a writer that reports only a
  total, or a row written before the cache columns existed — lands in
  `unattributed_per_min` rather than being folded into a model's rate, the
  same rule `get_costs` applies to pricing.
* `stall.*` and `merges_per_hour` come from bus events that are never
  persisted, so they restart at zero with the daemon. `daemon.uptime_seconds`
  is graphed beside them precisely so the reset is visible rather than
  silently smoothed over.

---

## 2. Storage

```
metrics_samples(id, resolution, bucket_ts, payload)
  UNIQUE (resolution, bucket_ts)
  INDEX  (resolution, bucket_ts)
```

`payload` is the JSON sample body rather than a wide column set: the metric
surface is dict-shaped (counts per harness, per profile, per model) and still
growing, and a schema migration per new series would make the sampler
expensive to extend. The unique constraint is what makes every writer
idempotent — a tick that fires twice for the same second, or a roll-up re-run
after a restart, updates the bucket it already wrote.

Migration `d3e7b1c9a204` also adds `idx_token_ledger_timestamp` and
`idx_task_completion_records_completed_at`. Both tables are append-only and
unbounded, and neither had an index on the column the sampler's trailing
windows filter on; without them the "cheap sampler" requirement turns into a
full scan of the ledger every few seconds.

### Resolutions and retention

| Tier | Step | Default retention | Config |
| --- | --- | --- | --- |
| `1s` | 1 s | 1 hour | `metrics.retain_seconds_1s` |
| `1m` | 60 s | 30 days | `metrics.retain_seconds_1m` |
| `1h` | 3600 s | 365 days | `metrics.retain_seconds_1h` |

`0` means keep forever. The sweep runs on the same tick as the roll-up and
deletes all three tiers in one transaction.

### Roll-up arithmetic

Numeric leaves are **averaged**, not summed, because every series is a gauge —
a count of things true right now, or a rate already expressed per minute or per
hour. Summing would turn "12 agents for 60 seconds" into 720 agents. Booleans
and strings take the **last** value: for a completeness flag the newest reading
is the honest one. A key only some samples carried averages over the samples
that actually had it, so a series that started mid-minute is not diluted by
zeros it never reported.

Only *closed* buckets roll up — averaging the bucket containing `now` would
publish a minute that later changes. The resume point is the newest stored
bucket at the coarser tier, so a daemon that was down for ten minutes backfills
those ten minutes on its first pass; it is then held in memory and advanced
past every bucket examined, empty ones included, because an idle install whose
target tier stays empty would otherwise re-walk its whole retention window
(720 hour-buckets) every minute forever.

---

## 3. Cost

Cost is the design constraint, so the work is split by how expensive it is:

| Cadence | Work | Config |
| --- | --- | --- |
| every tick (1 s) | 2 grouped counts on **one** connection, `getloadavg`, one `/proc/meminfo` read, in-memory counters | `metrics.interval_seconds` |
| every 5 s | sub-agent fold, delegation fold, token-ledger window, slot count — all on **one** connection; results carried into intervening samples | `metrics.slow_interval_seconds` |
| every 5 s | one batched commit of the buffered per-second samples | `metrics.flush_interval_seconds` |
| every 60 s | completions/PRs per hour, roll-up, retention sweep | `metrics.rollup_interval_seconds` |

Measured against a 20-session fleet (20 live sessions, 260 sub-agent events,
420 tasks, 20,000 ledger rows, 120 simulated wall-clock seconds so every
cadence fires):

| Backend | p50 per tick | amortised | share of one core |
| --- | --- | --- | --- |
| PostgreSQL | 3.6 ms | 7.3 ms/s | 0.73 % |
| SQLite (WAL) | 5.5 ms | 20 ms/s | 2.0 % |

The first implementation measured **70 ms per tick** on SQLite. Profiling
found two costs, neither of them the queries: a bare `engine.connect()` on
SQLite's `NullPool` builds a fresh connection and replays its PRAGMA setup
(~3.7 ms), and every commit is an fsync (~55 ms on the reference box). Hence
the two grouped readers and the write buffer. Samples buffer in memory and
commit once per flush window; the live WebSocket tick is emitted *before* the
write, so nothing on screen waits for the disk, and the only thing at risk on
a hard kill is the newest few seconds of stored history. `stop()` flushes.

`sampler.collect_ms` is itself a series, so the sampler's overhead is one of
the things you can graph.

---

## 4. Transport

**Live.** `metrics.tick` is emitted on the EventBus every tick and forwarded
to WebSocket clients (`"metrics."` is in `_FORWARDED_PREFIXES`). It is never
written through `db.log_event` — at one per second that would add ~86k rows a
day to `events` and flood every reconnect's replay.

A sample is fleet-wide by construction (session names, profile ids and machine
load across every project), so there is no per-project projection to hand a
scoped worker: `_metrics_event_allowed` forwards only to the trusted local
surface and to elevated sessions. That is also what keeps a 1 Hz tick off
twenty worker connections that would never render it.

**History.** `GET /api/metrics/series?from&to&step`. `step` is `auto` (the
default), `1s`, `1m` or `1h`. `auto` picks the finest tier whose point count
fits `MAX_POINTS`; an explicit step that would blow that budget is coarsened
and the response sets `truncated`, because silently returning a short window
would make the chart lie about the range its axis is labelled with.

---

## 5. Dashboard

Route `/metrics`, linked from the left rail. `dashboard/src/pages/metrics/`.

History is fetched once per range change (5 m / 1 h / 24 h / 7 d); everything
after that is appended client-side from `metrics.tick`, so a one-second cadence
never costs a request. On a coarse range the live tail is thinned to the served
step, and the appended point is the latest instantaneous sample until the next
history load replaces it with the server's roll-up.

Metrics ticks are excluded from the `EventStreamProvider` activity buffer —
at one a second they would evict its 500-entry window every eight minutes —
and short-circuit the query-invalidation switch in `useEventStream`, which they
have no keys in. The page subscribes through `useRawEventSubscription`
instead.

### Chart library: uPlot

No chart library was installed before this page, so the choice was open.

| | uPlot | Recharts |
| --- | --- | --- |
| Bundle | ~45 KB min | ~500 KB with d3 deps |
| Rendering | canvas | SVG, one DOM node per point |
| 1 h at 1 s × 8 series | one canvas repaint | tens of thousands of nodes, full React reconciliation at 1 Hz |
| Update API | imperative `setData` | props → re-render |

The lazy-loaded `/metrics` chunk is 64.8 KB (28.2 KB gzipped) including uPlot,
and costs nothing on any other route. The price is
`TimeSeriesChart.tsx`: uPlot owns its own DOM, so the instance is created once
in an effect and fed by ref afterwards. Its light-theme stylesheet is
overridden in `index.css` to match the dark-only shell.

---

## 6. Configuration

```yaml
metrics:
  enabled: true
  interval_seconds: 1.0
  slow_interval_seconds: 5.0
  flush_interval_seconds: 5.0
  rollup_interval_seconds: 60.0
  retain_seconds_1s: 3600
  retain_seconds_1m: 2592000    # 30d
  retain_seconds_1h: 31536000   # 365d
```

Hot-reloadable. `enabled: false` starts nothing.

---

## 7. Files

| Path | Role |
| --- | --- |
| `src/metrics/sampler.py` | collection, roll-up, retention, the tick loop |
| `src/database/queries/metrics_queries.py` | grouped aggregates + the sample store |
| `src/database/tables.py` | `metrics_samples` |
| `migrations/versions/d3e7b1c9a204_*.py` | the table and two supporting indexes |
| `src/api/metrics.py` · `src/api/models/metrics.py` | `GET /api/metrics/series` |
| `src/api/websocket.py` | `metrics.` forwarding + scope gate |
| `src/event_schemas.py` | `metrics.tick` schema |
| `src/main.py` | sampler lifecycle |
| `dashboard/src/pages/metrics/` | the tab |
| `dashboard/src/api/metrics.ts` | history hook |
| `tests/test_metrics_sampler.py` · `tests/test_api_metrics.py` | backend |
| `dashboard/src/pages/metrics/__tests__/Metrics.test.tsx` | frontend |
