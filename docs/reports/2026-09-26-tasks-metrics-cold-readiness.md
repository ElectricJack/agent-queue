# Tasks and Metrics cold readiness: matched experiment

Task: `wise-ember.17`. Specification: operator vault
`projects/agent-queue/specs/2026-09-24-dashboard-performance-under-load-and-separation.md`
§§4–5. The required idle and loaded protocol completed three repetitions each.
Loaded cold p95 for client settings 1/3 is **Tasks 1458/1114 ms**, **Metrics
2377/2714 ms**, and **Agents 1329/1300 ms**. Task-pane visibility p95 is
119/108 ms. Metrics still misses 2000 ms, so task acceptance is **not met**.
All raw samples are retained. Fleet tests added ambient load to several
repetitions; the measured differences do not isolate the patch's causal effect.

## Profile and fix

The cold Tasks view uses a complete project graph; its request contended with
the lazy shell's initial roster/sidebar reads. The Metrics view requests an hour
of 1-second history, about 9.36 MB on this fixture. The published direct API
probe requests only 60 seconds, so its 17–40 ms result does not measure the
page's history read. Chrome traces showed about 2.0–2.4 seconds between the
history request starting and its body finishing, followed by 332–366 ms to
readiness. Browser script time was 338–355 ms. Optimizing chart construction
alone would not remove the dominant delay.

A diagnostic interception experiment deferred shell reads until after page
readiness. Tasks improved, but Metrics still missed the target. These diagnostic
requests were withheld and are not an acceptance run or a shipped policy.

The patch starts the initial selected-project Tasks graph or Metrics history
request before the lazy shell mounts, using the mounted hook's identical query
key, function, retry policy and stale time. An in-flight request is shared;
other routes retain their existing behavior. Existing WebSocket invalidation,
reconciliation and live 1 Hz metrics remain in place.

Captured-row ASGI profiling isolated a second cost in `src/api/metrics.py`:
3471 separate `MetricsSample.model_validate` calls took 850 ms of a 1041 ms
response. Validating the complete nested `MetricsSeriesResponse` once reduced
the same replay to 542 ms, including 370 ms validation and 112 ms encoding. Both
responses were 9,355,043 bytes. This retains every row, the one-hour range,
resolution selection, neutral defaults, histogram buckets and null readings.
No model/schema, dependency, retention or response cache changes are involved.
The replay excludes database, network, concurrent daemon activity and load.

Source: `dashboard/src/routeData.ts:12–25`, called before render in
`dashboard/src/main.tsx:28`; shared options in `dashboard/src/api/graph.ts`
and `dashboard/src/api/metrics.ts`; nested validation in
`src/api/metrics.py:69–80`. The deployed patch is `7449021d4`.

## Raw diagnostic readiness

These are three sequential cold pages per surface, Chrome 150.0.7871.46 at
1600×1000, through the verified worker-owned dashboard server on :8092 and
existing isolated daemon on :8099. CPU profiling and tracing add overhead.
No helper load, 30-second warm-up, 120-second observation, three-client idle
window, task-pane interaction or LAN/tailnet measurement is represented here.
The client-only after run overlapped local area-test activity; it is diagnostic
and cannot establish a matched idle or loaded improvement.

| Diagnostic | Surface | Raw ms | Median ms | Nearest-rank p95 ms | (max−min)/median |
|---|---|---|---:|---:|---:|
| Before | Tasks | 1437.7, 1314.1, 1659.7 | 1437.7 | 1659.7 | 0.240 |
| Before | Metrics | 3171.9, 2821.2, 3146.8 | 3146.8 | 3171.9 | 0.111 |
| Before | Agents | 731.3, 1561.7, 1040.4 | 1040.4 | 1561.7 | 0.798 |
| Shell reads deferred | Tasks | 995.1, 1041.4, 701.6 | 995.1 | 1041.4 | 0.341 |
| Shell reads deferred | Metrics | 2432.6, 3196.1, 3048.5 | 3048.5 | 3196.1 | 0.250 |
| Shell reads deferred | Agents (unchanged) | 968.2, 2072.8, 1478.6 | 1478.6 | 2072.8 | 0.747 |
| Client change only | Tasks | 1325.6, 767.4, 477.8 | 767.4 | 1325.6 | 1.105 |
| Client change only | Metrics | 1851.3, 2252.1, 3617.0 | 2252.1 | 3617.0 | 0.784 |
| Client change only | Agents | 1298.5, 1158.8, 1326.4 | 1298.5 | 1326.4 | 0.129 |

Client-only traces confirm the page read starts at 56–74 ms, ahead of the shell
reads at 385–410 ms. The old backend remained running, so those traces do not
exercise the nested-validation change. Metrics still misses 2 seconds.

## Diagnostic artifact identity and local verification

Artifacts:
`/home/jkern/.agent-queue-e2e/dashboard-perf/2026-09-26/wise-ember.17/claim-epoch-1/`.
`profile-before/`, `profile-defer-shell/` and `profile-after-client/` each contain
nine Chrome traces, CPU profiles and `summary.json` with raw request, navigation,
resource, DOM and main-thread samples. `hour-series-before.json` is the replay
input; `metrics-response-{before,after}.{prof,txt}` record ASGI profiles. The
profiling script is preserved in the artifact directory, not shipped code.

The baseline bundle/runner checkout was
`d9a3931b663a35a67fd6331bb2b5decd4614ee9d`; the client-only after bundle includes
this patch. The target daemon remained
`49f5393165c7680fd076327ea774d95e7d4aec93`, PID 729640, started by the previous
operator profiling launcher. No worker restart, migration, fixture mutation or
load against that daemon occurred. Only this task's own dashboard server was
started/replaced. The :8092 health response identified :8099 and a verified
bundle with 103 files.

Local checks:

- `npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__/Metrics.test.tsx`: 29 passed.
- `npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__ src/pages/command-center/__tests__/Tasks.test.tsx src/pages/command-center/__tests__/useGraphLive.test.tsx`: 106 passed across seven files. The actual invocation also named the nonexistent `src/ws/__tests__/useEventStream.test.tsx`; it selected no additional suite.
- `npx vitest run src/ws/__tests__/useEventStream.wire.test.tsx src/ws/__tests__/useEventStream.agents.test.tsx`: 25 passed; event replay and roster invalidation retained.
- `POSTGRES_TEST_DSN=… aq test tests/test_api_metrics.py`: 22 passed. An initial attempt without the test DSN ran nothing.
- `POSTGRES_TEST_DSN=… aq test tests/test_api_metrics.py tests/test_metrics_sampler.py tests/test_metrics_histogram.py tests/test_metrics_perf.py tests/test_dashboard_browser_storage.py`: 104 passed. DSN is the repository's disposable :5534 maintenance database; tests created/removed their own databases.
- `npm run typecheck`, `npm run lint`, `python scripts/build_release_artifact.py` (runs `npm run build`): pass. Lint has zero errors and 37 existing warnings.
- `ruff check src/api/metrics.py`: pass.

## Completed matched protocol

Artifacts: `/home/jkern/.agent-queue-e2e/dashboard-perf/2026-09-26/wise-ember.17/claim-epoch-2`. Both exact commands are preserved in
`pair-status.json`; both exited 0. Each mode has three repetitions, client
settings 1/3, Chrome 150.0.7871.46, 1600×1000, 30 s warm-up, 120 s surface
observations, and the preserved paused 10k-task fixture. `analyze-pair.py`
recomputed cold, pane and direct API statistics from the twelve harness files;
80/80 protocol/raw comparisons pass. `pair-analysis.json` contains the complete
comparison. `evidence-index.json` records hashes and sizes for 40 raw, inventory,
manifest and summary JSON artifacts. No repetitions were discarded or rerun.

Baseline target `49f5393165c7680fd076327ea774d95e7d4aec93` and runner
`006bcbddf95df1803b421cb866786934d3b46f03` come from pair-3’s
`final-measurement-verification.json`. After target and runner are both
`7449021d44c9101061aca0129a23c9f0c065d354`; target PID 18825 and edge PID 20097
were checked against this worktree and the isolated config before shutdown.
The config SHA256 is unchanged: `9201ace09a62ce90592ab2da12dca942f0e3bc6d5155c9a4449ffb3cae498425`.

The supervisor authorized the exclusive isolated window and baseline child
caps **CPU/thread share 3, test workers 3, test slots 2**. Both manifests match
the original caps. The worker’s own provisioned environment stayed 1/4/4;
only experiment/isolated children used the authorized baseline factors.
Parent nice is 10 and every helper reports actual nice **19**, matching baseline.
The isolated daemon retained `AQ_DB_SCOPE=worker`; no schema migration or reset
was performed. `identity-before.json` proves separate operator/isolated
PostgreSQL endpoints (:5533 / :5534).

After downtime the hour query had only 21 samples / 51,085 bytes. Before the
pair it had 3,530 samples / 8,737,772 bytes covering 3,597 s after real 1 Hz
warm-up. The original captured profile had 3,471 samples / 9,355,043 bytes.
The full hour and 1 Hz resolution stayed in place. Measurement traffic is
loopback; LAN/tailnet delay and mutation-error-rate changes were not measured.

### Cold readiness: every raw sample

Nearest-rank p95 of three cold samples is their maximum. Spread is
`(max−min)/median`. Client settings identify concurrent idle observations;
cold navigation, pane opens and direct API probes remain sequential.

| Mode | Setting | Surface | Before raw ms | After raw ms | Before median / p95 | After median / p95 | Before spread | After spread |
|---|---:|---|---|---|---:|---:|---:|---:|
| idle | 1 | tasks | [1476, 1542, 1855] | [1340, 1878, 1032] | 1542 / 1855 | 1340 / 1878 | 0.246 | 0.631 |
| idle | 1 | metrics | [2626, 3258, 3022] | [2745, 2563, 1620] | 3022 / 3258 | 2563 / 2745 | 0.209 | 0.439 |
| idle | 1 | agents | [731, 984, 744] | [791, 1271, 1358] | 744 / 984 | 1271 / 1358 | 0.340 | 0.446 |
| idle | 3 | tasks | [1874, 1281, 1319] | [1516, 1312, 1521] | 1319 / 1874 | 1516 / 1521 | 0.450 | 0.138 |
| idle | 3 | metrics | [2927, 2651, 2723] | [2686, 2040, 2638] | 2723 / 2927 | 2638 / 2686 | 0.101 | 0.245 |
| idle | 3 | agents | [1124, 838, 1095] | [1045, 738, 854] | 1095 / 1124 | 854 / 1045 | 0.261 | 0.359 |
| loaded | 1 | tasks | [1242, 1975, 2860] | [1458, 961, 922] | 1975 / 2860 | 961 / 1458 | 0.819 | 0.558 |
| loaded | 1 | metrics | [2731, 5114, 6342] | [2377, 2057, 1972] | 5114 / 6342 | 2057 / 2377 | 0.706 | 0.197 |
| loaded | 1 | agents | [1186, 1009, 1491] | [1329, 897, 1188] | 1186 / 1491 | 1188 / 1329 | 0.406 | 0.364 |
| loaded | 3 | tasks | [2494, 1328, 1732] | [1114, 780, 757] | 1732 / 2494 | 780 / 1114 | 0.673 | 0.458 |
| loaded | 3 | metrics | [3313, 2676, 3409] | [2714, 2204, 1937] | 3313 / 3409 | 2204 / 2714 | 0.221 | 0.353 |
| loaded | 3 | agents | [990, 1138, 1551] | [1300, 1260, 813] | 1138 / 1551 | 1260 / 1300 | 0.493 | 0.387 |

Tasks, Agents and task-pane visibility meet their local limits in both modes.
Metrics fails in both client settings in both modes. The selected patch does
not complete the task’s required cold-readiness acceptance. Further work needs
coordination with the separate read/loop remediation owner; these results do
not establish a single cause for the remaining delay.

### Task-pane visibility

Both task interactions contribute six raw samples per setting.

| Mode | Setting | Before raw ms | After raw ms | Before p95 ms | After p95 ms |
|---|---:|---|---|---:|---:|
| idle | 1 | [110, 60, 114, 56, 107, 62] | [112, 59, 128, 111, 95, 58] | 114 | 128 |
| idle | 3 | [114, 60, 112, 56, 100, 60] | [193, 73, 120, 59, 117, 63] | 114 | 193 |
| loaded | 1 | [111, 47, 127, 60, 159, 79] | [119, 59, 111, 59, 103, 58] | 159 | 119 |
| loaded | 3 | [146, 57, 138, 60, 143, 63] | [103, 55, 108, 45, 103, 72] | 146 | 108 |

### Daemon telemetry in both scopes

Percentiles below come from merged histogram buckets, not averaged p95 values.
The browser-active scope includes cold/warm loads, interactions, observations
and direct probes. The full scope includes warm-up and the helper-only tail.

| Scope | Metric | Before idle | After idle | Before loaded | After loaded |
|---|---|---:|---:|---:|---:|
| full | loop_drift_p95_ms | 72.72 | 96.14 | 53.71 | 39.64 |
| full | loop_drift_max_ms | 2026.14 | 5801.87 | 2870.20 | 2752.50 |
| full | stalls_over_500ms | 61.00 | 285.00 | 419.00 | 191.00 |
| full | api_all_p95_ms | 934.03 | 1014.81 | 1160.62 | 934.96 |
| full | pool_wait_p95_ms | 0.95 | 0.95 | 0.95 | 0.95 |
| full | query_p95_ms | 22.17 | 28.55 | 22.51 | 17.87 |
| full | relay_p95_ms | 988.92 | 1744.37 | 1538.70 | 1377.22 |
| full | sampler_perf_ms_p95 | 11.19 | 12.59 | 13.68 | 10.12 |
| active | loop_drift_p95_ms | 75.32 | 98.87 | 123.72 | 86.68 |
| active | loop_drift_max_ms | 2026.14 | 5801.87 | 2870.20 | 2752.50 |
| active | stalls_over_500ms | 59.00 | 280.00 | 316.00 | 152.00 |
| active | api_all_p95_ms | 934.18 | 1022.22 | 1168.15 | 936.37 |
| active | pool_wait_p95_ms | 0.95 | 0.95 | 0.95 | 0.95 |
| active | query_p95_ms | 22.48 | 28.99 | 29.15 | 23.09 |
| active | relay_p95_ms | 988.92 | 1744.37 | 1538.70 | 1377.22 |
| active | sampler_perf_ms_p95 | 11.21 | 12.65 | 14.01 | 10.85 |

Loaded full-window loop p95 39.64 ms meets 50 ms, but the relevant
browser-active p95 **86.68 ms** does not. Its 2,752.50 ms maximum also misses
the 500 ms individual-stall limit. The 702–717 s helper-only tails explain why
both scopes must remain visible. The original corresponding values were
53.71 ms full and 123.72 ms browser-active. `solid-rapids` tracks that scope
accounting separately; no duplicate implementation was added here.

| Mode | Repetition | Full samples | Browser-active samples | Excluded | Helper-only tail s |
|---|---:|---:|---:|---:|---:|
| idle | 1 | 1048 | 1017 | 31 | — |
| idle | 2 | 1031 | 1000 | 31 | — |
| idle | 3 | 1038 | 1008 | 30 | — |
| loaded | 1 | 1726 | 1017 | 709 | 702.338 |
| loaded | 2 | 1740 | 1010 | 730 | 716.72 |
| loaded | 3 | 1734 | 1013 | 721 | 712.843 |

Browser-active loop-p95 repetition spread: idle 0.227, loaded 0.278
(before 0.040 / 0.398). All other raw repetition estimates and spreads are
in `pair-analysis.json` and both `summary.json` files.

### Ambient fleet tests and PSI

The sampler’s `ungated_processes_max` counts are preserved under their original
field name. The supervisor confirmed these pytest processes were fleet workers
using `aq test` slots, not rogue processes. The fleet was not frozen. Every
idle repetition had ambient test activity; loaded repetitions 1 and 3 did too.
The baseline idle repetitions had no recorded pytest activity. This limits
causal before/after attribution; no favourable-window rerun replaced originals.

The table reports full-window maxima of PSI `some_avg10`. Browser-active
maxima and all `full_avg10` readings are also retained in the summaries.

| Mode | Repetition | Ambient pytest max | CPU PSI | IO PSI | Memory PSI |
|---|---:|---:|---:|---:|---:|
| idle | 1 | 13 | 5.42 | 4.60 | 5.75 |
| idle | 2 | 15 | 4.67 | 1.21 | 1.63 |
| idle | 3 | 16 | 1.51 | 0.32 | 0.00 |
| loaded | 1 | 18 | 18.22 | 4.42 | 0.00 |
| loaded | 2 | 0 | 0.26 | 3.13 | 0.00 |
| loaded | 3 | 6 | 5.69 | 3.54 | 1.18 |

### Fixed work volume, throughput and deadlines

Each helper requested 200,000,000,000 CPU iterations and 137,438,953,472 IO
bytes with four workers, a 1,800 s deadline and 256 MiB disk cap. All three
helpers completed the requested IO, reached the deadline with partial CPU work,
covered every browser/direct-API interval, exited 0, removed their temporary
directories and had no wrapper timeout. Peak temporary storage was 268,435,456
bytes in each repetition.

| Repetition | Before completed iterations | After completed iterations | Before iter/s | After iter/s | After elapsed s |
|---|---:|---:|---:|---:|---:|
| 1 | 83,495,700,000 | 79,485,900,000 | 46,385,091.8 | 44,157,329.2 | 1800.061 |
| 2 | 74,991,500,000 | 84,428,700,000 | 41,646,220.6 | 46,903,118.7 | 1800.066 |
| 3 | 74,650,000,000 | 79,166,500,000 | 41,470,222.5 | 43,979,934.8 | 1800.060 |

Median throughput: **44,157,329.2 iter/s**, before 41,646,220.6 iter/s.
Repetition spread: 0.066, before 0.118. Fixed requested work and all deadline
outcomes are reported; latency was not improved by suppressing the workload.

### Direct reads and verification

Every mode/setting has 90 sequential reads per route and zero API errors.
The short direct metrics probe uses 60 s, so it still cannot explain or
certify the one-hour Metrics page by itself.

| Mode | Setting | Task list p95 ms | Agent list p95 ms | Task get p95 ms | Short metrics p95 ms |
|---|---:|---:|---:|---:|---:|
| idle | 1 | 1079.53 | 807.65 | 50.60 | 31.92 |
| idle | 3 | 1114.10 | 928.28 | 65.97 | 24.46 |
| loaded | 1 | 1095.68 | 817.81 | 38.57 | 21.76 |
| loaded | 3 | 1221.26 | 727.05 | 44.92 | 16.92 |

The broad small-read/loop envelope also remains unmet; this task does not
claim to solve the separate backend read/loop remediation.

Local checks were confirmed before this pair on the unchanged patch: the nine
dashboard suites passed **131 tests**, the five Python modules passed **104**
with the disposable PostgreSQL DSN, typecheck/build/Ruff passed, and lint had
zero errors and 37 existing warnings. The Python command was
`aq test tests/test_api_metrics.py tests/test_metrics_sampler.py tests/test_metrics_histogram.py tests/test_metrics_perf.py tests/test_dashboard_browser_storage.py`.
The dashboard command was
`npx vitest run src/api/__tests__/graph.test.tsx src/pages/metrics/__tests__ src/pages/command-center/__tests__/Tasks.test.tsx src/pages/command-center/__tests__/useGraphLive.test.tsx src/ws/__tests__/useEventStream.wire.test.tsx src/ws/__tests__/useEventStream.agents.test.tsx`.
`npm run typecheck`, `npm run lint`, `python scripts/build_release_artifact.py`
(includes `npm run build`) and `ruff check src/api/metrics.py` passed.

Both own isolated servers were stopped gracefully after the pair. Helpers had
already finished and cleaned up. The temporary script node_modules symlink was
removed; the operator daemon/database were untouched. `cleanup.json` proves
8099/8092 are free. The supervisor was notified to release the window to
wise-ember.14’s quiet full-suite run. No additional stress or tests followed.

The patch and report are published for continued work. The task must close
**fail** because Metrics cold p95 remains above 2,000 ms; passing local tests
and protocol checks are not a passing performance-acceptance verdict.
