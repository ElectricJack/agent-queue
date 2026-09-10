# Testing the installer

How `aq install` is tested without a Windows machine, a Mac, a PostgreSQL
server, a provider account or a browser — and, just as importantly, which
claims that testing is **not** allowed to make.

If you are changing an installer adapter, read [what runs where](#what-runs-where)
and run the two commands at the bottom. If you are signing off a platform, read
[what still needs a real machine](#what-still-needs-a-real-machine): those rows
are the acceptance evidence, and no amount of green CI substitutes for them.

## Why this page exists

The installer is the one part of AQ whose job is to change a machine. That
makes it the part with the worst test/reality ratio: the interesting behaviour
is `apt-get install`, `brew services start`, a browser login and a daemon that
answers a port, and none of that can run in CI without either doing real damage
or proving nothing.

AQ's answer is a seam, not a mock library. Every adapter reaches the host
through an injected callable — a command runner, a `which`, a TCP probe, a
database connector, an HTTP probe — declared as a constructor argument with a
real default. The suites replace those five callables and nothing else, so what
runs in a test is the real engine, the real steps, the real ordering and the
real result vocabulary. What is faked is only the host.

The consequence is worth stating plainly: **a green installer suite proves the
installer's decisions, not the platform's behaviour.** It proves that a port
conflict stops the run before the daemon starts. It cannot prove that
`brew services start postgresql@17` works on macOS 15.

## What runs where

| Suite | What it composes | What it is about |
| --- | --- | --- |
| `tests/test_install_engine.py` | one toy registry | ordering, consent, resume, redaction, exit codes |
| `tests/test_install_platform.py` | nothing | the support matrix and host facts |
| `tests/test_install_macos.py` | the macOS adapter | Homebrew prefixes, shell/PATH, runtime services |
| `tests/test_install_postgres.py` | the engine + the database adapter | packages, roles, credentials, rotation, boot |
| `tests/test_install_providers.py` | one provider step | detection and the documented installer |
| `tests/test_install_logins.py` | the login steps | installed vs authenticated, headless routes |
| `tests/test_install_onboarding.py` | the engine + the onboarding steps | config, Discord, daemon, dashboard |
| `tests/test_install_lifecycle.py` | a toy registry | repair, upgrade, uninstall planning |
| `tests/test_install_cli.py` | the CLI | flags, input file, JSON payload, exit codes |
| `tests/test_install_integration.py` | **the registry `aq install` runs** | what happens *between* adapters |

The last one is the subject of this page. It uses `tests/installer_machine.py`,
one scripted machine that sits behind every seam at once, and asserts observable
outcomes only: the exit code, which commands ran and in what order, what the
resume record owns, what a following `aq uninstall` would remove.

The journeys it covers, each end to end on the composed registry:

* **Fresh install** — a bare WSL2 box reaches a ready daemon through every
  adapter, and the daemon is started only after the database answers.
* **Rerun** — nothing is installed twice, no statement recreates a role or a
  database, and the owned-resource set is identical.
* **Restart** — `--restart-from` replays one branch and carries the other
  forward.
* **Interruption** — a daemon that will not start fails the run, keeps the
  database work in the record, and the next run resumes into a ready daemon. An
  upgrade killed halfway resumes with its version transition intact.
* **Existing PostgreSQL** — a server somebody else installed is reused, owned by
  nobody, and a later uninstall removes none of it.
* **Port conflict** — something else on 5432 stops the run in the database
  branch, names the port, and never starts a daemon.
* **Missing provider** — a selected CLI is installed by its documented
  installer; one whose installer fails stops the run with the provider's own
  documentation; an unselected one is skipped and blocks nothing.
* **Authentication failure** — an installed but unauthenticated harness stops at
  the human with exit code 10, and the rerun after the login continues into a
  ready daemon without reinstalling the CLI.
* **Default activation** — only providers that are actually authenticated
  activate worker profiles, a later login adds its own without overwriting an
  edited profile, and `aq install` writes the activation record beside the
  resume record.
* **Export exclusions** — the configuration a real run wrote exports as a
  portable bundle with the database section and the credential excluded.

## What is faked

Each row is a seam, not a patched function: the production default is the real
thing, and the test passes something else in.

| Step | The real host access | What the suite injects |
| --- | --- | --- |
| `prereq.git` | `shutil.which` | a lookup over a scripted PATH |
| `prereq.tmux` | `shutil.which` | the same lookup |
| `postgres.package` | `apt-get` / `brew` through `subprocess` | a command runner over a scripted package set |
| `postgres.service` | `systemctl` / `brew services` | the same runner |
| `postgres.server` | a TCP connect and an `asyncpg` connection | a probe and an in-memory server |
| `postgres.role` | `psql` as an administrator | the same runner and server |
| `postgres.credentials` | a `0600` file and `config.yaml` under `~/.agent-queue` | the same writes under a temporary root |
| `postgres.boot` | `systemctl enable` | the command runner |
| `provider.claude-cli` | the provider's own installer script | a runner that records the command |
| `provider.claude-login` | `claude auth status` and a credential store | the runner and a file under a temporary home |
| `daemon.start` | `aq start` and an HTTP `/health` probe | a command runner and an HTTP probe |
| `daemon.dashboard` | an HTTP probe of `/dashboard` | the same probe |

Two properties are asserted mechanically rather than trusted:

* **The seams are the only host access.** One test replaces
  `subprocess.run`, `subprocess.Popen`, `socket.create_connection` and
  `urllib.request.urlopen` with a refusal and runs a full install; it must still
  reach `ready`.
* **Nothing resolves to the operator's machine.** Every path — AQ's install
  home, the user's home, the provider credential stores — is under the test's
  own `tmp_path`. No installer test reads a real credential, and no installer
  step ever runs a migration: schema work belongs to the daemon, and
  `tests/test_install_postgres.py` pins that separately.

## What still needs a real machine

Everything below is invisible to the seams above by construction. These are the
rows a platform acceptance run has to fill in with real versions, real commands
and real output; a cross-platform mock is not evidence for any of them.

macOS now has a harness for exactly that. `scripts/acceptance/macos_acceptance.py`
runs the documented journey unattended on a Mac and writes one JSON evidence
record plus its Markdown rendering; `.github/workflows/macos-acceptance.yml`
runs it on GitHub's hosted macOS runners on both architectures the matrix names
and uploads the record. The rows it filled in, and the ones it could not, are in
[the macOS acceptance evidence](../plans/install-onboarding/acceptance/macos.md).
The script refuses to run anywhere but Darwin: a run on Linux is not evidence
for the macOS row and the harness will not pretend otherwise.

### Windows and WSL2

* WSL2 itself: the Windows entry point, `wsl --install`, distribution detection,
  and the reboot-and-resume cycle around it.
* Elevation: what a real UAC prompt and a real `sudo` password prompt do to an
  interactive run, and to an unattended one.
* Filesystem placement: an install on `/mnt/c` versus the Linux filesystem, and
  the performance and permission consequences.
* `systemd` inside WSL: whether the distribution has it, and whether
  `postgres.boot` survives a Windows restart and a `wsl --shutdown`.
* Windows-to-WSL networking: reaching the dashboard from a Windows browser.
* The real Ubuntu package: `apt-get install postgresql` version, cluster
  creation, socket location and peer authentication.

### macOS

* Homebrew for real, on both prefixes (`/opt/homebrew`, `/usr/local`), including
  a machine that has never had it and a machine whose Homebrew is stale.
* Apple Silicon and Intel, including Rosetta shells, as separate runs.
* `brew services` as the process manager: start, restart, and survival across a
  reboot and a logout.
* The Keychain-backed provider credential store, which the probe deliberately
  cannot read — only `claude auth status` answers there.
* Shell and PATH: what a new terminal actually resolves after the install, under
  `zsh` and under `bash`.

### Every platform

* A real browser login for each provider, including the device-code and headless
  routes the installer prints.
* A real `aq start` against a real PostgreSQL, and a dashboard opened in a
  browser.
* Timing: package installs and first-boot database initialisation that take
  minutes, and the timeouts around them.
* Failure modes nobody scripts: a disk that fills, a proxy that intercepts TLS,
  a corporate MDM that blocks an installer.

Record what was run, the OS and tool versions, and the observed result. An
environment that was not available is recorded as *unmet evidence* — never as a
pass by analogy from another platform. The macOS harness does this itself: every
row above that one unattended run cannot produce is written into its record as
`unmet` with the reason, so the gap stays visible instead of disappearing.

## Known gaps this coverage found

* On a fresh managed install the PostgreSQL adapter creates `config.yaml` (it
  writes the database URL into it), so the later onboarding step sees a file
  that already exists and records it as *not owned*. `aq uninstall
  --remove-config` therefore keeps AQ's own configuration and reports it as
  "found on this host and reused". Filed as `noble-apex.22`; the integration
  suite asserts the resources each adapter records, not this classification.

## Running it

```bash
aq test tests/test_install_integration.py
aq test tests/test_install_engine.py tests/test_install_postgres.py \
        tests/test_install_onboarding.py tests/test_install_cli.py
```

And on a Mac you are willing to have changed — a disposable machine, a fresh VM
or a CI runner, because the journey installs Homebrew formulae, starts a
PostgreSQL service and writes `~/.agent-queue`:

```bash
python3 scripts/acceptance/macos_acceptance.py --output ./macos-acceptance
```

Neither needs PostgreSQL, a network or a provider account. If a test in this
area starts wanting one, that is the signal that a seam is missing — add the
injection point rather than the dependency.
