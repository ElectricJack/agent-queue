# E2E fixture reuse, lifecycle audit and runtime measurements

Task: `wise-horizon-85`, 2026-09-30. Baseline source: `bd35e46f0` in an immutable
`git archive` at `/tmp/aq-e2e-wise-baseline`. Changed source is this task branch.
Python 3.12.3, pytest 9.0.3, local disposable PostgreSQL test service on port 5534,
fake session providers, the same gated four-worker allowance and the same scenario
assertions. Measurements include daemon build/start/stop and CLI preload. They do
not include installing dependencies or provisioning hosted runners.

| Measurement | Baseline | Shared fixtures | Complete backend + fixture change |
| --- | ---: | ---: | ---: |
| Pytest wall time | 225.18 s | 92.83 s | 104.44 s |
| Sum of JUnit item times, including fixtures | 802.562 s | 325.348 s | 354.769 s |
| Independently reported acceptance items | 4 groups | 20 scenarios | 20 scenarios |
| Disposable worlds / parallel shards | 4 / 4 | 4 / 4 | 4 / 4 |
| Result | 4/4 passed | 20/20 passed | 20/20 passed |

The complete change decreased wall time 53.6% and summed item/fixture time 55.8%.
The fixture-only checkpoint decreased wall time 58.8%; an initial optimized run
also passed all 20 scenarios in 92.62 s before rebalancing. Final runs overlapped
bounded area checks, so differences between optimized runs include machine load.
A subsequent deployment-compatible candidate rebased onto operator HEAD
`acab4674469be918bb894aff557909181a37ac33` passed all 20 scenarios in
131.83 s while the combined 671-test area arm ran concurrently (145.47 s).
Its summed JUnit item time was 448.482 s. The exact candidate preserves deployed
pool-drain/integration fixes and the ten-minute full CI job budget from
fair-current; its raw timings are recorded separately in the JSON artifact.

These are local measurements, not a prediction of hosted job time. The task supplied
hosted graph timings (S10 84.5 s, S18 25.4 s, S16b 134.3 s, 260.91 s total);
these are background evidence, not the denominator above.

The fixture-only checkpoint per-shard item/fixture totals were claims 87.617 s, cli 74.337 s, graphs
84.034 s, failover 79.360 s. Moving independent S6 from claims to graphs balanced
the remaining tail. Four parallel environments remain preferable to consolidating
all work into one serial daemon: their lifecycle costs are only about 16 s each,
while serial scenario/item work alone would exceed the measured parallel wall time.

[Raw aggregate and per-scenario timings](2026-09-30-e2e-shared-fixtures.json) contain
CLI counts, condition waits, lifecycle phases and provider recovery. Each final
world recorded exactly one build, start, CLI preload and teardown. Build took
2.14–2.33 s, start 9.56–9.70 s, preload about 3.02 s, teardown 1.18–1.32 s.
Condition wait time includes predicate execution and can overlap CLI time.

## What consumed the time

Before implementation, fresh `aq --help` / `aq --json schema` took 2.63 / 2.37 s
locally. Python import profiling attributed 2.01 s to `src.cli.app`; discovering
commands and internal formatters imports the handler/orchestrator graph. The
first fixture split, still using fresh CLI interpreters, took 224.06 s and did
not provide a meaningful runtime improvement. S10 spent all 72.25 s of its
scenario time in CLI processes, and S19 spent 70.6 of 72.0 s there.

The opt-in test preload launcher keeps full Click/REST/output behavior in separate
fork children; fresh interpreters remain for claim and graph races, waits, plugin
startup, help and version. At the fixture checkpoint S10 took 10.56 s for 22 CLI calls, while S19 took
37.88 s, including its fresh graph invocations and quota races. Production CLI
imports have not been changed or cached. The standalone shell acceptance runner
retains fresh interpreter startup for every command.

The original scheduler delay was 5 s and config watcher 30 s. Pytest selects
1 s / 0.5 s through production scheduling configuration, sets graph sweeps to 1 s
and polls bounded predicates every 0.25 s. At the fixture checkpoint S16b spent 7.92 s in condition waits
and 27.62 s in the recovery/all-down phase, within its 33.21 s scenario total.
Provider suppression and capacity checks now observe two actual completed
scheduler cycles. No negative assertion depends on an assumed shorter sleep.

## Production backend responsiveness

A queued operator instruction explicitly extended this follow-up to production
responsiveness. `src.main` now wakes its deterministic scheduler on committed
task/routing/gate/provider/session changes. Bursts coalesce into a normal cycle;
events during a running cycle remain pending. The default one-second minimum
start-to-start cadence bounds busy wakeups, while idle scheduling keeps the
five-second backstop. Degraded cycles retain the full periodic retry delay even
when events continue arriving. Subscriptions and waiter tasks are removed during
shutdown/cancellation. No readiness, capacity, rate-limit/backoff or identity
logic was replaced.

Three scheduling fields are hot-reloadable: `cycle_interval_seconds` (5),
`min_cycle_interval_seconds` (1) and `config_poll_interval_seconds` (30). Values
must be finite and positive; the minimum cannot exceed the periodic interval.
The config watcher applies its updated polling interval on reload. The E2E
launcher now only observes completed cycles, using the production cadence.

The production comparison used identical five-second scheduler / 30-second config
cadence, three transition samples and 20 seconds of idle observation. The before
archive retains pre-event-wakeup production code; its parameter-only scheduler
seam and test observer provide completed-cycle measurement without changing the
original cadence. Both arms used the same fixture/CLI preloader and public API
measurement driver. Task latency includes creation and an audited route override;
provider latency measures clearing a disabled override through a running worker,
not external authentication. S16 retains separate authentication/canary coverage.

| Metric | Before backend wakeups | After backend wakeups |
| --- | ---: | ---: |
| Mean routed task to running worker (3 samples) | 4.110 s | 0.535 s |
| Mean provider override recovery to worker (3 samples) | 4.522 s | 0.870 s |
| Idle completed cycles / 20 s | 4 | 4 |
| Idle PostgreSQL transactions / 20 s | 690 | 684 |
| Idle daemon CPU | 5.3% | 4.95% |

The committed strict benchmark passed in 54.48 s; the earlier after run measured
0.571 / 0.829 s means, four idle cycles, 697 transactions and 5.7% CPU. Both
after samples are retained in the JSON artifact. The short sample shows lower
transition latency with unchanged idle cycle cadence
and about 1% transaction-count variation. CPU is one process's utime+stime from
`/proc`, transactions come from the owned database's `pg_stat_database`; they
include background metrics/services and the two observation queries. This is a
small local sample, not a statistically established CPU/DB-load ceiling.
The deployment-compatible candidate strict benchmark also passed (55.46 s):
three-sample routed-task mean 0.644 s, provider recovery mean 0.573 s,
four idle cycles, 701 transactions and 5.95% daemon CPU in 20 s. It includes
the subsequently deployed integration services, so its idle figures are retained
separately from the controlled original-base comparison above.

Production fresh-interpreter CLI imports remain a measured separate cost; the
CLI preload optimization is confined to the acceptance fixture.

## Lifecycle and duplication audit

- `tests/test_e2e_cli_stateful.py` already shared setup within each of four
  aggregate groups. It now shares a module fixture while reporting each scenario
  independently. Selecting S3 prepares S1/S2 once in that world's explicit chain.
  Failure clears the chain; other boundaries delete both open and terminal tasks,
  kill live sessions and restore the full swarm section, fake modes, provider
  overrides and the provider-failover playbook. Cleanup refusal poisons the world.
- `scripts/e2e-smoke.sh` starts/stops only the daemon it started. Pytest explicitly
  starts its fixture daemon once; each item reuses it through the wrapper's status
  and readiness gates. Registration remains idempotent and cheap public API
  scaffolding. Final fixture cleanup also terminates the CLI launcher process group.
- CI's PostgreSQL integration arm excludes this acceptance module. The four E2E
  shards execute each S1–S19 capability once, replacing S16 with S16a/S16b.
  S16b prepares the outage needed for recovery without replaying S16a's assertions.
  Running the standalone full transcript as an additional CI arm would duplicate
  scenario assertions; no such arm was added.
- `tests/test_e2e_kit_fixtures.py` tests stand-ins and launcher/probe invariants;
  `tests/test_e2e_probe.py` uses fake processes and broken schema states. These
  are separate safety cases, not duplicate real-daemon scenario runs, and remain.
  Its validation-preset success/failure subprocesses deliberately test both outcomes.
- `tests/test_main_lifecycle.py` uses a mocked orchestrator to check cancellation,
  startup ordering and degraded messaging, not another disposable daemon. Broader
  database tests already use per-worker template-cloned lease pools in
  `tests/db_fixtures.py` with session teardown in `tests/conftest.py`. Optional real
  provider authentication is already session-scoped. Sharing mutable daemons or
  databases across these unrelated tests would remove isolation without eliminating
  another execution of the CLI acceptance scenarios.

One intermediate fresh-interpreter split run failed S15's unchanged provenance
refusal assertion (`source is not an ancestor` instead of `provenance migration`).
Standalone S15 passed, and both complete optimized runs passed S15. This was not
counted as baseline behavior or fixed by relaxing the assertion; its cause was
not reproduced in subsequent runs.

## Repeatable verification

Set `POSTGRES_TEST_DSN` to the disposable test service, never the operator database.

```bash
# Baseline: run this command from the immutable baseline archive.
aq test tests/test_e2e_cli_stateful.py -m integration -s --durations=0 \
  -n auto --dist load --junitxml=/tmp/wise-before.xml

# Final: timings live outside the world that cleanup destroys.
AQ_E2E_TIMINGS_DIR=/tmp/wise-balanced-timings aq test \
  tests/test_e2e_cli_stateful.py -m integration -s --durations=0 \
  -n auto --dist loadgroup --junitxml=/tmp/wise-balanced.xml

# Independent selection: prerequisites and a freshly prepared provider outage.
aq test 'tests/test_e2e_cli_stateful.py::test_disposable_daemon_scenario[claims-S3]' \
  'tests/test_e2e_cli_stateful.py::test_disposable_daemon_scenario[graphs-S16b]' \
  -m integration -s -n auto --dist loadgroup

# Area and startup safety checks.
aq test tests/test_e2e_kit_fixtures.py tests/test_e2e_probe.py \
  tests/test_main_lifecycle.py tests/test_script_modes.py \
  tests/test_ci_trigger_policy.py tests/test_cli_startup_offline.py \
  tests/test_selection_catalogue.py -q
```

Independent selection passed 2/2 in 45.47 s. The area arm passed 174 tests and
initially failed two catalogue drift checks before regeneration; the regenerated
catalogue then passed 37/37 checks. The final complete area command passed
176/176 tests in 43.67 s. Focused fixture/probe/main/script checks passed
80/80 before expanding launcher coverage. The complete backend/fixture area command passed 447 tests in 69.62 s, and
the orchestrator/scheduler/pool/provider/editor area passed 274 tests in 89.05 s.
Backend config/watcher/main checks passed 241 tests. Final check results are
recorded on the task and bound to the published commit.

The deployment-compatible candidate also passed the combined area command
(671 tests in 145.47 s):

```bash
aq test tests/test_e2e_kit_fixtures.py tests/test_e2e_probe.py \
  tests/test_main_lifecycle.py tests/test_script_modes.py \
  tests/test_ci_trigger_policy.py tests/test_cli_startup_offline.py \
  tests/test_selection_catalogue.py tests/test_config.py \
  tests/test_config_watcher.py tests/test_config_editor.py \
  tests/test_config_schema_inventory.py tests/test_orchestrator.py \
  tests/test_scheduler.py tests/test_pool_sizing.py \
  tests/test_provider_suppression.py tests/test_provider_commands.py -q
```

The production benchmark is separately selectable and excluded from ordinary and
integration runs:

```bash
AQ_PERF_STRICT=1 AQ_E2E_BENCHMARK_OUT=/tmp/backend-timing.json aq test \
  tests/test_e2e_cli_stateful.py::test_backend_responsiveness_benchmark \
  -m perf -p no:xdist -s
```

The CI timeout policy and four-runner matrix are preserved; CI now selects a shard
with `-k` and uploads individual JUnit results plus timing JSONL. This follow-up is
on its own branch and enters normal train integration after the ready deployment;
coordination was sent to the project supervisor. No daemon restart, merge or
operator database migration was performed by the worker.
