# Metrics minute overview and second-level refinement

Task `wise-ember.17`, claim epoch 3. This continues the preserved Tasks fix
and the [previous matched experiment](2026-09-26-tasks-metrics-cold-readiness.md).
That experiment's loaded Metrics cold p95 was 2377/2714 ms for client settings
1/3, above the 2000 ms target. This checkpoint has passing local checks and
an offline browser diagnostic; **the required matched idle/loaded experiment
has not run and task acceptance is not established**. The supervisor queues
that window after `wise-ember.14`, `.15`, and `.16`.

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

The remaining required run is the unchanged three-repetition idle/loaded
pair through 8092→8099 with the paused 10k-task fixture, client settings 1/3,
30-second warmup, 120-second observations, baseline child caps 3/3/2,
four helpers, 200B CPU iterations, 128 GiB IO, 1800-second deadlines,
256 MiB disk cap, and actual nice 19. Retain throughput, completion/coverage,
ambient admitted tests, PSI, spread, and both full-window and browser-active
histograms. Warm one full hour of second history after any target downtime
before measuring; a reduced retained row count is not a performance win.
Only the supervisor's new window grant permits that load. This checkpoint
does not change the prior unmet verdict or claim delivery to the default branch.
