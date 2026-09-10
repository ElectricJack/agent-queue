# Install and start Agent Queue

Install AQ as a background service that turns a task description into an
isolated coding-agent session against a Git repository. This page gets a new
operator from a fresh machine to a healthy daemon and an open dashboard;
[the next tutorial](first-task.md) creates the first disposable project and
task.

There is one command: **`aq install`**. It asks a few questions, installs or
reuses what this machine needs, writes a configuration tuned for this box,
starts the daemon and tells you where your data lives. Rerunning it is how you
resume, repair and add things later.

## Why this exists

AQ keeps work separated: you supply a repository and a task, then AQ assigns a
worker a separate checkout and records the result. That means the machine
running AQ needs its own database, Git tooling, and at least one authenticated
coding-agent command-line interface (CLI). It is not a library you import into
the repository being changed.

## Vocabulary

* A **daemon** is the long-running AQ process that owns scheduling and starts
  workers.
* A **harness** is the coding-agent CLI that runs inside a worker session;
  profiles select a harness. See [profiles in source](../../src/profiles/default_selection.py).
* A **project root** is an operator-approved directory below which AQ may
  onboard repositories. It is different from AQ's worktree directory.
* A **worktree** is a worker's isolated Git checkout. It is not your source
  repository. See the [glossary](../reference/glossary.md) for these terms.

## Prerequisites and supported boundary

| Host | Baseline | Tier |
| --- | --- | --- |
| Windows via WSL2 | Windows 10 2004 / build 19041 or newer, or Windows 11, with WSL2 and Ubuntu 24.04 LTS | supported |
| macOS, Apple Silicon | macOS 14 (Sonoma) or newer | supported |
| macOS, Intel | macOS 14 (Sonoma) or newer | compatibility |
| Anything else | — | refused, with the observed facts, before anything is changed |

On Windows, everything — AQ, PostgreSQL, your project checkouts and the
harness — runs *inside* the WSL2 distribution. Do not mix a Windows path with a
Linux daemon path.

The installer checks Python 3.12+, Git and `tmux` itself, and on macOS it can
install the missing ones through Homebrew. PostgreSQL is required and the
installer will either use a server you already run or, when you ask it to,
install a local one. SQLite is not a runtime option; an existing SQLite
database can only be imported (`aq db import-sqlite`).

The [installation release record](../validation/installation-release-0.1.0.md)
keeps the versioned artifact checks, native platform evidence, and known
acceptance limits together. Read it before treating a source-checkout run as
evidence for a published artifact.

## Platform quickstarts

Choose one path below, then continue with [the first task](first-task.md).
These commands get AQ onto the machine; [`aq install`'s reference](../reference/cli/install.md)
is the authoritative list of installer flags, steps, exit codes, and JSON
fields. Do not run both paths on one computer.

### Windows + WSL2 quickstart

AQ is not a native-Windows service. Its database client, daemon, project
checkouts, and agent CLIs must all live in the same **Ubuntu 24.04 WSL2**
distribution and in its Linux filesystem (for example, `/home/alex`), never
under `/mnt/c`.

1. In an **Administrator PowerShell** window, install the supported
   distribution if you do not have it yet:

   ```powershell
   wsl --install -d Ubuntu-24.04
   ```

   Restart Windows when asked, launch **Ubuntu 24.04** once from Start, and
   create its Linux username and password. Microsoft documents the same setup
   and its WSL2 check with `wsl --list --verbose` in its [WSL installation
   guide](https://learn.microsoft.com/windows/wsl/install).

2. Back in a regular PowerShell window, run the AQ Windows bootstrap:

   ```powershell
   irm https://raw.githubusercontent.com/ElectricJack/agent-queue/main/scripts/install-windows.ps1 | iex
   ```

   It reuses an existing Ubuntu 24.04 distribution, converts WSL1 to WSL2 if
   needed, opens a Linux-home shell, installs the minimal WSL prerequisites,
   and calls the common `aq install --interactive` flow. It does not reset an
   existing AQ checkout or install a separate Windows copy.

   Expect `Using existing Ubuntu-24.04 on WSL2` followed by `Running the
   common AQ installer inside WSL2...`. A reboot, WSL setup, or a provider
   login is a checkpoint, not a failed install: complete the named action and
   run the same PowerShell command again. When AQ prints a dashboard URL,
   open that `localhost` URL in your Windows browser.

### macOS quickstart

Use Terminal in a native shell: Apple Silicon is supported on macOS 14+
(Sonoma or newer); Intel macOS 14+ is a compatibility tier. AQ refuses a
Rosetta-based primary install, because it must not build an Intel Homebrew
toolchain on an Apple Silicon Mac.

1. If macOS has not installed its Command Line Tools yet, run this and accept
   the macOS dialog:

   ```bash
   xcode-select --install
   ```

2. Clone AQ and use the source-checkout bootstrap:

   ```bash
   git clone https://github.com/ElectricJack/agent-queue.git
   cd agent-queue
   ./setup.sh
   ```

   `setup.sh` prepares the checkout and hands all machine setup to `aq
   install`. On a new Mac, it may stop for the Homebrew administrator prompt,
   a shell-PATH change, or a harness browser login. Complete that human-only
   checkpoint in the terminal, then rerun `./setup.sh` (or `aq install` after
   the checkout bootstrap succeeds). The installer records progress and
   revalidates completed steps rather than repeating them.

On either platform, a healthy closing summary includes `AQ is installed and
ready.` and a dashboard URL. If it instead says `needs_user`, read its `next:`
line, complete only that action, and rerun; see [structured outcomes for people
and automation](#structured-outcomes-for-people-and-automation).

## Install

From a release install, `aq install` is the whole thing. From a source
checkout, `./setup.sh` creates the virtual environment, installs the `aq`
command and then runs `aq install` for you:

```bash
git clone <AQ-REPOSITORY-URL> agent-queue
cd agent-queue
./setup.sh
```

`aq install` then asks a short set of questions. Pressing Enter accepts each
default, and `aq install --yes` takes them all without asking:

```text
Setting up AQ on this machine. Press Enter to accept each default.
  (already installed)
  Use Claude Code? [Y/n]:
  (not installed here; AQ would install it with the provider's own installer)
  Use Codex CLI? [y/N]:
  ...
  (no server answered on the configured host and port)
  Let AQ install and run a local PostgreSQL server? [Y/n]:
  (this is what serves the dashboard and runs your tasks)
  Start the AQ daemon when setup finishes? [Y/n]:
```

Add `--advanced` to be asked about the optional extras as well — today that is
Discord delivery, which is off by default.

Each step then reports what it did, and every step that would change the
machine asks first:

```text
[1/25] OK Confirm the host is supported
[5/25] OK Prepare the AQ data directory
…
AQ installation is complete.

First-task readiness
  OK Database: AQ connected to its PostgreSQL database in this install run.
  OK Daemon: the daemon answered its health endpoint.
  OK Dashboard: the dashboard is reachable at http://127.0.0.1:8081/dashboard.
  OK Agent authentication: at least one harness has non-secret authentication evidence.
  OK Profile routing: an authenticated worker profile is active.
  OK Workspace prerequisites: Git, tmux, and the worktree location were verified.
  !! Project root: No project root is configured, so `aq project onboard` has no
     `--root-id` to onboard into.
     next: Add one under Settings → Project Roots in the dashboard, or put a
     `project_roots:` entry (`id`, `label`, `path`) in config.yaml with
     `aq system config edit`; then rerun `aq install` to recheck it.
```

**Project root** is the one check a correct, complete installation still
reports as needing attention. AQ's defaults come from this machine's cores and
memory and name no filesystem location, and choosing where your repositories
live is a project decision rather than a machine one — so the installer
measures the gap and names the page that closes it instead of creating a
directory in your home. Adding one root is a one-time step;
[the first-task tutorial](first-task.md#a-realistic-disposable-example) shows
the YAML.

The exact step list depends on the host and what you selected; `aq install
--list-steps` prints it, and [the `aq install` reference](../reference/cli/install.md)
documents every step, flag and exit code.

### Signing in to a harness

**AQ never logs you in.** When a selected harness is not authenticated, the run
stops at that step, names the provider's own login command (`claude auth
login`, `codex login`, `gemini` then `/auth`) and exits `10`. Run it in your
own terminal, then run `aq install` again: it revalidates everything that was
already done and carries on from where it stopped. Skipping a harness entirely
is a supported answer — the install still finishes.

The browser/device prompt belongs to the provider and must be completed by a
human. Do not give an API key, device code, password, or browser session to AQ,
an agent prompt, or an installation log. For provider-specific choices and
account restrictions, use the provider's current guide: [Codex CLI](https://learn.chatgpt.com/docs/codex/cli),
[Claude Code](https://code.claude.com/docs/en/quickstart), or [Gemini CLI
authentication](https://geminicli.com/docs/get-started/authentication/).

### Interruptions

A rerun is the recovery path for everything: a closed terminal, a failed step,
a reboot in the middle. `aq install` records what it completed and what it owns
in `~/.agent-queue/install-state.json` (never a credential), revalidates a
completed step instead of repeating it, and stops at the first thing that still
needs attention. Nothing is done twice and nothing starts over.

### Discord is optional

AQ is operated from the CLI and the dashboard. Discord receives an hourly
activity digest and one escalation thread per human decision, and nothing else
depends on it: an install that never mentions it finishes ready, and the
configuration it writes says `messaging_platform: none`. To add it later:

```bash
aq install --with discord
```

You supply the bot token yourself by putting it in `~/.agent-queue/.env` as
`DISCORD_BOT_TOKEN=…`; the installer never asks for, prints or stores it.

## When it finishes

The closing summary is the same information a script gets from
`aq install --json` under `onboarding`:

```text
AQ is installed and ready.

Where AQ stores your data
  Configuration  /home/you/.agent-queue/config.yaml
  Secrets        /home/you/.agent-queue/.env
  Vault          /home/you/.agent-queue/vault
  Worktrees      /home/you/.agent-queue/workspaces
  Daemon log     /home/you/.agent-queue/daemon.log
  Resume record  /home/you/.agent-queue/install-state.json
  Database       postgresql+asyncpg://agent_queue@localhost:5432/agent_queue

Dashboard
  http://127.0.0.1:8081/dashboard

Next
  1. Open the dashboard at http://127.0.0.1:8081/dashboard.
  2. Create your first project and task: `aq project onboard --root-id <root>
     --help`, or follow docs/tutorials/first-task.md.
  3. `aq doctor` checks this installation whenever something looks wrong.
```

Open that URL in a browser. A **source checkout** ships no built dashboard, so
the summary gives you the development command instead — run
`npm -w dashboard run dev` in the checkout and open `http://localhost:5173`.

The install outcome and first-task readiness answer different questions. You
can deliberately finish an installation with every provider skipped, but AQ
will then show **needs attention** for agent authentication and profile routing
instead of inviting you to create a task that cannot run. The same is true of
**Project root** on any fresh machine, which is why step 2 above names a
`--root-id` you have yet to configure. Follow the named
remediation and rerun `aq install`; it rechecks rather than repeats completed
work. The machine-readable equivalent is `onboarding.readiness` in
`aq install --json`.

`aq status` reports what the daemon thinks of itself, and `aq stop` /
`aq restart` control it.

## Unattended installation

The same engine runs without a terminal, for a script or a new machine image:

```bash
aq install --non-interactive --config install.yaml --json
```

```yaml
version: 1
capabilities: [provider.claude, daemon, postgres-managed]
approve: ["*"]          # or name each mutating step
settings:
  postgres:
    host: db.internal
```

It never reads a terminal and never opens a browser: a missing human
credential comes back as `needs_user` (exit `10`) with the provider's
documented environment-credential route, rather than a guess. Exit codes: `0`
ready, `10` needs_user, `11` invalid_input, `12` unsupported_host, `20` failed.

### Structured outcomes for people and automation

For a person, the final human-readable `next:` line is the only action to take.
For an agent or deployment script, discover the current step names first and
then branch on the JSON result; never scrape progress text or assume that an
installed executable is a usable harness.

```bash
aq install --list-steps --json
aq install --non-interactive --config install.yaml --json
```

| JSON field / exit | Meaning | Agent action |
| --- | --- | --- |
| `outcome: "ready"` / `0` | Every selected step is satisfied. | Read `onboarding.readiness`; create a first task only when its `ready` field is true. |
| `outcome: "needs_user"` / `10` | A human-only login, device flow, consent, or similar action is required. | Stop. Surface `blocking_step` and `next_action` to a human; do not retry a browser login or invent credentials. |
| `outcome: "invalid_input"` / `11` | The input file, flag, capability, or step id is invalid. | Correct the named input and rerun. |
| `outcome: "unsupported_host"` / `12` | AQ refused the observed platform before changing it. | Stop and move to a supported WSL2/macOS path. |
| `outcome: "failed"` / `20` | A step could not complete. | Preserve the redacted step result and `next_action`, fix the named condition, then rerun. |

`steps[].state`, `blocking_step`, `next_action`, and `onboarding.readiness`
are stable, non-secret fields. The full payload contract and the complete list
of plan actions are in the [`aq install` reference](../reference/cli/install.md#machine-readable-output).

## Move portable defaults and profiles

After both installations are healthy and their daemons are running, move
reviewed AQ policy—not an AQ home directory—from one machine to another. A
portable bundle contains allowlisted tuning plus global agent profiles; it
excludes project vaults and memory, task/session history and logs, provider
credentials, secrets, and machine/repository paths.

On the source machine, inspect before writing a bundle:

```bash
aq system preview-portable-config
aq system export-portable-config --destination ~/aq-defaults.aqbundle
```

Copy `aq-defaults.aqbundle` by a secure channel. On the destination, validate
without changing it, then import with the default non-destructive conflict
policy:

```bash
aq system import-portable-config --source ~/aq-defaults.aqbundle --dry-run --conflict error
aq system import-portable-config --source ~/aq-defaults.aqbundle
```

The normal import policy is `keep`: existing configuration sections and
profiles remain in place. Use `--conflict replace` only after reviewing the
dry-run result and deciding that the destination's conflicting configuration
should be replaced. Some imported sections require a daemon restart; the
result names them. Re-derive resource limits for the destination hardware
after import:

```bash
aq system config tune --apply --overwrite
```

The detailed allowlist and tuning rationale are in [Default tuning](../guides/default-tuning.md#relationship-to-portable-bundles).

## Recovery, upgrade, and uninstall

Use the smallest lifecycle action that matches the problem. All installer
commands retain unrelated projects, provider credentials, and reused machine
software unless an explicit uninstall scope says otherwise.

| Situation | Command | What to expect |
| --- | --- | --- |
| Interrupted install or a completed login | `aq install` | Revalidates completed steps and resumes at the first unsatisfied one. |
| A host changed in a way the normal checks cannot verify | `aq install --repair` | Reconciles the recorded installation; does not delete resources. |
| Update AQ and make the version transition resumable | `aq install --upgrade` | Performs repair plus records an upgrade transaction before changing steps. |
| Inspect removal without changing anything | `aq uninstall --dry-run` | Shows what AQ owns, keeps, or leaves for manual removal. |
| Remove AQ runtime but keep data | `aq uninstall` | Stops AQ and removes its runtime records; configuration, data, and database stay. |

If the daemon already starts but a task or delivery needs diagnosis, continue in
the [operations guide](../guides/operations.md). For every installer-specific
failure, retain the redacted JSON result and follow the command reference's
[rerun, repair, database, and uninstall guidance](../reference/cli/install.md#rerunning-resuming-and-repair).

## Inputs and outputs

| Input | Why AQ needs it | Output / where to check |
| --- | --- | --- |
| A PostgreSQL server (or permission to install one) | Durable tasks, projects, sessions, and results | `aq doctor` and `aq status` report health. |
| A harness login | Lets a selected worker profile run its CLI | `aq agent list-profiles` shows available profiles; `aq agent check-profile <id>` checks one. |
| A project root (next tutorial) | Limits where AQ can create/link projects | `aq project list-roots` lists the approved roots. |

Shipped worker profiles are provider-explicit (`worker-standard-medium-claude`
and its siblings), and only the ones whose harness is installed *and*
authenticated are activated — `aq install` refreshes that eligibility on every
run, so a default route never points at a provider you do not have.

## State ownership

| State | Owner | Location |
| --- | --- | --- |
| AQ settings and PostgreSQL DSN | Operator / `aq install` | `~/.agent-queue/config.yaml` |
| Secrets the configuration refers to | Operator | `~/.agent-queue/.env`, mode `0600` |
| What the installer completed and owns | `aq install` | `~/.agent-queue/install-state.json` |
| Tasks, projects, sessions, results | AQ daemon | PostgreSQL |
| Provider credentials | The provider's own CLI | its own store (Keychain, `~/.claude/.credentials.json`, …) — AQ never reads or moves one |
| Repository source and commits | Your Git repository | Your configured project root |
| Worker edits | AQ worker | AQ-managed worktree and task branch |

Keep the configuration directory private because a database DSN can contain a
password. Never paste its contents, a `gh auth token` result, or a provider key
into an issue or task. A worker must never run Alembic or start/upgrade the
operator database; database upgrades are an operator action described in
[migrations](../guides/migrations.md).

## Common failures and recovery

| Symptom | Diagnose | Recovery |
| --- | --- | --- |
| The run stopped with `needs_user` | Read the named step and its `next:` line | Do the named thing — a login, a password prompt, a setting — and run `aq install` again. |
| No PostgreSQL server is reachable | `aq install --dry-run` | Rerun with `--with postgres-managed` to install one, or point `settings.postgres.host`/`port` at the server you already run. |
| The daemon did not come up | `~/.agent-queue/daemon.log`, then `aq doctor` | Fix what the log names (an unreachable database is the usual answer) and rerun `aq install`. |
| The configuration does not parse | The `config.check` step names the keys | `aq system config edit`, then rerun. Leave `messaging_platform: none` unless you selected Discord. |
| No worker can start | `aq agent list-profiles`, then `aq agent check-profile <id>` | Sign in to that profile's harness and rerun `aq install` to refresh eligibility. |
| The dashboard does not open | Check the summary's Dashboard line | A source checkout needs `npm -w dashboard run dev`; a release install serves it from the daemon. |
| You need to stop AQ | `aq stop` | This stops the daemon and its agent sessions. Use `aq restart` for a restart that re-adopts live sessions. |

## Related pages

* [`aq install` reference](../reference/cli/install.md) — every step, flag,
  setting, exit code and the JSON payload.
* [Your first isolated task](first-task.md) — creates a throwaway project and
  watches a worker finish it.
* [Project onboarding](../guides/project-onboarding.md) — explains roots,
  GitHub authentication, idempotency, and recovery in depth.
* [Default tuning](../guides/default-tuning.md) — what the installer wrote into
  your configuration, and how to override it.
* [Migrations](../guides/migrations.md) — safe database ownership boundaries.

## Source and tests

Implementation: [src/install/](../../src/install/) — the engine
([engine.py](../../src/install/engine.py)), the onboarding steps
([onboarding.py](../../src/install/onboarding.py)), the wizard
([wizard.py](../../src/install/wizard.py)) — and the command
[src/cli/install.py](../../src/cli/install.py). `setup.sh` is the contributor
bootstrap for a source checkout and hands machine setup to the same command.

Focused verification: `aq install --list-steps`, `aq install --dry-run`, and
`aq test tests/test_install_cli.py tests/test_install_onboarding.py
tests/test_install_wizard.py`.
