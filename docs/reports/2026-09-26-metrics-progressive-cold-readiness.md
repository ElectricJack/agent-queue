# Metrics minute overview and second-level refinement

Task `wise-ember.17`, claim epoch 3. This continues the preserved Tasks fix
and the [previous matched experiment](2026-09-26-tasks-metrics-cold-readiness.md).
That experiment's loaded Metrics cold p95 was 2377/2714 ms for client settings
1/3, above the 2000 ms target. The supervisor-authorized matched pair completed
on 2026-09-27 against published candidate
`1c5bcb351b168b7c6006082db04404c6e7a89a85`. Metrics cold p95 passes in both modes:
775/857 ms idle and 881/1665 ms loaded. **Overall task acceptance remains unmet:**
Tasks and several Agents/pane results exceed their targets. Ambient contention
and naturally reduced history density differ from the earlier baseline; this
pair does not establish a causal regression in the preserved Tasks fix or a
performance result on LAN/tailnet. The isolated services were stopped and the
window released after all repetitions.

## Server request profile

The bounded profile read only the isolated `agent_queue_e2e` database on
localhost:5534, under `SET TRANSACTION READ ONLY`. No migration, daemon
lifecycle, fixture writes, operator database access, or helper load occurred.
The query ends at the newest retained second sample, rather than the wall
clock: the isolated daemon was stopped, and using the present hour would
silently shrink the series. This is a retained-data diagnostic, not a live
request or performance acceptance run.

| Stage | Second history | Minute history |
| --- | --- | --- |
| Rows | 3482 | 58 |
| Stored payload bytes | 8,573,338 | 175,616 |
| EXPLAIN ANALYZE execution | 36.745 ms | 0.828 ms |
| Query/transfer raw ms | 76.88, 77.64, 87.19 | 4.75, 2.94, 2.27 |
| JSON decode raw ms | 564.35, 565.73, 495.86 | 12.42, 273.65, 6.61 |
| ASGI validation/encoding raw ms | 1942.06, 1855.03, 2439.59 | 14.18, 19.23, 14.51 |
| Encoded response bytes | about 9,484,815 | about 190,083 |
| Diagnostic gzip level 6 raw ms | 75.75, 85.02, 90.29 | 3.05, 2.80, 2.81 |
| Diagnostic compressed bytes | about 525,122 | about 19,529 |

The second-tier query uses a sequential scan and external merge sort (4760 KB
disk); the minute-tier query uses `idx_metrics_samples_res_ts` and an in-memory
sort. The observed SQL cost is much smaller than JSON decoding and nested
model validation. The profile includes Python profiling and ambient contention;
the 273.65 ms minute decode outlier is retained, without assigning a cause.
ASGI excludes database reads. The gzip measurements are isolated compression
diagnostics: neither the router nor the captured browser responses apply gzip.
Compression would reduce transfer bytes but still leave decode/validation work.

A separate replay of the already-normalized earlier capture (3471 rows) took
902.96/850.14/767.30 ms for ASGI validation/encoding and produced about 9.36 MB.
Its synthesized 61 minute buckets took 9.47/21.21/15.41 ms. These are different
inputs from the retained raw database payloads and must not be conflated.

## Change and data semantics

The selected hour requests the existing stored minute tier for first paint,
then requests its complete second tier after two animation frames. Both reads
cover the selected hour, use distinct TanStack Query keys, and retain the
initial-route prefetch. The page labels minute averages and pending detail;
successful refinement replaces that overview with the second-level series.
Minute rollups already average gauges and merge histogram buckets by addition.
The change adds no server query, index, sampler tier, cache, dependency, schema,
or retention policy.

The hour retains every incoming one-second WebSocket tick during refinement.
History replacement removes only the overlap covered by the returned series,
so ticks received after a slow request began remain visible. Full detail
restores the sustained-lag reader's second-level evidence. An empty minute
overview waits for detail; it does not produce a falsely ready empty chart.
A failed detail read retains a labelled minute overview and a retry message.
Range changes discard late detail from the previous range. The other ranges
retain their single history read and existing coarse-tail behavior.

Source: `dashboard/src/api/metrics.ts`,
`dashboard/src/pages/metrics/useMetricsFeed.ts`, and
`dashboard/src/pages/metrics/Metrics.tsx`. The behavior is documented in
`docs/specs/design/fleet-metrics.md` §5. Tasks and its prefetch are preserved.

## Production-bundle browser diagnostic

Chrome 150 at 1600×1000 loaded the production bundle against an ephemeral
localhost ASGI server using the router and retained 3482/58-row inputs.
Timestamps were rebased onto the diagnostic page's hour; every stored value
and row count remained. Shell/provider reads and the socket were mocked.
There was no helper load, database read during navigation, real daemon,
three-client idle observation, task-pane exercise, or LAN/tailnet measurement.
This demonstrates first-paint/refinement behavior, not the matched acceptance
protocol.

| Milestone | Raw ms | Median ms | Nearest-rank p95 ms | (max−min)/median |
| --- | --- | --- | --- | --- |
| Minute overview, 15 canvases | 621.2, 573.9, 517.1 | 573.9 | 621.2 | 0.181 |
| Complete second-level detail | 2536.9, 2477.7, 2790.7 | 2536.9 | 2790.7 | 0.123 |

All three first paints visibly said `1-minute averages · loading 1-second
detail`; all three refinements said `1-second samples`. Request timing shows
the detailed read starts after the recorded overview paint. All runs have
zero page errors. This browser run preceded the final failed-reload labelling
guard; that guard only changes the error message after successful detail has
already loaded, and the subsequent focused checks cover it. The fixture server was stopped gracefully and its ephemeral
port was verified free.

## Local verification and evidence

- Focused: `npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__/Metrics.test.tsx` — 35 passed after adding sustained-lag restoration and failed-reload labelling cases.
- Related area: `npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__ src/pages/command-center/__tests__/Tasks.test.tsx src/pages/command-center/__tests__/useGraphLive.test.tsx src/ws/__tests__/useEventStream.wire.test.tsx src/ws/__tests__/useEventStream.agents.test.tsx` — 135 passed across nine files, before the two additional cases. The subsequent focused run covers both added cases.
- `npm run typecheck` — pass.
- `npm run lint` — zero errors, 37 existing warnings.
- `python scripts/build_release_artifact.py` — pass; invokes `npm run build` and stages a verified 103-file bundle.
- `git diff --check` — pass.

No server production files or API models changed in this checkpoint; the
preserved backend patch's checks are recorded in the previous report. No
full-suite run was started. The profile scripts and browser fixture are
diagnostic artifacts, not shipped production code or new test modules.

Artifacts:
`/home/jkern/.agent-queue-e2e/dashboard-perf/2026-09-26/wise-ember.17/claim-epoch-3/`.
`profile-server-replay.json` and `profile-server-isolated.json` retain every
stage's raw timings and SQL plans; `local-browser-input.json` preserves raw
retained payloads; `local-browser-results.json` contains browser captions,
request/resources and timings; `local-browser-cleanup.json` proves cleanup.
The initial replay's per-call `.prof` files were replaced by the later raw-row
profile; the replay's raw timing JSON remains preserved. Later profiling uses
fresh artifact paths. Source and runner identities and artifact hashes are
recorded separately in the checkpoint manifest.

## Matched idle/loaded pair — 2026-09-27

Artifacts:
`/home/jkern/.agent-queue-e2e/dashboard-perf/2026-09-27/wise-ember.17/claim-epoch-3/pair-1/`.
The original before run is
`/home/jkern/.agent-queue-e2e/dashboard-perf/2026-09-25/wise-ember.12/claim-epoch-4/pair-3/`.
The preserved previous implementation pair is recorded separately in the linked
report; it is not substituted for this original baseline.

Target revision and runner revision are independently recorded as
`1c5bcb351b168b7c6006082db04404c6e7a89a85`; report-only publication follows this
run. The deployed 103-file production bundle was verified against its manifest.
Both mode commands exited zero; no repetition was discarded. The exact commands
are retained in `pair-status.json` and are the task's prescribed commands:

```bash
python scripts/dashboard-perf/experiment.py \
  --dashboard-url http://127.0.0.1:8092 --api-url http://127.0.0.1:8099 \
  --config /home/jkern/.agent-queue-e2e/config.yaml --project perf-fixture \
  --repetitions 3 --clients 1,3 --warmup-ms 30000 --observe-ms 120000 \
  --surfaces tasks,agents,metrics \
  --load-args '--seconds 1800 --iterations 200000000000 --io-bytes 137438953472 --tmp-max-mib 256 --workers 4' \
  --mode idle --out '<artifact-directory>/idle'
# Repeat the identical command with --mode loaded and --out '<artifact-directory>/loaded'.
```

All 80 raw-data/protocol comparison checks pass: browser Chrome
150.0.7871.46, viewport 1600×1000, PAUSED fixture with 10000 READY tasks and
zero live sessions, three repetitions, client settings 1/3, timing, surfaces,
parent/target/runner nice 10, and baseline child caps 3/3/2 match. Actual loaded
helper nice is 19, matching the baseline. The isolated database is
localhost:5534/agent_queue_e2e, distinct from the operator database. No reset,
migration, operator lifecycle change or exclusive BoxLock occurred.

The target began warming at 04:46 UTC. Its history persists across restarts,
but the old second history ended before startup. The 05:48 timer resolved
without a worker turn; the supervisor re-woke the worker and the pair began at
08:27 UTC. Read-only verification then found 3258 samples spanning 3594 seconds,
last sample age 5.5 seconds and maximum gap 4 seconds. The restart gap had aged
out. An external diagnostic check expecting at least 3400 rows failed; its
original script/result remain preserved. Full-hour coverage was accepted without
changing natural samples or waiting for a favorable density. This is below the
original baseline's roughly 3480 rows and is an additional comparison limit.
The 4.46 MB stored-row size includes PostgreSQL row sizing and must not be
compared with the earlier 8.57 MB JSON payload measurement.

### Cold readiness and task-pane visibility

Nearest-rank p95 is the maximum of three cold repetitions; pane p95 is the
maximum of six interaction samples per client setting. Spread is
(max−min)/median. Client setting 3 does not mean three simultaneous cold starts.

| Mode | Clients | Surface | Before raw ms | Before p95 | Before spread | After raw ms | After p95 | After spread |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| idle | 1 | Tasks | 1476, 1542, 1855 | 1855 | 0.246 | 3374, 2858, 3506 | 3506 | 0.192 |
| idle | 1 | Agents | 731, 984, 744 | 984 | 0.340 | 2530, 2718, 1630 | 2718 | 0.430 |
| idle | 1 | Metrics | 2626, 3258, 3022 | 3258 | 0.209 | 654, 529, 775 | 775 | 0.376 |
| idle | 3 | Tasks | 1874, 1281, 1319 | 1874 | 0.450 | 1285, 2342, 1871 | 2342 | 0.565 |
| idle | 3 | Agents | 1124, 838, 1095 | 1124 | 0.261 | 1857, 2183, 1052 | 2183 | 0.609 |
| idle | 3 | Metrics | 2927, 2651, 2723 | 2927 | 0.101 | 600, 857, 514 | 857 | 0.572 |
| loaded | 1 | Tasks | 1242, 1975, 2860 | 2860 | 0.819 | 1745, 2541, 2791 | 2791 | 0.412 |
| loaded | 1 | Agents | 1186, 1009, 1491 | 1491 | 0.406 | 3599, 1215, 1509 | 3599 | 1.580 |
| loaded | 1 | Metrics | 2731, 5114, 6342 | 6342 | 0.706 | 881, 626, 650 | 881 | 0.392 |
| loaded | 3 | Tasks | 2494, 1328, 1732 | 2494 | 0.673 | 3538, 874, 2100 | 3538 | 1.269 |
| loaded | 3 | Agents | 990, 1138, 1551 | 1551 | 0.493 | 1914, 1819, 845 | 1914 | 0.588 |
| loaded | 3 | Metrics | 3313, 2676, 3409 | 3409 | 0.221 | 1665, 554, 515 | 1665 | 2.076 |

| Mode | Clients | Before pane raw ms | Before p95 | After pane raw ms | After p95 | After spread |
| --- | --- | --- | --- | --- | --- | --- |
| idle | 1 | 110, 60, 114, 56, 107, 62 | 114 | 184, 81, 158, 72, 226, 69 | 226 | 1.314 |
| idle | 3 | 114, 60, 112, 56, 100, 60 | 114 | 152, 100, 314, 140, 159, 78 | 314 | 1.616 |
| loaded | 1 | 111, 47, 127, 60, 159, 79 | 159 | 210, 108, 182, 107, 160, 130 | 210 | 0.710 |
| loaded | 3 | 146, 57, 138, 60, 143, 63 | 146 | 272, 105, 150, 99, 126, 59 | 272 | 1.844 |

Metrics meets 2000 ms for both settings in both modes. Tasks misses it in all
four combinations. Agents passes only loaded clients 3. All pane p95 values
miss 200 ms. Overall acceptance is therefore false. The local progressive-load
fix and its earlier tests are preserved, but a passing task close or a claim
that the old Tasks gains regressed solely because of this change is unsupported.

All measured direct API routes have zero errors (90 reads per route and client
setting in each mode). These metrics-series reads exercise a short direct API
window, not the browser's initial hour overview/refinement request.

| Mode | Clients | Direct metrics-series p95 ms | Task-list p95 ms | Agent-list p95 ms |
| --- | --- | --- | --- | --- |
| idle | 1 | 96.58 | 2729.01 | 2168.92 |
| idle | 3 | 67.79 | 2436.04 | 1928.55 |
| loaded | 1 | 41.77 | 2414.46 | 1569.17 |
| loaded | 3 | 42.26 | 2180.75 | 1495.13 |

### Workload completion and ambient conditions

The requested CPU work remains 200000000000 iterations and requested IO remains
137438953472 bytes per repetition, with four workers, an 1800-second deadline
and a 256 MiB disk cap. All repetitions cover every browser and API observation
at fraction 1.0. All complete the requested IO, reach the configured CPU deadline,
exit zero without a wrapper timeout, and remove their temporary directory. Peak
disk usage is exactly the cap; there was no interruption or suppressed workload.

| Repetition | Before completed CPU iterations | After completed CPU iterations | After iterations/s | After elapsed s | Helper-only tail s |
| --- | --- | --- | --- | --- | --- |
| 1 | 83495700000 | 52878500000 | 29365701.4 | 1800.689 | 567.787 |
| 2 | 74991500000 | 59601100000 | 33100606.4 | 1800.604 | 632.220 |
| 3 | 74650000000 | 72735300000 | 40396079.2 | 1800.553 | 700.535 |

Median throughput falls from 41646220.6 to 33100606.4 iterations/s (20.52% lower); spread rises from 0.118 to 0.333. Requested work matches, but completed CPU work and effective ambient contention do not. The latency improvement must be read beside that limitation.

Passive `/proc` records every five seconds retain host load, PSI, memory,
and process CPU snapshots every minute in `ambient.jsonl`. Idle means no bounded
helper, not an otherwise idle host. The host has 24 cores.

| Mode | Load1 median / p95 / max | CPU some PSI avg10 median / p95 / max | Memory some PSI max | IO some PSI max |
| --- | --- | --- | --- | --- |
| idle | 19.38 / 33.29 / 43.79 | 4.61 / 24.37 / 43.45 | 4.27 | 4.89 |
| loaded | 23.50 / 35.54 / 40.87 | 8.43 / 30.75 / 39.86 | 7.79 | 6.01 |

The isolated sampler's loaded browser-active peak CPU some PSI is 38.41%,
versus 5.44% in the baseline; memory some PSI is 5.80% versus 2.82%.
Its observed pytest-process count peaks at 36 versus 8. The raw records retain
these competing processes; this worker did not suppress or change other work.
This materially limits a before/after causal comparison, even though the fixed
protocol fields match.

### Full-window and browser-active histograms

Bucket counts are added, never averaged across percentile results. Browser-active
samples are stamped inside the complete harness interval from cold navigation
through direct reads, excluding warmup and helper-only tail. Raw merged loop,
API, pool wait, query and relay histograms for both before/after modes and scopes
are retained in `histogram-evidence.json`. All 48 independently reconstructed
sample-count and percentile comparisons match the summaries. The finite bucket
bounds in ms are 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
plus overflow. The percentile values below interpolate within those buckets.

| Run / mode | Scope | Samples | Loop p95 ms | API p95 ms | Query p95 ms | Pool wait p95 ms | Relay p95 ms | Loop observations >500 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| before idle | full | 3154 | 72.72 | 934.03 | 22.17 | 0.952 | 988.92 | 61 |
| before idle | active | 3063 | 75.32 | 934.18 | 22.48 | 0.952 | 988.92 | 59 |
| before loaded | full | 5197 | 53.71 | 1160.62 | 22.51 | 0.952 | 1538.70 | 419 |
| before loaded | active | 3137 | 123.72 | 1168.15 | 29.15 | 0.952 | 1538.70 | 316 |
| after idle | full | 3172 | 178.59 | 1924.78 | 46.99 | 0.953 | 1990.09 | 675 |
| after idle | active | 3083 | 184.48 | 1925.11 | 47.36 | 0.953 | 1990.09 | 665 |
| after loaded | full | 4935 | 90.86 | 2075.47 | 45.13 | 0.953 | 3062.22 | 791 |
| after loaded | active | 3040 | 192.95 | 2096.23 | 55.20 | 0.954 | 3062.22 | 605 |

After idle browser-active loop p95 raw by repetition: 168.526, 272.557, 157.484 ms; spread 0.683.

After loaded browser-active loop p95 raw by repetition: 302.961, 211.328, 130.975 ms; spread 0.814.


The after loaded full-window loop p95 of 90.86 ms must not replace its
browser-active 192.95 ms. Helper-only tails span 568/632/701 seconds, materially
diluting the full-window result. Neither result demonstrates a passing daemon
loop envelope.

There are 659 idle and 781 loaded sampler intervals whose maximum loop drift
exceeds 500 ms; multiple loop observations may occur in one interval, hence the
different histogram counts above. Complete intervals and route histograms are
retained in `idle-stall-samples.json` and `loaded-stall-samples.json`. This target
records only collapsed route labels `GET /` and `POST /`. The supervisor's .16
observation that every large stall involved metrics-series cannot be verified
or refuted from these route labels. The earlier server profile supports heavy
hour serialization cost; this browser change moves it after first paint rather
than removing that daemon work. Coincidence within a sampler interval alone is
not a causal attribution.

### Cleanup, evidence and verdict

The independent watcher stopped only verified owned edge PID 2147946 and target
PID 2145628. Ports 8092/8099 were verified free at 11:00:09 UTC and again by the
worker. The fixture database/container was retained. The ambient recorder stopped
and the supervisor's release notice was queued successfully. Timer/wake issues
and the unrelated start-marker reply that satisfied the first message wait are
recorded as coordination facts, not performance exclusions.

`pair-analysis.json` preserves all cold/pane/direct API raw samples and 80
comparison checks. Each mode's `summary.json`, `harness-*-clients-*.json`,
`series-*.json`, `load-*.json`, before/after inventories, manifests and workload
logs remain authoritative. `completion-summary.json`, service/preflight/bundle
verification, `warm-history.json`, `ambient.jsonl`, stall intervals and
`histogram-evidence.json` preserve environment, coverage and cleanup details.
`artifact-manifest.json` hashes the retained evidence and records target and
runner revisions separately from report publication.

The report-only update does not change the tested candidate. No local checks were
rerun without a source change; `git diff --check` is repeated for publication.
The required idle/loaded commands completed, Metrics readiness passes locally,
and overall acceptance remains unmet. Preserve the published patch for follow-on
work; close fail rather than waive Tasks/Agents/pane limits or claim a LAN result.

## Delivery onto main — 2026-09-27 (clear-delta)

Tasks/Agents limits are still unmet, but the Metrics change passes its local
readiness limits. It ships on `aq/clear-delta`, which is `aq/wise-ember.17`
(`3ab190f10`) with `origin/main` (`488c19b75`) merged in. The merge was clean,
and all three wise-ember.17 commits are ancestors of the delivery head. The
source diff against main is unchanged. The Tasks-page graph prefetch in
`routeData.ts` ships with it: it shares the mounted query and adds no new failure
path, but it did not bring Tasks within its limit. Checks rerun on the merged tree:

- `aq test tests/test_api_metrics.py` — 22 passed.
- `npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__ src/pages/command-center/__tests__/Tasks.test.tsx src/pages/command-center/__tests__/useGraphLive.test.tsx src/ws/__tests__/useEventStream.wire.test.tsx src/ws/__tests__/useEventStream.agents.test.tsx` — 143 passed across nine files.
- `npm run typecheck` — pass. `npm run lint` — zero errors, 37 existing warnings.
- `ruff check src/api/metrics.py` — pass. `git diff --check origin/main` — pass.

The browser timings above were not rerun. The merge adds no dashboard or metrics
source change, so the matched idle/loaded pair still describes the candidate.
