# Windows and WSL onboarding validation

Validated on 2026-09-10 from task `noble-apex.17`, using an attached native
Windows host and its actual WSL distribution. No credentials or secret values
were read or recorded.

## Environment

| Component | Observed version / state |
| --- | --- |
| Windows | `10.0.26200.9168`; PowerShell `5.1.26100.9168` |
| WSL | `2.7.10.0`; kernel `6.18.33.2-2` |
| Distribution | Display name `Ubuntu`, WSL2, Ubuntu `24.04.4 LTS`, x86_64 |
| Linux prerequisites | Python `3.12.3`, Git `2.43.0`, tmux `3.4` |
| Codex CLI | `0.153.4`; `codex login status` reported ChatGPT authentication |

The distribution's display name is intentionally recorded because the
documented install identifier (`Ubuntu-24.04`) is not necessarily the name an
existing installation retains. Microsoft documents `wsl.exe --list --verbose`
as the source for an installed distribution's name and WSL version, and
`wsl.exe --set-version` requires that installed name.

## Evidence collected

1. Ran the native Windows bootstrap from Windows PowerShell against the WSL
   checkout with `-CheckOnly`. It exited `0` and reported:

   ```text
   Using existing Ubuntu on WSL2. AQ will be installed in its Linux home filesystem.
   Windows and WSL preflight passed; no installation changes were made.
   ```

   `-CheckOnly` performs no package installation, conversion, daemon start,
   or bootstrap download. It is suitable for verifying the existing-WSL path.

2. Ran the WSL installer planner without mutations:

   ```bash
   aq install --dry-run --non-interactive \
     --with provider.codex --with postgres-managed --json
   ```

   It reported `host_path: windows-wsl2`, `tier: supported`, and a successful
   non-secret Codex authentication probe. It correctly reported no PostgreSQL
   server on localhost:5432 and skipped all mutating provisioning steps.

3. Verified dashboard reachability from both sides of the boundary. The source
   checkout's Vite dashboard returned HTTP `200` at `http://localhost:5173`
   from WSL and through native Windows PowerShell's
   `Invoke-WebRequest`. The daemon's bundled `/dashboard` path returned `404`,
   which is expected for this source checkout; the install tutorial directs
   source users to the Vite URL instead.

4. Ran `aq test tests/test_windows_bootstrap.py` with the repository's
   disposable PostgreSQL service and `POSTGRES_TEST_DSN` set to its documented
   test DSN: all four tests passed. Also ran the contract checks directly and
   linted their Python test file.

## Defects fixed during validation

* `scripts/install-windows.ps1` previously used Bash-style quote escaping in a
  PowerShell expression, so Windows PowerShell rejected the script before any
  bootstrap logic could run. Repository URLs are already restricted to a safe
  HTTPS GitHub form, so the script now uses valid PowerShell interpolation.
* `wsl.exe --list --verbose` produced NUL-padded table rows under the actual
  Windows PowerShell/WSL boundary, preventing the original regex from finding
  any installed distribution. Rows are now normalized before matching.
* Existing supported Ubuntu 24.04 instances displayed as `Ubuntu` are now
  reused only after their `/etc/os-release` confirms Ubuntu 24.04, rather than
  being mistaken for a missing distribution.

## Unmet clean-machine evidence

The following acceptance evidence remains intentionally unmet in this shared
worker environment:

* A clean installation, managed PostgreSQL setup, daemon start, and disposable
  first task would mutate the operator environment. Worker safeguards prohibit
  `aq start`, database migration, and changes to the configured database.
* Reboot/resume cannot be performed because `wsl --shutdown` or a Windows
  reboot would terminate the shared worker and other active sessions.
* The daemon-supplied `aq` executable in this worker accepts the dry-run
  planner but reports `No such option '--repair'` for `aq install --repair`.
  The checked-out source documents that repair flow, but exercising it needs a
  dedicated installed AQ environment; therefore native repair/rerun evidence
  remains unmet here.
* An interactive browser login was not initiated. The existing Codex login was
  verified non-secretly; provider login and browser completion remain human
  actions.

Run those steps on a dedicated Windows/WSL test machine using the install and
first-task tutorials, then append its non-secret command outcomes here.
