# Native macOS acceptance evidence

- Recorded: `2026-09-10T07:25:17+00:00`
- Runner: `GitHub Actions 1000007695`
- macOS: `15.7.9` (`ProductName:		macOS / ProductVersion:		15.7.9 / BuildVersion:		24G830`)
- Architecture: `arm64` (`arch` reports `arm64`)
- Homebrew: `Homebrew 6.0.20
Homebrew/homebrew-core (git revision b52ce43b075; last commit 2026-08-29)
Homebrew/homebrew-cask (git revision b8931574c9d; last commit 2026-08-29)` at `/opt/homebrew`
- Xcode CLT: `/Applications/Xcode_16.4.app/Contents/Developer`
- Python: `Python 3.12.10` (`/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`)
- Installer version: `aq, version 0.1.0`
- Selected capabilities: `postgres-managed, daemon`
- Verdict: **fail**

## Phases

| Phase | Verdict | Detail |
| --- | --- | --- |
| `host` | pass | macOS 15.7.9 on arm64 |
| `matrix` | pass | host_path=macos-apple-silicon tier=supported |
| `plan` | pass | 32 planned step(s); resume record written: False |
| `install` | pass | outcome=ready exit=0 |
| `rerun` | pass | outcome=ready exit=0; owned-resource set identical: True |
| `repair` | pass | outcome=ready exit=0 |
| `upgrade` | pass | outcome=ready exit=0 |
| `restart-from` | pass | outcome=ready exit=0 |
| `daemon` | pass | /health -> 200, /dashboard -> 404 |
| `database` | pass | a postgresql service is started |
| `postgres-client` | pass | psql on PATH is the one postgresql@17 installed (/opt/homebrew/bin/psql) |
| `first-task` | fail | the install configured no project root, so the documented `aq project onboard` first step has nowhere to put a repository |
| `uninstall-plan` | pass | 15 item(s): 0 to remove, 12 kept, 2 left to the operator |
| `clean-machine-homebrew` | unmet | This run found Homebrew already installed. The `macos.homebrew` needs_user branch (the official install one-liner, which asks for an administrator password AQ never types) is therefore not exercised natively here; it needs a Mac with no Homebrew. |
| `provider-login` | unmet | No provider credential exists in this environment and AQ never enters one. The `provider.*-login` steps and the Keychain-backed credential store need a human at a browser on a real Mac. |
| `live-first-task` | unmet | A live first task needs an authenticated harness, which depends on the unmet provider login above. The project and queue were exercised; a claim and an integrated result were not. |
| `reboot-survival` | unmet | `postgres.boot` installs a `brew services` login agent. Whether it comes back after a restart and after logging out cannot be observed inside one unattended run. |
| `rosetta-shell` | unmet | `macos.architecture` refuses a translated shell. Producing one needs an Apple-Silicon Mac with Rosetta installed and a terminal launched under it. |
| `interactive-wizard` | unmet | The short question set, its defaults and the consent prompts are read from a tty. This run is unattended by construction. |

## Commands

### `host` — Record the host

- `sw_vers` → exit `0` (0.02s)
- `uname -a` → exit `0` (0.01s)
- `arch` → exit `0` (0.01s)
- `sysctl -n sysctl.proc_translated` → exit `0` (0.0s)
- `sysctl -n hw.optional.arm64` → exit `0` (0.0s)
- `sysctl -n hw.model` → exit `0` (0.01s)
- `xcode-select -p` → exit `0` (0.01s)
- `brew --version` → exit `0` (0.13s)
- `brew --prefix` → exit `0` (0.02s)
- `git --version` → exit `0` (0.01s)
- `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -V` → exit `0` (0.02s)

### `matrix` — Supported-platform verdict

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --list-steps --json` → exit `0` (1.98s)
- `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -c import json;from src.install import describe_host;print(json.dumps(describe_host().to_dict()))` → exit `0` (0.17s)

### `plan` — Dry run

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --dry-run --json --with postgres-managed --with daemon` → exit `0` (2.9s)

### `install` — Unattended install to a ready daemon

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --yes --json --with postgres-managed --with daemon` → exit `0` (35.81s)

### `rerun` — Rerun (idempotence)

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --yes --json --with postgres-managed --with daemon` → exit `0` (4.47s)

### `repair` — Repair

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --yes --json --repair --with postgres-managed --with daemon` → exit `0` (4.41s)

### `upgrade` — Upgrade

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --yes --json --upgrade --with postgres-managed --with daemon` → exit `0` (4.73s)

### `restart-from` — Resume one branch (--restart-from)

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq install --non-interactive --yes --json --restart-from config.defaults --with postgres-managed --with daemon` → exit `0` (4.64s)

### `daemon` — Daemon and dashboard answer

- `curl -sS -o /dev/null -w %{http_code} http://127.0.0.1:8081/health` → exit `0` (0.04s)
- `curl -sS -o /dev/null -w %{http_code} http://127.0.0.1:8081/dashboard` → exit `0` (0.02s)
- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq status` → exit `0` (2.36s)

### `database` — PostgreSQL under brew services

- `brew services list` → exit `0` (0.69s)
- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq doctor --check database.connection --json` → exit `2` (1.77s)

### `postgres-client` — Where psql comes from

- `brew --prefix` → exit `0` (0.02s)
- `brew --prefix postgresql@17` → exit `0` (0.03s)
- `/bin/sh -c command -v psql || true` → exit `0` (0.01s)
- `/bin/sh -c brew list --versions | grep -i postgres || true` → exit `0` (0.48s)

### `first-task` — First project and task

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq project list-roots --json` → exit `0` (1.63s)

### `uninstall-plan` — Uninstall plan (dry run)

- `/Users/runner/hostedtoolcache/Python/3.12.10/arm64/bin/aq uninstall --dry-run --json` → exit `0` (1.92s)

