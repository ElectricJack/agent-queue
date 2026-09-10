# Scripts

Everything in [`scripts/`](../../scripts/) and the loose scripts at the
repository root: what each one is for, what it needs, what it changes, and
whether it is still supported.

## Why this page exists

`scripts/` accumulated over the life of the project. Some of its contents are
load-bearing (the two API-client regenerators, the end-to-end kit), some are
one-off migration aids kept for the record, and a few no longer work at all
because the subsystem they drove was deleted. Running the wrong one wastes an
afternoon; this page tells you which is which before you do.

## Vocabulary

* **Supported** — currently used by a documented workflow, by CI, or by both.
  Safe to run, given its prerequisites.
* **Historical** — kept as a record of a completed migration or a retired
  design. Not expected to work; do not build on it.
* **Side effects** — what changes outside your terminal. "None" means it only
  prints.

## Supported: code generation

These write tracked files. See [code generation](codegen.md) for what checks
each one.

| Script | Purpose | Inputs | Side effects |
|---|---|---|---|
| [`regenerate-api-client.sh`](../../scripts/regenerate-api-client.sh) | Rebuild `openapi.json` and the typed Python client. | `--offline` (build the spec from the checkout), `--from-file`, or no argument (fetch from a daemon at `AGENT_QUEUE_API_URL`). | Rewrites `openapi.json`, deletes and regenerates `packages/aq-client/`, rewrites `scripts/aq-client-boilerplate.sha256`, and `pip install -e`s the client. Refuses to run on the wrong generator version or without `ruff`. |
| [`regenerate-ts-client.sh`](../../scripts/regenerate-ts-client.sh) | Rebuild the dashboard's TypeScript client. | `--from-file` (canonical), `--offline`, or no argument. | Writes `packages/aq-ts-client/src/`, which is gitignored. |
| [`openapi-python-client.yaml`](../../scripts/openapi-python-client.yaml) | Not a script — the generator config. Pins the package name and scopes the post-generation `ruff` hooks to the Python package. | — | — |
| [`aq-client-boilerplate.sha256`](../../scripts/aq-client-boilerplate.sha256) | Not a script — recorded digests of the generator-only boilerplate, so a box without the pinned generator can still verify it. | — | — |
| [`generate-playbook-schema.py`](../../scripts/generate-playbook-schema.py) | Project the Playbook V2 Pydantic model to `src/playbook_v2_schema.json`. | `--check` for a drift diff. | Writes that one file. Imports only `src.playbooks.definition` — no daemon, database or config. |
| [`generate-cli-command-inventory.py`](../../scripts/generate-cli-command-inventory.py) | Regenerate or verify `docs/reference/cli-command-inventory.json`. | `--check`, `--output PATH`. | Writes that one file. |
| [`rebuild-reviewed-playbook-artifacts.py`](../../scripts/rebuild-reviewed-playbook-artifacts.py) | Recording aid for the **human** review of shipped playbook artefacts. Nothing in CI, the daemon or a release runs it. | The shipped playbook markdown. | Rewrites fixtures under `src/prompts/reviewed_playbooks/` — only ever checked in alongside a hand-written `review.md`. |

## Supported: end-to-end kit

Five shell entry points plus a Python driver, all operating inside an isolated
world under `$AQ_E2E_HOME` (default `~/.agent-queue-e2e`): its own database
(`agent_queue_e2e`), API port (8099), tmux socket, vault, workspaces and
throwaway Git repository. Nothing touches `~/.agent-queue` or the `agent_queue`
database. Full guide: [e2e swarm](../guides/e2e-swarm.md).

| Script | Purpose | Inputs | Side effects |
|---|---|---|---|
| [`e2e-common.sh`](../../scripts/e2e-common.sh) | Shared settings, sourced by the others. **Never run directly.** | `AQ_E2E_HOME`, `AQ_E2E_PORT`, `AQ_E2E_SESSION_PROVIDER`, `E2E_PG_*` — every value overridable, so two checkouts can run side by side. | None. |
| [`e2e-env.sh`](../../scripts/e2e-env.sh) | Create or refresh the isolated environment. | `--reset` (also drop the DB, repo and vault), `--register` (register projects and workspaces; needs a running daemon). | Creates `$AQ_E2E_HOME`, its config, vault and seed repository; `--reset` destroys and rebuilds them, stopping the daemon this home owns first and refusing (exit 2) when something else holds `$AQ_E2E_API_URL`. |
| [`e2e-daemon.sh`](../../scripts/e2e-daemon.sh) | Start / stop / inspect the isolated daemon. | `start`, `stop`, `status`, `logs [n]`. | Runs `python3 -m src.main` from this worktree against the e2e config; writes a pid file it validates by cmdline before signalling. `start` and `status` gate on `/ready` (via `e2e/probe.py`), not on the `/api/health` liveness stub. |
| [`e2e-smoke.sh`](../../scripts/e2e-smoke.sh) | Run the **fifteen** Tier 1 scenarios, no LLM. | Scenario ids, e.g. `S2 S8`; all of them by default. | Starts a daemon if none is up and stops whatever it started, including on Ctrl-C. Preflights the schema and exits 2 rather than running the scenarios against an unusable database. Exits nonzero if any scenario fails. |
| [`e2e-clean.sh`](../../scripts/e2e-clean.sh) | Destroy only the disposable resources. | — | Refuses to act unless `$AQ_E2E_HOME` resolves to a real directory carrying an `.aq-e2e` marker and is not `/`, `$HOME`, `~/.agent-queue` or the repository root. |
| [`e2e-dashboard.sh`](../../scripts/e2e-dashboard.sh) | Vite dev server pointed at the e2e daemon. | `AQ_E2E_PORT`, `DASHBOARD_PORT` (default 5173). | Foreground process. Refuses to start without `node_modules/` or the generated TS client. |
| [`e2e/aq.py`](../../scripts/e2e/aq.py) | Run *this worktree's* `aq`, not whatever is pip-installed. | Same arguments as `aq`. | Whatever the command does. |
| [`e2e/dbsetup.py`](../../scripts/e2e/dbsetup.py) | Create, reset or drop the isolated PostgreSQL database via `asyncpg` (no `psql` needed). | `<admin-dsn> <db-name> [--reset\|--drop]`. | Creates or drops that one database. The admin DSN must point elsewhere. |
| [`e2e/probe.py`](../../scripts/e2e/probe.py) | Ask `/ready` whether the daemon can use its schema. | `--url`, `--timeout`, `--quiet`. | None. Exits `0` ready, `1` answering but the database is unusable (prints the error and how two checkouts collide), `2` not answering. |
| [`e2e/register.py`](../../scripts/e2e/register.py) | Register the e2e projects and workspaces against a running daemon. | The daemon's URL from the shared settings. | Idempotent: existing projects and workspaces are left alone. `e2e-daemon.sh start` runs it for you. |
| [`e2e/smoke.py`](../../scripts/e2e/smoke.py) | The Tier 1 driver itself: stateful CLI scenarios through the public surface. | Scenario ids. | With `sessions.provider: fake` nothing is spawned — the script *is* the pool worker, minting session tokens and running the same `aq` commands a real harness would. |

```bash
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh
```

The fifteen scenarios cover pool sizing, the claim loop, worker-filed work,
formulas, fence and scope refusals, doctor, a concurrent-claim race, project
onboarding, task lifecycle, workspace and file writes, messages, the MCP
registry, plugin extensions, and graph plus vault operations.

## Supported: CI and integration

| Script | Purpose | Inputs | Side effects |
|---|---|---|---|
| [`check-integration-attestation.py`](../../scripts/check-integration-attestation.py) | The fail-closed decision about whether a `main` run may reuse an integration candidate's CI evidence, and whether a duplicate PR run should be suppressed. Called by [`tests.yml`](../../.github/workflows/tests.yml). | Event name, ref, default branch, repository, checkout SHA, run id/attempt, and a JSON evidence file. | Prints `true`/`false`. No state. |
| [`.github/agent-queue-integration.example.json`](../../.github/agent-queue-integration.example.json) | Not a script — the template for a repository's integration-trust configuration (`aq.integration-trust.v1`): app ids, canonical repository id, required check names and version. | — | — |
| [`setup-cgroup-delegation.sh`](../../scripts/setup-cgroup-delegation.sh) | One-time **root** step enabling cgroup v2 delegation, so the daemon can put each session in its own scope — layer 3 of [resource gating](../guides/resource-gating.md). | `sudo scripts/setup-cgroup-delegation.sh [user]`, defaulting to `$SUDO_USER` then `$USER`. | Sets `Delegate=yes` on the user's systemd slice. Idempotent. |

Layers 1 and 2 of resource gating (per-session environment caps and the
`aq test` semaphore) are cooperative and need no privileged setup; layer 3 is
what stops a process that ignores them.

## Supported: diagnostics and one-off tooling

| Script | Purpose | Inputs | Side effects |
|---|---|---|---|
| [`check-outdated-deps.py`](../../scripts/check-outdated-deps.py) | `pip list --outdated` that survives system packages with non-PEP-440 versions (Ubuntu's `distro-info` and friends), which otherwise crash it. | `--json`. | None. Exits 0 even when packages are outdated; 1 only if pip itself fails. |
| [`check-merge-conflicts.sh`](../../scripts/check-merge-conflicts.sh) | Report, as JSON, which task branches no longer merge cleanly into `origin/main`. | `<repo-path>`. | Runs `git fetch origin --prune`. Exit 0 = clean, 1 = conflicts, 2 = no `origin/main`. |
| [`inflate_llm_logs.py`](../../scripts/inflate_llm_logs.py) | Turn JSONL LLM logs into a browsable folder of per-turn markdown. | A date, or `--all`; defaults to today. | Writes under `~/.agent-queue/logs/llm/<date>/inflated/`. |
| [`seed_layout_perf.py`](../../scripts/seed_layout_perf.py) | Seed a project shaped for the graph-layout performance tests: 100 epics, one 1,000-task epic, one hub with 50 dependents. | A database handle and a project id, from a test or a REPL. | Creates a lot of rows. Use it against a throwaway database only. |
| [`pkg5_scenarios/build_payloads.py`](../../scripts/pkg5_scenarios/build_payloads.py) | Build the manual playbook-review scenario payloads by calling the real projections on the real checked-in artefacts — no daemon, no database. | `[OUT_DIR]`. | Writes one JSON file per scenario for `dashboard/scenarios/` to mount. |
| [`pkg5_scenarios/capture.sh`](../../scripts/pkg5_scenarios/capture.sh) | Capture the seven review screenshots from that scenario page. | `[PORT] [OUT_DIR]`; needs `agent-browser` and a Vite server. | Writes PNGs under a report directory. |

## Historical and unsupported

Kept for the record. Read them as evidence of a past design, not as
instructions — and expect the ones marked *broken* to fail immediately.

| Script | Status | Why |
|---|---|---|
| [`register-merge-conflict-hook.py`](../../scripts/register-merge-conflict-hook.py) | **Broken.** | Imports `models.Hook` to register a periodic *hook*. Hooks were replaced by playbooks (`docs/concepts/playbooks.md` — **planned**); there is no `Hook` model in `src/models.py`, so the import fails. The conflict *detector* it drove, `check-merge-conflicts.sh`, still works on its own. |
| [`migrate_task_records.py`](../../scripts/migrate_task_records.py) | **Historical.** | A completed vault migration: moves task-record markdown from `{data_dir}/memory/{project}/tasks/` to `{data_dir}/tasks/{project}/`. Dry-run by default, `--execute` to act; byte-for-byte and idempotent. Nothing new needs it. |
| [`run_tests.sh`](../../run_tests.sh) | **Historical.** | `python -m pytest tests/ -v` — the entire suite, serially, with no gating. Predates `aq test`; running it stalls every agent on the box. See [testing](testing.md). |
| [`test_suite.bat`](../../test_suite.bat) | **Historical.** | The same, for Windows, with a hard-coded personal path. |
| [`setup.sh`](../../setup.sh) | **Supported for operators, stale in two places.** | Installs Python, the package and the npm workspaces. It requests a `gemini` extra `pyproject.toml` does not define, and installs `acpx`, a retired runtime. Contributors should use the explicit commands in [setup](setup.md#install). |
| [`uninstall.sh`](../../uninstall.sh) | **Supported, destructive.** | Returns the checkout to a fresh-clone state and can delete `~/.agent-queue/`. Re-execs itself from a temporary copy so `git clean` cannot delete it mid-run. Read `--help` first. |

> **Note.** `.vibecop.yml` excludes `scripts/**` from code-quality scanning for
> exactly this reason: the directory mixes production tooling with one-off
> utilities, and findings in the latter are noise.

## Inputs and outputs

| Category | Reads | Writes |
|---|---|---|
| Code generation | The checkout, and a pinned generator | Tracked artefacts — commit them with the change that caused them |
| End-to-end kit | `$AQ_E2E_HOME`, a PostgreSQL server, this worktree's `src/` | An isolated world outside the repository |
| CI helpers | Workflow inputs and GitHub API evidence | Standard output only |
| Diagnostics | Logs, `pip`, Git | Report files, or nothing |

## State ownership

No script in this directory writes the daemon's database or `~/.agent-queue/`
except `inflate_llm_logs.py` (which writes only under that directory's `logs/`)
and `uninstall.sh` (which offers to delete it). The end-to-end kit owns
`$AQ_E2E_HOME` and the `agent_queue_e2e` database and nothing else; its cleanup
script refuses to run without a marker file proving that.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `refusing to destroy '<path>' — no .aq-e2e marker file` | `AQ_E2E_HOME` points somewhere the kit did not create. | Point it at the kit's own directory. The guard is working. |
| `answering but NOT usable` / `refusing to run the scenarios against an unusable database` | The daemon is listening but cannot query its schema — usually another checkout's `e2e-env.sh --reset` dropped the database under it. | Rebuild: `e2e-daemon.sh stop && e2e-env.sh --reset && e2e-daemon.sh start`. To run two kits at once, override `AQ_E2E_HOME`, `AQ_E2E_PORT` and `E2E_DB_NAME`. |
| `something is answering on <url> that <pid file> does not name` | `e2e-env.sh --reset` would drop the database and delete the home of a daemon this home does not own. | Stop that daemon, or override `AQ_E2E_HOME` / `AQ_E2E_PORT` / `E2E_DB_NAME`. The guard is working. |
| `node_modules missing — run: npm install` | `e2e-dashboard.sh` without a Node install. | `npm install` at the repository root. |
| `TS client not generated` | The gitignored client has never been generated here. | `./scripts/regenerate-ts-client.sh --from-file` |
| `ModuleNotFoundError: No module named 'models'` from `register-merge-conflict-hook.py` | The script is broken; hooks no longer exist. | Do not use it; playbooks replaced hooks. |

## Related pages

* [Code generation](codegen.md) — the artefacts the generator scripts produce.
* [Testing](testing.md) — why `run_tests.sh` is the wrong entry point.
* [e2e swarm](../guides/e2e-swarm.md) — the full guide to the kit.
* [Resource gating](../guides/resource-gating.md) — what the cgroup script sets up.
* [CI](ci.md) — where `check-integration-attestation.py` is called from.

## Source and tests

[`scripts/`](../../scripts/), plus the root
[`run_tests.sh`](../../run_tests.sh), [`test_suite.bat`](../../test_suite.bat),
[`setup.sh`](../../setup.sh) and [`uninstall.sh`](../../uninstall.sh). The
per-file catalog is [the contributing coverage shard](../reference/modules/contributing.md).

```bash
aq test tests/test_integration_attestation.py tests/test_merge_conflict_hook.py tests/test_e2e_kit_fixtures.py
```
