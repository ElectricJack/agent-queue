# Native macOS acceptance evidence

The [installation and onboarding contract](../contract.md) says "Supported"
means a clean-install, rerun, repair and first-task journey has **native**
acceptance evidence, and that an environment which was not available stays
visible as *unmet evidence* rather than expanding the matrix by assertion.
This page is that record for macOS (`noble-apex.18`).

It is a record, not a claim of completeness: read the [unmet rows](#unmet-rows)
before quoting the passing ones.

## How this evidence is produced

`scripts/acceptance/macos_acceptance.py` runs the documented journey unattended
on a Mac and writes one JSON record plus its Markdown rendering.
`.github/workflows/macos-acceptance.yml` runs it on GitHub-hosted macOS runners
across the matrix's architectures and uploads the record as an artifact. The
script refuses to run on anything but Darwin — a Linux run is not evidence for
this page, and the harness will not pretend otherwise.

Rerun it per release, and after any change to `src/install/macos.py`, the
PostgreSQL adapter's Homebrew branch, or the macOS quickstart in
[the install tutorial](../../../tutorials/install.md):

```bash
# On a Mac you are willing to have changed — it installs Homebrew formulae,
# starts a PostgreSQL service and writes ~/.agent-queue.
python3 scripts/acceptance/macos_acceptance.py --output ./macos-acceptance

# Or on hosted runners, from a branch under ci/macos-acceptance**:
gh workflow run macos-acceptance.yml
```

## Evidence rows

Recorded 2026-09-10 from GitHub Actions run
[34449681216](https://github.com/ElectricJack/agent-queue/actions/runs/34449681216),
installer version `0.1.0`, capabilities `postgres-managed` and `daemon`
selected. Each row is one runner; the `postgres` column says whether the
image's own PostgreSQL was left linked or unlinked first. All three images
turned out to ship no PostgreSQL at all (`brew list --versions | grep postgres`
found only the `postgresql@17 17.11` AQ installed), so the two arms observed
the same machine; the arm is kept because an image that starts shipping one
again would otherwise silently hide the clean-machine path. Only one arm per
runner is listed below — the other is identical and is in the same artifact
set.

| Host | macOS | Arch | Tier | Homebrew | Xcode CLT | Git | Python | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `macos-14` (keep) | 14.8.9 (23J631) | arm64 | `macos-apple-silicon` / supported | 6.0.20 at `/opt/homebrew` | Xcode 15.4 | 2.55.0 | 3.12.10 | all phases pass except `first-task` |
| `macos-14` (unlinked) | 14.8.9 (23J631) | arm64 | `macos-apple-silicon` / supported | 6.0.20 at `/opt/homebrew` | Xcode 15.4 | 2.55.0 | 3.12.10 | all phases pass except `first-task` |
| `macos-15` (keep) | 15.7.9 (24G830) | arm64 | `macos-apple-silicon` / supported | 6.0.20 at `/opt/homebrew` | Xcode 16.4 | 2.55.0 | 3.12.10 | all phases pass except `first-task` |
| `macos-15` (unlinked) | 15.7.9 (24G830) | arm64 | `macos-apple-silicon` / supported | 6.0.20 at `/opt/homebrew` | Xcode 16.4 | 2.55.0 | 3.12.10 | all phases pass except `first-task` |
| `macos-15-intel` (unlinked) | 15.7.9 (24G830) | x86_64 | `macos-intel` / compatibility | 6.0.18 at `/usr/local` | Xcode 16.4 | 2.55.0 | 3.12.10 | all phases pass except `first-task` |

The harness's own rendering of each of those runs is kept beside this page, so
the evidence outlives the ninety-day artifact retention:
[`records-2026-09-10/`](records-2026-09-10/macos-14-keep.md)
([`macos-14-unlinked`](records-2026-09-10/macos-14-unlinked.md),
[`macos-15-keep`](records-2026-09-10/macos-15-keep.md),
[`macos-15-unlinked`](records-2026-09-10/macos-15-unlinked.md),
[`macos-15-intel-unlinked`](records-2026-09-10/macos-15-intel-unlinked.md)).
The JSON record each was rendered from stays in the run's artifacts.

Every Apple-Silicon host reported `sysctl.proc_translated = 0` and
`hw.optional.arm64 = 1`: the runs are native, not translated, which is what the
contract's "No Rosetta-based primary installation" row requires the evidence to
show. The Intel host (`Macmini6,2`, real hardware rather than a VM) has neither
key, and `sysctl` exits non-zero for both — the branch `macos.architecture`
reads as "not translated, not Apple Silicon", confirmed here rather than
assumed.

### What each phase observed

| Phase | Result | What it means |
| --- | --- | --- |
| `matrix` | pass | `describe_host()` placed every arm64 host in `macos-apple-silicon` / supported before anything was changed. |
| `plan` | pass | `aq install --non-interactive --dry-run --json` printed all 32 steps and wrote no resume record. |
| `install` | pass | `aq install --non-interactive --yes --with postgres-managed --with daemon` reached `outcome: ready`, exit 0, in 18–36 s. |
| `rerun` | pass | The same command again reached `ready` with a byte-identical owned-resource set. |
| `repair` | pass | `--repair` reconciled the installation against the host and reached `ready`. |
| `upgrade` | pass | `--upgrade` repaired and recorded its version transition, reaching `ready`. |
| `restart-from` | pass | `--restart-from config.defaults` replayed that branch and carried the rest forward. |
| `daemon` | pass | `/health` answered `200`. `/dashboard` answered `404` — correct for a source checkout, which ships no built dashboard; readiness reports it as `needs_attention` with the Vite hint rather than claiming success. |
| `database` | pass | `brew services` shows a started `postgresql@17`; `postgres.boot` enabled it at login. |
| `postgres-client` | pass | `brew install postgresql@17` left `psql` at `/opt/homebrew/bin/psql`, resolving into `/opt/homebrew/opt/postgresql@17/bin`. Homebrew's API marks the formula `keg_only` (`:versioned_formula`), which would have meant no client on PATH and no administrator route for `postgres.role`; natively it is linked. The probe stays in the harness so a Homebrew change that stops linking it is caught rather than assumed. |
| `first-task` | **fail** | A completed install configures no `project_roots`, so `aq project onboard` — which requires `--root-id` — has no root to onboard into. Filed as `noble-apex.23`. |
| `uninstall-plan` | pass | `aq uninstall --dry-run` planned 15 items: nothing removed by default, 12 kept, and 2 (`tmux`, `postgresql@17`) reported as the operator's to remove because they are shared with the rest of the machine. |

## Defects this found, and their fixes

Two defects failed the first run (`34448564471`) identically on macOS 14, 15
and 15 Intel. Both are fixed on this branch and pass on the rerun above.

1. **`aq install --dry-run` stopped at step four of thirty-two on a clean Mac.**
   `macos.packages` installs Git and tmux and a dry run executes no mutating
   step, so `prereq.tmux` failed and the plan the user ran `--dry-run` to see
   was never printed. A read-only check now declares
   `provisioned_by=(<mutating step>,)` and the engine records such a dry-run
   failure as skipped — "would be satisfied by `macos.packages`" — instead of
   stopping. The relation is declared rather than inferred from the dependency
   graph, so `config.check` on a `config.yaml` that is already broken still
   fails a dry run.
2. **An unattended install failed after the daemon had started.**
   `daemon.start` ran plain `aq start`, which asks `click.confirm` whether to
   launch the Vite dev server from a source checkout; with no terminal behind
   it, click raised `Abort` and the step reported "the daemon did not come up:
   Aborted!" with a healthy daemon on the box. The step now passes
   `--no-dashboard`, and `aq start` skips the prompt outright when stdin is not
   a tty.

A third defect on the documented macOS path was found while reading the
quickstart: `setup.sh` asked four questions with `read -rp`, and `read` returns
non-zero at end of input, so under `set -euo pipefail` the script aborted with
a bare exit 1 whenever stdin was not a terminal. It now takes the documented
default when there is nobody to ask.

Two further findings were filed rather than fixed here, because each needs a
decision that belongs to another task rather than to platform acceptance:

* `noble-apex.23` — no `project_roots` after a completed install (the failing
  `first-task` row above).
* `noble-apex.24` — a rerun, repair or upgrade prints an empty "Where AQ stores
  your data", because the engine's `REVALIDATE` path replaces the step detail
  the summary reads and the resume record stores no detail to carry forward.

## Unmet rows

Recorded as missing evidence, not as passes by analogy. Each needs a machine or
a human this run did not have.

| Row | Why it is unmet | What would meet it |
| --- | --- | --- |
| A Mac that has never had Homebrew | Every runner arrives with Homebrew installed, so `macos.homebrew`'s `needs_user` branch — the official install one-liner, which asks for an administrator password AQ never types — was not exercised. | A clean Mac or a fresh VM with no Homebrew. |
| A real browser login for each harness | No provider credential exists here and AQ never enters one. `provider.*-login` and the Keychain-backed credential store were not exercised. | A human at a browser on a real Mac, per harness. |
| A live agent claiming and completing the first task | Depends on the unmet login above. The queue and the daemon were exercised; a claim, an execution and an integrated result were not. | The same Mac, after one harness login. |
| PostgreSQL surviving a reboot and a logout | `postgres.boot` enabled a `brew services` login agent; whether it returns after a restart cannot be observed inside one unattended run. | A Mac that can be rebooted and logged out of between two runs. |
| A Rosetta-translated shell | `macos.architecture` refuses one; producing it needs Rosetta installed and a terminal launched under it. | An Apple-Silicon Mac with Rosetta. |
| The interactive wizard | The question set, its defaults and the consent prompts are read from a tty; this run is unattended by construction. | A human at a terminal running `aq install`. |
| A stale Homebrew, a non-default prefix, `/usr/local` on Apple Silicon | The runners' Homebrew is current and at its documented prefix for the architecture. | Machines in those states. |
| A release artifact rather than a source checkout | The journey installs from this checkout, so the dashboard is the Vite one (`/dashboard` → 404) and `_repo_root()` is set. `noble-apex.5` owns the packaged runtime. | A published artifact installed on a Mac with no checkout. |

## Intel (compatibility tier)

The contract puts Intel macOS in a compatibility tier, requires the common
installer to work without assuming `/opt/homebrew`, and requires the Intel
evidence to be recorded separately for each release. It is recorded above and
it holds: on a real `Macmini6,2` the matrix placed the host as
`macos-intel` / compatibility, `macos.homebrew` discovered Homebrew at
`/usr/local` (its documented Intel prefix, and *not* the arm64 default the
adapter is forbidden to assume), `brew install postgresql@17` put `psql` at
`/usr/local/bin/psql`, and the install, rerun, repair, upgrade and
`--restart-from` journeys all reached `ready` exactly as they did on Apple
Silicon.

The Intel row carries the same single failure as the others (`first-task`,
`noble-apex.23`) and the same unmet rows. The first Intel run
(`34448564471`) failed the two defects fixed above, identically to the
Apple-Silicon runs — which is itself worth recording: nothing in either defect
or its fix was architecture-specific.
