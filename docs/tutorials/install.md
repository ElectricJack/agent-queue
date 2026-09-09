# Install and start Agent Queue

Install AQ as a background service that turns a task description into an
isolated coding-agent session against a Git repository. This page gets a new
operator to a healthy local daemon; [the next tutorial](first-task.md) creates
the first disposable project and task.

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

Use a current Linux machine or macOS host with Git, Docker (recommended for
the bundled PostgreSQL service), Python 3.12 or later, and `tmux`. Node.js and
`npm` are needed to launch the source-checkout dashboard. The installer
detects Python and offers apt/Homebrew installation; its supported branches are
Linux and macOS ([setup.sh](../../setup.sh)).

Windows is not a native target in this installer. Use a Linux environment such
as WSL2, keep Docker and Git available *inside that environment*, and run AQ,
its database connection, and the harness from the same side of the boundary.
Do not mix a Windows path with a Linux daemon path. If a harness must run on
another host, configure and operate that deployment as a separate AQ host.

PostgreSQL is required. SQLite is not a current runtime option; an older
SQLite database can only be imported through the setup migration path
([database validation](../../src/config.py)). The development compose file
ships PostgreSQL on host port `5533` ([docker-compose.yml](../../docker-compose.yml)).

## A realistic first installation

The following commands are suitable for a disposable local trial. They were
checked against `setup.sh`, `aq start --help`, and `aq status --help` in this
checkout. Clone AQ wherever you normally keep tools; the example repository
comes in the next tutorial.

```bash
git clone <AQ-REPOSITORY-URL> agent-queue
cd agent-queue
docker compose up -d postgres
./setup.sh
```

```text
Using python3.12 (Python 3.12…)
Installing typed API client (packages/aq-client)...
Installing agent-queue and dependencies (dev + cli + gemini)...
…
agent-queue setup wizard
```

The exact Python patch version, download output, and prompts vary. The
installer creates `.venv`, installs the `aq` and `agent-queue` commands, and,
when `npm` is present, installs dashboard dependencies. It then starts the
interactive wizard ([setup.sh](../../setup.sh), [wizard](../../src/setup_wizard.py)).

In the wizard:

1. Choose or enter a PostgreSQL DSN. For the compose service, the offered
   local DSN is `postgresql://agent_queue:agent_queue_dev@localhost:5533/agent_queue`.
2. Provide the requested Discord values only if you want the currently
   shipped digest/escalation transport. The wizard presently asks for them,
   but Discord is not how you create, review, or control tasks.
3. Authenticate a harness. For Claude, use `claude login` or an Anthropic key.
   The wizard stores entered service secrets in `~/.agent-queue/.env` with
   mode `0600`; it writes configuration to `~/.agent-queue/config.yaml`.
4. Confirm that `tmux` and the CLI chosen by your worker profile are on the
   daemon host's `PATH`.

> **Current implementation detail.** The wizard's menu still calls Codex
> “not yet implemented,” but AQ's session layer has shipped `claude`, `codex`,
> and `gemini` harness definitions ([probe](../../src/setup_wizard.py),
> [session specification](../../src/sessions/spec.py)). Treat that menu text
> as a wizard limitation: use `aq agent list-profiles` after startup and
> install/authenticate the CLI named by the profile you select. Shipped default
> worker profiles are Claude profiles; a Codex or Gemini profile is a local
> configuration choice, not a claimed shipped default.

Start AQ after the wizard exits:

```bash
aq start
aq status
```

```text
… daemon started …
… system status overview …
```

`aq start` may offer to launch the dashboard from a source checkout. Accept
the prompt or start it with `npm -w dashboard run dev`; the dashboard's source
development port is `5173` ([daemon CLI](../../src/cli/daemon.py)). Open the
local URL it reports in a browser. The daemon's command API is a separate
local service; use `aq` rather than guessing its port.

## Inputs and outputs

| Input | Why AQ needs it | Output / where to check |
| --- | --- | --- |
| PostgreSQL DSN | Durable tasks, projects, sessions, and results | `aq doctor` and `aq status` report health. |
| A harness login or provider credential | Lets a selected worker profile run its CLI | `aq agent list-profiles` shows available profiles; `aq agent check-profile <id>` checks one. |
| Optional Node.js install | Runs the source-checkout dashboard | Browser at the URL reported by `aq start`. |
| A project root (next tutorial) | Limits where AQ can create/link projects | `aq project list-roots` lists the approved roots. |

The default worker profile selection prefers `worker-standard-medium-claude`
when it is installed ([default selection](../../src/profiles/default_selection.py)).
That is a shipped default; a project's configured default profile or a
task-level `--profile` is local policy and wins when provided.

## State ownership

| State | Owner | Location |
| --- | --- | --- |
| AQ settings and PostgreSQL DSN | Operator / setup wizard | `~/.agent-queue/config.yaml` |
| Entered service secrets | Operator / setup wizard | `~/.agent-queue/.env`, mode `0600` |
| Tasks, projects, sessions, results | AQ daemon | PostgreSQL |
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
| Cannot connect to PostgreSQL | `docker compose ps`, then `aq doctor` | Start the compose service or correct the DSN; re-run the wizard. |
| No worker can start | `aq agent list-profiles`, then `aq agent check-profile <id>` | Install/login to that profile's harness and ensure `tmux` is on `PATH`. |
| Dashboard does not open | Check the `aq start` prompt/log output | Install Node/npm, run the dashboard command from the source checkout, or continue with the CLI. |
| Project onboarding says no roots | `aq project list-roots` | Add a readable/writable `project_roots` entry through Settings or `aq system config edit`; see the next tutorial. |
| You need to stop AQ | `aq stop` | This stops the daemon and its agent sessions. Use `aq restart` for a restart that re-adopts live sessions. |

## Related pages

* [Your first isolated task](first-task.md) — creates a throwaway project and
  watches a worker finish it.
* [Project onboarding](../guides/project-onboarding.md) — explains roots,
  GitHub authentication, idempotency, and recovery in depth.
* [Worker pools](../guides/worker-pools.md) — explains pull-based workers once
  you are operating more than one.
* [Migrations](../guides/migrations.md) — safe database ownership boundaries.

## Source and tests

Implementation: [setup.sh](../../setup.sh), [src/setup_wizard.py](../../src/setup_wizard.py),
[src/cli/daemon.py](../../src/cli/daemon.py),
[src/config.py](../../src/config.py), and
[src/profiles/default_selection.py](../../src/profiles/default_selection.py).

Focused verification: `aq start --help`, `aq stop --help`, `aq status --help`,
`aq agent list-profiles --help`, and `aq test tests/test_setup_wizard.py`.
