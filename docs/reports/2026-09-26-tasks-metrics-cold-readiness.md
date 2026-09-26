# Tasks and Metrics cold-readiness implementation checkpoint

Task: `wise-ember.17`. Specification: operator vault
`projects/agent-queue/specs/2026-09-24-dashboard-performance-under-load-and-separation.md`
§§4–5. This checkpoint does **not** establish the required loaded p95 ≤2000 ms.
The combined patch still needs an operator-provisioned isolated target and a
coordinated, matching idle/load experiment window.

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

## Artifact identity and verification

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

## Required matched experiment remains pending

The original pair-3 baseline is under
`dashboard-perf/2026-09-25/wise-ember.12/claim-epoch-4/pair-3/` in the same e2e
root. Loaded cold p95 for client settings 1/3: Tasks 2860/2494 ms, Metrics
6342/3409 ms; Agents 1491/1551 ms and task-pane visibility 159/146 ms pass.
Original full-window loop p95 was 53.71 ms versus browser-active 123.72 ms.
The current runner already reports both scopes; `solid-rapids` is the separate
scope-accounting filing, and the loop/read remediation has a separate owner.
Coordination requests were sent through the supported message surface.

The required idle and loaded commands are those in the held task, with three
repetitions, clients 1,3, 30s warm-up, 120s observations, Tasks/Agents/Metrics,
10k tasks and fresh output directories. Keep load arguments exactly
`--seconds 1800 --iterations 200000000000 --io-bytes 137438953472 --tmp-max-mib 256 --workers 4`.
Record target revision separately from runner revision, actual nice 19,
work completed and throughput, deadline/coverage, PSI and both histogram scopes.
Baseline throughput median is 41,646,220.6 CPU iterations/s; all three helpers
completed the requested IO and hit the 1800s deadline with partial CPU work.

This session's provisioned caps differ from the original manifest: CPU/thread
share 1, test workers 4, test slots 4, compared with baseline 3/3/2; parent nice
is 10 in both. Changing them silently, raising caps beyond the assigned share,
or calling these diagnostics a matching experiment would be incorrect.
Operator coordination must provide the fixed target, authorize the isolated
window and resolve the caps comparison before acceptance measurement. No
latency, throughput, histogram, task-pane or LAN/tailnet acceptance verdict is
claimed by this checkpoint.
