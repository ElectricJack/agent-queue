---
tags: [guide, ops, resources, testing]
---

# Resource gating — keeping N agents from taking the box down

Read [Scheduling, worker pools and resource limits](../concepts/scheduling.md)
first for why capacity and test throughput are separate concerns. This page is
the operational guide for the installed resource policy; its numbers are
configuration defaults or local policy, not a benchmark promise.

On 2026-09-01 this repo's own daemon ran eight concurrent agents on a
24-core / 31 GB box. Each of them was following `CLAUDE.md`'s advice to run
`pytest -n auto`. `-n auto` asks the *machine* how many cores it has, and
every agent got the same answer, so eight agents became up to **192 test
processes**: load average past 60, memory pressure, and sessions killed by
the OOM reaper mid-task.

Nothing was misconfigured. The failure is structural: every agent sizes its
own work against the whole box, and no agent can see the other seven. The
fix is to make the box's capacity something the agents are *told*, not
something they measure.

Three layers do that, and each works on its own.

| Layer | Bounds | Enforced by | Needs root |
|---|---|---|---|
| 1. Session env caps + `nice` | what one session's tools *think* they may use | the launcher, at spawn | no |
| 2. `aq test` semaphore | how many test runs happen at once, box-wide | `flock` | no |
| 3. cgroup scopes | what a session *can* use, cooperative or not | the kernel | once |

Layers 1 and 2 are cooperative and on by default. Layer 3 is the backstop
for the processes that ignore the first two, and it is opt-in because it
needs a one-time privileged step.

---

## Configuration

Everything lives in one section of `~/.agent-queue/config.yaml`. The
defaults below are the shipped ones.

```yaml
resources:
  enabled: true
  # Physical budget. null → os.cpu_count().
  cores: null
  # How many agents this box is expected to run at once. This is the
  # denominator of the per-session share, so it should match the largest
  # project's max_concurrent_agents.
  max_concurrent_agents: 8
  # Explicit override for the per-session share. null → cores // agents.
  per_session_cpu_share: null
  # nice increment for the harness process. 0 disables.
  session_nice: 10

  # Layer 2 — the global test semaphore.
  test_slots: 2
  test_workers: null            # null → the per-session share
  test_wait_timeout: 1800
  test_poll_interval: 2.0
  test_deselect_markers: "not perf and not migration and not slow and not tmux and not integration"

  # Doctor thresholds.
  load_warn_ratio: 1.0          # warn when 5-min load > cores × this
  max_pytest_processes: 24

  # Layer 3 — hard limits. See "cgroups" below.
  cgroups:
    enabled: false
    cpu_quota_percent: 600
    memory_max: 6G
```

The section is hot-reloadable: the launcher reads it per launch and `aq
test` reads it per run, so a change takes effect on the next session
without a daemon restart.

On the 24-core box above, the defaults derive **3 workers per session** and
**2 concurrent test runs**, which bounds the worst case at 6 test processes
instead of 192.

---

## Layer 1 — session env caps and `nice`

`src/sessions/spec.py` folds `session_env_caps()` into every launch, so the
session's environment carries:

| Variable | What it stops |
|---|---|
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | `-n auto` resolving to the core count |
| `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` | BLAS backends spawning one thread per core *per process* |
| `UV_THREADPOOL_SIZE` | libuv (and therefore Node, and therefore the Claude CLI) sizing its blocking pool from the core count |
| `AQ_CPU_SHARE`, `AQ_CPU_CORES`, `AQ_TEST_SLOTS`, `AQ_TEST_WORKERS` | not caps — these tell `aq test` inside the worktree what the daemon derived |

The harness process is then launched under `nice -n 10`. That is the
cheapest half of the whole fix: the agents still get the CPU when it is
free, but the daemon, the dashboard, the API and tmux stay schedulable when
it is not, which is the difference between "the box is slow" and "the box
is unreachable".

**An operator-pinned value always wins.** If a key appears in a harness's
`env` block in `vault/harnesses/<id>.md`, the derived cap is not applied to
it. That is how the original stopgap (`PYTEST_XDIST_AUTO_NUM_WORKERS: "4"`)
keeps working: to switch a box over to the config-derived number, delete
that line from the harness file.

---

## Layer 2 — `aq test`

Layer 1 bounds each session. It does not bound the sum: four workers each
is still 32 processes when eight agents test at the same moment, and test
runs are exactly the bursty, everyone-at-once workload that produces the
spike. `aq test` is a pytest wrapper that takes one of `test_slots` slots
first.

```bash
aq test tests/test_pools.py              # one file, still slot-gated
aq test tests/ -k claim                  # a slice of the suite
aq test --aq-status                      # who is holding the slots
aq test --aq-no-wait tests/              # fail instead of queueing
aq test --aq-dry-run tests/              # print the pytest command
aq test --aq-reap-orphans [--aq-apply]   # free slots held by dead sessions
aq test --aq-help                        # this help (-h belongs to pytest)
```

### One full-suite run at a time

A run that selects the whole suite also takes a box-wide **full-suite lock**
with capacity one, before it takes a normal slot. On 2026-09-24 three
workers each ran the full suite for over an hour, holding three of the four
slots, and the development publisher's focused validation timed out waiting
for one and filed bogus repairs. Asking workers not to do it was not enough,
so it is now enforced:

- A second full-suite run **queues** for the lock (printing a `waiting …
  for the full-suite lock` line each poll, naming the holder) and holds **no
  slot** while it waits. With `--aq-no-wait` it is refused at once with exit
  75, naming the holder's task, cwd and how long it has been running.
- **Focused runs never touch the lock**: they only compete for the
  `test_slots` slots, so at most one of those is ever spent on the whole
  suite.
- `aq test --aq-status` shows the lock's holder and waiters under the slot
  table, and marks the slot a full-suite run holds as `busy (full suite)`.

What counts as the full suite is decided from the command line, before any
lock is taken (collecting 14,000 tests to find out would itself be the heavy
run):

| Full suite | Focused |
|---|---|
| no path (pytest collects `testpaths`), `tests/`, `tests`, `.` | `tests/test_pools.py`, `tests/perf`, any `::node-id` |
| files covering at least half the tree's test modules (`tests/test_*.py` expanded by the shell) | an area such as `tests/test_playbook*.py` |
| a broad `-k`/`-m`: `-k "not slow"`, `-m "not perf"`, `-m ""` (`--aq-all-markers`) | a narrowing `-k`/`-m`: `tests/ -k claim`, `-m perf` |
| `--lf` with nothing recorded (pytest then runs everything) | `--lf` with failures on record, `--co` / `--collect-only` |

A `-k`/`-m` expression is judged with pytest's own grammar: it is broad
when it keeps a test that matches none of its words. The lock is
`{data_dir}/locks/test-slots/full-suite/slot-0.lock`, an `flock` exactly
like the slots, so a crashed holder releases it without a reaper. It stays
held while an orphaned pytest is still running, because pytest inherits the
descriptor.

### Test scope and the recorded baseline

The fleet series' `perf.host` block reports PSI, test-slot occupancy and
ungated pytest processes. For controlled dashboard measurements,
`scripts/dashboard-perf/load.py` is the sanctioned synthetic CPU and temporary
file load: it applies session niceness and caps, holds at most 256 MiB of
temporary files, and enforces a hard deadline with cleanup. See the
[experiment protocol](../../scripts/dashboard-perf/README.md); database load
belongs only on isolated test PostgreSQL, never the operator's database.

Run focused tests for changed behavior and then the related area suite. Record
the exact `aq test` commands. The full suite runs in CI or in a task whose
subject is the suite; it is not a routine worker close check. Task authors
should name the focused and area checks a worker must run rather than require
"run the full suite before closing".

For the agent-queue project, the recorded known-failing list on `origin/main`
is a dated note in the operator vault:
`projects/agent-queue/notes/full-suite-baseline-2026-09-22.md` (under the
configured vault root, normally `~/.agent-queue/vault/`). Read the latest
recorded note and its source main SHA or CI run before interpreting a failure.
Compare failing test node IDs with that list. Investigate failures absent from
the list as possible regressions. If a listed failure is unrelated to your
change, name it in the close summary and continue; do not fail the task or
weaken or skip the test because main is red. If your task changes the failing
area, investigate its result rather than assuming the old classification still
applies. Never run another full suite just to capture your own baseline.

PostgreSQL is required for the suite. Configure a disposable server before
running tests (the base database is used only as a maintenance connection):

```bash
docker compose up -d postgres-test
export POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres
aq test tests/test_config.py
```

If `POSTGRES_TEST_DSN` is absent, `aq test` exits before taking a global test
slot or launching pytest, with the setup commands above. Bare pytest has the
same session-level preflight, so a missing DSN is one configuration error, not
one fixture error per collected test. This is an environment failure: no test
assertions ran.

Each `aq test` invocation passes a fresh run token to pytest, and every pytest
process draws its own random owner token. Every xdist worker and every
schema-mutating scratch test therefore creates a unique database, named
`aq_test_ownv2_<owner>_<run>_<worker>` or
`aq_test_ownv2_<owner>_scratch_<suffix>_<unique>`. A normal session teardown
drops only databases that the current process successfully created.

A process killed before that teardown (SIGTERM, SIGKILL, a `timeout`) cannot
clean up, so its databases carry a liveness proof. Before its first
`CREATE DATABASE` the process takes a PostgreSQL advisory lock keyed by its
owner token, on a dedicated connection to the maintenance `postgres` database
(application name `aq-test-db-owner`). It keeps the lock until teardown's drops
have finished or hit their deadline. The server releases the lock when that
connection closes, however the process ended. If the connection drops while
the process lives on (a test-server restart), the process takes the lock back
at once and refuses to create another database until it has. The lease pool's
template clones (`tests/db_fixtures.py`) are named under the same token
(`aq_test_ownv2_<owner>_<run>_<pool>_<worker>_<index>`), so the one lock covers
them too.

Each new worker database starts one background sweep, with one sweeper per
server at a time, that drops `aq_test_ownv2_*` databases whose owner lock is
free: at most eight per pass, with a 30 second timeout per drop and no
`WITH (FORCE)`, so an orphan that somebody is still connected to stays put.
The sweep never delays test startup. Teardown cancels an unfinished sweep, and
orphans it could not drop are reported as a warning and left for a later run.
`DROP DATABASE` waits for a checkpoint, so a busy PostgreSQL checkpointer can
keep the sweep from dropping anything.

The sweep never touches a name outside that versioned shape: operator
databases, `aq_tmpl_*` schema templates and every `aq_test_*` name from before
this scheme (including the short-lived `aq_test_poolv2_*` pool clones) carry no
lock that could prove their owner is gone. Those are left to the operator
reaper ([below](#reaping-abandoned-postgresql-test-databases)), which decides by
age, connections and free test slots instead. If a name ever collides, the
harness inspects `alembic_version` read-only, reports stale or unknown
revisions, and refuses to drop, migrate, or stamp the foreign database.

PostgreSQL [forces a checkpoint for each `DROP DATABASE`](https://doxygen.postgresql.org/dbcommands_8c_source.html).
The test substrate issues a worker's drops concurrently so they can share a
checkpoint; row-level resets limit the files that checkpoint must flush. A
worker's drop batch is also bounded to 90 seconds. If the checkpointer still
stalls, pytest reports the timed-out database names and development validation
defers the batch as an infrastructure failure. The next test run uses fresh
names. Once the process exits, whatever its teardown could not drop is an
`aq_test_ownv2_*` orphan with a free owner lock, which a later run's sweep
takes; a timeout never authorizes removing any other database.

Never point `POSTGRES_TEST_DSN` at the daemon's `:5533` server or the database in
`~/.agent-queue/config.yaml`. The production-URL refusal, worker
`AQ_DB_SCOPE`, and `AQ_DATABASE_URL` / `AGENT_QUEUE_DB` sentinels remain in
force; tests create their own databases on the disposable server.

Everything that is not an `--aq-*` option goes to pytest untouched. The
wrapper adds `-n <cap> --dist loadfile` and the default marker deselects **only when you
did not pass your own** — `aq test -m perf tests/perf` and `aq test -p
no:xdist tests/` both do exactly what they say.

### Wall-clock budgets need a quiet box

Everything under `tests/perf/` carries the `perf` marker, which is what keeps
it out of the default suite and out of CI's `Tests (default)` job. The
*latency* budgets there — as opposed to the statement counts, which are
deterministic — additionally take the `perf_strict` fixture (defined in
`tests/conftest.py`) and skip unless `AQ_PERF_STRICT=1`. They measure the
machine as much as the query, so under `-n auto`, or on a box running several
agents, they fail on load rather than on a regression. Run them on purpose,
serially, when the box is idle:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s tests/perf
```

The same rule applies to a wall-clock budget that lives *outside*
`tests/perf/`: mark it `perf` and take `perf_strict`, which is why the fixture
is defined at `tests/conftest.py` rather than in the perf package. An ungated
one turns CI's `Tests (default)` arm red on runner load rather than on a real
regression — `test_pathological_artifact_is_bounded` did exactly that until it
was split into a correctness half (always runs) and a budget half (gated).

The compose PostgreSQL on `:5533` (`docker-compose.yml`, service `postgres`)
carries non-default `shared_buffers` (512MB), `work_mem` (32MB), and
`effective_cache_size` (2GB) — the 4 MB default `work_mem` was seen spilling
sorts to disk under a perf investigation. A production PostgreSQL should be
tuned at least as far. `pg_stat_statements` is also preloaded via
`shared_preload_libraries`; it still needs `CREATE EXTENSION
pg_stat_statements;` run once by an operator before it is queryable.

`-m perf` matters as much as `AQ_PERF_STRICT`: `--aq-all-markers` only stops
`aq test` from adding its *own* `-m`, and pyproject's `addopts` still carries
`-m "not perf and ..."`, so without an explicit `-m` the run deselects
everything and reports "no tests were collected". `-s` matters too: each
latency test prints its p95, median and max, so a passing run records the
margin rather than only the verdict.

While it waits, it prints a line per poll naming the current holders. That
is deliberate: the daemon reads terminal silence as a stall, so an agent
queued behind a busy box has to *look* queued.

Exit codes are pytest's, with two additions: **75** (`EX_TEMPFAIL`) means no
slot (or, for a full-suite run, the full-suite lock) came free within
`test_wait_timeout` — that is "come back later", not
"your tests failed" — and **4** means one of the paths you named does not
exist, so nothing was run.

A caller that times a run can ask how much of it was queueing. With
`AQ_TEST_SLOT_REPORT=<file>` set, `aq test` appends one JSON line per slot
event (`waiting`, `acquired` with `waited`, `slot_timeout`, `released`;
format in `src/resources/slot_report.py`), and `AQ_TEST_WAIT_TIMEOUT=<s>`
overrides `test_wait_timeout` (`--aq-timeout` still wins). The development
publisher sets both so its `timeout_seconds` charges only the run, not the
wait for a slot. For a full-suite run the wait for the full-suite lock and
the slot after it are one reported wait, and the timeout bounds both
together.

The path check happens before a slot is taken, because pytest under xdist
turns a mistyped path into `no tests ran` rather than `file or directory not
found`: a run that collected and executed *nothing* otherwise reads as a
clean pass. Only unambiguously path-shaped arguments are checked (they
contain a `/` or end in `.py`), and a `::node-id` suffix is stripped before
the stat, so `-k` and `-m` expressions are never mistaken for paths. For the
same reason pytest's exit code **5** (nothing collected) is reported with an
explicit "nothing was verified" line.

### Why `flock`

The slot is held by an open file descriptor on
`{data_dir}/locks/test-slots/slot-N.lock` (by default,
`~/.agent-queue/locks/test-slots/slot-N.lock`). The kernel drops the lock
when the last descriptor closes — clean exit, `SIGKILL`, OOM kill,
`tmux kill-session`, all of them — so a crashed holder needs no reaper.
Holder metadata written into the file is advisory only: every reader
re-tests the lock rather than trusting the JSON, so a dead holder's stale
record can never make a free slot look busy.

It also works with the daemon down, which matters because `aq test` runs
inside worktrees during restarts, and a test wrapper that fails closed when
the daemon is unavailable would simply be routed around.

### Versioned box admission

Execution entry points now use `BoxLock` (`src/resources/box_lock.py`), with
protocol version 1 recorded in `protocol.json` under the same test-slot
directory. Lock inodes remain stable: `turnstile.lock`, then `box.lock`, then
the existing `slot-N.lock` capacity files. Do not delete or replace these
lock files while clients may be running. Unknown or corrupt protocol manifests
refuse admission; daemon availability is irrelevant.

Shared admission takes a shared box lock and the requested number of slots,
releasing partial claims before retrying. Exclusive admission holds the
turnstile while existing shared holders drain, blocking later shared work.
After admission it releases the turnstile and keeps the box and capacity
descriptors inheritable until execution ends. Closing a wrapper's descriptors
does not unlock a surviving child that inherited them.

This first rollout preserves today's test policy: both focused and full-suite
`aq test` runs use shared admission with weight one, and the separate full-suite
lock still limits full suites to one alongside focused tests. It introduces
neither managed jobs nor detached execution. The test database cleanup entry
point uses exclusive admission and reserves all observed slots, including
those left by a larger capacity override.

`aq test --aq-status` reports box mode and held slots from incompatible
slot-only clients. Only held locks count; stale JSON left by a dead process
does not. Exclusive admission refuses when such a holder is present. It also
reserves the old slot files as a migration fence against old clients racing
admission. Shared tests remain available during the upgrade. Upgrade every
local checkout/entry point and drain old runs before enabling managed jobs
or advertising box exclusion; the fence cannot control a legacy client that
creates previously unseen slots with an arbitrary capacity override.

### Orphaned runs

`flock` releases a *dead* holder for free. It cannot release a *live* run
nobody owns any more. On 2026-09-24 three stopped tasks' full-suite runs
outlived their sessions and held three of the box's four slots for well over
an hour. Each had been started from a detached Bash-tool shell. Harness Bash
calls run under `setsid`, so when the drained harness exited, the shell was
reparented to init. After that, nothing that stopped the session could reach
it.

Two things now close that gap.

- **Stopping a session sweeps its leftovers.** Every process a session spawns
  inherits its `AQ_INSTANCE_TOKEN`. The tmux and subprocess providers' `stop`
  still kill the pane's process tree. They then also terminate every process
  still carrying the token, plus its descendants: `SIGTERM`, then `SIGKILL`
  after the grace period (`proctable.kill_marked`). The sweep runs even when
  the tmux session is already gone. It never signals the caller, the caller's
  ancestors, or a tmux server. Tokens are minted per launch, so a same-named
  successor is never matched. The drain, stall-restart, task-close,
  pool-termination and `aq session kill` paths all end in `stop`. A process
  meant to outlive a session must be launched with the session markers
  stripped, as `aq start` does for the daemon and the dashboard server.
- **Slot holders are attributable, and orphans are reapable.** The slot record
  names the session (`session_id`), the task (read from `.aq/claim.json` for a
  pool worker, whose environment has no `AQ_TASK_ID`), the pid and start time
  of the session's harness (`session_root`), and the `AQ_TEST_RUN_ID` its
  pytest children inherit. A held slot is one of three kinds:
  - `live`: the harness is still running. **Never reaped**, whatever its
    task's status. Stop the session instead.
  - `orphaned`: the harness has exited. A record written before attribution
    existed counts as orphaned only when every session-marked process keeping
    the lock has been reparented to init.
  - `unattributed`: no session at all, such as a human's shell or CI. Reported,
    never reaped.

```bash
aq test --aq-status                          # Session column: live / orphaned / unattributed
aq test --aq-reap-orphans                    # dry run: what would be terminated
aq test --aq-reap-orphans --aq-apply         # terminate them; exit 1 if a slot stays held
aq doctor --check resources.orphaned_test_runs [--fix]
```

A reap terminates only processes that belong to the dead run: those that
keep the slot file open and carry the run's id, the dead session's token or
the recorded wrapper pid, plus every process carrying the run's
`AQ_TEST_RUN_ID` and their descendants. A live session's waiter probing the
same slot file is left alone. The verdict is re-derived immediately before
any signal, and the lock is re-tested afterwards.

---

## Layer 3 — cgroup v2 scopes (optional)

Layers 1 and 2 are cooperative. A script that hardcodes `-n 24`, or a build
that spawns per-core regardless of `OMP_NUM_THREADS`, still gets to take
the box down. Layer 3 launches each session inside a systemd scope with a
`CPUQuota` and a `MemoryMax` the kernel enforces whether the process
cooperates or not.

Creating such a scope is privileged unless the daemon user's slice has
`Delegate=yes`, which only root can set, and only once:

```bash
sudo scripts/setup-cgroup-delegation.sh          # defaults to $SUDO_USER
```

Then enable it and restart:

```yaml
resources:
  cgroups:
    enabled: true
    cpu_quota_percent: 600   # six cores per session
    memory_max: 6G
```

```bash
./run.sh restart
aq doctor --check resources.cgroups
```

**Absence degrades, it never blocks a launch.** The daemon probes
delegation once at startup, logs the reason if it is missing, and falls
back to layer 1. `resources.cgroups` reports `info` when layer 3 is off and
`warn` when it is switched on but not actually working — the latter being
the dangerous state, where an operator believes hard limits are protecting
them and they are not.

On WSL2 and in containers, delegation is frequently unavailable and the
script will say so. That is a supported configuration; layers 1 and 2 carry
the load.

---

## Rolling it out on an existing install

Layers 1 and 2 are on by default — there is nothing to add to
`~/.agent-queue/config.yaml` unless you want to change a number. Two
deployment steps do need a human, because both touch live shared state:

1. **Retire the stopgap.** `vault/harnesses/claude.md` and
   `vault/harnesses/codex.md` may still carry
   `"PYTEST_XDIST_AUTO_NUM_WORKERS": "4"` in their `env` block from before
   this existed. It still works — an operator-pinned key always wins — but
   it pins one harness to a number that no longer tracks the box. Delete
   the line from both files to let the derived share apply; the vault
   watcher picks it up live, no restart.
2. **Pin the denominator.** `resources.max_concurrent_agents` defaults to
   `8`. If your project's `max_concurrent_agents` differs, set it to match,
   or the derived share will be wrong in whichever direction the two
   disagree.

Then confirm the launcher is doing it — the daemon states the budget once
at startup:

```
Resource gating: 24 core(s) / 8 concurrent agent(s) -> 3 worker(s) per
session, nice +10, 2 global test slot(s)
```

## Diagnosing a saturated box

```bash
aq doctor --check resources.load
aq doctor --check resources.test_pressure
aq doctor --check resources.orphaned_test_runs
aq test --aq-status
```

| Check | Fires when | Reports |
|---|---|---|
| `resources.load` | 5-min load > `cores × load_warn_ratio` | the load figures plus the pytest processes per session |
| `resources.test_pressure` | more than `max_pytest_processes` pytest processes box-wide | the count and which sessions own them |
| `resources.cgroups` | always | whether hard limits are actually in force |
| `resources.orphaned_test_runs` | a test slot is held by a run whose session is gone (fixable) | each held slot's verdict; `--fix` terminates the orphans ([Orphaned runs](#orphaned-runs)) |

The load check reads the **5-minute** average on purpose. A 1-minute spike
is a build starting; five minutes above one runnable task per core is a box
where every agent is now slower than it needs to be and the OOM killer is
the next event.

Both checks attribute processes back to sessions from `/proc`: `AQ_TASK_ID`
and `AQ_SESSION_NAME` from the process environment (set on every launch and
inherited by everything the harness spawns), falling back to the worktree
slot in the process's `cwd`. A finding therefore names `slot-3 /
prime-ember (96)` rather than an anonymous number, because "which session?"
is always the operator's next question.

---

## Reaping abandoned PostgreSQL test databases

The test harness sweeps `aq_test_ownv2_*` orphans itself (see
[the owner lock](#test-scope-and-the-recorded-baseline) above). Everything that sweep
cannot prove dead stays behind: `aq_test_*` names from before the owner lock,
an orphan the sweep kept failing to drop, and schema templates named
`aq_tmpl_<slug>` whose source schema is no longer present in any linked
checkout. Run the standalone operator tool from the repository root with the
test server's maintenance DSN:

```bash
export POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres
python -m scripts.reap_test_databases
# Review the plan; when no test runs are active:
python -m scripts.reap_test_databases --apply
```

The default is a read-only dry run that lists each test/template database and
why it would be dropped or kept. It keeps any database named for the run token
of a held `aq test` slot, reading the token after the owner token in
`aq_test_ownv2_<owner>_<run>_*` names. `--apply` requires all `aq test` slots to be
free, reserves them for the cleanup, and refuses while a bare pytest process
is running. It also rechecks each database's identity and connections just
before a plain `DROP DATABASE`; it never uses `WITH (FORCE)`. The database
configured in `~/.agent-queue/config.yaml`, `postgres`, `template0`, and
`template1` are protected. The minimum age is six hours by default;
`--min-age-hours` can raise it. Age comes from the status-change time of the
database's `PG_VERSION` file, so missing or recent metadata keeps a database.

Template retention checks every linked Git worktree. Pass `--checkout PATH`
once for each independent clone that may produce a different schema slug.
If a checkout cannot be inspected, the tool keeps all templates and reports
why. This tool is operator-invoked. A test run removes only its own databases
at teardown and, in its background sweep, `aq_test_ownv2_*` databases whose
owner lock is free.

Runs from before the separate test server created their databases on the
daemon's `:5533` server. To inventory those, point `POSTGRES_TEST_DSN` at that
server's `/postgres` database for the reaper invocation only; the daemon's own
database is protected by name, as above. Never run tests with that DSN.

---

## Verification

[The verification note](../analysis/2026-09-01-resource-gating-verification.md) records the
before/after load numbers measured on the box this was built for.

Unit coverage:

```bash
aq test tests/test_resource_limits.py tests/test_resource_semaphore.py \
        tests/test_resource_doctor.py tests/test_cli_test_runner.py \
        tests/test_resource_test_runs.py
```
