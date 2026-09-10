# `aq install`

`aq install` prepares a machine to run AQ. It is the one common installer
interface: platform, packaging, database and provider adapters contribute
*steps* to it rather than shipping installers of their own, so a human and a
script see the same sequence, the same failure reports and the same exit codes
on every supported host.

It is the only `aq` command that expects **no running daemon** — it runs the
engine in-process and talks to nothing over the network.

> **Status.** The engine, the platform matrix, the built-in prerequisite steps
> and the PostgreSQL steps ship today. The steps that install WSL, Homebrew and
> the agent CLIs are being added by the remaining install-and-onboarding tasks;
> `aq install --list-steps` always prints what this build actually knows how
> to do.

## Usage

```bash
aq install                      # interactive: prompt before each change
aq install --dry-run            # report the plan, run only read-only checks
aq install --non-interactive --yes --json   # unattended, machine-readable
aq install --list-steps         # what this build can do, in run order
aq install --with postgres-managed          # let AQ install and run PostgreSQL
```

| Option | Meaning |
| --- | --- |
| `--interactive` / `--non-interactive` | Prompt for consent, or never read a terminal. Defaults to interactive on a TTY, unattended otherwise. |
| `--dry-run` | Print the plan and run the read-only checks. Mutating steps are reported as `would_run` and are not executed; no resume record is written. |
| `--json` | Print exactly one JSON object on stdout (see below). Implies unattended unless `--interactive` is given. |
| `--config PATH` | Unattended install input file, YAML or JSON (see below). |
| `--with CAPABILITY` | Select an optional capability. Repeatable. |
| `--approve STEP` | Authorise one mutating step in an unattended run. Repeatable. |
| `--yes` / `-y` | Authorise every mutating step. |
| `--resume` / `--no-resume` | Continue from the resume record (default), or ignore it for this run without deleting it. |
| `--fresh` | Start a new resume record. The existing one is left on disk. |
| `--restart-from STEP` | Redo `STEP` and the steps that depend on it. Nothing else is re-executed. |
| `--state-file PATH` | Where the resume record lives. Defaults to `~/.agent-queue/install-state.json`. |
| `--list-steps` | Print the registered steps and exit. |

## Exit codes

Scripts branch on these. A code may be added in a future release, but an
existing code is never reassigned.

| Code | Outcome | Meaning |
| --- | --- | --- |
| `0` | `ready` | Every selected step is satisfied. |
| `10` | `needs_user` | A human action is required — a browser login, a device code, or an approval an unattended run does not have. Nothing failed. |
| `11` | `invalid_input` | A flag, an input file, or the existing resume record cannot be used. Nothing was changed. |
| `12` | `unsupported_host` | The host is not in the supported-platform matrix. Nothing was changed. |
| `20` | `failed` | A step failed. The step and its remediation are named in the result. |

## Machine-readable output

`--json` prints one object:

```json
{
  "schema_version": 1,
  "outcome": "needs_user",
  "exit_code": 10,
  "installer_version": "0.1.0",
  "target_version": "0.1.0",
  "dry_run": false,
  "interactive": false,
  "platform": {
    "system": "linux", "arch": "x86_64", "wsl": true, "wsl_version": 2,
    "distro_id": "ubuntu", "distro_version": "24.04",
    "host_path": "windows-wsl2", "tier": "supported", "installable": true,
    "reasons": [], "remediation": null, "notes": []
  },
  "capabilities": [],
  "plan": [
    {"step_id": "prereq.data-dir", "title": "Prepare the AQ data directory",
     "action": "run", "reason": "not yet satisfied", "mutating": true, "capability": null}
  ],
  "steps": [
    {"step_id": "prereq.data-dir", "state": "needs_user",
     "summary": "unattended run has no approval for the mutating step 'prereq.data-dir'",
     "detail": {}, "remediation": "Rerun with `--approve prereq.data-dir` …",
     "retryable": true, "resources": [], "duration_ms": 3}
  ],
  "resources": [
    {"kind": "directory", "id": "/home/you/.agent-queue", "owned": true, "reused": false, "detail": {}}
  ],
  "state_path": "/home/you/.agent-queue/install-state.json",
  "blocking_step": "prereq.data-dir",
  "next_action": "Rerun with `--approve prereq.data-dir` …",
  "messages": []
}
```

* **`outcome`** and **`exit_code`** always agree; branch on either.
* **`steps[].state`** is one of `succeeded`, `skipped`, `needs_user`, `failed`.
* **`blocking_step`** is the id of the first step that stopped the run, or
  `null`. Everything after it reports `skipped` with a `not reached` summary.
* **`steps[].remediation`** is present on every `failed` and `needs_user` step
  and says what to do; **`next_action`** repeats the first one.
* **`plan[].action`** is `run`, `revalidate`, `would_run`, `skip_not_selected`,
  `skip_completed` or `blocked`.
* **`resources`** is what the installer owns (`owned: true`) or found and
  reused (`owned: false`). It is what repair and uninstall act on.

`aq install --list-steps --json` prints `{"schema_version": 1, "steps": [...]}`
with each step's `id`, `title`, `description`, `depends_on`, `capability`,
`mutating`, `owner` and `input_schema_version`.

## Rerunning, resuming and repair

Rerunning `aq install` is the normal recovery path, and it is safe:

* A step that a previous run completed is **revalidated** through its
  read-only check rather than re-executed, so a rerun does not reinstall a
  package or recreate a directory.
* If the observable condition is gone — the directory was deleted, the package
  was removed — the step runs again.
* Resources are recorded by `(kind, id)`, so repeating a step cannot grow the
  owned-resource list.
* The run stops at the first step that fails or needs a human, and the next run
  picks up there.
* `--restart-from STEP` redoes exactly `STEP` and its dependents. Unrelated
  completed steps are left alone. It never deletes a recorded resource:
  removing something is an explicit uninstall action, not a side effect.

## The resume record

`~/.agent-queue/install-state.json` (mode `0600`, written atomically) holds the
installer version, the target version, the observed platform facts, the
selected capabilities, each step's terminal state, and the identifiers of every
owned or reused resource.

It **never** holds a credential. The writer redacts secret-shaped keys and
values and then re-checks the payload; a step that tried to persist a token,
password or credentialed DSN fails the write instead of leaking it to disk.

A record written by a different installer version is reported, not deleted:
rerun with `--restart-from <step>` to redo part of the install, or `--fresh`
to start a new record beside the old one.

## Unattended input file

```yaml
version: 1
capabilities: [dashboard]
approve: ["prereq.data-dir"]     # or ["*"] for every mutating step
settings:
  some-adapter-option: value
```

Unknown top-level keys, a non-list `capabilities`/`approve`, an unknown
capability name and an unknown step id are all rejected with exit code `11`
before anything runs — an unattended installer that silently ignored a
misspelled key would install the wrong thing. Command-line flags are merged
with the file and win where they overlap.

An unattended run never prompts, never opens a browser and never invents an
approval: a mutating step with no approval stops the run as `needs_user`
(exit `10`) naming the flag that would authorise it.

## PostgreSQL

AQ needs one PostgreSQL database. `aq install` will either **use a server you
already run** or, when you ask it to, **install and run a local one**. It never
does the second without being asked: a server, a role and a database are things
other software may already depend on.

| Step | Mutating | What it does |
| --- | --- | --- |
| `postgres.package` | yes | Installs PostgreSQL with the platform's package manager — `apt-get install -y postgresql` on Ubuntu, `brew install postgresql@17` on macOS. Requires `--with postgres-managed`. |
| `postgres.service` | yes | Starts the server and waits for it to accept connections. Requires `--with postgres-managed`. |
| `postgres.server` | no | Confirms a PostgreSQL server answers on the configured host and port, and that it is PostgreSQL 14 or newer. |
| `postgres.role` | yes | Creates the `agent_queue` login role with a generated password, if it does not already exist. |
| `postgres.database` | yes | Creates the empty UTF-8 `agent_queue` database owned by that role, if it does not already exist. |
| `postgres.rotate` | yes | Replaces the role's password. Requires `--with postgres-rotate`; see [recovery](#recovering-from-a-credential-or-connection-problem). |
| `postgres.credentials` | yes | Writes the password to `~/.agent-queue/.env` (mode `0600`) and points `config.yaml` at it as `${AQ_DB_PASSWORD}`. |
| `postgres.connection` | no | Connects as the AQ role and reports the server version. |
| `postgres.boot` | yes | Makes the managed server start again after a restart. Requires `--with postgres-managed`. |

Two capabilities select the optional behaviour:

* `--with postgres-managed` — AQ may install, start and enable a local
  PostgreSQL server. Without it the installer only ever *uses* a server that is
  already reachable, which is what makes an unattended install against a
  managed or remote database safe by default.
* `--with postgres-rotate` — replace the AQ role's password on this run.

### What is never touched

* An **existing role** of the configured name keeps its password. The installer
  does not reset a credential it did not create, because on a shared server
  that role may be in use. It is recorded `reused`, with `owned: false`.
* An **existing database** of the configured name is used as it is — never
  dropped, re-owned or re-encoded.
* **Unrelated roles and databases** are never read, altered or removed.
* The **schema** is not the installer's. `postgres.connection` stops at "the
  database is reachable and empty"; creating and migrating tables is the
  daemon's or the operator's step (`aq db upgrade`), and no installer path runs
  Alembic. See [migrations](../../guides/migrations.md).

### Where the password lives

The generated password is written to `~/.agent-queue/.env` as
`AQ_DB_PASSWORD=…` with mode `0600`, and `config.yaml` refers to it:

```yaml
database:
  url: postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue
```

The password is never written to `config.yaml`, the `--json` result, the resume
record, or a command line — statements go to `psql` on standard input so they
are not visible in `ps`. If `config.yaml` already exists, a numbered backup
(`config.yaml.bak`, `config.yaml.bak.1`, …) is taken before it is changed, and
every key the installer does not own is preserved.

### Settings

All of these live under `settings.postgres` in the `--config` input file. An
unrecognised key is rejected by name before anything runs.

| Setting | Default | Meaning |
| --- | --- | --- |
| `postgres.host` | `localhost` | Server host. |
| `postgres.port` | `5432` | Server port. |
| `postgres.database` | `agent_queue` | Database name. Lower-case SQL identifier. |
| `postgres.role` | `agent_queue` | Login role name. Lower-case SQL identifier. |
| `postgres.password_env` | `AQ_DB_PASSWORD` | Environment variable `config.yaml` refers to. |
| `postgres.admin_url` | — | Administrator connection for an existing server, used instead of the local superuser route. |
| `postgres.admin_user` | platform default | Local administrator identity (`postgres` on Ubuntu, the invoking user on Homebrew). |
| `postgres.service_manager` | `auto` | `auto`, `systemd`, `sysv`, `brew` or `none`. `none` leaves the server's lifecycle alone. |
| `postgres.version` | platform default | Major version for a managed install (`16`, `17`…). |
| `postgres.connect_timeout` | `5` | Seconds to wait for a connection. |
| `postgres.startup_timeout` | `60` | Seconds to wait for a freshly started server. |

```yaml
version: 1
settings:
  postgres:
    host: db.internal
    port: 5432
    admin_url: postgresql://postgres@db.internal:5432/postgres
approve: ["postgres.role", "postgres.database", "postgres.credentials"]
```

An unattended **first** run still needs an approval for each mutating step it
reaches, including against a server that already has the role and the database
— an unattended installer that provisioned a database nobody approved would be
exactly the implicit mutation the contract forbids. Later reruns need no
approval: a completed step is revalidated, not repeated.

### Restarting the machine

`postgres.boot` is the last step, so restart-persistence is never what blocks
provisioning:

* **systemd** (Ubuntu with systemd, macOS is covered below): `systemctl enable
  postgresql`, verified with `systemctl is-enabled`.
* **macOS/Homebrew**: `brew services start postgresql@NN`, which registers a
  launchd agent that starts the server at login.
* **WSL2 without systemd**: nothing can enable a service at boot, so the step
  reports `needs_user` and gives the fix — add

  ```ini
  [boot]
  systemd=true
  ```

  to `/etc/wsl.conf`, run `wsl --shutdown` from Windows, reopen the
  distribution and rerun `aq install`.

After a restart, `aq install` is also the check: it starts the managed service
if it is not running, reconnects as the AQ role and reports what it found,
without changing anything that is already correct.

### Recovering from a credential or connection problem

Every failure names the step and the fix. The common ones:

| What you see | What to do |
| --- | --- |
| `no PostgreSQL server is reachable at localhost:5432` | Rerun with `--with postgres-managed` to install one, or set `postgres.host` / `postgres.port` / `postgres.admin_url` to the server you already run. |
| `something that is not PostgreSQL is already listening` | Find the listener (`ss -ltnp 'sport = :5432'`, or `lsof -nP -iTCP:5432 -sTCP:LISTEN` on macOS) and stop it, or set `postgres.port`. |
| `the role agent_queue already exists and was left unchanged`, then `no password is available` | The role predates AQ. Put its password in `~/.agent-queue/.env` as `AQ_DB_PASSWORD=…`, or run `aq install --with postgres-rotate` to replace it. |
| `the server rejected the stored password` | Same two routes: correct `AQ_DB_PASSWORD`, or `aq install --with postgres-rotate`. |
| `the database agent_queue does not exist` | `aq install --restart-from postgres.database`. |
| `no PostgreSQL administrator connection is available` | Run `sudo -v` and rerun, run the installer as the `postgres` user, or set `postgres.admin_url`. |
| `is older than the supported PostgreSQL 14` | Upgrade the server, or point AQ at a newer one. |

Rotation changes the password on the server and then stores it, in that order
and in adjacent steps: if storing fails, the run says so and says that the
database is unreachable until a new password is stored.

## Supported hosts

| Host | Baseline | Tier |
| --- | --- | --- |
| Windows via WSL2 | Windows 10 2004 / build 19041+ or Windows 11, WSL2, Ubuntu 24.04 LTS, x86_64 or arm64 | supported |
| macOS, Apple Silicon | macOS 14 (Sonoma) or newer | supported |
| macOS, Intel | macOS 14 (Sonoma) or newer | compatibility |
| Anything else | — | refused with the observed facts, before any change |

An unsupported host exits `12` and changes nothing. The full matrix, including
its explicit boundaries, is [the installation and onboarding
contract](../../plans/install-onboarding/contract.md).

## Related pages

* [CLI reference](README.md) — the whole `aq` surface.
* [Operations guide](../../guides/operations.md) — `aq doctor` and recovery
  once AQ is installed.
* [Install tutorial](../../tutorials/install.md) — the current
  development-checkout setup path.
