---
tags: [spec, system, operations-and-recovery]
system: 17
status: current
as_of: 2026-10-22 (dev 52d2efe4c)
---

# System 17: operations-and-recovery

This is a **current** system spec, not a design record. It describes what the
code at `dev` 52d2efe4c does. Where it and the code disagree, the code is
right and this file should be fixed. Older specs it links to, such as
[`../setup-wizard.md`](../setup-wizard.md), [`../packaging.md`](../packaging.md)
and [`../design/fleet-metrics.md`](../design/fleet-metrics.md), are kept for
the reasoning behind the design, not as a description of what runs today.

operations-and-recovery is system 17 of the 18 in the system inventory
(`~/.agent-queue/vault/projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, *approved*, revision 1). The vision and system map
that first linked here (`2026-10-20-agent-queue-the-vision-and-the-system-map.md`,
review `rev-smart-nexus`) is *withdrawn*. Sibling specs live in the same
directory as `<system-slug>.md`.

The operator's day-to-day manual is
[`../../guides/operations.md`](../../guides/operations.md). This spec does not
repeat its runbooks. It explains how the machinery behind them is built, where
it lives and what it promises.

## 1. Purpose and problem

Agent Queue is a long-running daemon. It holds dozens of live agent sessions,
a PostgreSQL database and many git worktrees on one machine, which usually
belongs to a single operator. That machine gets rebooted, the code changes
several times a day, migrations land, disks fill and processes die. Each of
those events can lose work that only exists in a worktree or a tmux pane. It
can also leave rows that claim something is running when it is not.

operations-and-recovery is everything that gets AQ onto a machine, keeps it
running, changes its code safely, and gets it back to a correct state when
something breaks. Concretely:

- **Install**: the `aq install` engine and wizard. It provisions PostgreSQL
  and writes config, then starts the daemon and the dashboard.
- **Run**: `aq start`, `aq stop` and `aq restart`, the pid file and locks, and
  a watchdog unit that brings the daemon back after a crash or a reboot.
- **Change**: `aq update`. It moves the checkout to a pinned tag or a tracked
  branch, backs up before migrations, restarts without killing agents, and
  rolls back or holds for recovery when something goes wrong. Every deploy is
  recorded in `deploy.json` and `deploy-history.json`.
- **Protect data**: `aq db backup` and `aq db restore`, plus the migration
  guard that stops anyone but the daemon or an operator from migrating.
- **Observe**: `aq doctor` (125 checks, 31 with a fix), fleet metrics,
  benchmark exports, structured logs and `aq logs`.
- **Recover**: stranded unpushed work, orphaned playbook runs, released
  integration owners, and corrupted retained git object stores.

The problem it solves is that an orchestrator for unattended agents must be
safe to leave alone. An update must not kill the agent that requested it, a
crash must not strand commits, and a repair must not rewrite delivery history.
The operator must also be able to tell which layer is broken: task, project,
pool or daemon.

## 2. How it works today

### 2.1 Installer and setup wizard

Code: `src/install/`, CLI `src/cli/install.py`, bootstrap `scripts/install.sh`.
Tutorial: [`../../tutorials/install.md`](../../tutorials/install.md). Reference:
[`../../reference/cli/install.md`](../../reference/cli/install.md). Testing:
[`../../contributing/installer-testing.md`](../../contributing/installer-testing.md).
The old interactive `aq setup` wizard (`src/setup_wizard.py`) is deleted; its
spec [`../setup-wizard.md`](../setup-wizard.md) is retired.

**Bootstrap.**
- `scripts/install.sh` clones AQ into `~/.local/share/agent-queue`, builds a
  virtualenv, links `aq` into `~/.local/bin`, then runs `aq install`.
- `install-wsl.sh` is a shim for it.
- `install-windows.ps1` prepares an Ubuntu-24.04 WSL distribution and hands
  over.

**Engine.** The engine (`engine.py`) is synchronous, because it runs before any
event loop exists. Each step is a `StepSpec` (`steps.py:80-137`) with these
fields:

- an id and `depends_on`;
- an optional capability;
- the flags `mutating`, `halts` and `advisory`;
- a consent prompt;
- a `verify` function.

`build_registry` (`registry.py:102-269`) fixes the order:

1. `host.supported`.
2. The platform adapter: the macOS steps (`macos.py`) or `wsl.packages`.
3. Prerequisites: Python 3.12 or later, git, tmux, the data directory.
4. PostgreSQL: `postgres.package`, `service`, `server`, `role`, `database`,
   `rotate`, `credentials`, `connection`, `boot` (§2.2).
5. Per provider: `provider.<id>-cli`, then `provider.<id>-login`. The login
   step does not halt the run.
6. Onboarding (`onboarding.py:1435-1478`): `config.defaults`, `project-root`,
   `git-identity` (advisory), `check`, `discord`, `daemon.start`,
   `daemon.autostart`, `dashboard.build`, `serve`, `daemon.dashboard`,
   `dashboard.open`.
7. Advisory steps that install graft: `graft.cli` and `graft.repos`.

Optional steps are gated by capabilities: `graft`, `discord`, `daemon`,
`postgres-managed`, `postgres-rotate`, `autostart` and `provider.<id>`.

**Running the steps** (`engine.py:191-371`).
- A run stops at the first halting step that is not satisfied.
- Progress is saved to `~/.agent-queue/install-state.json` after every step.
  That file is schema v1, mode 0600, replaced atomically, and redacted: it
  never holds a secret (`state.py`, `redaction.py`). A secret that would leak
  raises `SecretLeakError`.
- A state file from another version is refused unless the run is `--repair`
  or `--upgrade`.
- `ResourceRecord.owned` only ever goes from false to true. It decides what
  `aq uninstall` may remove.
- Exit codes (`results.py:38-62`): 0 ready, 10 needs user, 11 invalid input,
  12 unsupported host, 20 failed.

**Wizard.** `wizard.py` holds pure functions. They ask "Use Claude/Codex/
Gemini?" and whether to install graft. The PostgreSQL, daemon, autostart and
Discord questions appear only with `--advanced`. The CLI shows an "AQ will:"
plan and asks "Go ahead?" (`src/cli/install.py:686-743`).

**CLI flags** (`src/cli/install.py:557-623`): `--interactive`, `--dry-run`,
`--non-interactive`, `--json`, `--config`, `--with`, `--approve`, `--yes`,
`--resume`, `--fresh`, `--no-resume`, `--state-file`, `--repair`, `--upgrade`,
`--restart-from`, `--list-steps`, `--advanced`,
`--git-name`, `--git-email`. After a run, the CLI probes provider logins,
refreshes the profile catalogue and ensures the default worker pools
(`src/install/pools.py`).

**What onboarding writes.**
- `config.defaults` writes `config.yaml` with the default tuning, the same as
  `aq system config tune --apply` (see
  [`../../guides/default-tuning.md`](../../guides/default-tuning.md)), and
  `sessions.provider: tmux`.
- `.env` is mode 0600 and holds `AQ_DB_PASSWORD`. The installer never stores a
  Discord token. When Playbooks V2 is explicitly enabled and has no private-value
  key configuration, it also provisions a new 32-byte private-value key in
  `.env` and records its identifier/environment-variable mapping in config.
  It never replaces configured keys or repairs missing historical key material;
  `config.check` and `playbooks.private_value_key` report that condition.
- `daemon.start` runs `aq start --no-dashboard` with a 180 s limit and checks
  health at `127.0.0.1:8081`.

**Lifecycle** (`src/install/lifecycle.py`) covers repair, upgrade and
uninstall. `aq uninstall` removes the runtime by default. `--remove-config`,
`--remove-data`, `--remove-database` and `--all` widen the scope, and
`--dry-run` and `--yes` are also accepted
([`../../reference/cli/uninstall.md`](../../reference/cli/uninstall.md)). It
never removes provider CLIs, OS packages, the PostgreSQL server or
credentials; it reports them instead.

The install and onboarding epic `noble-apex` and its plan
([`../../plans/install-onboarding/README.md`](../../plans/install-onboarding/README.md))
produced this design.

### 2.2 PostgreSQL provisioning

Code: `src/install/postgres.py`, `src/install/postgres_steps.py`.

- **Native packages, not Docker.** On macOS it installs Homebrew
  `postgresql@17`; on WSL and Ubuntu it uses apt. The minimum server version
  is 14, and a server that is already running is reused.
- **Defaults:** `localhost:5432`, with role and database `agent_queue`. The
  DSN written to config is
  `postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue`.
- **Admin access** is `sudo -n -u postgres psql`. If that needs a password,
  the step returns needs-user instead of prompting.
- **Passwords.** The installer never resets an existing role's password.
  Rotation is opt-in (`postgres-rotate`). It uses `secrets.token_urlsafe(24)`
  and writes the result to `.env`.
- **No Alembic.** The installer creates the empty database; the daemon
  migrates it on its first start (§2.6).

`docker-compose.yml` exists for development only. It runs `aq-postgres` on
5533 and `aq-postgres-test` on 5534.

### 2.3 Daemon lifecycle: start, stop, restart

Code: `src/cli/daemon.py`; the daemon side is `src/main.py`,
`src/daemon_state.py` and the shutdown path owned by
[daemon-core](daemon-core.md) §2.10.

**Files** under the data directory:
- `daemon.pid`. Staleness is detected from `/proc/<pid>/cmdline`.
- The `daemon.lock` directory, which records the owner pid and boot id.
- The `daemon.stopped` marker, written by `aq stop`.
- `daemon.log`, rotated at 256 MiB, keeping 3 gzipped copies.

**`aq start`** (`daemon.py:629-788`):
1. Clear the stopped marker. Config must exist.
2. Take the lock and check the database. If the database is down, start the
   development container only when the effective configuration's host, port
   and database match the compose `postgres` service's published endpoint.
   Unknown or unresolved compose definitions require manual startup. Otherwise
   report that no PostgreSQL server is answering at the configured endpoint.
3. If the schema is behind head, take a `pre-deploy-<UTC>.dump` backup first.
   A failed backup exits with 12 to 15 and does not start the daemon.
4. Check the marker again, then spawn the daemon.
5. Wait up to 120 s for `/health` and 60 s for `/ready`.
6. Run `aq doctor --check pools.stale_worktree_checkouts --fix` with a 120 s
   limit. A failure here is only a warning.

`--unless-stopped` refuses to start when the marker is present; the watchdog
uses this flag.

**`aq stop`** writes the marker, sends SIGTERM, waits 10 s, then sends SIGKILL.
It also kills the agent tmux sessions (`s-`, `n-` and `p-` names on AQ's
socket) unless `--keep-sessions` is passed.

**`aq restart`** (`daemon.py:1245-1274`) stops the dashboard server, stops the
daemon without killing sessions, sleeps 1 s and starts it. It never kills
sessions, because `sessions.adopt_on_start` defaults to true
(`src/config.py:1776`) and the new daemon re-adopts every live pane
(`src/sessions/reconciler.py:370-433`). `aq restart` does not pull code; that
is `aq update` (§2.5).

All three commands refuse a worker session with exit 10.

**In-process restart.** The `restart_daemon` command
(`src/commands/system_commands.py:858-883`) sets `_restart_requested` and
sends SIGTERM to the daemon itself. `main()` waits 15 s, then calls
`os.execv` (`src/main.py:762-773`). Graceful shutdown waits at most 300 s,
then calls `os._exit(0)`.

The postconditions an operator should verify after a start or restart are in
[the operations guide](../../guides/operations.md#daemon-updates).

### 2.4 Service and watchdog

Code: `src/install/service.py`, `src/install/watchdog.py`; CLI
`aq service install|uninstall|status|check [--boot] [--reset]|run`
(`src/cli/service.py`).

Autostart registers a **watchdog**, never the daemon itself. The unit runs
`python -P -m src.install.watchdog run` (or `check`). There are three
mechanisms:

- a systemd user unit `aq-watchdog.service` with `Restart=on-failure` and
  linger enabled;
- a launchd agent `com.agent-queue.watchdog.plist`;
- on WSL, cron: `@reboot … check --boot` plus a check every 120 s.

Exactly one mechanism is installed at a time. It is recorded in
`~/.agent-queue/service/install.json`. The install step is `daemon.autostart`,
and doctor reports its state as `daemon.autostart`.

**Policy**, checked every 30 s:

1. A running daemon resets the failure counters.
2. The `daemon.stopped` marker means the operator wants it stopped, so stay
   stopped.
3. A held `daemon.lock`, or an `update.lock` younger than 2 hours, means an
   update or start is in progress, so wait.
4. The daemon must be seen down twice in a row. A boot check skips this.
5. Wait up to 180 s for the database's TCP port.
6. Back off from 60 s to 1800 s, with at most 5 starts per hour. Give up after
   5 failures while the database is reachable.

Each start is `aq start --no-dashboard --unless-stopped`. Exit 16 means a stop
arrived during the start. The watchdog exits 75 when its own source changes,
so the service manager restarts it on new code. Its state is
`service/state.json` and its log is `logs/aq-service.log`, capped at 1 MiB.
It refuses to run inside a worker session.

### 2.5 Update and deploy

Code: `src/cli/update.py`, `src/install/update.py`, `src/install/deploy.py`,
`src/install/deploy_history.py`, `src/install/update_finish.py`. Guides:
[`../../guides/releases.md`](../../guides/releases.md) and
[`../../guides/operations.md#daemon-updates`](../../guides/operations.md#daemon-updates).
Branch tracking design:
[`../design/branch-tracking-deployment.md`](../design/branch-tracking-deployment.md).

**Selection.** `deploy` in operator config chooses one of two modes:

- **Tag deploy** (`deploy.tag_glob`, `deploy.target`). `DeploySelector.resolve`
  reads the remote with `ls-remote`, filters by the glob (default `v*`) and
  picks the highest semantic version. It accepts annotated tags only, and the
  tag must be an ancestor of `origin/<target>` (otherwise
  `deploy_not_related`). `aq update --ref <tag>` pins one tag.
- **Branch tracking** (`deploy.track_branch`). Fast-forward only.

Both modes use `validate_target_migrations` to check that the pinned target has
one Alembic head, reading revisions with `git show` without executing them.

`track_branch` cannot be combined with a ref, glob or recorded tag selection.

**Plan.** The update refuses:

- a dirty tree, local commits, or a detached HEAD with no deploy record;
- a sideways move;
- a backward move without `--allow-rollback`;
- a rollback that crosses a breaking migration
  (`rollback_crosses_breaking_migration`).

The tag annotation's `AQ-Migration-Admission:` lines feed that decision.

It also refuses in a worker slot, under `AQ_DB_SCOPE=worker`, and on a package
install (exit 10).

**Apply.**

1. Create `update.lock` with `O_EXCL` and re-check the plan.
2. If the target adds migrations, take a backup. A failed backup refuses the
   update before anything is stopped.
3. Stop the dashboard server, then run `aq stop --keep-sessions`.
4. Check out, reset or fast-forward the checkout, and write `deploy.json`.
5. Run `update_finish` from the **new** code. It rebuilds the dashboard, runs
   `aq start --no-dashboard`, polls `/health` and `/ready` up to 30 times,
   and checks the dashboard. The start migrates the schema (§2.6).

**Failure** (`_recover`).
- If migrations ran and the new daemon started, the update is marked `failed`.
  It writes a `deploy-recovery-required` hold instead of rolling back,
  because the old code cannot run against the new schema.
- Otherwise it resets to the previous commit, reinstalls, rebuilds and starts
  the old daemon, and records `rolled_back`.

After an operator has fixed a hold,
`aq update --yes --resume-after-recovery` verifies health, readiness and the
dashboard, then records `recovered`.

**Records.**
- `deploy.json`: the selection, the previous commit, the tag, `installed_at`
  and the kind. Written atomically.
- `deploy-history.json`: capped at 100 entries and guarded with `flock`. The
  dashboard's System Config page shows it.

CLI exit codes are 0, 10 and 20. Flags: `--check`, `--yes`, `--ref`,
`--allow-rollback`, `--no-backup`, `--resume-after-recovery`, and the hidden
`--automatic`.

**Automatic deploys.** `DeployConfig` (`src/config.py:3853-3883`) has these
defaults: `tag_glob=None`, `target="main"`, `track_branch=None`, and
`poll_interval_seconds=300`, with a minimum of 60.

`_schedule_tracked_deploy_check` is the first call in `run_one_cycle`
(`src/orchestrator/core.py:3395`). At most one check runs at a time. Checks
are throttled to the poll interval, and the first one runs one interval after
the daemon starts. The check calls `update_and_restart` with `automatic=True`
(`system_commands.py:963-1038`). That call does nothing when:

- a recovery hold is set;
- the update lock is held;
- the plan is refused;
- the target already failed once;
- there is nothing to update.

Otherwise it spawns a detached `python -m src.cli.app update --yes
--automatic` in a new session with a scrubbed environment, logging to
`update.log`. The updater stops the daemon that spawned it, so it must not be
the daemon's child.

When neither a glob nor a tracked branch is configured, `update_and_restart`
refuses with configuration guidance. The operator may still invoke standalone
`aq update` for a guarded branch-following update. All updater backups delegate
to `src/database/backup.py`, including Docker fallback and archive inspection
before any service stops.

**Releases** ([`../../contributing/releases.md`](../../contributing/releases.md)).
`aq promote prepare|request|approve` builds a notes digest and runs the
additive-migration check. `PromotionPublisher` creates the annotated tag that
tag deploys consume. No GitHub Release is created. The version is 0.1.1
(`pyproject.toml:7`).

### 2.6 Migrations at deploy time

Code: `src/database/migration_guard.py`, `src/database/engine.py`. Guides:
[`../../guides/migrations.md`](../../guides/migrations.md),
[`../../reference/database/migrations.md`](../../reference/database/migrations.md).
The guard itself is daemon-core's ([daemon-core](daemon-core.md) §2.7); this
system owns when it runs during operations.

- Under the DAEMON scope, `run_schema_setup` **migrates** to head. Under
  every other scope it only **verifies**, and a schema that is behind raises
  "schema behind code; ask the operator to upgrade".
- `SchemaAheadPolicy` (config `database.schema_ahead_migrations`) lets a
  daemon run against a schema that a newer checkout migrated additively. This
  is how a rollback across additive revisions stays possible.
- `scripts/check_additive_migrations.py` decides, without running them,
  whether pending revisions are additive. Promotion and tag admission use it.
- `aq db current` is read-only. `aq db upgrade --yes` runs under the OPERATOR
  scope and is refused in a worker.
- A stamp this checkout does not know is diagnosed by doctor
  `db.alembic_orphan` (§2.8).

### 2.7 Backup and restore

Code: `src/database/backup.py`, `src/cli/db.py`. Reference:
[`../../reference/database/migrations.md#backup-and-restore`](../../reference/database/migrations.md#backup-and-restore).

**Backup** (`aq db backup [DEST]`).
- Runs `pg_dump --format=custom --no-owner --no-acl` into a mode-0600 file.
  The default destination is `<config dir>/backups/agent-queue-<stamp>.dump`.
- Checks the `PGDMP` header.
- Falls back to `docker exec` when there is no local `pg_dump`.
- Passes the password through the environment, never on the command line.
- Refuses in the worker scope or when `AQ_API_TOKEN` is set
  (`db.py:143-150`).

**Restore** (`aq db restore DUMP --accept-data-loss <timestamp> [--force]`).
- Refuses while the daemon is live unless `--force` is passed.
- Requires every agent session to have ended (`assert_sessions_ended`).
- Holds the daemon start lock, validates the archive timestamp and known
  Alembic revisions, and requires the exact timestamp with `--accept-data-loss`.
  The preview reports newer database rows that will be lost.
- Takes a `pre-restore-<UTC>.dump` before a final session check.
- Renders the custom archive through `pg_restore`; drops and recreates `public`
  and loads the SQL through `psql --no-psqlrc --single-transaction` with
  `ON_ERROR_STOP`. A SQL error rolls back the transaction.
- Runs no migrations. Inspect `aq db current` and use the operator upgrade
  procedure when restoring a known older revision; `--force` does not waive
  session or revision checks.

Automatic backups are taken before migrations by `aq start` (§2.3) and
`aq update` (§2.5); manual `aq update --no-backup` skips only the updater
backup, while automatic updates refuse that flag. `aq db import-sqlite` converts
a legacy SQLite database.

### 2.8 Doctor

Code: `src/doctor/` (models, runner, `builtin.py`, and the check modules);
handler `_cmd_doctor` in `src/commands/ops_commands.py:63-109`; CLI
`src/cli/doctor.py`. Operator scope:
[the operations guide](../../guides/operations.md#what-doctor-fixes--and-what-it-will-not-do).

**Model** (`models.py`).
- Severities are `ok`, `info`, `warn` and `error`. `info` never fails CI.
- A `DoctorCheck` has a namespaced id (`db.migrations`,
  `plugin.<name>.<id>`), a `run`, an optional `fix`, a `timeout_s` that
  defaults to 5 and an `owner`.
- A fix must be idempotent and must not destroy primary data.
- Results also carry execution status, completeness, coverage and reasons.
  Severity alone does not prove that an observation succeeded; an unavailable
  observation can be INFO with incomplete coverage.
- Checks receive a `DoctorContext` with the config, the database and the
  `CommandHandler`.

**Registration.**
- `default_registry()` (`src/doctor/__init__.py:88-149`) registers the built-in
  factory groups in a fixed order: **125 checks, 31 with a fix**.
- Two `worktrees.*` checks are added when `worktrees.enabled`.
- The daemon builds the registry before `orch.initialize()`, so plugins can
  add checks through `PluginContext.register_doctor_check`
  (`src/main.py:315-334`).
- A duplicate id raises.
- Four ids are reserved: `sessions.stale`, `tmux.server`, `worktrees.orphans`
  and `leases.stale`. An unregistered reserved id reports INFO "check not
  registered".

**Running** (`run_doctor`, `runner.py:159-243`).
- All selected checks run concurrently, each under its timeout.
- A timeout, an exception or a bad return becomes an ERROR result rather than
  a crash.
- `--check` filters by id. An unknown id is an ERROR.
- `--fix` runs the fixes, also concurrently, only for WARN or ERROR results
  whose check declares a fix, then re-runs those checks and marks them
  `fix_applied`. There is no dry run.
- Exit codes: 0 for ok or info, 1 for warn, 2 for any error, and 3 when doctor
  itself failed (no registry, runner crash or transport error).

**Where it runs.**
- Normally `aq doctor` (and the generated `aq system doctor`) is a REST client
  with a 330 s timeout. `aq doctor --offline --check db.alembic_orphan` is a
  local operator exception for a daemon that cannot start. It opens an engine
  without schema initialization and runs the existing orphan check and fix.
  It supports no other checks and refuses worker or agent sessions. `--fix`
  preserves the borrowed-revision downgrade, explicit stamp opt-in and retained
  migration restrictions of the online check.
- `--json` emits the versioned envelope; its `.data` contains
  `{checks, summary, exit_code}`.

- Worker tokens get "out of scope". Operators and supervisors may run it.
- The automatic runs are the post-start step in §2.3, and the integration
  train tick, which reuses `integration.train_batches_stalled` to open
  `integration_train_stall` escalations (`src/integration/stall_alerts.py`).

**Checks that serve recovery.**

| Area | Checks (fix marked *) |
|---|---|
| Pools and claims (`pool_checks.py`) | `pools.stuck`* (releases the claim of a session whose task left its claim status), `pools.preparing_stuck`* (releases to READY with `prepare_failed`, epoch-guarded), `pools.orphan_agents`* (releases leaked locks, retires dead agents, never touches busy ones), `pools.stale_worktree_checkouts`* (detaches slots left on an `aq/<task>` branch), `pools.stranded_feature_branches`* (prints a `gh pr create`, never opens one), `agents.dangling_current_task`*, `claims.container_held`* |
| Playbooks (`playbook_v2_checks.py`) | `playbooks.orphaned_runs` (report only; restart recovery retries them), `playbooks.artifact_integrity` and `playbooks.activation_stale` (no fix by design), `playbooks.required`*, `playbooks.reviewed_bundles`* |
| Integration (`integration_checks.py`) | `integration.subjects_held`, `integration.subjects_overdue`, `integration.train_batches_stalled`, `integration.branch_origins`*, `integration.finished_branch_owners`*, `integration.promotion_stranded`, `integration.completed_undelivered` |
| Database (`db_checks.py`, `builtin.py`) | `db.connect`, `db.migrations`, `db.alembic_orphan`* (borrows the missing revision file from up to 200 refs and runs its own `downgrade()`; with no file it refuses unless `AQ_DOCTOR_ALEMBIC_STAMP=1`) |
| Stalls (`stall_checks.py`) | `stall.sweep`: one read-only check with a 55 s timeout and about 35 finding kinds, such as unclaimed READY work, crash-looping workers, publisher errors, orphaned PRs and stranded children ([`../operator-stall-sweep.md`](../operator-stall-sweep.md)) |
| Verified waits (`wait_checks.py`) | `waits.schema` (report-only flags, v1 bodies and table inventory; never migrates), `waits.orphans`* (repairs via `CommandHandler`, preserving summaries and saved heads) |
| Vault and drift | `harness.drift`*, `profiles.system_drift`, `profiles.supervisor_capability_drift`*, `skills.installed_drift`* |
| Resources and services | `resources.orphaned_test_runs`*, `resources.jobs_cleanup`*, `daemon.autostart`, `dashboard.server.running`*, `perf.sustained_loop_lag`, `perf.health_latency` |

### 2.9 Fleet metrics and performance telemetry

Code: `src/metrics/`. Historical design:
[`../design/fleet-metrics.md`](../design/fleet-metrics.md). Reference:
[the metrics section of the operations guide](../../guides/operations.md#metrics-reference).

The sampler is started by `src/main.py:359-366`, not by the orchestrator. It
samples on three tiers:

- **every 1 s:** counts, load average and `/proc/meminfo`;
- **every 5 s:** a slower block plus host PSI;
- **every 60 s:** completions, PRs, the rollup and pruning.

Each sample is published on the bus as `metrics.tick`; it is not logged as an
event. Rows are buffered and flushed every 5 s to the `metrics_samples` table,
which has the columns resolution, `bucket_ts` and a `payload` stored as Text
(`src/database/tables.py:2945-2970`). Resolutions are 1 s, 1 m and 1 h, kept
for 3600 s, 30 days and 365 days. Counters taken from the bus, such as nudges,
kills and merges per hour, reset when the daemon restarts.

The performance block in the same package:

- `PerfRegistry` (`perf.py`) and `histogram.py`;
- `HostSampler` (`host.py`), with a 20 ms budget;
- the relay endpoint `/__aq/metrics` (`relay.py`);
- `loop_watchdog.py`, a thread named `aq-loop-watchdog` that logs "Event loop
  heartbeat overdue" when the loop stalls for more than 1.0 s.

`token_efficiency.py` is a read-only report run by
`scripts/token-efficiency-report.py`. `MetricsConfig` is at
`src/config.py` (`MetricsConfig`). The API is `GET /api/metrics/series`, capped at
12,000 points, and the dashboard reads it. There is no `aq metrics` command.

`metrics.enabled` and `metrics.perf_enabled` default to true. The default
`interval_seconds`, `slow_interval_seconds`, `flush_interval_seconds` and
`rollup_interval_seconds` are 1, 5, 5 and 60. Token rates use a 300 s window,
scaled per minute, with raw trailing-minute values beside them; subagent spawn
rates use a 3600 s window, scaled per hour. Numeric gauges average on rollup,
histogram buckets and sum counters add, and strings/booleans take the latest
value. Missing and null observations remain unknown rather than becoming zero.

`waits.py` adds in-place and hibernated wait counts, scheduler reasons, wake and
escalation counts, released slot time, and resume-cost estimates. Duration and
resume estimates use preceding 30-day cohorts. Counterfactual token savings
require an explicit baseline and are estimates, not measured consumption.

**Delivery latency** (`delivery_latency.py`) is separate from sampler rollups.
`GET /api/metrics/delivery-latency` accepts `window=24h|7d`, optional `project_id`
and `until`, and powers the dashboard delivery panel. It reports completion
generations, median/p90, unknown and excluded cohorts, and overlapping slot,
CI, train, repair and author-merge segments. The headline runs from approval
(or task creation without a linked spec decision) to first fresh containment
on dev, minus linked human-wait time; completion-to-dev is reported beside it.
Transition-only `delivery.observation` events use fresh git evidence. Missing
history is unknown, not zero; segment durations need not sum to the headline.
The target defaults to the larger of 30 minutes and twice the prior seven-day
p90 batch CI duration. These observations never control scheduling or cleanup.

### 2.10 Benchmarks

Code: `src/benchmark/`; handlers in `src/commands/ops_commands.py:116-200`;
CLI `aq benchmark export|stage-record` (defined in `src/cli/doctor.py:28-80`).
Guide: [`../../guides/benchmark-routing-export.md`](../../guides/benchmark-routing-export.md).

- `report.py` defines 8 stages and a v1 manifest.
- It never infers attribution: a stage is only credited when it was recorded.
- `opencode.py` reads OpenCode exports.
- Stage spans are written to `benchmark_stage_spans`.
- `scripts/benchmark-git-truth.py` and `scripts/benchmark-train-selection.py`
  are offline benchmarks for integration code.

### 2.11 Logging and retention

Code: `src/logging_config.py`, `LoggingConfig` at `src/config.py:999-1026`,
`aq logs` in `src/cli/logs.py:387-435`.

**Format.** Logging uses structlog with three formats: `dev`, `json` and
`plain`. `text` is an alias for `dev`. Records carry these correlation fields:
`task_id`, `project_id`, `cycle_id`, `component`, `hook_id`, `agent_id` and
`command`. Tracebacks are capped at 20 frames and are not rendered with Rich.

**Log files.**

| File | Writer | Rotation or retention |
|---|---|---|
| `<data_dir>/logs/agent-queue.log` | daemon (JSON lines) | 50 MB × 5 |
| `daemon.log` | stdout and stderr of the daemon | 256 MiB × 3, gzipped |
| `logs/aq-service.log` | watchdog | 1 MiB |
| `update.log` | detached updater | none |
| LLM call logs | [daemon-core](daemon-core.md) phase 3 | 30 days |

`aq logs` reads the JSON lines directly, so it works with the daemon down.
The correlation recipe is in
[the operations guide](../../guides/operations.md#logs-and-correlation).

**Retention sweeps** run in phase 3 of the cycle ([daemon-core](daemon-core.md)
§2.3):

- hourly LLM-log cleanup and the analytics flush (`core.py:3600-3611`);
- onboarding requests purged after 30 days (`monitoring.py:879-897`);
- test-selection records purged after 90 days, only while the feature is
  enabled (`monitoring.py:899-922`);
- `_auto_archive_tasks`;
- the Playbooks V2 `ArtifactRetentionSweeper`: pending events after 7 days,
  receipts and artifacts after 90.

[`../../reference/database/data-lifecycle.md`](../../reference/database/data-lifecycle.md)
lists what every table keeps.

### 2.12 Recovery mechanisms

The four runbooks in
[the operations guide](../../guides/operations.md#evidence-first-recovery-runbooks)
are the operator's procedure. The mechanisms behind them follow.

**Stranded work** (`src/orchestrator/stranded_work.py`; owned with
[scheduling-and-claims](scheduling-and-claims.md)).
- Before a non-pass close or a slot reset, unpushed commits are pushed.
- It never force-pushes. On divergence it pushes to `aq/<id>-wip` instead.
- A failed push refuses the close rather than discarding work.
- Callers: `execution.py:2569`, `worktree_manager.py:1364` and
  `src/providers/inflight.py:311`.

`aq task stop` keeps the workspace through the checkpoint flow in
`retained_workspace.py` ([projects-workspaces-git](projects-workspaces-git.md)).
`src/integration/stranded_children.py` opens the PR for a completed epic child
that no collector picked up.

**Orphaned workflows.** Playbooks V2 runs interrupted by a crash or restart
are recovered at startup and on every cycle (`core.py:2815`, `core.py:3649`,
`monitoring.py:775-783`). `playbooks.orphaned_runs` lists any that are older
than the process and have no driver. The older `OrphanWorkflowRecovery`
class (`src/orphan_workflow_recovery.py`) is never constructed (§5).

**Integration owner recovery** (`src/integration/owner_recovery.py`; behaviour
owned by [integration-and-delivery](integration-and-delivery.md)).
- It releases an `attached`, `handoff_pending` or session-bound `reserved`
  branch owner only with proof that the writer is gone. Unbound reservations
  remain outside routine recovery.
- It pushes the writer's work without forcing.
- It releases with a compare-and-set.
- It is reached from `integration_release_owner`, `restore_task_origin`,
  `integration_release` and `stop_task`.
- It is also scheduled at the end of each cycle by
  `schedule_ready_owner_recovery` (`core.py:4485-4487`), behind the config
  switch `integration.owner_recovery_sweep` (default true).
- The periodic `integration_reconcile_git_truth` command is also wired in
  `core.py`; its owner-recovery helper is gated by that same switch. Both
  paths exist in this checkout.

Its design is in the operator vault
(`specs/2026-09-21-integration-owner-recovery-design.md` and
`plans/2026-09-21-integration-owner-recovery.md`). Jack approved it in chat;
it was never a tracked review.

**Retained git object corruption** (`src/integration/repository_recovery.py`).
Guide: [`../../guides/retained-git-corruption.md`](../../guides/retained-git-corruption.md).
Design: [`../design/retained-object-integrity.md`](../design/retained-object-integrity.md),
review `rev-swift-flare`, *approved*, revision 2. It is implemented. When the
retained clone of a project has empty or corrupt loose objects:

1. Pause new integration batches for that project only.
2. Move the bad objects to `aq-empty-object-backups`, with `manifest.json` and
   `incident.json`.
3. Refetch everything.
4. Re-publish readable AQ heads that exist only locally, through absent-head
   publication.
5. Run a full `git fsck` (900 s timeout).

There are at most two attempts (`MAX_ATTEMPTS = 2`), and the count is
persisted. The supervisor gets one coalesced notice.
`aq integration clear-retained-recovery --project-id <p> [--incident-id <id>]`
clears the pause, and only when the cache is clean. Retained stores run with
`gc.auto=0` and `maintenance.auto=false`; garbage collection happens in the
foreground.

**Provider outages** are recovered by [providers](providers.md)
([`../../guides/provider-outage.md`](../../guides/provider-outage.md)). Stuck
tasks are watched by [`../stuck-task-watch.md`](../stuck-task-watch.md).
Session cleanup is in
[`../operations/session-cleanup.md`](../operations/session-cleanup.md).

### 2.13 Operator authority

Code: `src/commands/system_commands.py` (`SystemCommandsMixin`),
`src/commands/ops_commands.py` (`OpsCommandsMixin`),
`src/commands/worktree_commands.py`, `src/commands/operator_authority.py`.

- **`SystemCommandsMixin`**: config get, update and reload; pause and resume
  through `orchestrator_control`; `shutdown`; `restart_daemon`;
  `update_and_restart`.
- **`OpsCommandsMixin`**: doctor, costs, benchmarks and pool status.
- **`WorktreeCommandsMixin`**: the slot repair commands
  `repair_workspace_checkout`, `workspace_doctor`, `workspace_reap`,
  `retire_claim` and `clear_sentinel`.

**Routable operator workers** (`operator_authority.py`, review
`rev-vivid-dune`, *approved*). A worker can perform operator actions only
under these conditions:

- its profile grants `operator_execute`;
- it runs on class `standard-high` or `deep-high` with the `claude` or
  `codex` harness;
- it holds a per-action sha256 grant, covering at most 50 actions;
- it emitted an `operator.preflight` event within the last 600 s.

`OPERATOR_FORBIDDEN` keeps `review_decide`, `promote_*`, `pr_merge` and
`git_merge` out of reach. Refusals are audited as `operator.refused`.

Config is edited with `aq system config get|set|edit|schema|tune|git-identity`
([configuration-and-vault](configuration-and-vault.md)). Other operator
commands are `aq status`, `aq vault reset-harness`,
`aq dashboard serve|start|stop|status`, `aq costs` and `aq test`
([test-service-and-jobs](test-service-and-jobs.md)).

### 2.14 Scripts

`scripts/` holds operational tools that are not CLI commands:

- **Delivery and history:** `backfill-legacy-deliveries`,
  `cleanup-origin-integration-refs`, `integration-cutover-evidence` and
  `build_release_artifact`.
- **Database:** `rollback_drill`, `check_additive_migrations` and
  `reap_test_databases`.
- **Reports:** `token-efficiency-report` and `reconcile-claude-usage`.
- **Generated files:** `regenerate-generated.sh` and the client scripts
  ([`../../contributing/codegen.md`](../../contributing/codegen.md)).
- **Docs:** `check-docs.py`.
- **End-to-end kit:** `e2e-env.sh`, `e2e-smoke.sh` (20 scenarios, about
  8 minutes) and `e2e-daemon.sh`
  ([`../../guides/e2e-swarm.md`](../../guides/e2e-swarm.md)).
- **Host setup:** `install.sh` and `setup-cgroup-delegation.sh`
  ([`../../guides/resource-gating.md`](../../guides/resource-gating.md)).

## 3. Boundaries and interactions

### 3.1 What this system owns

| Owned here | Owned elsewhere |
|---|---|
| `src/install/` (engine, wizard, PostgreSQL steps, service, watchdog, update, deploy, lifecycle), `src/cli/install.py`, `uninstall`, `service.py`, `daemon.py`, `update.py`, `db.py`, `doctor.py`, `logs.py` | The `run_one_cycle` ordering, startup and shutdown sequence, the migration guard and `_health_checks`: **[daemon-core](daemon-core.md)**. This system decides *what* the deploy check and retention sweeps do; daemon-core decides *where in the cycle* they run and how their failures are isolated. |
| `src/doctor/` runner, models and registry, plus the generic checks (`builtin`, `db`, `service`, `perf`, `stall`, `resource`) | Domain checks are written and owned by their system: pool checks by 3, integration checks by 7, playbook checks by 8, and so on. This system owns the contract they implement. |
| `src/metrics/`, `src/benchmark/`, `src/logging_config.py` | The dashboard pages that chart metrics: **[dashboard](dashboard.md)**. |
| `src/database/backup.py`, `src/daemon_state.py` | Tables and revisions: **[daemon-core](daemon-core.md)**. |
| `src/orphan_workflow_recovery.py` (dead), the retention sweeps' policy | Stranded-work push: **3** with **6**; owner recovery and retained-repository recovery: **7**; playbook run recovery: **8**. This spec indexes them as recovery paths. |
| `SystemCommandsMixin`, `OpsCommandsMixin`, `operator_authority.py` | The operator decision workflow and supervisor policy: **[supervisors-and-policy](supervisors-and-policy.md)**. |
| Release tagging (`aq promote`, `PromotionPublisher`) as the input to tag deploys | Promotion of `dev` to `main` itself: **[integration-and-delivery](integration-and-delivery.md)**. |

### 3.2 Interactions with the other 17 systems

| # | System | How it meets operations-and-recovery |
|---|---|---|
| 1 | [daemon-core](daemon-core.md) | Is the process this system starts, stops, updates and watches. Runs the deploy check first in each cycle, the retention sweeps in phase 3 and owner recovery at the end. Its shutdown leaves tmux sessions alive so `aq restart` can re-adopt them. |
| 2 | [task-graph](task-graph.md) | Doctor's `tasks.*`, `hierarchy.*` and `waits.*` checks report graph damage. Auto-archive is a retention sweep. |
| 3 | [scheduling-and-claims](scheduling-and-claims.md) | Owns the `pools.*` and `claims.*` checks and their fixes, and `stranded_work.py`. The post-start doctor step fixes stale slot checkouts. |
| 4 | [routing-and-profiles](routing-and-profiles.md) | `routing.*`, `profiles.*` and `capability.*` checks. Install refreshes the profile catalogue and default pools. |
| 5 | [sessions-and-harnesses](sessions-and-harnesses.md) | Session adoption is what makes `aq restart` and `aq update` safe. `aq stop` kills sessions unless `--keep-sessions` is passed. `sessions.*` and `harness.*` checks; `aq vault reset-harness`. |
| 6 | [projects-workspaces-git](projects-workspaces-git.md) | Worktree repair and reap commands, `worktrees.*` and `workspaces.*` checks, the retained-workspace checkpoint. |
| 7 | [integration-and-delivery](integration-and-delivery.md) | Owner recovery, retained-repository corruption recovery and the train stall escalation. Its promotion produces the tags that tag deploys install. `integration.*` checks. |
| 8 | [playbooks](playbooks.md) | Orphaned-run recovery, artifact retention and `playbooks.*` checks. Judgment-based recovery (failover, escalation) runs as playbooks. |
| 9 | [messaging-and-human-asks](messaging-and-human-asks.md) | Recovery notices, such as the coalesced retained-corruption notice and train stalls, reach the operator as escalations and messages. `asks.*` and `escalations.*` checks. |
| 10 | [supervisors-and-policy](supervisors-and-policy.md) | Supervisors may run doctor and fixes. Routable operator workers run under `operator_authority.py`. |
| 11 | [reviews-and-knowledge](reviews-and-knowledge.md) | `reviews.*` and `records.*` checks. |
| 12 | [providers](providers.md) | `providers.*` checks. The provider-outage runbook. The installer installs provider CLIs and probes logins. |
| 13 | [configuration-and-vault](configuration-and-vault.md) | The installer writes `config.yaml`, `.env` and the default tuning. `deploy.*`, `logging.*` and `metrics.*` are config sections. Shipped vault content is write-if-absent, so drift checks and reseed commands are how updates reach an install. |
| 14 | [api-and-clients](api-and-clients.md) | `aq doctor`, `aq status` and `aq update` reach the daemon through the API. `/health`, `/ready` and `/api/metrics/series` are served there. |
| 15 | [dashboard](dashboard.md) | `aq update` rebuilds and checks it. It shows deploy history and charts metrics. `dashboard.*` checks. |
| 16 | [test-service-and-jobs](test-service-and-jobs.md) | `aq test` slots and `resources.*` checks. Test-selection retention is a sweep here. The e2e kit is how operations changes are verified. |
| 18 | [plugins-mcp-and-tools](plugins-mcp-and-tools.md) | Plugins register doctor checks. `mcp.probes`. The doctor MCP tool description (§5). |

## 4. Design principles and invariants

These hold today, and changes to this system must keep them.

1. **Updating never kills the agents.** `aq restart` and `aq update` stop with
   sessions kept, and the new daemon re-adopts them. Only an explicit
   `aq stop` without `--keep-sessions` kills sessions. Never update with
   `aq stop` followed by `aq start`.
2. **Back up before the schema moves by default.** `aq start` with a behind
   schema and the guarded updater with new revisions take a `pg_dump` first
   and refuse on failure. Manual `aq update --no-backup` bypasses the updater
   backup; automatic updates refuse it. The legacy command path is a gap (§5).
3. **Only the daemon or an operator migrates.** Workers verify. `aq db
   upgrade`, `aq db restore`, `aq update`, `aq start` and `aq service` all
   refuse a worker session or worker database scope.
4. **Roll back only when it is safe.** Before migrations, a failed update
   returns to the previous commit. After migrations it holds for recovery and
   does not run old code on a new schema.
5. **Deploys are reproducible and recorded.** Tag deploys accept only
   annotated tags that are ancestors of the target. Guarded deploys are in
   `deploy.json` and `deploy-history.json`.
6. **The operator's stop wins.** The `daemon.stopped` marker beats the
   watchdog, and update and daemon locks make it wait. The watchdog backs
   off and gives up rather than looping.
7. **Never lose committed work.** Recovery pushes before it resets, never
   force-pushes, and refuses to close when it cannot push. Corrupt objects
   are moved aside with a manifest, never deleted.
8. **Release a writer only on proof.** An integration owner or claim is
   released only with evidence that its writer has stopped. Report-only
   checks stay report-only when that proof needs a human.
9. **Doctor fixes are idempotent and verified.** A fix runs only for a
   failing check that declares one, and the check is re-run to prove it.
   Exit codes are stable (0, 1, 2, 3) so scripts and CI can rely on them.
10. **Installer records exclude secrets.** Install state, logs written by the
    installer and deploy records are redacted. Passwords reach `pg_dump` and
    `psql` through the environment.
11. **Async in the daemon, synchronous before it.** The installer and
    watchdog are synchronous because no event loop exists yet. Anything that
    runs inside the daemon uses async I/O.

## 5. Known gaps

The operator guide, installer reference, module catalog and current-system
metrics description were refreshed in the 2026-10-22 audit. Older design
records remain explicitly historical. The following code issues remain;
this documentation audit does not change their implementation.

**Behaviour.**
- **Messages over-promise.** The update confirmation says it "rolls back… if
  anything fails", which is false once migrations have run.
- **Updates can back up twice.** An update with migrations backs up in
  `update.py`, then `aq start` backs up again because the schema is behind.
- **Backups never expire.** Nothing prunes `backups/` or the pre-deploy
  dumps, and no doctor check watches backup age or disk use.
- **Some doctor ids are placeholders.** Unregistered reserved ids such as
  `sessions.stale`, `tmux.server` and `leases.stale` report INFO without doing
  a health probe. Placeholders are synthesized in output and do not prevent
  a real check from registering that id.

- **`stall.sweep` hard-codes values from this box.** It probes the container
  `aq-triage-test-20260831` (`stall_checks.py:57`), assumes the profile
  `deep-high-claude`, and has one uppercase finding kind,
  `WAITING-ON-DELIVERY`.
- **Supervisor doctor is global.** A per-project supervisor can run and fix
  the whole catalogue, because `doctor` has no command contract and
  `_cmd_doctor` ignores `project_id`.
- **`OrphanWorkflowRecovery` is dead code.** `core.py:3592` checks for an
  `orphan_workflow_recovery` attribute that nothing sets. V2 orphaned-run
  recovery replaced it, so the module and its cycle step can be removed.
- **Metrics have rough edges.** `metrics_samples.payload` is Text, not JSONB,
  so it cannot be compared or queried as JSON. Bus counters reset on restart.
  There is no `aq metrics` CLI.
- **Logs are not redacted.** The logging layer does not redact. Only the
  installer redacts its own output.
- **Test-selection retention can stall.** It runs only while the feature is
  enabled, so records stay forever after it is turned off.
- **Install has smaller gaps.**
  - The wizard's PostgreSQL probe ignores `--config`.
  - There is no Windows-side autostart; WSL relies on cron inside the
    distribution.
- **Release publication is separate.** A deploy record is not a GitHub Release;
  publishing a tag does not itself publish release notes.

## 6. Shipped cutovers and roadmap context

The original 2026-10-20 queue snapshot is superseded by source inspection at
the revision named above. This section records shipped behavior rather than
asserting current task statuses; operators should inspect their own queue for
remaining rollout and acceptance work.

- **Verified waits:** `waits.schema`, `waits.orphans`, wait metrics and the
  [hibernation cutover runbook](../../guides/hibernation-cutover-runbook.md)
  now ship. Schema inspection is report-only; orphan repair is the separate
  fix. Consult the runbook for feature flags and rollback order.
- **Integration:** generation-based delivery metrics and the dashboard panel
  ship, as does the periodic git-truth reconciler. Owner recovery remains
  wired alongside reconciliation (§2.12). The original fast-integration plan
  also proposes native-storage relocation and additional operator acceptance;
  their proposal does not imply those surfaces are present in this checkout.
- **Held ingestion and human asks:** their operational rollout belongs to
  [reviews-and-knowledge](reviews-and-knowledge.md) and
  [messaging-and-human-asks](messaging-and-human-asks.md).
- **Distributed workers and restore rehearsals:** reviewed designs describe
  future operating procedures. `scripts/rollback_drill.py` is an executable
  rehearsal; a reviewed proposal alone is not live acceptance evidence.

This spec is one of the 18 system documents from `eager-vault-55`; the approved
inventory is `rev-bright-rapids`. Implementation gaps in §5 remain separate
from documentation corrections.

## 7. Further reading

- [`../../guides/operations.md`](../../guides/operations.md): the operator
  manual (health layers, updates, symptoms, runbooks, metrics, logs, doctor
  scope).
- [`../../guides/releases.md`](../../guides/releases.md),
  [`../../contributing/releases.md`](../../contributing/releases.md),
  [`../design/branch-tracking-deployment.md`](../design/branch-tracking-deployment.md).
- [`../../tutorials/install.md`](../../tutorials/install.md),
  [`../../reference/cli/install.md`](../../reference/cli/install.md),
  [`../../reference/cli/uninstall.md`](../../reference/cli/uninstall.md),
  [`../../contributing/installer-testing.md`](../../contributing/installer-testing.md),
  [`../../plans/install-onboarding/README.md`](../../plans/install-onboarding/README.md).
- [`../../guides/migrations.md`](../../guides/migrations.md),
  [`../../reference/database/migrations.md`](../../reference/database/migrations.md),
  [`../../reference/database/data-lifecycle.md`](../../reference/database/data-lifecycle.md).
- [`../../guides/retained-git-corruption.md`](../../guides/retained-git-corruption.md),
  [`../design/retained-object-integrity.md`](../design/retained-object-integrity.md),
  [`../../guides/integration-troubleshooting.md`](../../guides/integration-troubleshooting.md),
  [`../../guides/provider-outage.md`](../../guides/provider-outage.md).
- [`../../guides/resource-gating.md`](../../guides/resource-gating.md),
  [`../../guides/e2e-swarm.md`](../../guides/e2e-swarm.md),
  [`../../guides/benchmark-routing-export.md`](../../guides/benchmark-routing-export.md).
- [`../../reference/modules/operations.md`](../../reference/modules/operations.md),
  [`../../reference/modules/cli.md`](../../reference/modules/cli.md),
  [`../design/trust-and-ops.md`](../design/trust-and-ops.md).
- Historical: [`../setup-wizard.md`](../setup-wizard.md),
  [`../packaging.md`](../packaging.md),
  [`../design/fleet-metrics.md`](../design/fleet-metrics.md),
  [`../operator-stall-sweep.md`](../operator-stall-sweep.md),
  [`../stuck-task-watch.md`](../stuck-task-watch.md),
  [`../../superpowers/specs/2026-09-06-release-roadmap.md`](../../superpowers/specs/2026-09-06-release-roadmap.md).
