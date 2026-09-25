# AGENTS.md — instructions for coding agents in this repository

Agent Queue (`aq`) orchestrates AI coding agents: task queues, playbook-driven
multi-agent workflows, scoped memory and automated reflection. Fully async Python
3.12+, SQLAlchemy Core on **PostgreSQL only**, controlled from the CLI, MCP and one
Discord channel. Every `CLAUDE.md` in this repo is a one-line `@AGENTS.md` import;
this file is the single source of truth. `dashboard/AGENTS.md` and `src/cli/AGENTS.md`
add the rules for those directories.

Orientation, in this order: [profile.md](profile.md) (architecture and design
decisions), [docs/contributing/repo-map.md](docs/contributing/repo-map.md) (which
package owns what), [docs/specs/](docs/specs/) (specs are the source of truth — spec
first, then code; design specs in `docs/specs/design/`, dated plans and design specs
in `docs/superpowers/specs/`). Operator config is `~/.agent-queue/config.yaml`; the
vault (playbooks, profiles, memory — all markdown) is `~/.agent-queue/vault/`.

## Where the non-obvious things live

| Area | Code | Read |
|---|---|---|
| Orchestrator cycle, workspaces, pools | `orchestrator.py`, `src/orchestrator/` | `docs/specs/design/agent-coordination.md`, `docs/guides/worker-pools.md` |
| Commands (the single entry point) | `src/commands/` (handler + mixins, `contracts/builtin.py`) | `docs/specs/design/aq-surface.md` |
| Task graph: hierarchy, phases, subtasks, formulas | `src/database/queries/hierarchy_queries.py`, `src/task_graph/`, `src/commands/phase_commands.py`, `task_subtask_commands.py` | `docs/specs/design/work-graph.md` |
| Claims and the frontier | `src/database/queries/claim_queries.py`, `src/orchestrator/pools.py`, `src/claim_file.py` | `docs/guides/worker-pools.md`, `docs/guides/e2e-swarm.md` |
| Playbooks (V2 only) | `src/playbooks/`; shipped markdown and reviewed bundles in `src/prompts/` | `docs/specs/design/playbooks.md` |
| Profiles, intelligence classes, MCP registry | `src/profiles/`, `src/intelligence_classes/`, `src/prompts/default_intelligence_classes/` | `docs/specs/design/profiles.md`, `docs/guides/worker-pools.md` §7b |
| Sessions and harnesses | `src/sessions/` (`default_harnesses/`) | `docs/specs/design/session-runtime.md`, `docs/reference/harnesses.md` |
| Integration, delivery, branch cleanup | `src/integration/`, `src/commands/ci_commands.py` | `docs/guides/hierarchical-integration-trains.md` |
| Provider availability and failover | `src/providers/` | `docs/specs/provider-failover.md` |
| Escalations, digest, Discord, messages | `src/escalations/`, `src/digest/`, `src/discord/`, `src/messages/` | `docs/guides/escalations.md` |
| Resource gating, fleet metrics | `src/resources/`, `src/metrics/` | `docs/guides/resource-gating.md`, `docs/specs/design/fleet-metrics.md` |
| Graph layout | `src/task_graph/layout/`, `src/api/graph_layout.py`, `dashboard/src/pages/command-center/layout-v2/` | `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` |
| API, generated clients, dashboard server | `src/api/` (`spec.py`), `packages/`, `src/dashboard_server/` | `docs/contributing/codegen.md`, `docs/specs/dashboard-server.md` |
| Config, secrets, tuning | `src/config.py`, `config_editor.py`, `config_secrets.py`, `config_tuning.py` | `docs/specs/config.md`, `docs/guides/default-tuning.md` |
| Plugins (internal: files, git, inbox, notes, vibecop; external `aq-memory`) | `src/plugins/` | `docs/specs/plugin-system.md`, `docs/specs/design/memory-plugin.md` |

## Invariants and gotchas

- **Every state change goes through `CommandHandler`** (`src/commands/`), behind CLI,
  MCP, API and Discord alike. Commands return `{"success": bool, ...}` dicts.
- **Mechanism in code, policy in playbooks.** Scheduling is deterministic and makes no
  LLM calls; judgment (reflection, failover, escalation) is a playbook driving commands.
  Playbooks are V2 only — `tests/test_v1_removal.py` is the ratchet.
- **Every agent is a tmux session** selected by the profile's `harness` (`claude`,
  `codex`, `gemini`). There is no in-process runtime: `src/runtimes/` is an injection
  seam that registers nothing, and the profile keys `runtime` / `agent_name` are rejected.
- **Profiles are global; worker rungs are derived, not authored**
  (`src/profiles/catalog.py`): one `<class>-<harness>` rung per intelligence class ×
  installed harness, each inheriting from the `worker-<harness>` template. Edit the
  template or the class file, never a rung, and never hard-code a rung id in help text.
- **Shipped vault content is write-if-absent.** `ensure_default_profiles` and
  `ensure_default_aq_skills` (`src/vault.py`) never overwrite an installed copy, so editing
  a shipped profile or skill changes nothing on an existing install. `aq doctor --check
  profiles.system_drift` / `skills.installed_drift` name the gap; `aq agent
  profile-reseed --profile-id <id> --grants-only` merges new grants without clobbering
  operator edits. The exception is the supervisor's `## Capabilities`: the daemon merges
  shipped grants its vault copy lacks on every start and profile reload, additively
  (`src/profiles/capability_sync.py`; frontmatter `capability_sync: false` opts out).
- **Reviewed playbook bundles** (`tests/fixtures/playbooks/v2/<id>/`): after editing, run
  `scripts/rebuild-reviewed-playbook-artifacts.py`, update the `manifest.md` digests, and
  copy the bundle to `src/prompts/reviewed_playbooks/<id>/` — the only path an install
  imports from.
- **Import the `.aq/claim.json` helpers from `src/claim_file.py`**, never from
  `src.commands.claim_commands`, or `src.sessions` and `src.commands` form an import cycle.
- **`openapi.json` and both API clients are generated.** After any change to
  `src/api/models` or a codegen router: `./scripts/regenerate-api-client.sh --offline`,
  then `./scripts/regenerate-ts-client.sh --from-file`. Never hand-edit
  `packages/aq-client/`. The script writes tracked files only; `--install` is opt-in and
  refused from a worker slot because it re-points the shared venv.
- **Async first.** Use `GitManager`'s `a`-prefixed API; never `subprocess.run()` in
  production code.
- The spec `docs/superpowers/specs/2026-09-08-discord-simplification-implementation.md`
  has a byte-identical mirror at `vault/projects/agent-queue/specs/` in the operator
  vault; update both together.

## Development

```bash
pip install -e ".[dev,cli]"
pip install -e packages/aq-client        # generated typed API client
ruff check <changed paths>               # changed files only; line length 100, target py312
```

Tests are one file per area (`tests/test_<area>.py`, plus `tests/perf/`, `tests/llm/`,
`tests/fixtures/`); pytest-asyncio runs in auto mode.

## Testing — read before running anything

The suite is ~14k tests; a full run is a whole-box, many-minute affair that stalls every
agent on the machine. Rationale and the baseline workflow:
[docs/guides/resource-gating.md](docs/guides/resource-gating.md).

- **`aq test`, not bare `pytest`, for anything past one file.** It takes a box-wide test
  slot, applies the per-session worker cap and the default marker deselect
  (`-m "not perf and not migration and not slow and not tmux and not integration"`),
  and passes every non-`--aq-*` argument to pytest untouched.
  ```bash
  aq test tests/test_playbook_runner.py                  # the file for the module you changed
  aq test tests/test_claim_queries.py tests/test_pools.py
  aq test tests/ -k "schema_setup or run_schema"         # by name, across files
  aq test tests/test_x.py -x                             # stop at the first failure
  aq test --lf                                           # only what failed last time
  aq test --aq-status                                    # who holds the slots (--aq-help; -h is pytest's)
  pytest --co -q -k <term> | tail -20                    # collection only, no slot needed
  ```
  `waiting for 1 of N test slot(s)` means the box is busy. Exit code 75 means no slot came
  free: retry, it is not a test failure.
- **Never run the whole suite mid-task.** One broader run at the end, the area suite for
  what you changed (`aq test tests/test_playbook*.py tests/test_pipeline*.py`). A
  whole-suite `aq test` also takes the single box-wide full-suite lock; that run belongs
  to CI and to tasks whose subject is the suite.
- **Never raise `-n`.** `-n auto` inside a session already resolves to this box's
  per-session share (`PYTEST_XDIST_AUTO_NUM_WORKERS`); a bigger value bypasses the gating.
- **Slow-by-nature markers** (real tmux, Milvus, migrations, latency budgets) stay
  deselected unless the change is about them: pass your own `-m` or `--aq-all-markers`.
- **Wall-clock budgets measure the machine as much as the query.** Everything in
  `tests/perf/`, and every budget elsewhere, takes the `perf_strict` fixture and skips
  unless `AQ_PERF_STRICT=1`. Run them serially on a quiet box:
  `AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s tests/perf`.
- **Compare against the recorded baseline, never one you capture:** the latest
  `projects/agent-queue/notes/full-suite-baseline-<date>.md` vault note. A pre-existing
  failure never fails your task and never justifies weakening or skipping a test; name it
  in the close summary. Task authors specify focused and area checks, never "run the full
  suite before closing".
- **Swarm end to end:** after any change to claims, pools, formulas, the task hierarchy or
  provider failover, run `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` (real daemon,
  real PostgreSQL, no LLM, ~8 min) — [docs/guides/e2e-swarm.md](docs/guides/e2e-swarm.md).

## Database migrations (Alembic)

The schema is SQLAlchemy Core `Table`s in `src/database/tables.py`; revisions live in
`migrations/versions/` (a squashed baseline plus incremental revisions). Guide:
[docs/guides/migrations.md](docs/guides/migrations.md).

- **Never change `tables.py` without a revision.** Both commands need
  `AGENT_QUEUE_DB_URL` (or `sqlalchemy.url` in `alembic.ini`) pointing at a
  **disposable** database — no URL is a hard error, never a local file:
  ```bash
  alembic revision --autogenerate -m "description"   # then review the file
  alembic upgrade head
  ```
- **Review every autogenerated file** — Alembic sees a rename as drop+add. Revisions are
  idempotent: the squashed baseline is built from live metadata, so a new column or table
  is guarded with `sa.inspect(bind)` (`has_table` / `get_columns`) before it is added; copy
  the pattern from a recent revision. In a worker slot there is no database to autogenerate
  against — write the revision by hand.
- **PostgreSQL only:** no `batch_alter_table`, no `dialect.name` branches
  (`tests/test_sqlite_removal.py`).
- **Name every `CheckConstraint`** (`name="ck_<table>_<what>"`): autogenerate matches them
  by name only, and an unnamed one yields a spurious `drop_constraint` on every later run.
- **`server_default` takes the bare value** (`"system"`, not `"'system'"`); booleans use
  `sa.false()` / `sa.true()` (`tests/test_migration_string_defaults.py`,
  `tests/test_migration_boolean_defaults.py`).
- Revision ids are sequential (`a000000000NN`), so two sibling branches can claim the
  same one: after rebasing, check `alembic heads` and re-chain the later file.

## Never migrate the operator's database

Migrations against the database in `~/.agent-queue/config.yaml` are daemon/operator
only. In a worktree slot never run `alembic upgrade`, `alembic stamp` or `aq start`.

- Your session carries `AQ_DB_SCOPE=worker` and `AQ_DATABASE_URL` / `AGENT_QUEUE_DB` set
  to a refusal sentinel; leave all three alone. Tests build their own databases.
- **"schema behind code; ask the operator to upgrade"** from `run_schema_setup` is the
  guard working (`src/database/migration_guard.py`): report it, do not upgrade around it.
  `aq db current` is the read-only answer; only an operator runs `aq db upgrade`.
- A database stamped with a revision this checkout lacks:
  `aq doctor --check db.alembic_orphan [--fix]`.

## Restarting after an update

Use `aq restart --no-dashboard`: it preserves agent sessions, the updating supervisor's
included, so the daemon re-adopts them. **Never plain `aq stop` then `aq start` for an
update** — `aq stop` kills every agent tmux session. If a stopped phase is needed:
`aq stop --keep-sessions`, then `aq start --no-dashboard`. Verify health, readiness and
the supervisor's live terminal afterwards; a stored session status can be stale. None of
this lets a worker session manage the daemon.

## Working as an aq worker

- Run `aq prime` first and follow it: it carries the task, role, rules, deliverables and
  the completion protocol. Record findings on the task with `aq task comment`.
- Emergent work: `aq task create --project "$AQ_PROJECT_ID" --reason "..."`. It lands as
  a child of the task you hold and blocks that task's passing close until resolved.
- Publish before you close: `aq git push`, then `aq task close <id> --outcome pass|fail
  --summary "..."`, recording each check with `--test` / `--command`. Never push main or
  merge yourself.
- Operator surfaces (`aq task list`, `aq doctor`, `aq session …`) answer `out of scope`
  to a worker token; that is scope, not a bug. The normative policy for admitting,
  delivering and recovering work is [docs/concepts/factory-policy.md](docs/concepts/factory-policy.md).
