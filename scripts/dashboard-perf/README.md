# Dashboard performance measurement

This kit implements the controlled experiment in the approved dashboard
performance spec §4.2. It measures a built bundle through the separate dashboard
server and probes matching reads directly on the isolated daemon. Browser
numbers are explicit performance checks with a quiet control window, never CI
assertions. Compare idle and loaded runs taken on the same revision and host.

## Prepare an isolated target

Never target the operator's database, API or dashboard with an experiment,
seeder or database workload. Do not start a whole suite as a casual benchmark.
The separately scheduled representativeness task may use `aq test` through the
normal box-wide slots and full-suite lock. Workers do not manage the operator's
daemon, migrate its database, or reset a shared e2e environment.

Use an operator-provisioned isolated daemon; [the e2e guide](../../docs/guides/e2e-swarm.md)
explains provisioning it, its own vault/data directory and PostgreSQL database.
The e2e API defaults to **8099** (`AQ_E2E_API_URL`), not the operator's 8081.
Use a separate dashboard server on an unused port, for example 8092. A distinct
database on the same PostgreSQL instance isolates data but shares buffers, IO
and connections; a separate instance on the same disk still shares disk IO.
`pg_identity.py` records endpoint identity without printing credentials.

Node needs `puppeteer-core@24` (24.33 or later, for window pages and
`page.windowId()`) and Chrome (`CHROME`, default `/usr/bin/google-chrome`).
Install Puppeteer in a temporary tools directory, then expose its
`node_modules` to these scripts with a local, untracked symlink:

```bash
perf_tools=$(mktemp -d)
npm install --prefix "$perf_tools" puppeteer-core@24
ln -s "$perf_tools/node_modules" scripts/dashboard-perf/node_modules
```

Build and stage the verified bundle in this checkout, with API URL overrides
cleared by the release builder. This does not install it or restart anything:

```bash
python scripts/build_release_artifact.py
python -m src.dashboard_server --config "$AQ_E2E_HOME/config.yaml" \
  --host 127.0.0.1 --port 8092 --api-url "$AQ_E2E_API_URL"
```

Keep that server in its own terminal. Its `/__aq/health` must name the isolated
API and report `upstream_ok`. The experiment checks that identity and checks
`--api-url` against the isolated config's `mcp_server.port` before launching
load. `--config` defaults to `$AQ_E2E_HOME/config.yaml` (otherwise
`~/.agent-queue-e2e/config.yaml`). The target database must be provably different
from `~/.agent-queue/config.yaml`; unknown identities are refused. Config reads
do not import a daemon's `.env` into the worker's environment.

Seed the isolated daemon's database before warming it; use its DSN supplied by
the operator, never the worker's refusal sentinel:

```bash
python scripts/dashboard-perf/seed.py --dsn "$E2E_DB_URL" \
  --project perf-fixture --tasks 10000 --status READY
```

The seeder creates a paused project, so fixture tasks do not dispatch. Hold
fixture size and live agent/session count constant across comparisons. Ensure
the selected surfaces have rows/controls and metrics enabled before measuring.

## Fixed protocol

Default factors: three repetitions, one and three concurrent browser clients
per idle surface, 1600×1000 viewport, 30 s warm-up after readiness and 120 s
observation. Each repetition inventories the host, starts the workload for a
loaded run, warms up, runs the harness for each client count, waits for the
workload, then captures the host inventory and the repetition's 1 s fleet
series. Daemon targets are summarized over the browser-active part of that
series (see `summary.json` below). Client counts remain separate in
`summary.by_clients`. Each concurrent client uses its own Chrome window so
background tabs cannot suspend animation frames; the harness records each client's
`window_id` and refuses a surface whose clients share one.

```bash
python scripts/dashboard-perf/experiment.py --mode idle \
  --dashboard-url http://127.0.0.1:8092 --api-url "$AQ_E2E_API_URL" \
  --config "$AQ_E2E_HOME/config.yaml" --project perf-fixture \
  --out "$AQ_E2E_HOME/dashboard-perf/idle" --repetitions 3 --clients 1,3 \
  --warmup-ms 30000 --observe-ms 120000 --surfaces tasks,agents,metrics

python scripts/dashboard-perf/experiment.py --mode loaded \
  --dashboard-url http://127.0.0.1:8092 --api-url "$AQ_E2E_API_URL" \
  --config "$AQ_E2E_HOME/config.yaml" --project perf-fixture \
  --out "$AQ_E2E_HOME/dashboard-perf/loaded" --repetitions 3 --clients 1,3 \
  --warmup-ms 30000 --observe-ms 120000 --surfaces tasks,agents,metrics
```

Outputs must be empty directories; existing evidence is never overwritten.
Keep browser version, fixture, sessions, surfaces, timings, caps and work volume
identical across modes. Report LAN/tailnet delay separately: this runner accepts
loopback HTTP origins and does not measure network delay.

The default workload is `load.py --seconds 120 --iterations 200000000
--io-bytes 1073741824 --tmp-max-mib 256 --workers min(4, cpu_count)`, launched
through `wrap_session_argv` and `session_env_caps`. It does **fixed work**, so it
can finish early. These defaults are not calibration: on the reference host
work may finish before warm-up, and sequential surfaces/client counts take
longer than one 120 s deadline. Use `--load-args "..."` to calibrate a fixed
work volume and hard deadline covering the complete repetition, then use that
same list for every mode compared. The manifest records the final list,
throughput records work accomplished, and each load artifact records overlap
with observation/API windows. `loaded_observations_covered: false` is an
invalid loaded comparison, not evidence of a performance win. No loop silently
restarts a completed helper to manufacture load. Temporary space stays at most
256 MiB and is cleaned on completion, deadline and handled signals.

`--workload-cmd "..."` is an explicit external workload hook (split into argv,
no shell expansion), recorded as unwrapped. `--workload-timeout-s` sets its hard
wrapper deadline (default 3600 s; built-in helper default is `--seconds + 30`).
An external command should wait for its workload and write a JSON summary with
`throughput_iter_per_s` and `timed_out` as its final stdout line; otherwise
throughput is unknown and logs remain evidence. `--mode queued` is refused
without this hook: the exclusive job queue is not implemented on this checkout;
pass the command once `aq job submit` exists. Submitting a detached job and
immediately exiting does not establish load coverage. The runner stops its
workload process group on harness failure/timeout. An external database
workload must use the isolated PostgreSQL only. This hook is not authorization
to run the real suite outside its separately scheduled task.

## Artifacts and interpretation

- `manifest.json`: revision, mode, repetitions, client counts, seed/task and
  agent counts, Chrome/version, viewport, host kernel/CPU/MemTotal, URLs,
  surfaces, workload argv/deadline, resource caps, configured niceness and
  parent niceness, PostgreSQL identity. `nice -n` increments inherited nice;
  `load-*.json` records actual helper niceness.
- `harness-<n>-clients-<N>.json` and `.log`: raw cold/warm timing samples,
  per-client idle counters and window ids, task-detail visible/loaded timings,
  direct API samples, p95 and errors, browser manifest and window timestamps.
- `inventory-before-<n>.json`, `inventory-after-<n>.json`: process parents,
  session/job/test markers and class totals; instance tokens are redacted.
- `workload-<n>.stdout.log` and `.stdout.stderr.log`, `load-<n>.json`: raw
  workload output, completion/timeout/exit, throughput, observed start/end,
  coverage and `helper_only_s` (time run after the last browser window). Idle
  mode records null throughput/timeout rather than zero work.
- `series-<n>.json`: raw 1 s samples for the whole repetition window.
- `summary.json`: medians across repetitions, raw values and `(max-min)/median`
  spread (null for fewer than two values or zero median), separate client-count
  summaries, daemon histogram-derived loop p95/max/stalls >500 ms, API, pool,
  query and relay p95, PSI maxima, ungated process maximum, sampler cost p95,
  workload throughput and timeout count. Histograms merge by addition;
  percentiles are never averaged. Missing probes stay null. Per-repetition
  daemon values and spread are retained alongside merged results.

Daemon results have two activity scopes. `daemon`, `daemon_repetitions` and
`daemon_spread` are **browser-active** (`daemon_scope`): only 1 s samples whose
timestamp falls inside one of that repetition's harness runs, from its first cold
load through its direct API reads. `by_clients.<N>.daemon` uses only that client
count's runs. `whole_repetition` holds the same three fields over every sample.
That includes the controller warm-up and, in loaded mode, the helper-only tail,
because a repetition waits for its workload to finish. Idle has no such tail, so
the whole-repetition scope compares unequal envelopes: a long quiet tail can pull
its p95 under a target the browsers never met. It is accounting, not a verdict.
`activity` lists, per repetition, the browser windows (`clients`, `start_ts`,
`end_ts`), total/browser-active/excluded sample counts and `helper_only_s`: the
seconds the workload ran after the repetition's last browser window, for every
client count (null in idle mode). Workload completion, timeout, coverage and that
tail stay in `load` and `load-<n>.json`. With no browser window the browser-active
scope is empty and its values are null; it is never widened to the whole repetition.

Compare the browser-active scope against the envelope: task detail visible p95
≤200 ms; small task/roster API reads p95 ≤500 ms and ≤2× idle; loop p95 ≤50 ms
with no stall >500 ms; cold page ready ≤2 s. Derive browser p95 from the raw
timings, and include errors, spread, load coverage and the helper-only tail in the
evidence. Read probes cannot establish mutation error rate; that target requires a
separately specified write workload.

## Standalone tools

```bash
node scripts/dashboard-perf/harness.mjs http://127.0.0.1:8092 result.json \
  --api "$AQ_E2E_API_URL" --clients 3 --warmup-ms 30000 --observe-ms 120000 \
  --runs 3 --only tasks,agents,metrics --project perf-fixture --task-detail-only
node scripts/dashboard-perf/api.mjs "$AQ_E2E_API_URL" perf-fixture
```

`--observe-ms` aliases `--idle-ms` (the new flag wins). `--no-interactions`
skips interactions; `--task-detail-only` measures only the two task-row pane
opens used by the experiment. The full interaction mode also measures search,
graph tabs/nodes, reviews filters and metrics range. Cold loads use fresh pages;
warm loads navigate within one SPA. Idle opens N pages concurrently after each
becomes ready and warms, then observes them concurrently. React commits and
rendered fibers are counted by a hook installed before the bundle runs.
Dashboard preference writes (`state-put`/`state-reset`) are answered locally,
so measurements do not change roaming preferences. API probes cover health,
readiness, series, tasks, agents, pools, gates and one discovered task detail,
including full response delivery and bounded request timeouts. A missing task
id is reported explicitly rather than silently dropped.

`node scripts/dashboard-perf/smoke.mjs` checks three concurrent clients in
separate windows on two consecutive surfaces, duration-alias precedence,
manifests and raw browser/API samples against an ephemeral local HTTP fixture
with real Chrome. It touches no daemon or database
and bounds/cleans its browser process group. This is a harness regression check,
not a latency benchmark.

`focus.mjs` profiles one surface; `chunks.mjs` reports static chunk sizes. Prior
observations live in
[`2026-09-23-dashboard-performance.md`](../../docs/superpowers/specs/2026-09-23-dashboard-performance.md).
