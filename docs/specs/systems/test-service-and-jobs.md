# System spec: test service and jobs

How Agent Queue runs tests and other long, finite commands without freezing
the box or losing the result. This covers `aq test` (the foreground test
runner every agent uses), the run-owned test databases, the managed job queue
in `src/jobs/` (detached runs that survive a daemon restart and report a
durable result), the end-to-end kit, smart test selection, and the GitHub
Actions workflows whose check names the integration service waits on.

This is system 16 of the 18 in the system inventory. It describes `main` as of
2026-10-20. Where a guide already explains something well, this page gives the
short version and links to it. Line numbers are a starting point for a reader;
they drift.

## Contents

- [Purpose](#purpose)
- [Vocabulary](#vocabulary)
- [How it works today](#how-it-works-today)
- [Data model](#data-model)
- [Boundaries](#boundaries)
- [Design principles and invariants](#design-principles-and-invariants)
- [Known gaps](#known-gaps)
- [Planned work](#planned-work)
- [Source and tests](#source-and-tests)

## Purpose

Agent Queue's own test suite has about 14,000 tests and needs PostgreSQL. One
full run takes many minutes and every core on the machine. On a box running
twenty or more coding agents, each of them told to "run the tests", this goes
wrong in three ways:

1. **The box freezes.** Eight agents each start `pytest -n auto` and the
   machine spends its time switching between a few hundred processes.
2. **Results get lost or misread.** A test run started from an agent's
   terminal dies with that terminal. Its output scrolls away. An agent cannot
   tell a broken environment (no database, no free slot) from a broken test.
3. **Nobody knows what to run.** A change to one module needs a few test
   files, not the whole suite. Agents guess, and either run too little or run
   everything.

This system answers each of those:

1. **`aq test` runs tests politely.** It waits for one of a small number of
   box-wide test slots, caps xdist workers to the session's CPU share, and
   deselects the slow marker families unless asked. At most one full-suite run
   is in progress on the box at any time.
2. **Managed jobs run long commands durably.** A job is a finite, server-built
   command (a test run, a lint, an npm build, the e2e smoke) that the daemon
   queues, admits against the same slots, runs in a detached supervisor
   process, and finishes with an immutable result. It survives a daemon
   restart. The result reaches the waiting agent through the durable wait
   system, and the integration service uses jobs as its local CI.
3. **Smart test selection proposes what to run.** It maps a change to the test
   modules it can affect and records that proposal. Today it only observes. It
   never narrows a run.

The GitHub Actions workflows are the fourth piece. They are the hosted CI
whose check names the integration train and task completion require.

## Vocabulary

| Term | Meaning |
|---|---|
| **Test slot** | One of `resources.test_slots` `flock` files under `<data_dir>/locks/test-slots/`. A run holds one slot while pytest runs. The kernel frees it when the holder dies. |
| **Full-suite lock** | A separate one-slot lock under `test-slots/full-suite/`. A run that `aq test` judges to be a full suite must take it before it takes a test slot. |
| **Box lock** | The versioned admission protocol over the slot files ([box_lock.py](../../../src/resources/box_lock.py)): a turnstile, a box lock, then the slots. Shared runs take some slots. Exclusive runs take all of them. |
| **Worker cap** | The xdist worker count `aq test` passes as `-n`. It is the session's CPU share unless `resources.test_workers` sets it. |
| **Default deselect** | `-m "not perf and not migration and not slow and not tmux and not integration"`. Both `pyproject.toml` and `aq test` apply it unless the caller passes `-m`. |
| **Marker arm** | One of the slow marker families run on purpose: `-m "migration or slow"` or `-m "integration or perf"`. CI runs each as its own job. |
| **Run-owned database** | A PostgreSQL database named `aq_test_ownv2_<owner>_<run>_<worker>`, created per test process and proven alive by an advisory lock. |
| **Job** | One row in `jobs`: a server-built command with an owner, a workspace pin, a state machine and, when it finishes, an immutable result. |
| **Preset** | The finite kind of command a job may run: `test`, `lint`, `e2e`, the Node presets, or `matter_render`. The server builds the argv. There is no shell. |
| **Runner** | The detached supervisor process, `python -m src.jobs.runner <dir>`. It holds the slot locks, runs the child, writes fsynced receipts. It has no database access. |
| **Receipt** | A file the runner writes in the job directory: `intent.json`, `started.json`, `completion.json`. The daemon reads receipts to move a job forward. |
| **Workspace pin** | A `job_workspace_pins` row that stops the workspace a job reads from being released, reset or deleted until the job's cleanup is proved. |
| **Selection** | A recorded smart-test-selection proposal: four sets of modules (M, S, J, F) and a final set, kept in `test_selections`. |
| **Catalogue** | `tests/selection_catalogue.json`: every test module, its areas and the `src` modules it imports. Generated, never hand-edited. |
| **Required checks** | The hosted check names, at a named version, that the train policy requires on a candidate commit. |

## How it works today

### The big picture

```
 agent session (tmux)                       daemon
 ───────────────────                        ──────
 aq test tests/test_x.py ──► test slot ──► pytest (foreground, dies with caller)
         │
         └─ --aq-detach ─────────────┐
                                     ▼
 aq job submit ──► job_submit ──► jobs row (queued) + optional durable wait
                                     │
                    run_one_cycle ───┤ job_reconcile: admit, launch, reconcile
                                     ▼
                         python -m src.jobs.runner  (detached, same slots)
                                     │ receipts + output files
                                     ▼
                     result v1 ──► job_outbox ──► wait result or task message
                                     ▲
 integration train / publisher ──────┘ job_submit_integration (snapshot, band 0)
```

There are two ways to run a test. The **foreground path** is `aq test`. It
runs pytest in the caller's terminal after waiting for a slot. It is simple,
and it is what most agents use. The **managed path** is a job. The daemon
owns it, it outlives the caller, and it produces a durable result. Both paths
take the same box-wide slots, so they share one budget.

Smart test selection sits beside both. It can be asked for a plan
(`aq test --aq-smart --aq-plan-only`). In shadow mode it records a proposal
and then runs exactly what the caller named.

### `aq test`

The command lives in [src/cli/test_runner.py](../../../src/cli/test_runner.py).
The rules an agent must follow when using it are in
[AGENTS.md](../../../AGENTS.md) and
[resource-gating.md](../../guides/resource-gating.md). The mechanics:

1. **Refuse the obvious mistakes.** With no arguments it exits 2. It never runs
   the whole suite by default. A named path that does not exist exits 4. For
   agent-queue's own suite it requires `POSTGRES_TEST_DSN` and prints the
   setup steps (a disposable `postgres-test` container on port 5534, never the
   daemon's 5533). [tests/conftest.py](../../../tests/conftest.py) also refuses
   a DSN that names the production database.
2. **Compose the pytest command** (`_compose_pytest_argv`). It adds
   `-n <worker cap> --dist loadfile` unless the caller set `-n` or `--dist`,
   or xdist is off or missing. It adds the default deselect unless the caller
   passed `-m`. `--aq-all-markers` passes `-m ""` instead, which beats the
   `addopts` deselect.
3. **Decide whether it is a full suite** (`_is_full_suite`). The decision is
   static and made from argv alone. A run is full when it would select at least
   half of the test modules: `tests/`, no path at all with a broad `-k` or
   `-m`, or `--lf` with nothing recorded. `--collect-only`, a narrowing
   expression, or a real `--lf` list are slices.
4. **Wait for capacity.** A full suite first takes the full-suite lock, then
   one shared slot from the box lock. A slice takes only the shared slot. The
   wait is bounded by `--aq-timeout`, then `AQ_TEST_WAIT_TIMEOUT`, then
   `resources.test_wait_timeout` (1800 s). On timeout it exits 75
   (`EX_TEMPFAIL`). That is "the box is busy, retry", not a test failure.
5. **Run pytest.** The slot file descriptor is inheritable, so pytest and its
   xdist workers keep holding the lock even if the wrapper is killed. The
   child gets a fresh `AQ_TEST_RUN_ID`. SIGINT, SIGTERM and SIGHUP are
   forwarded. Exit 5 is reported as "no tests were collected" rather than as a
   pass.

Each slot file records who holds it: pid and start time, task, session,
`AQ_TEST_RUN_ID`, working directory, a hash of `AQ_INSTANCE_TOKEN`, and, for a
managed job, the job id and nonce
([src/resources/test_runs.py](../../../src/resources/test_runs.py)).
`aq test --aq-status` reads the slots directly and classifies each holder as
live, orphaned (its session root died) or unattributed. It also shows waiters,
managed jobs and the full-suite holder.
`aq test --aq-reap-orphans [--aq-apply]` kills orphaned holders. It is a dry run
unless `--aq-apply` is given. A holder that belongs to a live managed job is
never treated as an orphan. When a session stops, the sessions system kills
processes carrying its instance token, which covers most leaked pytest runs.

Other flags: `--aq-dry-run` prints the composed command. `--aq-project` picks
the project. `--aq-detach [--aq-wait]` hands the run to a managed job instead
(see [Managed jobs](#managed-jobs)). `--aq-help` lists them all; `-h` is
pytest's.

### Test slots, the gate and its owner

The slot files, the box-lock protocol and the per-session caps live in
[src/resources/](../../../src/resources/). The
[scheduling-and-claims](scheduling-and-claims.md#resource-gating) spec owns
them as the box-wide resource gate. This system is their main user. In short:

- `resources.test_slots` (default 2) slots, one `flock` file each. The kernel
  releases a slot when its holder dies, so a crashed run cannot leak one.
- Every session gets `PYTEST_XDIST_AUTO_NUM_WORKERS` set to its CPU share
  (`cores // max_concurrent_agents`, at least 1), plus thread caps and
  `AQ_TEST_SLOTS` / `AQ_TEST_WORKERS`
  ([limits.py](../../../src/resources/limits.py)). So `-n auto` inside a
  session already means "my share". `-n` with a bigger number bypasses it.
- The box lock is in its phase 1. Full suites still take *shared* admission
  plus the full-suite lock. Exclusive admission is used today by the managed
  e2e and matter presets, and by the test-database reaper.
- Nothing in the daemon enforces any of this for a bare `pytest`. The gate
  works because agents are told to use `aq test` and because the job runner
  takes the same locks.

### Test databases

Every pytest process builds its own databases, so concurrent runs from many
slots never share state ([tests/pg_dsn.py](../../../tests/pg_dsn.py),
[tests/db_fixtures.py](../../../tests/db_fixtures.py)).

- A schema template `aq_tmpl_<schema_key_slug>` is built once, under an
  advisory lock, and cloned with `CREATE DATABASE … TEMPLATE`.
- Each process takes an owner token and creates `aq_test_ownv2_<owner>_…`
  databases. A session-level advisory lock on the owner proves the run is
  alive.
- `pytest_unconfigure` drops what the run owns. A background sweep drops
  orphans whose owner lock is free, at most 8 per pass.
- For what that misses, the operator runs
  [scripts/reap_test_databases.py](../../../scripts/reap_test_databases.py)
  ([src/resources/test_db_reaper.py](../../../src/resources/test_db_reaper.py)).
  It keeps anything connected, young, still used by a checkout's template, or
  tied to a live run. It takes the box lock exclusively with a zero timeout,
  refuses `--apply` while any pytest is running, and drops without `FORCE`.

CI uses the same fixtures against a disposable PostgreSQL 18 service per job.

### Other projects' tests

`aq test` also runs other projects' suites
([src/resources/project_tests.py](../../../src/resources/project_tests.py)).
The interpreter is, in order: `resources.test_interpreters[<project>]` (a
missing path is an error, never a silent fallback); the daemon's own
interpreter for agent-queue; the project's `.venv` (found through the main
checkout, because worker slots have no venv of their own); then the daemon's
interpreter. It probes the chosen interpreter for xdist and runs serially if
it has none. Only agent-queue's suite requires PostgreSQL.

### Managed jobs

The implementation spec is
[managed-jobs.md](../implementation/managed-jobs.md). The code is
[src/jobs/](../../../src/jobs/), the commands
[src/commands/job_commands.py](../../../src/commands/job_commands.py), the
queries [job_queries.py](../../../src/database/queries/job_queries.py), and the
CLI [src/cli/jobs.py](../../../src/cli/jobs.py).

**Switched off by default.** `resources.jobs.enabled` defaults to false. The
flag gates new submissions and new launches only. Recovery, reconciliation,
cancellation and cleanup keep running when it is off, so turning the flag off
never strands a running job. Test and e2e jobs also need
`resources.jobs.test_database_url`, which must name a different database from
the daemon's.

**Submit.** `aq job submit` (alias `aq run`), or `aq test --aq-detach`, calls
`job_submit`. The command:

1. Maps the request to a preset and validates its arguments
   ([policy.py](../../../src/jobs/policy.py)). It refuses `--aq-*` flags,
   nonzero or malformed worker-count overrides, absolute paths and `..`.
   The `test` preset accepts `-n 0`, `-n0`, `--numprocesses 0` and
   `--numprocesses=0` for serial execution under the same slot admission.
   Otherwise pytest uses the configured worker cap. Worker-count options are
   omitted when xdist is missing or explicitly disabled; the default marker
   deselect still applies.
2. Checks quotas under a host-wide advisory lock: 100 queued jobs on the host,
   10 per owner, and the 2 GiB output budget.
3. Checks the owner. A task-owned job needs the task `IN_PROGRESS` at the
   submitting claim's epoch (`jobs.stale_claim`), and the workspace generation
   and lock must match (`jobs.workspace_busy`).
4. Inserts the row, pins the workspace and, with `--wait`, registers a durable
   job wait, all in one transaction.

Submission is idempotent. The CLI mints the idempotency key before it calls the
daemon, so a retry after a timeout returns the same job. The same key with a
different request is `jobs.idempotency_conflict`.

**Admit and launch.** `job_reconcile` runs at the top of every orchestrator
cycle ([src/orchestrator/core.py](../../../src/orchestrator/core.py), around
line 3401). `daemon-core` owns that ordering. Each pass:

- cancels jobs whose owning task is no longer in progress, and fails jobs that
  waited past their queue deadline (1800 s shared, 5400 s exclusive or full
  suite);
- launches at most one job, chosen by `next_admission`: priority band
  (0 integration, 1 elevated, 2 worker, aging one band per 600 s), then age,
  then id, against a weighted capacity of `resources.test_slots`, at most 2
  active jobs per owner, one full suite at a time, and exclusive jobs that
  drain shared work ahead of them;
- reconciles running jobs from their receipts.

A full-suite test job weighs `test_slots - full_suite_reserved_slots` (default
reserve 2, at least 1), so foreground `aq test` slices still get through while
it runs.

**Run.** The daemon starts the runner detached (`start_new_session`) in
`<data_dir>/runs/<job_id>/` ([runner.py](../../../src/jobs/runner.py)). The
runner takes the full-suite lock if needed, then the box lock with the job's
weight and mode, and starts the child with the session resource caps and none
of the daemon's credentials. It pumps output into a bounded store, enforces
the run deadline (7200 s by default), and writes `started.json` and
`completion.json` with fsync and rename. GPU jobs lock a per-device file
instead, one at a time.

**Reconcile and recover.** A job moves through
`queued → starting → running → succeeded | failed | cancelled | lost`, with
`cancelling` on the way to a cancel. Every move is a compare-and-set on
`state_version`. A terminal move writes the result and a `job_outbox` row in
the same transaction. Because the runner is detached, it survives a daemon
restart. On the next cycle `reconcile` adopts it by matching the receipt nonce
and checking boot id, pid and process start time. A job is never relaunched.
If there is any doubt (unreadable `/proc`, slots still held, descendants still
alive), the job keeps its workspace pin with `cleanup_blocked` set and is
retried next cycle. `aq doctor --check resources.jobs_cleanup [--fix]` reports
and clears these.

**Results.** [result.py](../../../src/jobs/result.py) builds result v1: state,
outcome, exit code or signal, an infrastructure reason, timings, output
retention, and a hash. For pytest, [pytest_output.py](../../../src/jobs/pytest_output.py)
streams the output (and JUnit XML when present) into a failures-first excerpt
of at most 8,192 bytes with up to 100 failing node ids. It also classifies
what went wrong. Exit 5 is a failure ("no tests collected"). Exits 3, 4, 75,
124, 126, 127, kill signals, and failures that all match infrastructure
signatures (database connection errors, ENOSPC, "no test slot free") are
**infrastructure**, not test failures. An agent can trust that distinction.

**Delivery.** The `job_outbox` row becomes either the result of a matching
durable wait (`aq job submit --wait`, `aq test --aq-detach --aq-wait`,
`aq wait register --kind job --ref <id>`) or a `job_result` message to the
owning task. `aq prime` shows recent job results. The wait deadline defaults to
the job's remaining queue and run budget plus 300 s. Waits belong to the
sessions system ([agent-waits.md](../../guides/agent-waits.md)).

**Output and retention.** `output.head` keeps the first 1 MiB and
`output.tail` is a 63 MiB ring, with a per-block hash manifest.
`aq job logs [--follow]` and `aq job attach` read them by cursor and show gaps
explicitly; the stream is `GET /api/jobs/{job_id}/output`. The hourly sweep
expires output after 14 days (sooner under budget pressure) and deletes rows
and directories after 90 days.

**Presets.** `test` (pytest under the project's interpreter, chosen by the same
rules as `aq test`), `lint` (`ruff check`), `e2e` (exclusive; see
[the e2e kit](#the-e2e-kit)), the Node presets `build`, `npm_ci`, `npm_test`,
`pnpm_install`, `pnpm_check` and `pnpm_build` when Node is installed, and
`matter_render`. [adapters.py](../../../src/jobs/adapters.py) maps command
strings such as `aq test …`, `pytest …` or `npm run build` to presets. That is
how the integration service turns a configured validation command into a job.

### Matter render jobs

`matter_render` is an exclusive, GPU-locked preset for the object loop of the
external matter-engine project ([object-loop.md](../../guides/object-loop.md)).
It runs a trusted capture adapter against one candidate bundle, with the
editor binary pinned per attempt under `<data_dir>/editor-pins/` so a rebuild
cannot change it mid-attempt ([editor_pin.py](../../../src/jobs/editor_pin.py)).
When the configured interpreter is a Windows `.exe`, a native owner process
runs the capture inside a Windows Job Object and holds a GPU mutex
([windows_owner.py](../../../src/jobs/windows_owner.py)). `aq job retain`
copies a validated capture into object-loop artifacts. The preset is refused
(`jobs.matter_unconfigured`) until `matter_python`, `matter_capture_script` and
`matter_editor` are set.

### The e2e kit

[e2e-swarm.md](../../guides/e2e-swarm.md) is the full guide. The kit runs a
real daemon against a real, disposable PostgreSQL database with no language
model. It builds an isolated home (`~/.agent-queue-e2e`), vault, throwaway
repository and tmux socket, and never touches `~/.agent-queue`.

- `scripts/e2e-env.sh --reset` builds the environment.
  `scripts/e2e-smoke.sh` runs twenty Tier 1 scenarios with a fake harness in
  about eight minutes. AGENTS.md requires it after any change to claims,
  pools, formulas, the task hierarchy or provider failover.
- Tier 2 runs real Claude sessions. `scripts/e2e-app-train.sh` proves the
  App-mode train against a disposable GitHub repository.
- The `e2e` job preset ([src/jobs/e2e.py](../../../src/jobs/e2e.py)) runs
  `e2e-env.sh --reset` then `e2e-smoke.sh` as one exclusive job.
- [tests/test_e2e_cli_stateful.py](../../../tests/test_e2e_cli_stateful.py) is a
  pytest form of the CLI scenarios. CI runs it in four groups.

### Smart test selection

[smart-test-selection.md](../../guides/smart-test-selection.md) is the guide.
The design is
[2026-09-25-smart-test-selection-design.md](../../superpowers/specs/2026-09-25-smart-test-selection-design.md).
The code is [src/test_selection/](../../../src/test_selection/).

**Inputs.** Three files are checked in and kept fresh by tests:

- `tests/selection_areas.yaml`: 96 areas, each a set of test-module globs.
  Every module must match an area and every area a module.
- `tests/selection_rules.yaml`: which changed paths invalidate everything,
  which are non-behavioural, which source paths own which areas, and which
  tests are critical.
- `tests/selection_catalogue.json`: generated by
  `scripts/generate-selection-catalogue.py` from each test module's AST, with
  a per-module digest. About 935 modules.

**A selection** (`SelectionService.select`) is computed in stages:

1. **Snapshot** the change from the merge base to the working tree, untracked
   files included. Anything it cannot read or bound makes it incomplete.
2. **M, the mandatory set.** Each changed path is mapped by the rules, in a
   fixed order, to the modules that must run. An unmapped path or an
   incomplete snapshot makes M the whole universe.
3. **S, static impact.** A pinned `pytest-impacted` engine plus a load-closure
   pass for `__init__.py` and conftest edges. Deleted or renamed files widen S
   to everything.
4. **J, judgments.** Optionally, one question per area to an external model
   (Jev, through the TypeSafe SDK), with strict token and time budgets.
5. **Compose.** Without a promoted policy, the fallback F = M ∪ S is what
   counts. In shadow mode the recorded proposal is M ∪ J. The result always
   satisfies M ⊆ final ⊆ universe.

The selection is stored in `test_selections` and a `test_selection.recorded.v1`
event is emitted.

**Modes.**

- `aq test --aq-smart --aq-plan-only` prints the plan, the proposed commands and
  the marker arms still owed, and exits 0 with "Nothing was verified". If the
  daemon is down it computes an unrecorded offline plan.
- **Shadow** (`--aq-smart`) records the proposal, runs exactly the targets the
  caller named, and records an execution observation. It never changes what
  runs.
- **Enforce** runs only the selected modules. It needs
  `test_selection.enforce_enabled` and a promoted project policy, refuses
  narrowing flags, and exits 4 when the plan says a full run is required.

Promotion and revocation are operator-only commands
(`aq test-selection promote|revoke`). The promotion bar in the design (at least
30 held-out replay cases, recall of at least 0.98, no critical misses) is
measured by `scripts/replay-test-selection.py`. It has not been met, and no
project is promoted. Phase 3 of the daemon cycle deletes selections older than
`test_selection.retention_days` (90), at most once an hour.

### CI in GitHub Actions

[docs/contributing/ci.md](../../contributing/ci.md) is the full reference.

[tests.yml](../../../.github/workflows/tests.yml) runs on pull requests into
`dev`, `staging` and `main`, and on pushes to the integration, batch, parent,
promotion and back-merge branches. It does **not** run on a push to `main`.
Its arms:

- `cli-conformance`, through `aq test`;
- `default-1/8` … `default-8/8`: plain `pytest tests/ -n 4` split by
  `pytest-split` using `.test_durations`;
- `migration-and-slow` and `postgres-integration`: the marker arms;
- `E2E CLI` in four groups (claims, cli, graphs, failover);
- `Dashboard (typecheck/build)`.

Sixteen of these names, at version `ci-4f7c710bba01`, are the required checks
in [docs/config/agent-queue-train-policy.json](../../config/agent-queue-train-policy.json).
`train-candidate.yml` turns a green run on an integration or batch branch into
the `aq-train/candidate` status. `main-attestation.yml` audits pushes to the
protected branches and re-runs the tests for an unattested commit.
`venv-cache.yml` keeps the venv cache warm. `macos-acceptance.yml` is manual
and not a merge gate. Each shard uploads its durations;
[scripts/merge_test_durations.py](../../../scripts/merge_test_durations.py)
merges them back into `.test_durations` when someone refreshes it.

CI does not use smart selection, the catalogue, `aq test` slots (except the
conformance arm), or the recorded baseline.

### How results leave this system

- **Workers** read a foreground run's output directly, or a job result through
  a wait. They record the commands they ran with `aq task close --test` and
  `--command`.
- **The recorded baseline.** A pre-existing failure never fails a task. Workers
  compare against the latest `notes/full-suite-baseline-<date>.md` vault note
  ([resource-gating.md](../../guides/resource-gating.md#test-scope-and-the-recorded-baseline)).
  [src/resources/test_baseline.py](../../../src/resources/test_baseline.py)
  can parse those notes, but nothing calls it yet.
- **Task completion** needs the required hosted checks green on the exact
  published commit ([task-completion-ci.md](../design/task-completion-ci.md)).
  That rule belongs to integration-and-delivery. This system supplies the
  workflows.
- **The integration service** runs local and hybrid train CI and the
  development publisher's validation as managed jobs
  (`job_submit_integration`, band 0, on a detached snapshot of the candidate).
  It goes through `PublisherJobs` in [adapters.py](../../../src/jobs/adapters.py)
  and `LocalCIProducer` in
  [src/integration/ci_producers.py](../../../src/integration/ci_producers.py).
  With jobs disabled, that producer reports `unavailable` / `jobs.disabled`.

## Data model

Jobs (tables in [src/database/tables.py](../../../src/database/tables.py),
around line 5840):

| Table | What one row is | Key facts |
|---|---|---|
| `jobs` | One job | Owner (`owner_kind` task or integration, `owner_id`, `claim_epoch`), preset and server-built `argv`, `contract` JSON, workspace and generation, input mode (live or snapshot) and fingerprint, class (shared or exclusive), weight, priority band, `state` and `state_version`, deadlines, runner identity (nonce, boot id, pid, start ticks), `result`, output retention, `cleanup_blocked`. Unique on (project, owner kind, owner, idempotency key). |
| `job_workspace_pins` | A job's hold on a workspace generation | Never expires. `workspaces.job_pin_count` mirrors it. Release, reuse, deletion and pause recovery all check it. |
| `job_outbox` | A terminal delivery intent | Key `job:<id>:terminal`. Consumed into a wait result or a task message. |

The job's files live in `<data_dir>/runs/<job_id>/` (request, intent, started,
completion, result, cancel, `instance.lock`, output head and tail, manifest,
JUnit). Snapshot clones are in `<data_dir>/job-snapshots/` and editor pins in
`<data_dir>/editor-pins/`. One id names the row, the `AQ_JOB_ID` environment
variable and the directory.

Selection (migration `a00000000028_test_selections`):

| Table | What one row is |
|---|---|
| `test_selections` | One immutable proposal: mode, snapshot fingerprint, the M/S/J/F/final sets, Jev status, fallback reason, marker policy. |
| `test_selection_observations` | What happened afterwards: kind `execution`, `ci` or `replay`. Cascades on delete. |
| `test_selection_promotions` | An operator promotion of a project's policy. At most one active per project. |

Neither `aq test` nor the slot files have a table. The slot files and their
JSON holder records are the only state, and the kernel is the authority on who
holds a slot.

Configuration ([src/config.py](../../../src/config.py)):

| Section | Keys | Defaults |
|---|---|---|
| `resources` | `test_slots`, `test_workers`, `test_wait_timeout`, `test_poll_interval`, `test_deselect_markers`, `test_interpreters`, `max_pytest_processes`, `cores`, `max_concurrent_agents`, `session_nice` | 2, CPU share, 1800 s, 2.0 s, the default deselect, {}, 24, CPU count, 8, 10 |
| `resources.jobs` | `enabled`, `test_database_url`, queue and run deadlines, quotas, `full_suite_reserved_slots`, log and result retention, `matter_*` | off, unset, 1800/5400/7200 s, 100/10/2, 2, 14/90 days, unset |
| `resources.cgroups` | `enabled`, `cpu_quota_percent`, `memory_max` | off, 600, 6G |
| `test_selection` | `enabled`, `jev_enabled`, `enforce_enabled`, model and budgets, `retention_days` | all off, `jev-1.13.0`, 90 |

All of them hot-reload. `aq agent tune` writes the `resources` values a fresh
install starts with ([default-tuning.md](../../guides/default-tuning.md)). As
of this writing the operator's box runs with jobs enabled, 6 slots, 6 workers,
and selection enabled in shadow with Jev and enforcement off.

## Boundaries

| Neighbour | What it owns | Where it meets this system |
|---|---|---|
| `daemon-core` | `run_one_cycle` ordering | Runs `job_reconcile` at the top of each cycle and the selection retention sweep in phase 3 (housekeeping). This system owns what those passes do, not when they run. |
| `scheduling-and-claims` | The box-wide resource gate: slot files, box lock, per-session caps, cgroups | `aq test` and the job runner are the gate's main clients. This system owns `aq test`'s use of it, full-suite detection and job weights. |
| `task-graph` | Task status | A task-owned job requires its task in progress at the submitting claim's epoch. A job is cancelled when its task leaves that state. |
| `sessions-and-harnesses` | Durable waits, `aq prime`, the session-stop process sweep | Job waits and job results in prime. The instance-token sweep kills leaked test processes. Job runners are deliberately not sessions. |
| `projects-workspaces-git` | Workspaces, generations, worktree release and reset | Jobs pin a workspace generation. `@guard_workspace` serializes worktree mutations against pins. |
| `integration-and-delivery` | Trains, the development publisher, required checks, completion CI, the CI main sentinel | Submits integration jobs as local CI. Reads the required check names the workflows produce. Owns `aq ci completed-status`, `aq ci baseline-status` and the `ci-main-sentinel` playbook. |
| `object-loop` (in integration-and-delivery) | The matter-engine object loop | Consumes `matter_render` jobs and `aq job retain`. |
| `operations-and-recovery` | `aq doctor`, restart | Doctor checks `resources.load`, `resources.test_pressure` and `resources.jobs_cleanup`. A restart leaves runners running; the next cycle adopts them. |
| `configuration-and-vault` | Config loading, the tuner, the vault | Owns the `resources`, `resources.jobs` and `test_selection` keys and the baseline note in the vault. |
| `api-and-clients`, `dashboard` | REST, SSE, generated clients | The job and test-selection commands are on the API. Job output streams at `GET /api/jobs/{job_id}/output`. `job_reconcile` is excluded from public transports. |
| `playbooks` | Policy | No playbook drives tests or jobs today. Judgment about failing tests lives in repair tasks and the CI sentinel playbook, not here. |

## Design principles and invariants

1. **One budget for the box.** Foreground runs, managed jobs and the
   test-database reaper take the same slot files under the same protocol. No
   path runs tests outside the gate except a bare `pytest`, which agents are
   told never to use past one file.
2. **The kernel is the authority on slots.** A slot is a held `flock`, not a
   row or a JSON record. A dead process frees its slot with no cleanup code.
   Holder records are explanations, re-checked against the lock before anyone
   acts on them.
3. **Busy is not failure.** Exit 75 and the infrastructure classification
   exist so that a full box, a missing database or a killed run is never
   reported as a red test. Exit 5 (nothing collected) is never a pass.
4. **Never run everything by accident.** `aq test` with no arguments refuses.
   One full suite at a time. The slow markers stay off unless asked for.
5. **No shell, finite presets.** A job runs an argv the server built from a
   named preset and validated arguments. There is no arbitrary-command
   endpoint.
6. **One identity per job.** The row, `AQ_JOB_ID` and the run directory share
   one id. Every state change is a compare-and-set, and the terminal result and
   its delivery intent commit together.
7. **Never relaunch, never guess.** A job that may still be running is adopted
   or proved dead, never started twice. Doubt keeps the workspace pinned
   (`cleanup_blocked`) rather than releasing it.
8. **The flag gates new work only.** Disabling jobs stops submissions and
   launches. It never stops recovery, cancellation or cleanup.
9. **Selection may only add until promoted.** Shadow never changes what runs.
   M ⊆ final ⊆ universe always holds. Anything incomplete falls back to more
   tests, never fewer. Enforcement never replaces CI, the marker arms or a
   task's acceptance commands.
10. **Generated inputs are regenerated, not edited.** The catalogue is rebuilt
    by its script and checked by a test. On a merge conflict it is regenerated
    ([codegen.md](../../contributing/codegen.md)).
11. **Test databases are owned, never shared.** Each run creates and drops its
    own, guarded by an advisory lock, and the production database is refused.

## Known gaps

### Where the docs and the code disagree

- **What shadow mode runs.** The selection guide says shadow "then runs exactly
  the modules it proposed", and its flag table says "compute, record and run
  the proposed selection". The code runs the targets the caller named and
  never changes `pytest_args` (`src/test_selection/policy.py` docstring, the
  `aq test --aq-help` text, `src/cli/test_runner.py` around line 1214). The
  code is the intended behaviour. The guide should be corrected.
- **Who reads the slot-wait report.** The resource-gating guide (around line
  365) says the development publisher sets `AQ_TEST_SLOT_REPORT` and
  `AQ_TEST_WAIT_TIMEOUT` to keep queueing out of its run budget. The publisher
  now validates through managed jobs, whose queue and run deadlines are
  separate. `aq test` still writes the report, but nothing in production reads
  it (`read_slot_wait` is used only by tests).
- **The system inventory** says CI "consumes the same selection rules and
  baselines". It does not. CI's only "selection" is `pytest-split` timing
  shards. It also lists "workspace jobs". There is no workspace preset;
  `src/jobs/workspace.py` is the pin guard. Its plan citations are also
  inexact: the selection plan is `plans/2026-09-25-smart-test-selection-plan.md`,
  and the bounded-checks plan is about delivery-decision latency, not tests
  (see [Planned work](#planned-work)).
- **managed-jobs.md** still opens "Phases 2 and 3 leave
  `resources.jobs.enabled: false`". That is the code default, but the
  integration service's local CI now depends on jobs being on. The spec does
  not say which phase turns the default on, or whether one will.

### Stale code and comments

- [src/resources/test_baseline.py](../../../src/resources/test_baseline.py)
  parses baseline notes and annotates known failures, but nothing in `src/` or
  `scripts/` calls it. A job result does not yet say which failures were
  already known.
- `DevelopmentPrimitives.job_client`
  ([src/integration/development.py](../../../src/integration/development.py),
  around line 397) is assigned in two places and never read there. The
  publisher's jobs go through the train sources' `LocalCIProducer`.
- The `jobs.measurements` and `jobs.retry_of` columns exist, but the job code
  never sets them.
- The `ci` and `replay` observation kinds exist in the schema. Only
  `execution` is written. Execution observations carry no failing node ids, and
  module ordering ignores durations.

### Behaviour gaps

- **Jobs are off by default** and need a disposable test database URL. On an
  install where they are off, local and hybrid train CI report
  `jobs.disabled` and cannot produce checks.
- **One launch per cycle.** `next_admission` launches at most one job per
  orchestrator cycle, so a burst of submissions starts at the cycle rate even
  when slots are free.
- **Full-suite detection is static.** It judges from argv and the module count.
  It cannot see what a conftest or plugin selects, and a run just under half
  the modules is a slice.
- **Full suites still take shared admission** (box lock phase 1). Exclusive
  admission for full suites is designed but not switched on.
- **No gate for a bare `pytest`.** The gate holds only for callers that use
  `aq test` or jobs.
- **cgroups are opt-in** and need systemd user delegation. Without them, CPU
  and memory limits are `nice` and xdist caps only.
- **Selection is shadow only.** All three switches default off, the promotion
  bar is unmet, runtime savings have never been measured, and the static
  engine and Jev SDK are optional extras whose absence silently forces the
  fallback.
- **The catalogue freshness check is skipped in CI** (`CI` set). Drift is caught
  there only by `tests/test_selection_catalogue.py`.
- **CI does not test `main` on push.** Only `main-attestation.yml` runs, and it
  does nothing until its repository variables are set.
- **Shard timings drift.** `.test_durations` was last refreshed on 2026-09-25.
  Nothing refreshes it automatically.
- **Wall-clock budgets are never enforced in CI.** `perf_strict` tests skip
  unless `AQ_PERF_STRICT=1`, and CI sets it only for the CLI startup check.
- **The recorded baseline is old.** The latest note is
  `full-suite-baseline-2026-09-22.md`. Nothing schedules a new one.
- **The test-database reaper is manual.** It has no timer.
- **Process identity is local.** Runner adoption depends on this host's boot
  id and `/proc`. Running jobs on another machine would need node-qualified
  identities (see the distributed-worker-nodes proposal below).

## Planned work

This is a snapshot taken on 2026-10-20. Open tasks were re-queried with
`aq task show` before writing. A worker token cannot list all open tasks, so
the open-task list starts from the system inventory's epic column. Vault
documents are under `projects/agent-queue/` in the operator vault and are cited
by file name, with the review id and state at the time of writing.

### Open epics and tasks

**`stark-willow-95`, fast and smooth integration** (IN_PROGRESS) is governed by
`specs/2026-10-12-fast-smooth-integration.md` (approved, revision 2). Two of its
tasks consume job results:

| Task | State | What it changes here |
|---|---|---|
| `.8` Group red batch failures deterministically with build precedence | DEFINED, blocked by `.7` | Pure functions over check annotations and job logs that group a red batch's failures, with build failures taking precedence (spec §7.1). It reads the failures-first excerpt and the infrastructure classification this system produces. It should not change the result format. Acceptance runs `aq test tests/test_integration_failure_groups…`. |
| `.16` Opt-in speculative next-batch CI on hosted runners | DEFINED, blocked by `.7` and `.9`, blocks `.18` | Adds `train.speculative_ci` (default off), which starts hosted CI for the next batch before the current one lands (spec §8). It is refused for local and hybrid root CI, so it adds no managed-job load. It changes how often the `tests.yml` arms run. |

No other open epic in the inventory targets this system.

**`eager-vault-55`** (this spec's parent) writes the per-system specs and then a
rewrite of the systems README. It changes no code.

### In-flight and recent specs

| Document | Review and state | What it means for this system |
|---|---|---|
| `specs/2026-09-24-exclusive-job-queue.md` | Final ("ready for implementation planning"). The duplicate `-2` copy is withdrawn (`rev-prime-dune`). | The design behind managed jobs: bands, weights, exclusive drain. Implemented through phase 4 in [managed-jobs.md](../implementation/managed-jobs.md). |
| `specs/2026-09-24-managed-long-running-commands.md` | Final. The `-2` copy is withdrawn (`rev-amber-crest`). | Detached runner, receipts, durable waits, output retention. Implemented. |
| `specs/2026-09-24-smart-test-selection-2.md` | `rev-sharp-crest`, revision 3, status implemented. Revision 1 (`…-selection.md`) is final. The repository copy is the design spec linked above. | The selection pipeline described here. Enforcement waits on the promotion bar. |
| `plans/2026-09-25-smart-test-selection-plan.md` | `rev-smart-beacon`, approved | The implementation plan. Delivered except promotion, which needs replay evidence that does not exist yet. |
| `specs/2026-09-24-resource-aware-planner-2.md` | `rev-grand-cascade`, in review (revision 1), backlog | Adaptive job admission (`resources.jobs.adaptive`, off by default) on top of the static slots, from measured per-command history. The exclusive job queue is its hard prerequisite, and that is now met. Not started. |
| `specs/2026-10-06-distributed-worker-nodes.md` | `rev-eager-quest`, in review (revision 2) | Moves job runners and sessions behind an execution-worker service, even on one machine. Jobs would get node-qualified identities, and test slots would become per node. It names `JobService.launch` and the runner as the boundary to factor. Not started. |
| `plans/2026-10-07-bounded-aq-checks-measured-audit-and-phased-implementation.md` | `rev-agile-orbit`, approved | **Adjacent, not test work.** Its "routine checks" are bounded delivery and scheduling decisions. It explicitly excludes executable validation and remote CI. Nothing in it changes this system. |
| `specs/test-coverage-uplift.md` | Draft, 2026-08-30 | Staged coverage baselines with `pytest-cov`. `pytest-cov` is now in the dev extras, but nothing in CI measures coverage. Not started. |
| [2026-09-05-ci-main-sentinel-design.md](../../superpowers/specs/2026-09-05-ci-main-sentinel-design.md) and [2026-09-16-ci-sentinel-repair-coverage-design.md](../../superpowers/specs/2026-09-16-ci-sentinel-repair-coverage-design.md) | Historical, implemented | The `ci-main-sentinel` playbook and `aq ci baseline-status`. They are integration's, but they are the only thing that watches `main`'s test health. |
| [2026-09-01-dashboard-vitest-flakiness.md](../../superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md) | Historical, fixed | Why the dashboard tests are run the way they are. See `dashboard/AGENTS.md`. |

## Source and tests

| Area | Source | Focused tests |
|---|---|---|
| `aq test` | [src/cli/test_runner.py](../../../src/cli/test_runner.py) | `aq test tests/test_cli_test_runner.py` |
| Slots, holders, reaping, slot report | [src/resources/semaphore.py](../../../src/resources/semaphore.py), [box_lock.py](../../../src/resources/box_lock.py), [test_runs.py](../../../src/resources/test_runs.py), [slot_report.py](../../../src/resources/slot_report.py) | `aq test tests/test_resource_semaphore.py tests/test_resource_test_runs.py tests/test_slot_report.py` |
| Session caps, cgroups, load | [limits.py](../../../src/resources/limits.py), [procs.py](../../../src/resources/procs.py) | `aq test tests/test_resource_limits.py tests/test_resource_doctor.py` |
| Other projects' interpreters | [project_tests.py](../../../src/resources/project_tests.py) | `aq test tests/ -k project_tests` |
| Test databases | [tests/pg_dsn.py](../../../tests/pg_dsn.py), [tests/db_fixtures.py](../../../tests/db_fixtures.py), [test_db_reaper.py](../../../src/resources/test_db_reaper.py) | `aq test tests/test_pg_dsn.py tests/test_test_db_reaper.py` |
| Baseline parser | [test_baseline.py](../../../src/resources/test_baseline.py) | `aq test tests/test_test_baseline.py` |
| Managed jobs | [src/jobs/](../../../src/jobs/), [job_commands.py](../../../src/commands/job_commands.py), [job_queries.py](../../../src/database/queries/job_queries.py), [src/cli/jobs.py](../../../src/cli/jobs.py) | `aq test tests/test_jobs.py tests/test_jobs_queries.py tests/test_jobs_runner.py tests/test_jobs_recovery.py tests/test_jobs_waits.py tests/test_jobs_output.py tests/test_jobs_attachments.py tests/test_jobs_matter.py` |
| Smart test selection | [src/test_selection/](../../../src/test_selection/), [test_selection_commands.py](../../../src/commands/test_selection_commands.py) | `aq test tests/test_selection_*.py` |
| Catalogue freshness | [scripts/generate-selection-catalogue.py](../../../scripts/generate-selection-catalogue.py) | `aq test tests/test_selection_catalogue.py` |
| e2e kit | `scripts/e2e-*.sh`, [scripts/e2e/](../../../scripts/e2e/), [src/jobs/e2e.py](../../../src/jobs/e2e.py) | `aq test tests/test_e2e_kit_fixtures.py tests/test_e2e_probe.py`, then `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` |
| CI workflows | [.github/workflows/](../../../.github/workflows/), [merge_test_durations.py](../../../scripts/merge_test_durations.py) | `aq test tests/test_ci_trigger_policy.py` |

After adding or moving a test module, run
`python scripts/generate-selection-catalogue.py` and commit the catalogue.

Related reading, in order: [testing.md](../../contributing/testing.md) for how to
write and run tests,
[resource-gating.md](../../guides/resource-gating.md) for the box and the
baseline, [managed-jobs.md](../implementation/managed-jobs.md) for the job
substrate, [smart-test-selection.md](../../guides/smart-test-selection.md) for
selection, [e2e-swarm.md](../../guides/e2e-swarm.md) for the kit, and
[ci.md](../../contributing/ci.md) for the workflows.
