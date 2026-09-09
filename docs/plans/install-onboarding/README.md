# Install & Onboarding

AQ epic: pending creation. Prerequisite: **solid-grove**, the complete GitHub documentation epic.

Deliver one straightforward installation path on Windows through WSL2 and on macOS, from a fresh machine to a working first task. This plan specifies implementation work; it does not claim these capabilities already ship.

The installer offers PostgreSQL setup and selected agent CLIs (Claude Code, Codex, Gemini CLI), including supported interactive login. Curated exportable configuration and profiles exclude project memory, credentials and machine-specific data. Enable only usable profiles for the installed platform and providers.

All child tasks use standard-high with no pinned profile. The foundation waits for solid-grove and every other child depends transitively on it. Native platform acceptance is required; local mocks alone are insufficient. No additional review-ticket workflow is introduced.

## Task breakdown

| Key | Task | Depends on |
| --- | --- | --- |
| foundation | Define seamless installation and onboarding contract | solid-grove |
| engine | Build resumable installer and prerequisite detection | foundation |
| wsl | Implement Windows and WSL2 installation bootstrap | engine |
| mac | Implement macOS installation bootstrap | engine |
| packaging | Ship installable AQ runtime and dashboard artifacts | engine |
| postgres | Install and configure PostgreSQL end to end | engine |
| agents | Offer installation and configuration of agent CLIs | engine |
| logins | Guide agent login and verify authentication | agents |
| portable | Export and import portable configuration and agent profiles | foundation |
| tuning | Ship excellent resource-aware default tuning | portable |
| profiles | Enable agent profiles according to installed platforms and providers | logins, tuning |
| wizard | Unify installation into a short onboarding wizard | wsl, mac, packaging, postgres, profiles |
| first-task | Verify readiness and guide the first project and task | wizard |
| lifecycle | Provide installation repair, upgrade and uninstall paths | wizard |
| docs | Write clear human and agent installation guides | first-task, lifecycle |
| tests | Add focused installer integration and recovery coverage | first-task, lifecycle |
| accept-wsl | Validate complete onboarding on native Windows and WSL | docs, tests |
| accept-mac | Validate complete onboarding on native macOS | docs, tests |
| release | Publish coherent installation release and final onboarding results | accept-wsl, accept-mac |

## Acceptance criteria by task

### Define seamless installation and onboarding contract

Audit setup.sh, src/setup_wizard.py, packaging, doctor, configuration and project onboarding. Define one obvious newcomer path, supported WSL2 distributions and macOS versions/architectures, install ownership, step interfaces, page ownership and upgrade compatibility. Reuse the completed documentation hierarchy.

- Publish platform matrix, happy-path walkthrough and measurable readiness criteria.
- Define interactive and unattended contracts, human login checkpoints, retry/resume behavior and adapter ownership.

### Build resumable installer and prerequisite detection

Implement shared installer orchestration with platform detection, structured step results, prerequisite checks, progress, sensible defaults, dry-run and noninteractive options. Track installed resources and completed steps without storing secrets.

- Rerunning and resuming after a failed step is safe and does not duplicate resources.
- Failures identify the exact step and actionable recovery; machine-readable output and exit codes are documented.

### Implement Windows and WSL2 installation bootstrap

Provide a simple Windows entry point and WSL-side installation path. Handle WSL2/distro detection, required Windows actions, reboot/resume, Linux filesystem placement, shell/PATH, browser handoff and Windows-to-WSL networking.

- A new Windows user can reach AQ installation without prior WSL knowledge.
- Existing WSL installations are reused; required elevation and reboot are clearly explained and resumable.

### Implement macOS installation bootstrap

Support Apple Silicon and Intel within the declared support matrix. Detect/install prerequisites through supported tooling, handle Homebrew locations, shell/PATH and runtime services without modifying system Python.

- Clean supported macOS installs and existing developer machines reach the same working result.
- Missing tools, permissions and architecture mismatches produce actionable results.

### Ship installable AQ runtime and dashboard artifacts

Make user installation independent of a development checkout. Package the runtime, CLI, built dashboard and required resources; define version selection, integrity verification and supported update behavior.

- Installed aq CLI and dashboard work without building the repository manually.
- Published artifacts contain required runtime assets and report their installed version.

### Install and configure PostgreSQL end to end

Offer managed PostgreSQL installation or reuse of a compatible existing instance. Configure dedicated role/database, protected credentials, connection checks, service lifecycle, reboot startup and port conflicts. Schema setup belongs to daemon/operator installation, never worker sessions.

- Fresh installation reaches a healthy database without manual SQL.
- Existing databases and unrelated roles are preserved; failures and credential changes have a documented recovery path.
- Verify database startup and AQ reconnection after restart on both supported platforms.

### Offer installation and configuration of agent CLIs

Implement an extensible provider installer registry for Claude Code, Codex and Gemini CLI, with optional additional supported providers. Verify current official installation methods during implementation. Detect existing installations, versions and PATH; let users select or skip providers.

- Selected CLIs install or reuse a compatible installation and pass executable checks.
- Unselected providers are optional and do not block AQ installation.

### Guide agent login and verify authentication

Guide supported browser/device login and API-key configuration where supported using official provider flows. Explain human interaction checkpoints, support retry and headless instructions, and distinguish installed from authenticated. Keep credentials in supported protected local stores.

- Each initial supported provider has a tested authentication readiness check and actionable failure instructions.
- Tokens/passwords never appear in logs, task records or exported defaults; no unsupported automated password entry.

### Export and import portable configuration and agent profiles

Create a versioned allowlisted bundle for reusable AQ tuning and global agent profiles. Normalize paths and validate imports. Exclude project memory, project vault content, task/session history, logs, provider credentials, secrets and personal repository paths. Preview and curate the working configuration before using it as a shipped default.

- Export/import roundtrip preserves portable settings and profile relationships.
- Automated exclusion tests demonstrate that project memory and credentials cannot enter the bundle.
- Import offers clear conflict handling and preserves existing user customization.

### Ship excellent resource-aware default tuning

Curate production-informed portable defaults for scheduling, resource/test slots, routing, retry/recovery, integration and quiet idle behavior. Use standard-high for normal work and deep-high only for exceptional difficulty. Scale concurrency to available resources and avoid obsolete per-task/final-review defaults.

- New users receive usable defaults without copying operator-specific projects, memory or machine paths.
- Document tuning rationale and overrides; verify that resource limits and routing remain usable on smaller machines.

### Enable agent profiles according to installed platforms and providers

Ship a reusable profile catalog and activate only profiles compatible with the OS and installed/authenticated provider CLIs. Distinguish installation availability from readiness; refresh activation after provider changes while preserving user choices.

- No default route points to an unavailable provider or unusable pool.
- Installing, authenticating or removing a provider refreshes eligibility predictably; missing providers have clear setup guidance.

### Unify installation into a short onboarding wizard

Compose installer steps into a single clear experience with good defaults, optional advanced configuration, terminal and machine-readable modes, and resumable authentication. Remove obsolete onboarding assumptions; Discord is optional. Explain where AQ stores data and how to open the dashboard.

- A newcomer follows one documented entry point from prerequisites through a ready daemon/dashboard.
- Skipping optional providers or Discord does not fail setup; interruption can resume without starting over.

### Verify readiness and guide the first project and task

Add a readiness summary covering database, daemon, dashboard, agent authentication, profile routing and workspace prerequisites. Guide creation of a disposable first project and task through actual configured integration, with an explicitly chosen small live-agent demonstration and clear cost implications.

- Readiness does not claim success just because a process started.
- A user can observe a task claim, execution and integrated result, then clean up the sample project.

### Provide installation repair, upgrade and uninstall paths

Implement supported repair/update/uninstall behavior using installer-owned resource records. Recover interrupted upgrades, preserve projects/configuration and PostgreSQL data by default, and clearly identify optional destructive removal.

- Repeated repair and upgrade preserve working customization and data.
- Uninstall removes owned runtime resources without deleting unrelated software or databases.

### Write clear human and agent installation guides

Update the documentation epic outputs to match the shipped installer. Provide WSL and macOS quickstarts, prerequisites, exact commands, expected output, login checkpoints, unattended configuration, defaults export/import and recovery. Maintain one authoritative command source where feasible.

- A human new to AQ can complete install and first task without internal architecture knowledge.
- An agent can discover options, interpret structured outcomes and stop for required human login steps using clear instructions.
- GitHub navigation, README and troubleshooting links point to current platform guides.

### Add focused installer integration and recovery coverage

Test adapters and installer contracts using disposable environments and mocked external/provider interactions. Cover fresh install, rerun, interrupted steps, existing PostgreSQL, port conflicts, missing providers, auth failures, default activation and export exclusions. Use resource-aware focused test subsets.

- Tests exercise failure recovery and observable outcomes rather than mirroring implementation.
- No test touches the operator database or real credentials; document limits that require native acceptance.

### Validate complete onboarding on native Windows and WSL

Execute documented clean-machine and existing-WSL journeys on actual supported Windows/WSL environments. Exercise reboot/resume, database service, browser login, dashboard reachability and the first task. Record OS/tool versions, commands and results; fix discovered defects.

- Provide actual Windows/WSL evidence for supported paths, including a rerun and repair scenario.
- Do not substitute Linux-only tests for Windows acceptance; record unavailable environments as unmet evidence.

### Validate complete onboarding on native macOS

Execute clean-machine and existing-developer-machine journeys on supported native macOS architectures. Exercise prerequisite setup, provider login, database lifecycle, dashboard, first task and upgrade/repair. Record versions and results; fix discovered defects.

- Provide native macOS evidence for the declared architecture matrix and rerun/repair scenarios.
- Do not treat cross-platform mocks as native proof; record unavailable environments as unmet evidence.

### Publish coherent installation release and final onboarding results

Assemble shipped installers, portable defaults, profiles and GitHub documentation into a coherent versioned release through normal AQ integration. Verify published entry points and artifacts, resolve remaining onboarding defects and record the final support matrix and acceptance evidence. This is delivery/acceptance work, not a review-ticket workflow.

- All required platform journeys have evidence, and published commands resolve to the intended artifacts.
- Release includes discoverable upgrade/repair instructions and explicit supported-platform limitations.

The authoritative creation graph is [tasks.graph.json](tasks.graph.json). Third-party installation and login methods must be verified against current official documentation during implementation.
