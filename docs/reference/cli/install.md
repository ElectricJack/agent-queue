# `aq install`

`aq install` prepares a machine to run AQ. It is the one common installer
interface: platform, packaging, database and provider adapters contribute
*steps* to it rather than shipping installers of their own, so a human and a
script see the same sequence, the same failure reports and the same exit codes
on every supported host.

It is the only `aq` command that expects **no running daemon** — it runs the
engine in-process and talks to nothing over the network.

> **Status.** The engine, platform matrix, built-in prerequisite steps,
> optional agent CLI steps, and the WSL2 and **macOS** bootstraps ship today.
> PostgreSQL installation remains adapter work; `aq install --list-steps`
> always prints what this build actually knows how to do **on the host you run
> it on** — the registry is composed for the detected host, so a Mac lists the
> `macos.*` steps and a WSL2 host does not.

## Usage

```bash
aq install                      # interactive: prompt before each change
aq install --dry-run            # report the plan, run only read-only checks
aq install --non-interactive --yes --json   # unattended, machine-readable
aq install --list-steps         # what this build can do, in run order
aq install --with provider.codex # select one optional agent CLI
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
with `--approve` and `--restart-from`.

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
