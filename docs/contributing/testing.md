# Testing

How to run the tests for the code you changed — and why running all of them is
the wrong move.

## Why this page exists

The suite is around **14,500 tests**. Counts rot, so here is the command:

```bash
pytest --co -q | tail -1
```

```text
14546/14694 tests collected (148 deselected) in 10.98s
```

Collection needs no test slot (it does need `POSTGRES_TEST_DSN` set — the
preflight runs before collection). A full serial run does not finish in a
useful amount of time, and a full parallel run is a whole-box event: on a
24-core machine it will happily consume every core, and if several people (or
several AI agents) start one at once the box stops responding. AQ's answer is
not "run less carefully"; it is **run the tests for what you touched**, and let
[CI](ci.md) run the rest.

Two mechanisms make that safe:

* **`aq test`** — a thin pytest wrapper that takes one of a small number of
  box-wide slots first, and applies the worker cap and the default marker
  deselects for you.
* **A PostgreSQL template database** — so a "fresh database" costs
  milliseconds instead of a full Alembic replay.

## Vocabulary

* **Marker** — a pytest label (`@pytest.mark.perf`) used to include or exclude
  a class of test. AQ declares seven and deselects five by default.
* **Test slot** — one of `resources.test_slots` (default **2**) `flock` slots
  held for the duration of a run, box-wide.
* **Worker cap** — the `-n` value `aq test` enforces, derived from cores ÷
  expected concurrent agents. Never raise it.
* **Lease database** — one of a small pool of PostgreSQL databases cloned from
  a migrated template, handed to a test and truncated on release.

## Run the tests for what you changed

The layout is one file per area, `tests/test_<area>.py`. Start there.

```bash
aq test tests/test_cli_test_runner.py
```

```text
aq test: slot 0 of 2, -n 3
$ /…/python -m pytest -n 3 --dist loadfile -m 'not perf and not migration and not slow and not tmux and not integration' tests/test_cli_test_runner.py
```

More ways to narrow, all of which pass straight through to pytest:

```bash
aq test tests/test_claim_queries.py tests/test_pool_sizing.py   # a few related files
aq test tests/ -k "schema_setup or run_schema"                  # by name, across files
aq test tests/test_orchestrator.py -x                           # stop at the first failure
aq test --lf                                                    # only what failed last time
pytest --co -q -k <term> | tail -20                             # collection only, no slot needed
```

Plain `pytest` still works for a single quick file. Use `aq test` for anything
past one file.

> **Warning.** Never run a bare `pytest tests/` mid-task, and never raise `-n`
> above what the wrapper gives you. `-n auto` inside an AQ session already
> resolves to that session's share via `PYTEST_XDIST_AUTO_NUM_WORKERS`; passing
> a larger `-n` bypasses the gating entirely. That is what took this box down
> on 2026-09-01, and it is why [resource gating](../guides/resource-gating.md)
> exists.

### Finding the right tests

1. **Guess the name.** `src/task_graph/formulas.py` →
   `tests/test_formulas_parse.py`, `tests/test_formulas_resolve.py`,
   `tests/test_formulas_provenance.py`.
2. **Ask pytest.** `pytest --co -q -k formula | tail -20` collects without
   running and needs no slot.
3. **Grep the imports.** `grep -rl "from src.task_graph.formulas" tests/`
   is exact.

## The `aq test` wrapper

[`src/cli/test_runner.py`](../../src/cli/test_runner.py) does five things, in
order:

1. **Resolves the caps.** `AQ_TEST_SLOTS` / `AQ_TEST_WORKERS` from the session
   environment win (the daemon derives them at launch, and they are visible
   from inside a worktree); otherwise the `resources:` section of
   `~/.agent-queue/config.yaml`; otherwise built-in fallbacks of 2 slots and 4
   workers. A worktree with no config still gets gating.
2. **Preflights.** A missing `POSTGRES_TEST_DSN` or a path-shaped argument that
   does not exist is refused *before* a slot is taken — because pytest under
   xdist turns a bad path into a green-looking "no tests ran".
3. **Takes a slot**, printing a `waiting …` line every poll so a queued run
   looks queued rather than hung.
4. **Execs pytest** with `-n <cap> --dist loadfile`, a fresh
   `AQ_TEST_RUN_ID` database-ownership token, and the default marker deselects
   — each only when you did not pass your own. An explicit `-n 0`,
   `-p no:xdist` or `-m perf` is always honoured.
5. **Treats "no tests collected" as the failure it is**, with a line saying so.

`--dist loadfile` keeps one module's tests on one worker, because their
database fixtures are per-module. It cannot live in `pyproject.toml`'s
`addopts`, since `-p no:xdist` unloads the option together with the plugin.

### Options and exit codes

Every wrapper option is `--aq-`-prefixed on purpose; everything else belongs to
pytest, including `-h`.

| Option | Effect |
|---|---|
| `--aq-help` | The wrapper's help (`-h`/`--help` go to pytest). |
| `--aq-status` | Print slot occupancy and exit. |
| `--aq-dry-run` | Print the composed pytest command line and exit. |
| `--aq-no-wait` | Fail immediately instead of queueing for a slot. |
| `--aq-workers N` | Override the enforced `-n`, clamped to the machine's core count. |
| `--aq-timeout N` | Seconds to wait for a slot (default 1800). |
| `--aq-all-markers` | Run the deselected markers too. |

| Exit code | Meaning |
|---|---|
| `0` | pytest passed. |
| `2` | No pytest arguments — the wrapper refuses to run the whole suite implicitly. |
| `4` | Preflight failure: a missing path, or no `POSTGRES_TEST_DSN`. Nothing ran. |
| `5` | pytest collected nothing. Nonzero, and the wrapper says why. |
| `75` | No slot came free (`EX_TEMPFAIL`). **Retryable — not a test failure.** |

Checking what a command will actually run, without running it:

```bash
aq test --aq-dry-run tests/test_cli_test_runner.py
```

```text
/…/python -m pytest -n 3 --dist loadfile -m 'not perf and not migration and not slow and not tmux and not integration' tests/test_cli_test_runner.py
```

Seeing who is holding the slots:

```bash
aq test --aq-status
```

```text
       Test slots — 2/2 free
┏━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┓
┃ Slot ┃ State ┃ Holder ┃ Held for ┃
┡━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━┩
│    0 │ free  │ -      │        - │
│    1 │ free  │ -      │        - │
└──────┴───────┴────────┴──────────┘
```

## Markers

Seven markers are declared in [`pyproject.toml`](../../pyproject.toml). Five of
them are deselected by default, both in `addopts` (so a bare `pytest` is safe)
and by `aq test` (so the wrapper behaves the same).

| Marker | Selects | Default | Needs |
|---|---|---|---|
| `perf` | Statement-count and latency budgets. | deselected | A quiet box; wall-clock ones also need `AQ_PERF_STRICT=1`. |
| `migration` | Tests that drive the real Alembic revision chain. | deselected | A scratch database per test. |
| `slow` | High-cost regression tests. | deselected | Time. |
| `tmux` | Tests requiring a real `tmux` binary. | deselected | `tmux`, POSIX. |
| `integration` | Tests requiring external dependencies (Milvus Lite and friends). | deselected | Those dependencies. |
| `functional` | Tests that launch the real `claude` CLI. | selected, skips itself | CLI auth + an API key. |
| `functional_mcp` | Functional tests needing `npm`/`npx` for MCP servers. | selected, skips itself | Node. |

`functional` and `functional_mcp` are not deselected because they *skip*
themselves when the CLI is missing or unauthenticated — an unauthenticated
development machine is a normal state, not a broken build
([`tests/conftest.py`](../../tests/conftest.py), `claude_cli_authenticated`).

Run a deselected class deliberately:

```bash
aq test tests/test_migration_single_head.py -m migration
aq test --aq-all-markers tests/test_tmux_path.py
```

`--aq-all-markers` passes an empty `-m ''` on the command line, which is the
only thing that overrides the configured `addopts` expression; simply omitting
`-m` would let the config take effect again.

### Latency budgets

Everything under [`tests/perf/`](../../tests/perf/) carries the `perf` marker.
Statement-count budgets are deterministic and always run when selected;
wall-clock budgets additionally take the `perf_strict` fixture and **skip**
unless `AQ_PERF_STRICT=1`, because they measure the machine as much as the
query. Run them deliberately and serially:

```bash
AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s tests/perf
```

The fixture lives in the root `conftest.py`, not in `tests/perf/conftest.py`,
because wall-clock assertions are not confined to that package — any suite that
asserts elapsed time needs the same gate, and an ungated one turns CI's default
arm red on runner load.

## The PostgreSQL substrate

PostgreSQL is the suite's **only** backend; SQLite was removed and
[`tests/test_sqlite_removal.py`](../../tests/test_sqlite_removal.py) is the
ratchet that keeps it out. No test uses the database in your
`~/.agent-queue/config.yaml`.

`POSTGRES_TEST_DSN` must be set. [`tests/conftest.py`](../../tests/conftest.py)
validates it once at `pytest_configure`, before collection or worker startup,
and fails the session with the setup instructions rather than producing
thousands of fixture errors.

### Three tiers, by what a test needs

[`tests/db_fixtures.py`](../../tests/db_fixtures.py) builds one **template**
database per distinct migrated schema, named `aq_tmpl_<schema slug>`. It is
built under a temporary name and renamed into place under a session advisory
lock, so a crashed build never leaves a half-migrated template for a later run
to clone, and it is marked `datistemplate` / `NOT datallowconn` once ready.

| Tier | Fixture | For |
|---|---|---|
| 1 — lease pool | `lease_dsn("name")` (armed by the autouse `_pg_backend` fixture) | ~95% of tests. Each xdist worker owns `AQ_TEST_DB_POOL_SIZE` (default 4) clones of the template; a lease is truncated and re-seeded on release, which costs milliseconds. |
| 2 — fresh clone | `db_fixtures.clone_database()` | A test that mutates schema and cannot hand the database back to the pool. |
| 3 — scratch | `tests.pg_dsn.create_scratch_database()` | A test that drives `alembic upgrade` / `downgrade` itself. |

Teardown truncates *and* replays the migration seed rows: the built-in
`workspace_kinds` live in the template, and a bare truncate would leave every
test after the first without them.

[`tests/pg_dsn.py`](../../tests/pg_dsn.py) derives a per-xdist-worker database
name from the base DSN (`…/agent_queue_gw0`, `…_gw1`, …) and creates it on
first use, so concurrent workers cannot truncate each other's in-flight state.
`dispose_owned_databases()` at session finish removes only what this run owns.

### Other fixtures worth knowing

| Fixture | Effect |
|---|---|
| `disable_schema_cache` | Forces a test through the real Alembic chain instead of the template (`AQ_SCHEMA_CACHE=0`). |
| `unpooled_postgres` | Swaps in a `NullPool` engine — a `TestClient` runs on another event loop, and asyncpg connections cannot cross loops. |
| `perf_strict` | The wall-clock gate described above. Take it as the *first* parameter, so an un-strict run skips before paying for the seed. |
| `claude_cli_path`, `claude_cli_authenticated` | Session-scoped; skip rather than fail when the CLI is missing, unauthenticated, or slow. |

### The production-database fence

`tests/conftest.py` calls `_refuse_production_database()` at import time. If
`POSTGRES_TEST_DSN`, `AGENT_QUEUE_DB` or `AQ_DATABASE_URL` addresses the
daemon's database, collection fails. `src.database.migration_guard` refuses the
migration itself at the engine; this is the outer fence, so the suite never
*addresses* the production database in the first place.

`AQ_ALLOW_PRODUCTION_TEST_DB=1` disables the fence. Do not set it. The incident
it exists for — a worker's pytest run migrating and stamping the operator's
database with an unmerged branch's revision, after which the daemon refused to
boot — is described in [migrations](../guides/migrations.md).

## The layout of `tests/`

```text
tests/
  conftest.py                  root fixtures, the DSN preflight, the production fence
  db_fixtures.py               template, lease pool, truncate/seed reset
  pg_dsn.py                    per-worker DSN derivation and owned-database cleanup
  *_helpers.py                 shared builders (git mocks, playbook V2, session dispatch, …)
  test_<area>.py               ~549 files, one per area
  perf/                        statement-count and latency budgets (marker: perf)
  llm/                         LLM client, adapters, providers, fakes
  task_graph/                  graph commands and the spatial layout engine
    layout/
  fixtures/                    ~180 data files, not tests
    contracts/  fake_gh/  formulas/  harnesses/
    playbooks/  task_graphs/  transcripts/
```

Tests are documented as a layout and a set of markers rather than file by file;
the coverage manifest assigns all 771 of them to this page. `pyproject.toml`'s
`testpaths` also lists `packages/`, which currently contains only the two
generated clients and no tests.

Helper modules are imported, not collected: `tests/git_mock_helpers.py`,
`tests/playbook_v2_helpers.py`, `tests/playbook_v2_engine_helpers.py`,
`tests/playbook_fixture_activation.py`, `tests/session_dispatch_helpers.py`,
`tests/assignment_routing_helpers.py`, `tests/pg_trigger_helpers.py`.

### Ratchets and drift guards

A handful of tests exist to stop something from coming back or drifting. When
one fails, the message names the side to fix.

| Test | Guards |
|---|---|
| [`test_v1_removal.py`](../../tests/test_v1_removal.py) | The deleted Playbook V1 compiler/runner/manager/store stay deleted. |
| [`test_sqlite_removal.py`](../../tests/test_sqlite_removal.py) | No `batch_alter_table`, no `dialect.name` branches — PostgreSQL only. |
| [`test_api_client_contract.py`](../../tests/test_api_client_contract.py) | `openapi.json` matches the live app surface; the generated client matches the pinned generator and the recorded digests. |
| [`test_cli_inventory.py`](../../tests/test_cli_inventory.py) | `docs/reference/cli-command-inventory.json` matches the real CLI. |
| [`test_docs_sync.py`](../../tests/test_docs_sync.py) | Every table in `src/database/tables.py` has a row in the database spec. |
| [`test_migration_string_defaults.py`](../../tests/test_migration_string_defaults.py), [`test_migration_boolean_defaults.py`](../../tests/test_migration_boolean_defaults.py) | Migration `server_default` values are bare, and booleans use `sa.false()`/`sa.true()`. |
| [`test_migration_single_head.py`](../../tests/test_migration_single_head.py) | The Alembic chain has exactly one head. |
| [`test_import_cycles.py`](../../tests/test_import_cycles.py) | Package import cycles stay broken. |

## Frontend tests

The dashboard has its own toolchain; none of it goes through `aq test`.

```bash
npm -w dashboard run test        # vitest run
npm -w dashboard run lint        # eslint
npm -w dashboard run typecheck   # tsc -b --noEmit
```

`typecheck` and `build` regenerate the TypeScript client first via a `pre`
script, so a fresh checkout does not need a separate generation step — see
[code generation](codegen.md#the-typescript-client).

## End-to-end

For changes to claims, pools, formulas or the task hierarchy there is a real
daemon on real PostgreSQL, driven through the real CLI, with no LLM:

```bash
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh
```

It runs fifteen scenarios in roughly two and a half minutes inside an isolated
world under `~/.agent-queue-e2e` — its own database, port, vault, tmux socket
and throwaway repository. Nothing it touches is shared with your daemon. The
full guide is [e2e swarm](../guides/e2e-swarm.md); the scripts are inventoried
in [scripts](scripts.md#supported-end-to-end-kit).

## Inputs and outputs

| Input | Output |
|---|---|
| `POSTGRES_TEST_DSN` | Template, per-worker and lease databases, all dropped on teardown. |
| A test path or `-k` expression | pytest's report, plus `--durations=10` from `addopts`. |
| `resources:` config or `AQ_TEST_*` env | The slot count and the `-n` cap actually used. |
| Nothing | `.pytest_cache/`, `.coverage.*` if you asked for coverage — both gitignored. |

## State ownership

* **The harness** owns every database under `POSTGRES_TEST_DSN` whose name
  carries this run's ownership token, and removes them at session finish.
* **The box** owns the `flock` slots; they outlive a crashed `aq test` only
  until the descriptor closes, and the wrapper forwards `SIGINT`/`SIGTERM` to
  pytest so a killed wrapper cannot orphan a running suite.
* **The daemon's database** is owned by the operator and is never addressed.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `POSTGRES_TEST_DSN is not set` | No test database configured. | See [setup](setup.md#postgresql-for-tests). Nothing ran. |
| `aq test: waiting 12s for 1 of 2 test slot(s); held by …` | The box is busy. | Wait, or `--aq-no-wait` to fail fast. Not an error. |
| exit `75` | No slot came free within the timeout. | Retry. It is not a test failure. |
| `aq test: no such test path: tests/test_typo.py` | A wrong path, refused before a slot. | Fix the path — nothing ran. |
| `aq test: no tests were collected` | Paths, `-k` or markers excluded everything. | Check the marker deselects; `--aq-dry-run` shows the exact command. |
| A wall-clock budget fails locally | Load, not a regression. | Re-run serially with `AQ_PERF_STRICT=1 … -p no:xdist`, on a quiet box. |
| A `migration`-marked test fails only for you | It was deselected by default and your run selected it. | It needs a scratch database; check the DSN points at a maintenance database you may create in. |
| `stale artefact(s): …cli-command-inventory.json` | You changed the CLI surface. | See [code generation](codegen.md#the-cli-command-inventory). |

## Related pages

* [Local checks](checks.md) — the shortest sufficient check list before a push.
* [Testing the installer](installer-testing.md) — the seams `aq install` is
  tested through, and the platform evidence they cannot stand in for.
* [Resource gating](../guides/resource-gating.md) — the three enforcement
  layers `aq test` is the middle of.
* [Code generation](codegen.md) — what to regenerate when a drift guard fails.
* [CI](ci.md) — the four suite arms that run everything you deselected.
* [e2e swarm](../guides/e2e-swarm.md) — the real-daemon functional kit.

## Source and tests

[`src/cli/test_runner.py`](../../src/cli/test_runner.py),
[`src/resources/semaphore.py`](../../src/resources/semaphore.py),
[`tests/conftest.py`](../../tests/conftest.py),
[`tests/db_fixtures.py`](../../tests/db_fixtures.py),
[`tests/pg_dsn.py`](../../tests/pg_dsn.py).

```bash
aq test tests/test_cli_test_runner.py tests/test_resource_semaphore.py tests/test_postgres_test_substrate.py tests/test_pg_dsn.py
```
