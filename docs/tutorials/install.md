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
```

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
  2. Create your first project and task: `aq project onboard --help`, or follow
     docs/tutorials/first-task.md.
  3. `aq doctor` checks this installation whenever something looks wrong.
```

Open that URL in a browser. A **source checkout** ships no built dashboard, so
the summary gives you the development command instead — run
`npm -w dashboard run dev` in the checkout and open `http://localhost:5173`.

The install outcome and first-task readiness answer different questions. You
can deliberately finish an installation with every provider skipped, but AQ
will then show **needs attention** for agent authentication and profile routing
instead of inviting you to create a task that cannot run. Follow the named
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
