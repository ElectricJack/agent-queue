# AQ 0.1.0 installation release record

This is the release-acceptance record for the installer and onboarding work
assembled under `noble-apex`. It is a record of the `0.1.0` source release,
not a claim that an unobserved machine journey passed. A publisher must build
and tag the exact commit being distributed; the version in
[`pyproject.toml`](../../pyproject.toml) is the artifact version and the
installer reports it in both human and JSON output.

Start with the public [installation tutorial](../tutorials/install.md), then
continue to [the first-task tutorial](../tutorials/first-task.md). The command
and JSON contract live in the [`aq install` reference](../reference/cli/install.md).

## Release artifact and entry-point checks

The distribution has two intended entry points:

| Entry point | Intended artifact | Verification |
| --- | --- | --- |
| `aq` | The installed `agent-queue` Python distribution | `aq --version` reads installed package metadata; `aq install --list-steps --json` exposes the common installer contract. |
| `agent-queue` | The same installed distribution, running the daemon | The wheel includes runtime Markdown/JSON resources and the staged dashboard bundle; `/dashboard/` is served only after its digest manifest verifies. |
| Windows bootstrap | `scripts/install-windows.ps1` from the selected release source | Starts or reuses **Ubuntu 24.04 on WSL2**, then transports only to `scripts/install-wsl.sh`; the Linux script invokes the common `aq install` surface. |
| macOS | The installed `aq` distribution | The common installer discovers Homebrew rather than assuming an architecture-specific prefix. |

Build a release wheel from a clean selected release commit. This stages the
embedded dashboard and gives the bundle the same version as the wheel:

```bash
npm ci
python scripts/build_release_artifact.py
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
python -m pip install --force-reinstall dist/agent_queue-0.1.0-py3-none-any.whl
aq --version
aq install --list-steps --json
```

The final two commands must report `0.1.0` and a JSON step list. Do not use a
source checkout's `/dashboard` result as proof of the release dashboard: source
checkouts intentionally omit built assets and direct the operator to Vite.
`tests/test_release_artifact.py` verifies staging, manifest integrity, wheel
package-data declarations, both console-entry-point targets, and browser-route
serving. The release publisher should retain the wheel hash and the output of
the two commands with the release tag.

## Support matrix and evidence status

“Installer accepts this host” and “the complete journey has native release
evidence” are separate facts. The first is enforced by
[`src/install/platform.py`](../../src/install/platform.py); the second is
recorded below. A row marked **unmet** is an explicit release limitation, not
a pass by analogy.

| Host path | Product tier | Evidence available for this release | Release statement |
| --- | --- | --- | --- |
| Windows 10 build 19041+ or Windows 11 → WSL2 → Ubuntu 24.04 | supported path | Native Windows/WSL preflight, documented planner, WSL bootstrap tests, and cross-boundary dashboard reachability in [Windows/WSL validation](windows-wsl-onboarding.md). Clean install, repair, reboot/resume, browser login, and a live first task are **unmet** in that shared-worker record. | Use only Ubuntu 24.04 on WSL2 and keep AQ, PostgreSQL, repositories, and harnesses in its Linux filesystem. The Windows bootstrap is not a native-Windows AQ installer. Run the outstanding journey on a dedicated Windows host before treating it as full native acceptance. |
| macOS 14+ Apple Silicon | supported path | Six native GitHub-hosted runs record plan, install, rerun, repair, upgrade, restart-from, daemon and PostgreSQL phases in [macOS acceptance](../plans/install-onboarding/acceptance/macos.md). The record explicitly retains the unavailable clean-Homebrew, browser-login, reboot, interactive-wizard, live-agent and release-artifact rows. | Native Apple-Silicon shells only; Rosetta is refused. The run used a source checkout, so a published wheel still needs its own artifact-install evidence. |
| macOS 14+ Intel | compatibility | Two native Intel runs record the same installer lifecycle and actual `/usr/local` Homebrew prefix in [macOS acceptance](../plans/install-onboarding/acceptance/macos.md). The same unmet rows apply. | Compatibility only. Do not represent Homebrew's Intel support tier as an AQ guarantee. |
| Other Linux, older macOS, containers, remote hosts, WSL1, or native Windows | refused | Platform tests cover the detect-and-explain boundary. | No installation support claim; the installer must make no mutation and name the supported route. |

The macOS record was captured before the project-root readiness defect was
fixed. The current installer now reports that missing root as `needs_attention`
rather than claiming the first-task walkthrough is ready; the implementation
and focused coverage are in `src/install/onboarding.py`,
`src/install/wizard.py`, and `tests/test_install_onboarding.py`. Re-run the
native first-task phase after publishing the wheel to turn that historical
failure into native release evidence.

## Upgrade, repair, and removal

The public recovery path is intentionally discoverable from the installation
tutorial's [recovery section](../tutorials/install.md#recovery-upgrade-and-uninstall)
and the [`aq install` lifecycle reference](../reference/cli/install.md#rerunning-resuming-and-repair):

```bash
aq install             # resume/revalidate an interrupted run
aq install --repair    # reconcile recorded, installer-owned resources
aq install --upgrade   # repair and persist a resumable version transition
aq uninstall --dry-run # inspect ownership before removing anything
```

Repair and upgrade preserve the installer ownership record and user
customization; they do not run a worker migration or delete unrelated machine
software, credentials, projects, vault data, or databases. `aq uninstall`
removes only runtime records by default. Its separately confirmed destructive
scopes and its ownership boundary are documented in the
[`aq uninstall` reference](../reference/cli/uninstall.md).

## Acceptance evidence retained with the release

* Unit and integration-style installer coverage: `tests/test_install_*.py`,
  including lifecycle recovery, provider and login redaction, portable
  configuration, platform detection, and composed onboarding.
* Release artifact boundary: `tests/test_release_artifact.py`.
* Native Windows/WSL command outcomes and limitations:
  [Windows/WSL validation](windows-wsl-onboarding.md).
* Native macOS records and their durable per-run renderings:
  [macOS acceptance](../plans/install-onboarding/acceptance/macos.md).

This ledger is intentionally the release gate's handoff: it names the evidence
that exists, the commands a publisher must verify, and the machine journeys
that still need a dedicated environment. It must be updated for every release
that changes installer, platform adapter, provider flow, bundled dashboard, or
the public quickstart.
