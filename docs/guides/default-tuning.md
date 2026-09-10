# Resource-aware default tuning

AQ's code defaults were written on one developer box. They assume eight
concurrent agents, two `aq test` slots, a metrics sample every second and an
"still failed" report every hour. On a four-core laptop that is a machine
that never stops thrashing; on a 64-core server it is a fleet that never uses
the box.

The install path therefore does not ship those numbers. It derives a curated
set from **this machine's cores and RAM** and writes them into
`~/.agent-queue/config.yaml` as ordinary sections you can read and edit.

```bash
aq system config tune                 # preview — writes nothing
aq system config tune --explain       # …with the reason for every value
aq system config tune --apply         # write it
```

`aq setup` does this automatically for a fresh config. Nothing consults the
tuner at runtime: once written, the file is the truth, and your edits stand.

## What the machine decides

Two numbers drive everything:

| Derived value | Formula | Why |
| --- | --- | --- |
| `resources.max_concurrent_agents` | `min(cores / 3, RAM_GiB / 3)`, floored at 1, capped at 12 | A session is a harness CLI plus its test runs; three cores and three gigabytes is what one needs to not fight its neighbours. The cap is there because past a dozen sessions the binding constraint stops being the box and becomes provider rate limits. |
| per-session CPU share | `cores / max_concurrent_agents` (derived at runtime) | What `-n auto`, `OMP_NUM_THREADS` and the BLAS caps resolve to inside a session. |
| `resources.test_slots` | `cores / 12`, floored at 1, capped at 4 | Each slot spends up to one CPU share on xdist workers. Slots are scarcer than agents on purpose: the fleet's steady state is agents thinking, not all of them testing at the same instant. |
| `resources.max_pytest_processes` | `max(4, cores)` | The `aq doctor` warning threshold — one pytest process per core is what the slot and worker caps above should already produce. |
| size class | `small` (1 agent) · `standard` (2–5) · `large` (6+) | Selects the cadence tier below. |

Worked examples:

| Box | Agents | Cores each | Test slots | Class |
| --- | --- | --- | --- | --- |
| 2 cores / 4 GiB | 1 | 2 | 1 | small |
| 4 cores / 8 GiB | 1 | 4 | 1 | small |
| 8 cores / 16 GiB | 2 | 4 | 1 | standard |
| 16 cores / 32 GiB | 5 | 3 | 1 | standard |
| 24 cores / 64 GiB | 8 | 3 | 2 | large |
| 64 cores / 256 GiB | 12 | 5 | 4 | large |

A small box is a first-class case, not a degraded one: one agent that gets
the whole machine, one test slot, and every background cadence stretched so
an idle fleet stays idle. The floor never reaches zero — a one-core box still
runs one agent with one test worker.

Preview another machine's numbers without touching yours:

```bash
aq system config tune --cores 4 --memory-gb 8
```

## Quiet when idle

An idle fleet should cost nothing but a heartbeat. Three of the tuned values
exist only for that, and they are the ones that differ most by size class:

| Key | small | standard | large |
| --- | --- | --- | --- |
| `metrics.interval_seconds` | 5.0 | 2.0 | 1.0 |
| `metrics.flush_interval_seconds` | 15.0 | 10.0 | 5.0 |
| `monitoring.failed_blocked_report_interval_seconds` | 21600 | 21600 | 21600 |
| `work_graph.gate_sweep_interval_seconds` | 60 | 30 | 30 |
| `llm_logging.retention_days` | 7 | 14 | 30 |
| `metrics.retain_seconds_1s` | 900 | 1800 | 3600 |

The per-second metrics sampler is a per-second database write whether or not
anything is running, and the repeat "still failed / still blocked" report is
the main source of notification noise on a queue with one old failed task in
it — the code default reports it every hour, forever. Six hours is the
shipped interval; drop it back to 3600 while actively triaging.

## Routing

Routing is policy, not configuration: it lives in the
`default-assignment-routing` playbook, which chooses the intelligence class
for each task. The shipped policy is `standard-high` for ordinary
implementation, debugging, refactoring, tests and coordinated multi-module
changes, and `deep-high` only for exceptional difficulty — an unresolved
architectural problem, or an investigation with concrete evidence that
standard reasoning was not enough. C++, many files, a native build, an
integration test or a red CI run are explicitly *not* reasons to escalate.

Nothing in the tuning shipped here creates per-task reviewer tickets or
final-branch review tasks. The `default-pipeline` playbook says so directly:
integration provides code validation and delivery, and the pipeline creates
no reviewer tasks or PR gates on downstream work. If you want different
routing, keep a project-scope copy of the routing playbook — no code change,
and no config key.

The one routing-adjacent knob the tuner writes is
`max_concurrent_playbook_runs`, scaled to the fleet size, because playbook
runs compete with agent sessions for the same box and the same provider
quota.

## Retry, recovery and integration

| Key | Value | Reason |
| --- | --- | --- |
| `pause_retry.rate_limit_backoff_seconds` | 60 | A provider 429 clears in seconds to minutes. |
| `pause_retry.rate_limit_max_retries` | 3 | Enough to ride out a burst; few enough that a real outage reaches `PAUSED` where an operator sees it. |
| `pause_retry.token_exhaustion_retry_seconds` | 900 | Raised from the code default of 300. A spent subscription quota window is measured in hours; retrying every five minutes only produces failures to look at. |
| `auto_task.max_verification_retries` | 2 | One reopen for a genuine flake, then a stop. |
| `swarm.prepare_timeout` | 120 | An abandoned preparation becomes eligible for recovery after two minutes. A live preparation request remains protected while its Git operations run under their own timeouts. |
| `agents_config.stuck_timeout_seconds` | 1800 (3600 on a small box) | A long tool call on a contended small box legitimately takes longer. |
| `integration.default_mode` | `pull_request` | Worker output lands on a branch and a PR, never straight onto the default branch. |
| `integration.merge_ci_policy` | `warn` | The merge path asks GitHub for the check rollup and records the verdict, but still merges. A new install usually has no CI yet, and `required` fails closed on an unreadable rollup — nothing would ever merge. Move to `required` once the default branch is reliably green. |
| `integration.merge_require_up_to_date` | `true` | A green rollup only proves the head passed against the base as it was when the run started. |

## Pull-based worker pools

The tuner sets `swarm.enabled: true`. Pull-based pools are how workers get
work — a pool worker claims the next ready task itself instead of waiting to
be pushed one — and the code default is `false` only because the pull path
post-dates the push path. `swarm.fresh_context_per_task` stays on so one
task's context does not leak into the next on a shared worker.

`swarm.max_starts_per_tick` (1 / 2 / 3 by size class) and
`swarm.scale_down_grace` (300s on a small box, 120s elsewhere) exist to stop
start/drain churn on a bursty queue. Set `swarm.enabled: false` to keep every
profile on `lifecycle: task` (push). Operator detail:
[worker pools](worker-pools.md).

## What is deliberately *not* written

Some values are correct precisely because they stay derived. The tuner omits
them so they keep deriving, and `--explain` lists them under "deliberately
left derived":

| Key | Why it is omitted |
| --- | --- |
| `resources.cores` | Keeps resolving to `os.cpu_count()` at read time. Writing this box's core count is exactly the machine-specific value that makes a config unportable. |
| `resources.per_session_cpu_share` | Stays `cores ÷ max_concurrent_agents`. |
| `resources.test_workers` | Keeps the `aq test` `-n` cap following the CPU share. |
| `swarm.global_max_active` | Inherits `resources.max_concurrent_agents`. An explicit `0` is refused — `swarm.enabled: false` is the honest way to say "no pool workers". |
| `global_token_budget_daily` | A shipped spend cap is either meaninglessly high or a surprise stop mid-task. Set it once you know what a normal day costs. |

Also absent, by design: nothing here reads your projects, memory, vault,
credentials or paths. The tuning is a function of two integers.

## Overriding

Every value is an ordinary config key. Three ways to change one, in
increasing order of blast radius:

```bash
aq system config set resources.test_slots=3       # one key, via the daemon
aq system config edit                             # the whole file in $EDITOR
aq system config tune --apply --overwrite         # re-derive and replace
```

`--apply` **keeps** any section you have already customised and reports it as
`kept`; only `--overwrite` replaces one. A section that already matches the
recommendation is reported `unchanged` and not rewritten, so re-running the
command is a no-op.

Two knobs move together and are the usual mistake:

- Raising `resources.max_concurrent_agents` to run more workers **shrinks
  every session's test parallelism**, because it is the denominator of the
  CPU share. Set `per_session_cpu_share` or `test_workers` alongside it if
  that is not what you meant.
- Raising `test_slots` without raising `max_pytest_processes` will make
  `aq doctor --check resources.test_pressure` warn as designed.

Sections that are not hot-reloadable need a daemon restart; `aq system config
set` says which, and the tuner prints a reminder after `--apply`.

## Model pricing

The tuner writes a `pricing:` table of published Claude list prices as globs,
so the token ledger and the dashboard show dollars instead of blank cost
columns and a new model in a known family is priced on arrival. **Rates
change, and Bedrock and Vertex bill separately** — check them, and add rows
for any non-Claude provider you run. A missing or stale row costs nothing but
an unpriced column.

## Relationship to portable bundles

`aq system export-portable-config` carries this tuning (and your global agent
profiles) to another install; see the module docstring in
`src/portable_config.py` for the allowlist. Because every tuned value is
either a constant or a function of cores and RAM, the recommendation exports
cleanly with no exclusions — a test asserts it. The `integration` section
travels as four keys only (`default_mode`, `merge_ci_policy`,
`merge_required_checks`, `merge_require_up_to_date`); `github_app` and
`scratch_probe` name one installation's own app, repository and key paths and
are reported as excluded rather than shipped.

Importing a bundle from a bigger machine will bring that machine's
`max_concurrent_agents` with it. Run `aq system config tune --apply
--overwrite` afterwards to re-derive for the box you are actually on.

## Where the rationale lives

`src/config_tuning.py` carries one `TuningNote` per emitted key — the reason
and the override — and `tests/test_config_tuning.py` fails if a key has no
note or a note names a key that is not emitted. `aq system config tune
--explain` prints them. This guide summarises; the notes are authoritative.
