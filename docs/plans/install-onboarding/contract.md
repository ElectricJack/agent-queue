# Installation and onboarding contract

**Status: planned contract for `noble-apex`; not a claim that the described
installer ships today.** The current, development-checkout path remains
[`docs/tutorials/install.md`](../../tutorials/install.md). This contract is the
handoff boundary for the implementation tasks in this plan.

## The one newcomer path

AQ will have one supported path from a fresh machine to a first task:

1. On Windows, install and enter the supported WSL2 distribution; on macOS,
   open a supported native shell. The installer runs entirely on that host.
2. Use the release-provided bootstrap for the requested AQ version. The
   bootstrap verifies the selected artifact and executes **one** common
   installer interface, `aq install`; it does not contain an independent
   platform installer.
3. Run `aq install --interactive`. It detects prerequisites, asks only for
   choices that cannot be inferred, installs or reuses installer-owned
   resources, and writes a non-secret resume record.
4. Select zero or more harnesses. For each selected harness, the installer
   stops at a human login checkpoint rather than attempting to enter a
   password, token, or browser consent. A user may skip a harness and return
   later.
5. Complete the readiness summary, start the local daemon/dashboard, then use
   the existing **Add project** flow or `aq project onboard` and the first-task
   tutorial. Project onboarding is deliberately after machine installation.

The same workflow is available to automation as `aq install --non-interactive
--config <install-input.yaml> --json`. It must neither read a terminal nor
open a browser. A missing human credential produces `needs_user` rather than
silently choosing an authentication method. A CI or headless user supplies a
provider's supported environment-based credentials before invoking it.

This makes the release artifact and `aq install` the public installation
surface. `setup.sh` stays a contributor convenience for a source checkout; it
is not the user-facing installer, package manager, or upgrade mechanism.

## Supported-platform matrix

The installer must reject an unsupported host before changing it, and report
the observed OS, version, architecture, distro, and WSL generation in its
machine-readable result. "Supported" below means a clean-install, rerun,
repair, and first-task journey has native acceptance evidence; it is not a
promise based only on a unit-test mock.

| Host path | Supported baseline | Architectures | Installer policy | Explicit boundary |
| --- | --- | --- | --- | --- |
| Windows via WSL2 | Windows 11, or Windows 10 version 2004 / build 19041 or newer, with **WSL2** and Ubuntu 24.04 LTS | x86_64, arm64 | Windows bootstrap may install or select WSL; every AQ process, PostgreSQL client, project checkout, and harness then runs inside the same Linux distribution. | WSL1, native-Windows AQ, and Windows-path project checkouts are unsupported. Ubuntu 22.04, Debian, and imported distros are detect-and-explain until they have their own native evidence. |
| macOS, Apple Silicon | macOS 14 (Sonoma) or newer | arm64 | Fully supported; discover Homebrew at its actual prefix and use the invoking user's shell. | No Rosetta-based primary installation. |
| macOS, Intel | macOS 14 (Sonoma) or newer | x86_64 | Compatibility tier: the common installer must work without assuming `/opt/homebrew`; native acceptance must be recorded separately for each release. | Do not represent Homebrew's Intel tier as an AQ guarantee; use a release runtime or another verified dependency route when Homebrew cannot meet the contract. |
| Other Linux, older macOS, containers, remote hosts | — | — | Detect, explain the unsupported field, make no mutation, and point to the supported path. | No best-effort success label. |

Microsoft documents `wsl --install` for Windows 10 version 2004/build 19041+
or Windows 11 and says new installations default to WSL2; it also recommends
keeping files in the Linux filesystem when the tools run there.
[Microsoft WSL installation guidance](https://learn.microsoft.com/en-us/windows/wsl/install)
and [WSL development-environment guidance](https://learn.microsoft.com/en-us/windows/wsl/setup/environment)
are the authority for the Windows side. Homebrew's current installation
guidance requires macOS 14+ for its supported Apple-Silicon path, uses
`/opt/homebrew` on Apple Silicon and `/usr/local` on Intel, and classifies
Intel as Tier 3; hence the contract does not equate "brew happened to run" with
AQ support. [Homebrew installation requirements](https://docs.brew.sh/Installation)
are the authority for that adapter's limits.

## Installer protocol

The engine task owns a small, stable step protocol. Each step has an immutable
`id`, input-schema version, dependency list, adapter owner, redacted summary,
and one terminal state:

| State | Meaning | Rerun behavior |
| --- | --- | --- |
| `succeeded` | The step's declared observable condition is true. | Revalidate; do not repeat a destructive action. |
| `skipped` | The optional capability was declined or is irrelevant. | Preserve the explicit reason and allow a later selected rerun. |
| `needs_user` | A browser, device code, privilege prompt, or other human-only action is required. | Resume after the named check succeeds; never persist a secret or claim authentication. |
| `failed` | A prerequisite or action failed. | Preserve the action id, sanitized diagnostic, remediation, and retryability. |

The durable resume record contains installer version, target version, platform
facts, selected capabilities, completed states, owned-resource identifiers,
and redacted diagnostics. It contains no provider credential, raw DSN
password, OAuth/device code, shell history, project path, or task data. A
rerun is the normal recovery mechanism: it recomputes preconditions, reuses
matching owned resources, and stops at the first unsatisfied step. Ownership is
monotone: a resource recorded as created by AQ stays owned no matter how many
later steps or later runs observe it already in place, because a step that
finds AQ's own handiwork present is not looking at something the host brought. `--resume`
selects the latest compatible record; `--restart-from <step>` invalidates that
step and its dependents only. The engine must expose the same records through
human output and `--json`, with stable result/exit classifications for
`ready`, `needs_user`, `invalid_input`, `unsupported_host`, and `failed`.

Interactive mode may request consent before installing a package, starting a
service, changing shell initialization, or creating an AQ-owned database role.
Unattended mode requires those choices in its input file or explicit flags;
there are no implicit package installs, shell edits, destructive upgrades, or
provider choices. Both modes use the same engine and adapters.

## Human authentication checkpoints

The provider adapter owns installation detection/version checks and a
non-secret readiness probe. The installer owns orchestration, redaction,
resume, and the distinction between executable and authenticated. The provider
owns its login flow and credential store.

| Harness | Installer checkpoint | Headless contract | Verification boundary |
| --- | --- | --- | --- |
| Claude Code | After a compatible `claude` is present, tell the user to run its supported login flow and return only after a non-secret readiness probe. | Use the provider-supported credential configuration; do not emulate browser login. | Anthropic documents browser-backed Console/Claude-app authentication and says credentials are stored by Claude Code. [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started) |
| Codex | After `codex` is installed, present `codex --login` as a human checkpoint or accept an already configured supported API credential. | Require the supported API-key environment configuration; never put it in the installer state or output. | OpenAI documents `codex --login` with ChatGPT and separately documents the CLI install/API-key path. [Codex sign-in](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt), [Codex CLI getting started](https://help.openai.com/en/articles/11096431) |
| Gemini CLI | Start the CLI's supported interactive Google-login selection, then verify without reading its credential cache. | Require Gemini's documented API-key or Vertex environment setup; return `needs_user` if neither is present. | Gemini documents browser/localhost Google login, API-key and Vertex alternatives, and a headless failure when no suitable environment credential exists. [Gemini authentication](https://google-gemini.github.io/gemini-cli/docs/get-started/authentication.html) |

Provider install commands and authentication methods must be rechecked against
those official pages by the provider-install and login tasks immediately before
implementation. The contract intentionally does not freeze third-party
commands in AQ source.

## Ownership and page boundaries

| Concern | Current owner / source | Successor owner | Boundary |
| --- | --- | --- | --- |
| Checkout setup | `setup.sh` and `src/setup_wizard.py` | `noble-apex.2` engine, then `.3`/`.4` platform adapters | Keep legacy setup compatible until the release installer replaces it; do not make it a second product installer. |
| Release runtime and dashboard assets | `pyproject.toml` currently defines Python console scripts; dashboard setup is checkout-local | `noble-apex.5` | Deliver a verified, versioned artifact that can execute the common installer without a repository checkout. |
| PostgreSQL | Legacy wizard plus daemon-owned schema setup | `noble-apex.6` | Install/reuse only AQ-owned resources; schema migration is daemon/operator authority, never a worker action. |
| Provider executables and credentials | Legacy wizard probes Claude and reports harness binaries | `noble-apex.7` and `.8` | Provider adapters are pluggable; the engine never parses or exports credentials. |
| Configuration and profile eligibility | `src/config.py`, profile catalog, and vault | `noble-apex.9` and `.10` | Version and validate portable, non-secret settings; preserve user customization. |
| Installation wizard | Legacy terminal wizard | `noble-apex.11` | Owns machine setup only. It must not absorb project selection. |
| Project onboarding UI/service | `dashboard/src/pages/project/onboarding/ProjectOnboardingWizard.tsx`, its step registry, and `src/commands/project_onboarding_commands.py` | Existing project-onboarding owners | **Add project** remains the page for link/init/clone beneath configured roots, after readiness. |
| Readiness, sample project, repair/upgrade | `aq doctor` reports general installation health today | `noble-apex.12` and `.13` | Readiness is an explicit aggregate result, not a process-started message. Repair and upgrade use the owned-resource record. |
| Human-facing documentation | Current tutorials/guides are owned by the completed documentation hierarchy | `noble-apex.15` | Update current pages only when behavior ships. This file is historical plan/contract material; it must never be linked as current instructions. |

## Readiness definition

The installer may report **ready** only when each selected, required condition
is measured, named, and emitted in its result:

1. The supported-host check passed and the executable/version is the requested
   release.
2. Configuration parses and its secret references resolve without displaying
   secret values.
3. PostgreSQL is reachable; the AQ database and role are usable; the
   daemon/operator has confirmed the schema state.
4. The daemon responds and the dashboard endpoint is reachable when the
   dashboard was selected.
5. `tmux`, Git, and every selected harness binary are available on the daemon
   host; each selected harness is independently marked `authenticated` or
   `needs_user`.
6. At least one selectable worker profile routes only to an available,
   authenticated harness; configured project roots are readable and writable
   where project creation needs them.
7. A disposable-project walkthrough can create/onboard a repository, submit a
   deliberately small task with stated cost implications, observe a claim and
   result, and clean the project up.

`aq doctor` remains the operator diagnostic surface and already covers such
building blocks as configuration parsing, database reachability/migration
state, and harness binaries. The readiness task must compose those facts and
add the missing profile-routing, dashboard, and first-task evidence; it must
not reinterpret a daemon PID as readiness.

## Upgrade, repair, and compatibility rules

* The installer creates a versioned backup of mutable AQ configuration before
  changing it, validates a proposed configuration before replacement, and
  preserves unknown/user-owned fields.
* Repeated `install`, `repair`, and compatible `upgrade` calls are idempotent:
  they reconcile installer-owned records and never delete unrelated package
  managers, databases, roles, repositories, project vaults, or credentials.
* An interrupted upgrade resumes or rolls back only resources named in the
  ownership record. A version-incompatible record requires an explicit repair
  plan, not automatic deletion.
* Database schema work is delegated to the daemon/operator installation path.
  Installer, pool-worker, and project-onboarding paths never run an arbitrary
  Alembic upgrade against an operator database.
* Uninstall defaults to removing only the AQ runtime/service and installer
  records. Removing data, a database, a role, or provider credentials is an
  explicit, separately confirmed destructive request.

## Acceptance evidence for downstream tasks

Each platform adapter records a native evidence row containing host version,
architecture, WSL/distro facts where applicable, installer version, selected
providers, command/result identifiers, a rerun, a repair or resume, and the
first-task outcome. Mocks can cover adapter and protocol branches, but cannot
substitute for the matrix's native support evidence. Failed or unavailable
native runs stay visible as unmet evidence rather than expanding the matrix by
assertion.
