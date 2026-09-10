# `aq install`

`aq install` prepares a machine to run AQ. It is the one common installer
interface: platform, packaging, database and provider adapters contribute
*steps* to it rather than shipping installers of their own, so a human and a
script see the same sequence, the same failure reports and the same exit codes
on every supported host.

It is the only `aq` command that expects **no running daemon** — it runs the
engine in-process and talks to nothing over the network.

It is also the whole newcomer path, not only its first half: prerequisites,
PostgreSQL, the agent CLIs and their logins, the configuration, the daemon and
the dashboard URL are one run with one resume record.

> **Status.** The engine, platform matrix, built-in prerequisite, PostgreSQL,
> optional agent CLI and onboarding steps, plus the WSL2 and **macOS**
> bootstraps, ship today. `aq install --list-steps` always prints what this
> build actually knows how to do **on the host you run it on** — the registry
> is composed for the detected host, so a Mac lists the `macos.*` steps and a
> WSL2 host does not.

## Usage

```bash
aq install                      # the wizard: a few questions, then install
aq install --advanced           # the same, plus the optional extras (Discord)
aq install --yes                # take every default without being asked
aq install --dry-run            # report the plan, run only read-only checks
aq install --non-interactive --yes --json   # unattended, machine-readable
aq install --list-steps         # what this build can do, in run order
aq install --with provider.codex            # select one optional agent CLI
aq install --with postgres-managed          # let AQ install and run PostgreSQL
aq install --repair             # reconcile this installation against the host
aq install --upgrade            # repair, and record the version transition
```

To remove an installation, see [`aq uninstall`](uninstall.md).

## The wizard

On a terminal, `aq install` asks a short set of yes/no questions before it
starts, then reports what it did:

| Question | Default | Where the default comes from |
| --- | --- | --- |
| Use Claude Code / Codex / Gemini? | the ones already installed | a read-only `PATH` and `--version` probe of each CLI. On a machine with none, the first supported harness is offered, because a machine with no harness cannot run a task. |
| Let AQ install and run a local PostgreSQL server? | yes when nothing answers on the configured host and port | a TCP probe. AQ never installs a database server without being asked. |
| Start the AQ daemon when setup finishes? | yes | this is what serves the dashboard and runs your tasks. |
| Deliver digests and escalations to Discord? | **no**, and only asked under `--advanced` | AQ is operated from the CLI and the dashboard; Discord is a delivery channel and nothing else depends on it. |

Pressing Enter through the list is the supported "just install it" path, and
`--yes` takes those same defaults without asking. The questions are skipped
entirely when the selection has already been made — `--with`, `--config`, or
`--json` (a script parsing stdout is driving consent, not being onboarded) —
and an unattended run never asks anything.

Answers only *select capabilities*; every mutating step still asks for consent
(or needs `--approve`/`--yes`) before it changes anything.

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
| `--advanced` | Ask the optional extra questions (Discord delivery) as well as the short set. |

## Agent CLI providers

Agent CLIs are optional. Select one or more with `--with`; leave a provider
unselected to record it as skipped without blocking AQ installation. A selected
provider is first detected on `PATH`, including its `--version` result. A
working existing executable is reused; otherwise the installer asks for
consent (or requires `--approve` in unattended mode) before using the
provider's documented installer. It never reads, writes, or exports provider
credentials.

| Capability | CLI | Installation route |
| --- | --- | --- |
| `provider.claude` | Claude Code | Claude's native macOS/Linux/WSL installer |
| `provider.codex` | Codex CLI | Codex's standalone macOS/Linux installer |
| `provider.gemini` | Gemini CLI | `npm install -g @google/gemini-cli` |

For example, `aq install --with provider.claude --with provider.codex` selects
two harnesses. `aq install --list-steps` includes the exact step ids for use
with `--approve` and `--restart-from`. Installing a CLI does not log it in;
that is the separate step described in
[Provider authentication](#provider-authentication).

## Provider authentication

Selecting a provider selects two steps, because *installed* and
*authenticated* are two different conditions: `provider.<name>-cli` puts the
executable on `PATH`, and `provider.<name>-login` reports whether that
executable can actually talk to its provider.

**AQ never logs you in.** It types no password, captures no token and drives no
browser. When a selected harness is not authenticated, the login step reports
`needs_user` (exit code `10`) and names the provider's own login command; you
run it, then rerun `aq install` — rerunning is the whole retry mechanism.

Readiness is observed without reading credential material, in this order:

1. The provider's own status command, if it documents one. Only its **exit
   status** is used; its output is never captured.
2. The **names** of provider-supported environment variables that are set —
   never their values.
3. The **existence** of the provider's credential file — never its contents.

| Capability | Login command | Status probe | Headless credential |
| --- | --- | --- | --- |
| `provider.claude` | `claude auth login` (or `/login` in a session) | `claude auth status` | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or `ANTHROPIC_API_KEY` |
| `provider.codex` | `codex login` | `codex login status` | `codex login --device-auth`, or `printenv OPENAI_API_KEY \| codex login --with-api-key` |
| `provider.gemini` | `gemini`, then `/auth` | *(none documented; store + environment)* | `GEMINI_API_KEY`, or `GOOGLE_GENAI_USE_VERTEXAI=true` with `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, or `GOOGLE_APPLICATION_CREDENTIALS` |

Credentials stay in the provider's own protected store — the macOS Keychain or
`~/.claude/.credentials.json`, `$CODEX_HOME/auth.json`,
`~/.gemini/oauth_creds.json`. AQ never creates, reads, moves or deletes one, so
a credential is never an installer-owned resource and uninstall never offers to
remove it.

An unattended run (`--non-interactive`) may not open a browser or read a
terminal, so it reports `needs_user` with the provider's documented
environment-credential or device-code route instead of choosing an
authentication method for you. Supply the credential in the environment before
invoking it, or complete the login once in an interactive shell on that host.

The login step's `detail` carries the distinction and nothing else:

```json
{"step_id": "provider.codex-login", "state": "succeeded",
 "summary": "Codex CLI is authenticated (api-key via OPENAI_API_KEY)",
 "detail": {"installed": true, "authenticated": true, "auth_method": "api-key",
            "credential_source": "OPENAI_API_KEY", "credential_store": "environment",
            "checked": ["codex login status", "OPENAI_API_KEY"],
            "missing_environment": []}}
```

`credential_source` is a variable name, a store label or a command — never
credential material. The resume record refuses to persist anything
secret-shaped, so a leak fails the write rather than reaching the disk.

## Configuration, the daemon and the dashboard

The last five steps are what turn an equipped machine into a running one. They
are ordered after `postgres.connection`, so a daemon can never be started
before the credential that lets it reach its database was written.

| Step | Mutating | Capability | What it does |
| --- | --- | --- | --- |
| `config.defaults` | yes | — | Creates `~/.agent-queue/config.yaml` when it is absent and adds resource-aware defaults derived from this box's cores and memory (the same values `aq system config tune --apply` writes). A section you have already written is **kept**, and a numbered backup is taken only when something is actually written. Rationale for every value: [default tuning](../../guides/default-tuning.md). |
| `config.check` | no | — | Loads the configuration exactly as the daemon does, including `${VAR}` references, and reports where AQ stores things. A configuration that does not parse stops the run here rather than at a daemon that dies with a stack trace. |
| `config.discord` | yes | `discord` | Optional. Points the hourly digest and escalation threads at one channel. |
| `daemon.start` | yes | `daemon` | Runs `aq start` and waits for `/health`. A daemon that already answers is reused, never restarted. `aq start` uses the PostgreSQL the configuration names; it reaches for the checkout's `docker-compose.yml` only when nothing is listening there and that file exists, so an installed native server needs no Docker. |
| `daemon.dashboard` | no | — | Reports the URL to open. Never blocks a run. |

### Discord is optional

AQ is operated from the CLI and the dashboard. Discord receives an hourly
activity digest and one escalation thread per human decision — nothing creates,
reviews or controls a task there. An install that never selects it records
`config.discord` as `skipped` and finishes `ready`, and the configuration it
writes says `messaging_platform: none`.

Selecting it (`--with discord`, or answering yes under `--advanced`) needs two
non-secret settings and one secret you place yourself:

```yaml
version: 1
capabilities: [discord]
settings:
  discord:
    channel_id: "123456789012345678"   # Copy ID, with Developer Mode on
    guild_id: "876543210987654321"
    credential_variable: DISCORD_BOT_TOKEN   # optional; this is the default
```

Both ids are numeric Discord IDs (17-20 digits), not names — a channel name
where an id belongs is reported by the step rather than by the daemon at its
next start. The bot token is never asked for, printed, or written by the
installer. Put it in `~/.agent-queue/.env` as `DISCORD_BOT_TOKEN=…` (mode
`0600`); `config.yaml` only ever refers to `${DISCORD_BOT_TOKEN}`. Until it is
there, the step reports `needs_user` (exit `10`) and says exactly where to put
it. Add Discord later at any time by rerunning with `--with discord`.

### Where AQ stores your data

`config.check` reports these, and so does the closing summary — they are
computed from the resolved configuration rather than repeated in prose:

| What | Where |
| --- | --- |
| Configuration | `~/.agent-queue/config.yaml` — edit with `aq system config edit` |
| Secrets | `~/.agent-queue/.env`, mode `0600` — the values `${VAR}` references resolve to |
| Vault | `~/.agent-queue/vault/` — playbooks, agent profiles, memory and facts, as markdown |
| Worktrees | `workspace_dir` from the configuration — each worker's isolated checkout |
| Daemon log | `~/.agent-queue/daemon.log` — `aq logs` reads it |
| Resume record | `~/.agent-queue/install-state.json` — what this command completed and owns |
| Tasks, projects, sessions, results | PostgreSQL, reported without its password |

### Opening the dashboard

A release install serves the dashboard from the daemon itself, at
`/dashboard` on the API base (`http://127.0.0.1:8081/dashboard` by default, or
whatever `mcp_server.host`/`port` and `AQ_API_URL` resolve to). A source
checkout ships no built assets, so `daemon.dashboard` says so and gives the
Vite command (`npm -w dashboard run dev`, `http://localhost:5173`) instead of a
URL that would 404 in a browser.

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

The same object carries the closing summary under `onboarding` — the human
view and a script are told the same things:

```json
{
  "onboarding": {
    "ready": true,
    "headline": "AQ is installed and ready.",
    "locations": [
      {"label": "Configuration", "path": "/home/you/.agent-queue/config.yaml",
       "note": "settings, tuned for this machine; edit with `aq system config edit`"}
    ],
    "dashboard": {"url": "http://127.0.0.1:8081/dashboard", "reachable": true,
                  "source": "bundled", "hint": ""},
    "skipped": ["Discord delivery for digests and escalations — not selected; add it with `aq install --with discord`"],
    "next_steps": ["Open the dashboard at http://127.0.0.1:8081/dashboard."]
  }
}
```

* **`onboarding.ready`** agrees with `outcome == "ready"`.
* **`onboarding.skipped`** lists the *optional* things this run did not do and
  the flag that would add each one. A skipped capability is a finished install,
  not a partial one.
* **`onboarding.dashboard.source`** is `bundled` (the daemon serves it),
  `dev-server` (a source checkout — run Vite) or `unknown` (no daemon answered).

`aq install --list-steps --json` prints `{"schema_version": 1, "steps": [...]}`
with each step's `id`, `title`, `description`, `depends_on`, `capability`,
`mutating`, `owner` and `input_schema_version`.

## macOS

On macOS the registry gains the platform adapter's steps, which run before the
generic prerequisite checks — the adapter installs what the engine then
verifies.

| Step | What it does | When it stops |
| --- | --- | --- |
| `macos.architecture` | Confirms the shell is native. Reads `sysctl.proc_translated` and `hw.optional.arm64`, so an x86_64 shell on Apple-Silicon hardware is caught even when `uname -m` says `x86_64`. | `failed` under Rosetta, naming `arch -arm64` and the Get Info toggle. AQ does not support a Rosetta-based installation. |
| `macos.developer-tools` | Checks the Xcode Command Line Tools with `xcode-select -p`. | `needs_user` when absent (`xcode-select --install` opens a dialog a human has to accept) or when the selected developer directory is missing (`sudo xcode-select --reset`). |
| `macos.homebrew` | Finds Homebrew at its **real** prefix: `HOMEBREW_PREFIX`, then `PATH`, then `/opt/homebrew` (Apple Silicon) and `/usr/local` (Intel). Records the prefix and version. | `needs_user` with Homebrew's official install command when it is absent — that installer asks for an administrator password, which `aq install` never types. `failed` when the only Homebrew is the Intel build on an Apple-Silicon Mac, or when the prefix is not writable by this user (with Homebrew's `chown` repair). |
| `macos.shell-path` | *Mutating.* Appends one marked block containing Homebrew's documented `eval "$(<prefix>/bin/brew shellenv)"` line to the login file of the invoking user's shell (`~/.zprofile`, `~/.bash_profile`, `~/.profile`). | `needs_user` for a shell it does not edit (fish, tcsh), quoting the line to add rather than guessing that shell's syntax. Skipped when consent is declined. |
| `macos.packages` | *Mutating.* `brew install`s the prerequisites that are missing (`tmux`, `git`). Anything already on `PATH` is left alone. | `failed` with the formula, the tail of Homebrew's output and the command to rerun by hand. |
| `macos.launch-services` | Checks that the invoking user's `launchd` domain is reachable, which is what `brew services` needs to install a service that starts at login. | `skipped` — not failed — in a session without one (a plain SSH login), explaining that a service will run now but not at login. |
| `macos.python-runtime` | Refuses to continue when the installer is running on `/usr/bin/python3`. | `failed`, not retryable, pointing at `brew install python@3.12`. AQ never modifies the macOS system Python: it is externally managed and macOS updates replace it. |

Consequences worth knowing:

* Nothing here enters a password or drives a GUI. Homebrew's installer and
  `xcode-select --install` are human checkpoints that the run resumes past.
* A formula this run installed lives in `<prefix>/bin`, which is on the *next*
  shell's `PATH`. `prereq.git` and `prereq.tmux` look inside the Homebrew
  prefix as well as on `PATH`, so one run both installs and verifies.
* Apple Silicon is the supported tier and Intel is the compatibility tier; the
  adapter never assumes `/opt/homebrew`, and `macos.architecture` names which
  one it observed.
* A Mac that already has Homebrew, the Command Line Tools and tmux runs the
  same steps and installs nothing — every macOS step is either read-only or
  guarded by its own verifier.

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
* Selecting an optional capability on a later run installs it. `aq install
  --with provider.codex` runs the Codex step even though an earlier run
  recorded it as `skipped` — a skip that was only "you did not ask for this"
  is not a decision the record holds you to.

### `--repair`

`--repair` is a rerun that does not take the record's word for it. A step whose
observable condition can be checked is still revalidated — that is the cheapest
possible repair — but a step with **no read-only verifier** is re-executed
instead of being carried forward. Use it when the host has drifted in a way the
record cannot see.

A repair is still bound by every other rule: it reuses the recorded resources
rather than creating new ones, it removes nothing, and it asks for consent
before a mutating step exactly as an install does. It also carries the
capabilities the record selected forward, so a repair reconciles the
installation you have rather than quietly narrowing it; an explicit `--with`
still wins.

`--repair` and `--fresh` are mutually exclusive: `--fresh` starts a new record,
which is the opposite of reconciling the existing one.

### `--upgrade`

`--upgrade` repairs, and additionally records the version transition **durably**:

```json
"upgrade": {"from_version": "1.0.0", "to_version": "1.1.0",
            "state": "in_progress", "started_at": "2026-09-09T09:00:00+00:00",
            "finished_at": null, "attempts": 1}
```

The record is written *before* the first step runs and marked `completed` only
when the run reaches `ready`. So an installer that is killed halfway leaves an
`in_progress` record behind, and the next run — of **any** mode, including a
plain `aq install` — finds it, says so in `messages`, and resumes from the first
unsatisfied step. Nothing is rolled back: the contract resumes over the
resources named in the ownership record rather than unwinding them.

`--upgrade` to a *different* target supersedes an abandoned transition and says
which one it replaced. A plain rerun with a different target reports the
pending transition and leaves it alone.

### A record from another installer version

A resume record written by a different installer version is refused by a plain
rerun — the message names both versions and the two ways forward. `--repair`
and `--upgrade` *are* the explicit repair plan the contract asks for: they adopt
the record, keep every owned resource (forgetting them is what would make the
next run create a second copy), re-verify every step and re-stamp the version.
`--fresh` remains the "start over beside the old one" option, and it deletes
nothing either.

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
capabilities: [provider.codex]
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
* [`aq uninstall`](uninstall.md) — removing what this command owns.
* [Operations guide](../../guides/operations.md) — `aq doctor` and recovery
  once AQ is installed.
* [Install tutorial](../../tutorials/install.md) — the current
  development-checkout setup path.
