# Exclusive job queue — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [agent sleep/wake](2026-09-24-agent-sleep-wake.md) ·
[wake context compaction](2026-09-24-wake-context-compaction.md) ·
[managed long-running commands](2026-09-24-managed-long-running-commands.md) ·
[smart test selection](2026-09-24-smart-test-selection.md) ·
[resource-aware planner](2026-09-24-resource-aware-planner.md) ·
[dashboard performance and separation](2026-09-24-dashboard-performance-and-separation.md) ·
existing: [resource gating guide](../../guides/resource-gating.md),
[scheduling concepts](../../concepts/scheduling.md),
[resource-gating verification](../../analysis/2026-09-01-resource-gating-verification.md)

## 1. The ask

Tests, builds and other heavy processes should go **through Agent Queue**
instead of being run directly from an agent's shell. Each one is
**categorised** as parallel-safe or exclusive, and exclusive work runs **one at
a time**. This replaces the fragile model in which an agent's own process
holds a lock for as long as its job runs.

The operator sees this as the keystone of the current feedback round. They
expect it to fix three things:

1. **Test contention.** Several agents running suites at once saturate the box.
2. **Idle-agent token waste.** An agent waiting on a long run stays live and
   spends tokens. The queue should let it go dormant and be woken when the job
   finishes (see [sleep/wake](2026-09-24-agent-sleep-wake.md)).
3. **Probably most of the frontend lag.** The operator noticed the dashboard
   lagging while a CI agent was hammering the machine.

The third claim is a **hypothesis**, and this spec treats it as one (§3.6,
§6 Q1). The operator also sees "wait on something without burning tokens, and
notify the supervisor when it finishes" as a pattern shared with voice,
hourly supervisor updates and realtime collaboration. The job queue is the
first consumer of that pattern, so its completion signal should be the generic
one (§7).

## 2. What exists today

### 2.1 Three layers of resource gating (`src/resources/`)

Documented in [resource-gating.md](../../guides/resource-gating.md) and in
`src/resources/__init__.py`:

| Layer | Code | What it bounds |
|---|---|---|
| 1. Env caps + `nice` | `limits.py` — `session_env_caps()`, `wrap_session_argv()`, applied in `src/sessions/spec.py` (~L521–523) | One session's share: `PYTEST_XDIST_AUTO_NUM_WORKERS`, BLAS/libuv threads, `nice -n resources.session_nice` (default 10) on the harness |
| 2. `aq test` semaphore | `semaphore.py` — `SlotSemaphore` (flock, one file per slot), `full_suite_lock_dir()` | How many pytest runs happen at once, box-wide (`resources.test_slots`, default 2) |
| 3. cgroup scopes | `limits.py` — `_cgroup_prefix()`, `ResourceCgroupConfig` (`src/config.py:2135`) | Hard `CPUQuota`/`MemoryMax` per **session**. Opt-in, and it needs delegation |

Supporting modules: `procs.py` (`scan_processes`, `pytest_processes`,
`summarize_by_session`, `load_average`) attributes load to sessions from
`/proc`. `test_runs.py` (`holder_identity`, `held_slots`, `reap_orphans`)
classifies each slot holder as live, orphaned or unattributed.
`slot_report.py` writes the JSONL wait report. `test_db_reaper.py` drops test
databases. The doctor checks are in `src/doctor/resource_checks.py`
(`resources.load`, `.test_pressure`, `.cgroups`, `.orphaned_test_runs`).
Config is `ResourcesConfig` (`src/config.py:2168`), which is hot-reloadable.
The defaults come from `src/config_tuning.py` (`MachineResources.test_slots`).

The flock design is deliberate and documented (semaphore.py module
docstring). Its two advantages are that **crash release is free** and that
there is **no daemon dependency**: `aq test` has to work in a worktree while
the daemon restarts. Any replacement must keep both properties or say why it
can give them up.

### 2.2 How a test run blocks an agent today

Traced through `src/cli/test_runner.py` (`test_command`, L692):

1. The agent's harness Bash tool runs `aq test <args>` in the worktree, and the
   harness turn blocks on that tool call.
2. `_is_full_suite()` (L395) classifies the command statically. There is no
   collection step; `_FULL_SUITE_SHARE = 0.5` at L278 decides a list of files.
   That makes it the **only categoriser that exists**, and its output is
   binary: `full` or `focused`.
3. A full-suite run takes the one-slot full-suite lock first, then a normal
   slot (L846–875). A focused run takes only a slot.
4. `SlotSemaphore.acquire()` (semaphore.py L189) **polls** every
   `test_poll_interval` (2 s). It writes a waiter file and calls `on_wait`,
   which prints a line on every poll. Printing is how the queue stays visible:
   the session reconciler's stall ladder (`_step_stall_ladder`,
   `src/sessions/reconciler.py:1300`) treats pane silence as a stall, and pane
   activity comes from tmux `#{window_activity}` (`src/sessions/tmux.py:583`).
   The queue has no order. Whichever poller retries first after a release wins.
5. With the slot held, `_run_forwarding_signals()` (L464) runs pytest with
   `Popen(close_fds=False)`. The inheritable flock descriptor is kept open, so
   pytest itself holds the slot, and SIGTERM/SIGINT/SIGHUP are forwarded to it.
6. Output streams into the harness tool result. The exit code is pytest's,
   plus `75` for no slot and `4` for a bad path. `AQ_TEST_RUN_ID` goes to
   children so the test databases and the reaper can find them.

For the whole of the wait and the run, the agent's session stays **live**. It
occupies a pool seat or push agent, holds its task claim and worktree lock,
and whatever the harness does while waiting is billed. A tool call that is
simply blocked sends no tokens. The cost comes from harness tool timeouts,
which push agents to background the run and poll it turn by turn, and from a
prompt cache that expires during a long wait. Both are **unverified here**;
the size of the cost should be measured from `token_ledger` rather than
assumed.

### 2.3 How a session death reaches a job

- **The kernel frees a dead holder's flock.** The session is a different
  matter. `SessionProvider.stop` in both the tmux and subprocess providers
  ends with `proctable.kill_marked(instance_token)`
  (`src/sessions/proctable.py:316`). That terminates **every** process still
  carrying the session's `AQ_INSTANCE_TOKEN`, including an `aq test` run.
  **So today, putting an agent to sleep or draining it kills its test run.**
  An agent cannot go dormant while its own descendants hold the job, which is
  the main structural reason the job has to leave the agent's process tree.
- A live run with no owner (a detached shell that init adopted) is handled by
  `reap_orphans` and `aq doctor --check resources.orphaned_test_runs --fix`.

### 2.4 Precedents in the daemon

- **Daemon-side test runs already exist.** `src/integration/development_validation.py`
  `run_check()` (L207) runs a validation command, normally `aq test …`, under
  `bash -c` with `start_new_session=True`. It captures output as it arrives,
  enforces a run budget separately from slot wait through
  `AQ_TEST_SLOT_REPORT`/`AQ_TEST_WAIT_TIMEOUT` (`slot_report.py`), and parses
  the result with `parse_pytest_output()`/`classify()`. So the daemon already
  runs a job outside any agent's process tree while competing for the same
  flock slots. One caveat: it passes `**os.environ`, the daemon's own
  environment, through to the child.
- **A DB-lease exclusive slot already exists.** `src/orchestrator/merge_slot.py`
  holds a per-project TTL lease on a DB row, renewed by its holder, and
  `break_expired_merge_slots` clears expired leases. That is the "agent holds a
  lock" pattern in database form, with the reaper the flock design wanted to
  avoid.
- **Sleeping sessions exist.** `sessions.state`/`desired_state` include
  `sleeping` (`src/database/tables.py` ~L1214). `_apply_rate_limit_cooldown`
  and the named-session idle drain (`reconciler.py` ~L1152, ~L1709) set it,
  and `session_wake` (`src/commands/session_commands.py:569`) plus message
  delivery (`src/messages/delivery.py`, `via="nudge"`) are the wake paths.
  Whether a **task/pool** session can sleep while it holds a claim is the
  [sleep/wake spec](2026-09-24-agent-sleep-wake.md)'s question, not this one's.

### 2.5 Observability

`aq test --aq-status` shows slot and full-suite holders and waiters, with a
live/orphaned/unattributed verdict for each. The metrics sampler
(`src/metrics/sampler.py`) records `load1/5/15` and memory once a second
(`read_machine`, L86), plus `sampler.collect_ms`, a rough proxy for daemon
and DB responsiveness. Its `slots` field counts **worktree** slots
(`_slot_cap`, L687), **not test slots**. Test-slot occupancy, queue depth and
per-run CPU are in no time series. Nothing durable records a run's duration,
its outcome or the resources it used.

### 2.6 What is not gated

Only `aq test`/pytest goes through the semaphore. A grep for
`SlotSemaphore`/`default_lock_dir` finds consumers only in `src/cli/test_runner.py`,
`src/resources/*`, `src/doctor/resource_checks.py` and
`scripts/reap_test_databases.py`. Dashboard builds (`npm`/Vite), Docker,
`scripts/e2e-smoke.sh`, which runs a real daemon for about 8 minutes,
repo-wide `ruff`, and a bare `pytest` that ignores CLAUDE.md all run
ungated. The only check on them is layer 1's `nice`, plus layer 3 when it is
on.

## 3. Gaps

1. **One categoriser, two classes, and exclusive is not really exclusive.** A
   full-suite run is exclusive only with respect to other full-suite runs. It
   runs next to up to `test_slots − 1` focused runs. A **quiet box**, which
   `perf` wall-clock budgets need (`AQ_PERF_STRICT=1`), is enforced by
   nothing.
2. **The wait lives in the agent.** Queueing, stall-avoidance printing and the
   run are all inside the harness's tool call. The agent cannot be released
   while its job waits or runs (§2.2, §2.3).
3. **No order, priority or fairness.** Waiting is a 2-second poll race. A
   development-publisher validation, which blocks a merge, competes on equal
   terms with a worker's exploratory run. There is also no writer preference,
   so under a steady stream of focused runs an "exclusive" request could wait
   indefinitely if we built one naively on flock `LOCK_SH`/`LOCK_EX`.
4. **No durable history.** Categorisation by learning and the
   [planner](2026-09-24-resource-aware-planner.md) both need per-command
   duration, CPU-seconds and peak RSS, and none of it is recorded.
5. **Non-pytest heavy work is ungated** (§2.6).
6. **The lag attribution is unmeasured.** Agent sessions, and therefore their
   pytest children, already run at `nice +10`. CPU contention alone should
   leave the daemon and the dashboard server schedulable, and the resource
   guide says as much ("the daemon, the dashboard, the API and tmux stay
   schedulable"). If the dashboard still lags, the likelier causes are
   (a) memory pressure or swap, (b) disk I/O, for example the test
   PostgreSQL's forced checkpoint on each `DROP DATABASE` (guide §"Test
   scope"), which shares a disk with the daemon's PostgreSQL, (c) the daemon's
   own query load, or (d) a process that escaped `nice`. None of these is
   measured today. PSI (`/proc/pressure/{cpu,io,memory}`) is not sampled.
   **A queue that serialises heavy work would reduce all four, but whether
   that is what fixes the lag is unproven.**

## 4. Implementation options

### Option A — Generalise the flock semaphore into job classes

**Sketch.** Keep everything client-side. A generic wrapper,
`aq run --class <c> -- <cmd…>`, joins `aq test`, with `aq test` becoming a
preset of it. Classes map onto flock modes on one box lock:

- `shared` jobs take `LOCK_SH` on `box.lock` plus one of N weighted slots.
- `exclusive` jobs take `LOCK_EX` on `box.lock`, so they run alone among jobs.
- A turnstile file (`gate.lock`, taken `LOCK_EX` briefly by every entrant and
  held by a waiting exclusive job) gives writer preference, so exclusive
  requests do not starve.
- An optional `quiet` class also writes a marker the daemon reads to hold new
  launches. That is a reach into planner territory.

The category comes from command-line heuristics (`_is_full_suite`, `-m perf`,
known build commands) plus an optional vault/config table. Each run appends a
line to a JSONL history (`<data_dir>/jobs/history.jsonl`) with argv
signature, class, wait, duration, exit code and `getrusage(RUSAGE_CHILDREN)`
CPU/max-RSS.

**Touches.** `src/resources/semaphore.py` (mode and turnstile),
`src/cli/test_runner.py`, a new `src/cli/run_wrapper.py`, `ResourcesConfig`,
`aq test --aq-status`, and `tests/test_resource_semaphore.py`.

**Pros.** Keeps crash-free release and daemon independence. Small, and
reuses tested code. Gives exclusive semantics and history within days.

**Cons.** Does nothing for gap 2: the agent still waits in-process, and
`kill_marked` still kills the job when the session stops. The history is a
file, not queryable state. flock has no FIFO; the turnstile orders exclusive
jobs against shared ones but not waiters within a class. Priority is not
possible without a coordinator.

**Size:** S–M.

### Option B — A daemon-owned durable job queue that runs jobs outside the agent's process tree

**Sketch.** Add a `jobs` table: id, task_id, attempt/session id, project_id,
principal, argv or a named preset, cwd, env allowlist snapshot, class,
priority, state (`queued|running|succeeded|failed|cancelled|lost`),
`submitted_at/started_at/ended_at`, exit code, `log_path`, parsed summary
JSON, resource usage, `git_head` and a dirty flag at submission. Commands
`job_submit`, `job_get`, `job_list`, `job_cancel` and `job_logs` go through
`CommandHandler`, which makes them MCP/CLI/API automatically.

A pure `plan_job_starts(queued, running, machine, policy)` decides what to
start each orchestrator cycle, in the style of `size_pools`. A runner
launches each job **detached** with its own `AQ_JOB_ID` marker and **without**
the session's `AQ_INSTANCE_TOKEN`, so stopping the requesting session does not
kill it. Output goes to `<data_dir>/jobs/<id>.log`, with a summary parsed by
reusing `development_validation.parse_pytest_output`.

When a job finishes, the daemon emits `job.finished`. It messages the task or
session (`src/messages/`), which wakes a sleeping agent, and it becomes a fact
for the digest and the supervisor. After a daemon restart, running jobs are
re-adopted through a proctable scan for `AQ_JOB_ID`, the same way sessions are
adopted.

There is a sub-choice for the runner. (B1) Plain detached process groups,
like `run_check`. (B2) Each job as a session-provider handle, reusing tmux's
adoption, `peek`, the kill fence and the cgroup wrapping
(`wrap_session_argv`), with a new `lifecycle: job`. B2 reuses more but
stretches the session model; the `sessions.lifecycle` comment lists only
`task|named` today, with `pool` added by the swarm work.

**Touches.** `src/database/tables.py` plus an alembic revision, a new
`src/jobs/` (policy, runner, adoption), `src/commands/job_commands.py`, the
orchestrator cycle, `src/sessions/proctable.py` for the marker scan, the
metrics sampler (queue depth), the dashboard, and an openapi regen.

**Pros.** Solves gap 2: the agent can detach and sleep, and a job survives
the agent's death by design. Real ordering, priority and fairness. The
history lives in PostgreSQL where the planner can query it. One place to
enforce the env allowlist and permissions. Cancellation is a command.

**Cons.** Loses "works with the daemon down". Loses free crash release:
`lost` detection and adoption are exactly the reaper the flock design
avoided. The daemon now runs arbitrary argv submitted by workers, and its own
environment holds the DB URL and provider keys (the `run_check` precedent
passes `os.environ`), so the job env must be built from an allowlist, as
`src/dashboard_server/process.py` does. Output is no longer inline in the
agent's tool result.

**Size:** L–XL.

### Option C — Hybrid: the daemon queue as authority, `aq test` as a thin client, flock as the floor

**Sketch.** Option B's table, policy and runner, with three compatibility
rules:

1. **The runner still takes the flock** (Option A's classes) around every job
   it launches. The flock stays the machine-level truth, so a bare `aq test`
   run during a daemon restart and daemon-run jobs contend on one semaphore.
   It also means a daemon crash releases the job's hold, provided the job
   process is not holding the descriptor, which is itself a design choice.
2. **`aq test` keeps its interface.** By default it submits, then *attaches*:
   it tails the log, prints the same queue lines, and exits with the job's
   exit code. Agents and CLAUDE.md need no change on day one.
   `aq test --detach` returns `{job_id}` at once, and that is the path to
   sleeping. If the daemon cannot be reached, `aq test` falls back to today's
   in-process flock path and records the history row when it can.
3. **Priority classes are a policy table.** Integration validation beats the
   supervisor's jobs, which beat worker focused runs, which beat worker full
   suites. It lives in config, not code.

**Touches.** The union of A and B, but it can land in the order A → B →
detach.

**Pros.** Each phase ships alone: A fixes exclusivity now, B adds durability,
and detach enables sleep. It keeps the flock design's guarantees as a floor
instead of discarding them. Agents migrate implicitly.

**Cons.** Two paths to keep coherent. Attach mode still keeps an agent live;
the saving needs `--detach` plus sleep/wake. The fallback path means history
has gaps.

**Size:** L overall (A: S–M, B core: L, detach and wake: M).

### 4.1 Cross-cutting design points (any option)

**Categorisation: who decides.**

- **Declared.** A class table keyed by command preset. Options for where it
  lives are `resources.jobs.classes` in config or `vault/job-classes/*.md`.
  An agent may ask for a *stricter* class but never a looser one, mirroring
  how `provider_intent` restricts pinning to trusted principals.
- **Heuristic**, as the default:
  - `_is_full_suite()` makes a run exclusive.
  - `-m perf`/`AQ_PERF_STRICT` makes it `quiet`.
  - `scripts/e2e-smoke.sh` makes it exclusive.
  - An `npm run build` or Vite build is shared with weight 2 (a guess).
  - A focused pytest run is shared with weight 1.
- **Learned.** From history rows, a command signature (normalised argv plus
  the test-file set) whose p90 CPU-seconds per wall-second or peak RSS
  crosses a threshold is promoted. Promotion only; demotion takes a human.
  Needs the Phase 1 history first.

**Where output goes.** A log file per job with a retention sweep, and a
parsed summary on the row (pass/fail counts, failing node ids through
`parse_pytest_output`). `aq job logs [-f]` reads it. The wake message carries
the summary, never the whole log, which serves
[wake context compaction](2026-09-24-wake-context-compaction.md).

**Cancellation.** Explicit (`aq job cancel`). Implicit when the owning task
leaves an active status: closed, failed, or deleted through the
`task_delete` path. Superseding, where a newer identical submission from the
same task cancels the queued older one, is optional. Running jobs get
SIGTERM, then SIGKILL, to the process group, by `AQ_JOB_ID` marker as in
`kill_marked_sync`.

**Requesting agent dies.** A job belongs to the task, not the session. If
the task is still active (a restart or re-claim is coming), the job keeps
running and the next attempt finds its result through `aq prime` or
`aq job list --task`. If the task ends, the job is cancelled. A worktree the
job is running in cannot be reclaimed until the job stops, so this needs an
interlock with workspace release.

**Worktree consistency.** The job runs in the agent's live worktree. If the
agent keeps editing, the results describe a moving tree. The minimum is to
record `git_head` and dirty state at submission and start. A stronger
alternative is to run against a `git stash create` snapshot or a detached
worktree at that SHA, which costs disk and time.

## 5. Initial take

Tentatively **Option C, phased**. This recommendation is provisional.

- **Phase 0, measure (S).** Add test-slot occupancy, queue depth and PSI
  (`/proc/pressure/*`) to the metrics sample, next to `read_machine()`, plus a
  dashboard-server request-latency series (coordinate with
  [dashboard performance](2026-09-24-dashboard-performance-and-separation.md)).
  Then compare a window with `test_slots: 1` against the current setting. This
  either supports or refutes the lag hypothesis before we build L-sized
  machinery on it.
- **Phase 1, Option A (S–M).** Add shared/exclusive/quiet classes to the flock
  semaphore with a turnstile, an `aq run` wrapper for builds and e2e, and a
  JSONL history. This alone delivers "exclusive runs one at a time".
- **Phase 2, the daemon queue (L).** Add the `jobs` table, a pure start policy,
  a detached runner with its own marker and an env allowlist, adoption, and
  `job.finished`, with `aq test` attaching by default.
- **Phase 3, detach plus sleep (M).** Add `aq test --detach` and a wake on
  `job.finished` through the generic "wait on X, wake me" primitive from the
  [sleep/wake spec](2026-09-24-agent-sleep-wake.md).

The reasoning: exclusivity and history are cheap and urgent. The token saving
and the survival of agent death depend on sleep/wake, which is being designed
separately. Discarding the flock guarantees wholesale would reintroduce a
reaper for no gain.

## 6. Open questions

1. **Is the frontend lag CPU, memory, I/O or daemon DB load?** This decides
   whether serialising jobs is *the* fix or only a contributor. Phase 0
   answers it, so Phase 2's priority should wait on the answer.
2. **What does "one at a time" mean?** Literally one heavy job box-wide, or
   one *exclusive* job with shared ones still concurrent up to N? The
   operator's wording could be read either way, and it sets throughput for
   focused runs.
3. **Does `quiet` also pause agent launches?** A perf budget needs the whole
   box idle, not just no other jobs. That puts the queue in charge of the
   scheduler, which overlaps the [planner](2026-09-24-resource-aware-planner.md).
4. **Should the daemon execute arbitrary argv, or only named presets**
   (`test`, `build-dashboard`, `e2e`, `lint`)? Presets are safer and make
   categorisation trivial. Arbitrary argv is more general. This decides the
   permission model and whether `aq run -- <anything>` exists.
5. **What is the env contract?** Which variables pass from the agent to the
   job: `POSTGRES_TEST_DSN`, `AQ_DB_SCOPE=worker` and the DB sentinels, the
   xdist cap, `PATH`/venv? The daemon's env must never leak through. What
   does `run_check` switch to?
6. **Does the job process hold the flock (inherited fd) or the runner?** If
   the job holds it, a daemon crash leaves the job running and holding the
   slot, which is correct. If the runner holds it, a crash frees a slot while
   the job still runs. Decides crash semantics.
7. **When a session dies and the task stays active, keep or cancel the job?**
   Keeping it saves a rerun, but its result may be for a tree the next attempt
   has changed. Also: do jobs of a task that is being provider-failed-over
   (`src/orchestrator/provider_failover.py` checkpoint) survive the hand-off?
8. **Worktree snapshot, or run in place?** Decides correctness of results
   against cost, and whether an agent may keep editing while a job runs.
9. **What are the priority classes?** Integration validation
   (`development_validation`) and CI-sentinel repairs clearly outrank
   worker runs. Who else, and is there aging to prevent starvation?
10. **Where does the categorisation table live** (config, vault or DB), and
    who may edit it? Vault matches the profiles and intelligence classes
    pattern and gets hot reload.
11. **Should the development publisher's `run_check` become a job client?**
    That would give one queue with priorities, instead of the publisher
    polling a slot report.
12. **Do harness tool timeouts force detach anyway?** If the harness Bash
    timeout (unverified per harness) is shorter than a typical queued run,
    attach mode pushes agents back to polling, and Phase 3 becomes more
    urgent. The retention of logs and rows is also open; the `metrics:`
    per-tier retention is a precedent.

## 7. Dependencies and sequencing

- **Blocks** the [resource-aware planner](2026-09-24-resource-aware-planner.md),
  which needs the per-command duration and resource history from Phase 1–2.
- **Shares a runner with** [managed long-running commands](2026-09-24-managed-long-running-commands.md).
  A dev server or a watch process is a job with no natural end. The two specs
  should agree on one runner, one marker scheme and one log location before
  either builds its own. This is the most likely place for duplicated effort.
- **Phase 3 depends on** [agent sleep/wake](2026-09-24-agent-sleep-wake.md)
  (dormancy while holding a claim, and a typed wake on `job.finished`) and
  benefits from [wake context compaction](2026-09-24-wake-context-compaction.md),
  since the wake message is a summary.
- **Complements** [smart test selection](2026-09-24-smart-test-selection.md).
  Selection shrinks the job before submission; the queue categorises what is
  left, and a selected slice is `shared` by `_is_full_suite`'s rules.
- **Feeds** [supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md)
  and the [morning report](2026-09-24-morning-report-playbook.md):
  `job.finished` is a durable fact. For the digest, only rows count, never
  bus-only events (see `src/digest/` and `collect_digest_activity`).
- **Phase 0 overlaps** [dashboard performance](2026-09-24-dashboard-performance-and-separation.md)
  on lag measurement, so land the measurement once.

## 8. Non-goals

- No distributed or multi-host scheduling: one box, one queue.
- No replacement of layer 1 (env caps, `nice`) or layer 3 (cgroups). The queue
  sits alongside them. Per-job cgroup scopes are a possible follow-up.
- No CI runner of our own. GitHub CI stays what it is, and `ci_baseline_status`
  keeps reading it through `gh`.
- No change to which tests a task must run. That is task-authoring policy and
  [smart test selection](2026-09-24-smart-test-selection.md).
- No sleep/wake mechanism here, only the event it consumes.
