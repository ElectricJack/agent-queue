# SQLite Removal — PostgreSQL as the Only Backend

**Date:** 2026-09-07
**Status:** Proposed
**Scope:** Delete SQLite support from `agent-queue` entirely — production, tests,
migrations, config, wizard, doctor and docs — leaving PostgreSQL as the single
supported backend.

---

## 1. Goal

One database. No `dialect.name` branch survives in application code, no test
runs against a backend the daemon will never use, and no code path exists that
can silently bring a daemon up on the wrong engine.

### Non-goals

- **Adopting Postgres-only primitives.** `SELECT … FOR UPDATE SKIP LOCKED`,
  advisory locks and `ON CONFLICT … RETURNING` become available once the SQLite
  fallbacks are gone, but this refactor is *behavior-preserving*. Exploiting
  them is a follow-on task (§9).
- **Rewriting migration history.** The 113 committed revisions already ran
  against real operator databases. They are not touched (§6.3).
- **Requiring Docker.** Docker Compose stays as *one* supported way to get a
  Postgres; it does not become the only one (§5.2).

---

## 2. Current-state inventory

Measured on `main` at `34045993`.

| Surface | Extent |
|---|---|
| `src/` files mentioning sqlite | 57 files, 143 references |
| `dialect.name` branch sites in `src/` | ~40, across 25 query modules |
| Heaviest query modules | `hierarchy_queries.py` (6), `playbook_artifact_queries.py` (4), `playbook_run_queries.py` (4) |
| `src/database/engine.py` | 605 lines, 34 sqlite references |
| `src/database/adapters/sqlite.py` | 204 lines (mixin composition only) |
| `src/database/legacy_sqlite_import.py` + `scripts/` twin | 444 + 228 lines |
| `src/setup_wizard.py` | 26 sqlite references |
| `tables.py` partial indexes | 14 `sqlite_where=` — **every one already paired with `postgresql_where=`** |
| Alembic revisions | 113 total; 29 with dialect branches, 71 using `batch_alter_table` |
| Test files mentioning sqlite | 82 |
| Test files constructing a SQLite `Database(...)` | **194 files, 232 call sites** |
| `database_path=` references | 31 in `src/`, 221 in `tests/` |
| Docs mentioning sqlite | 10 first-class files (specs/guides/READMEs) + ~50 historical analysis/plan files |

### The three things that will actually bite

**(a) Removing SQLite removes the test schema cache.**
`engine.py:40-222` implements a template cache that is *SQLite-only* — it builds
one fully-migrated `.db` file per schema hash and byte-copies it for each fresh
test database (`_build_schema_template`, `_copy_sqlite_database`,
`_restore_schema_from_cache`). This is the only reason the suite is 14 minutes
instead of days: without it, ~2,700 tests each replay 58+ Alembic migrations at
~8s. **Deleting SQLite deletes this cache.** A Postgres equivalent must exist
and be measured *before* anything else lands. This is the gate on the whole
project.

**(b) SQLite is the current default backend.**
`src/database/__init__.py:create_database` falls through to
`SQLiteDatabaseAdapter` for any unrecognized URL, and `config.py:784` treats a
bare path as SQLite. A fresh `pip install && aq start` works today with zero
external state. After this change it cannot. The first-run story needs an
answer that isn't "install Docker" (§5.2).

**(c) Existing operators have live SQLite databases.**
The import path must survive the cutover even though the backend does not.

---

## 3. Phase ordering

```
T0  PG test substrate           ── BLOCKING GATE, merge before anything else
T0.5 Name every constraint      ── prerequisite for T5's autogenerate gate
     │
T1  Config + backend selection ─┐
T2  SQLite import escape hatch ─┤  can land together
     │                          │
T3  Query-layer debranching  ───┤
T4  Engine / adapters / doctor ─┤  one PR; T3 and T4 are coupled
T5  Schema + alembic         ───┘
     │
T6  Test suite codemod          ── the large one; land in a single shot
     │
T7  CI, packaging, ops
T8  Docs + ratchet
```

T3–T5 touch 25 query modules; T6 touches 194 test files. Both will conflict with
essentially every open branch. **Coordinate a short merge freeze around T6**, or
land it as the first thing after a green integration train.

---

## T0 — Postgres test substrate (blocking gate)

Nothing else may merge until this is measured and green.

### Design

Two tiers, chosen by what the test actually needs:

**Tier 1 — the default (~95% of tests): per-worker database + truncate reset.**
`tests/pg_dsn.py` already gives every xdist worker its own database
(`ensure_worker_postgres_dsn`), and `PostgreSQLDatabaseAdapter.reset_for_tests`
(`adapters/postgresql.py:152`) already truncates. Wire these into a
session-scoped fixture: migrate the per-worker DB **once**, then
`TRUNCATE … RESTART IDENTITY CASCADE` all tables between tests. Cost is
milliseconds, versus a full schema build.

**Tier 2 — DDL/migration tests: template clone.**
Build `aq_test_template_<key>` once per schema key, then
`CREATE DATABASE x TEMPLATE aq_test_template_<key>` per test — a server-side
file copy, ~100–300ms. Reuse `engine.py:_schema_cache_key()` (Alembic heads +
`tables.py` digest) verbatim as the template name suffix; that logic is correct
and backend-agnostic already.

`tests/pg_dsn.py:create_scratch_database` already exists for tests that drive
`alembic upgrade/downgrade` and must not share schema. Keep it as tier 3.

### Deliverables

- `tests/db_fixtures.py` — `temp_db` (tier 1), `fresh_db` (tier 2),
  `scratch_db` (tier 3, re-export).
- Template builder + a `--aq-rebuild-template` escape in `src/cli/test_runner.py`.
- `AQ_SCHEMA_CACHE` / `disable_schema_cache` in `tests/conftest.py:15-49`
  replaced by the tier-2 template controls.

### Exit gate — do not proceed without these numbers

1. Record the SQLite baseline **now**, before any change:
   `aq test tests/test_orchestrator.py tests/test_task_commands.py tests/test_playbook_runner.py`
   and a 500-test sample, wall clock, on a quiet box.
2. Re-run the same sets on the T0 substrate.
3. **Gate: the PG substrate is within 1.5× of the SQLite baseline.** If it is
   not, stop and fix the substrate — a suite nobody can run locally is a worse
   outcome than dual-backend support.

---

### T0 results (measured 2026-09-07)

**Built and verified.** `tests/db_fixtures.py` (template build, clone, lease
pool, seed capture/restore), `tests/pg_backend_shim.py`, `src/database/
schema_key.py`, and the `_pg_backend` fixture in `tests/conftest.py`.

The shim routes the *existing* suite onto Postgres with **zero test edits**:
`Database` is `SQLiteDatabaseAdapter`, `PostgreSQLDatabaseAdapter` is a
sibling class rather than a subclass, so patching `SQLiteDatabaseAdapter.
__new__` to return a Postgres adapter makes Python skip `__init__` entirely
and the SQLite path argument is simply discarded. Patching the class object
(not a module attribute) works regardless of how a test imported `Database`.

**Measured, 321 tests, `aq test -n 3`, both halves run sequentially in one
script so they cannot contend:**

| | 321 tests | pytest | wall |
|---|---|---|---|
| SQLite, as found | passed | 231.68s | 243.57s |
| PG substrate, first cut | passed | 181.38s | 193.19s |
| SQLite + head fast path | passed | 119.26s | 127.76s |
| **PG substrate, final** | passed | **104.25s** | **111.81s** |

**0.87x — 13% faster than the SQLite path it replaces, against a gate that
only required staying under 1.5x.** The gate passes; Postgres is not the slow
option here.

The largest single win is backend-agnostic and worth landing on its own: the
`run_schema_setup` head check took **49% off SQLite** (231.68s -> 119.26s) and
36% off Postgres. Every `Database()` construction was running a full Alembic
upgrade — building a `ScriptDirectory` over 114 revision files, loading
`env.py`, opening a second connection — to conclude there was nothing to do.

Two further substrate fixes closed the rest of the gap (193.19s -> 111.81s):
`ensure_template` took the advisory lock on *every* call rather than only to
build, serialising twelve clones behind one lock across three xdist workers;
and `capture_seed` made 92 round trips where one `UNION ALL` probe does.

Caveat on reading these: the per-worker pool creation is paid once, in
parallel, so it inflates a seven-file run proportionally more than it will the
full 11,330-test suite. Expect the whole-suite gain to be larger, not smaller.

Three measurement traps, all of which produced wrong numbers before being
caught — record them so the next person does not repeat them:

1. **Unequal test sets.** The default SQLite run *skips* 23
   `..._on_both_backends[postgres]` tests for want of `POSTGRES_TEST_DSN`,
   and they are the expensive ones. Comparing 298 tests against 321 made the
   substrate look 2.05x slower. Always set `POSTGRES_TEST_DSN` on both sides.
2. **Concurrent runs.** A baseline taken while another pytest ran against the
   same Postgres inflated a pure-SQLite test from 5.33s to 28.49s. Run the two
   halves sequentially, in one script, and record `aq test --aq-status`.
3. **Those 23 parametrized tests are themselves dual-support tax.** They exist
   only because two backends exist; T6 deletes the parametrization outright.
   Much of the SQLite baseline is cost the removal deletes rather than
   optimises.

**Two defects found in the substrate itself** — both are previews of what T6
would otherwise have hit blind:

* **Seed loss.** The migration chain inserts three `workspace_kinds` rows
  (`project-repo`, `vault`, `readonly-dir`). A bare truncate deleted them, so
  every test after the first in a leased database ran without the built-in
  workspace kinds. Six workspace-heavy `test_orchestrator.py` tests failed —
  and each passed in isolation, so it only reproduced under xdist. Fixed by
  snapshotting the template's non-empty tables once and replaying them after
  each truncate; this generalises to any future seeding migration.
* **Truncate cost.** 92 tables truncated in a `DO` loop cost 2619ms per test
  and dominated the run. One statement over only the non-empty tables: ~180ms.

**The `run_schema_setup` fast path is a T0 deliverable, not an optimisation.**
Today SQLite tests skip Alembic entirely because `_restore_schema_from_cache`
returns before it loads. T4 deletes that cache, after which *nothing*
short-circuits: every template-cloned database would parse all 114 revision
files to conclude there is nothing to do. `_is_stamped_at_head` replaces that
early return in 15 lines instead of 180, and unlike a `migrate=False` flag it
also makes daemon restarts, worker startup and every CLI invocation cheap
while keeping one code path through `initialize()`.

## T0.5 — Name every constraint in `tables.py` (new, prerequisite for T5)

Added 2026-09-07 after the T0 work surfaced it.

T5's exit criterion is "`alembic revision --autogenerate` against a fresh
Postgres produces an empty migration". **That is not currently satisfiable.**
`tables.py` carries unnamed `ForeignKeyConstraint`s — at minimum on
`agents.current_task_id` and `tasks.preferred_workspace_id` — so autogenerate
emits spurious operations for them on every run.

Caught live: an agent working on an unrelated task autogenerated a revision on
2026-09-07 that contained

```python
with op.batch_alter_table('agents', schema=None) as batch_op:
    batch_op.drop_constraint(None, type_='foreignkey')
```

which **cannot execute on PostgreSQL**:

```
CompileError: Can't emit DROP CONSTRAINT for constraint
ForeignKeyConstraint(..., None, table=Table('agents', ...)); it has no name
```

It only "works" under SQLite's batch-table-rebuild emulation, which drops and
recreates the whole table rather than issuing `ALTER`. This is the same hazard
`CLAUDE.md` already documents for unnamed `CheckConstraint`s, one level up.

### Work

1. Give every `ForeignKeyConstraint` in `tables.py` an explicit
   `name="fk_<table>_<column>"`, matching the existing `ck_<table>_<what>`
   convention.
2. One Alembic revision renaming the constraints the database already carries
   (Postgres auto-named them `<table>_<column>_fkey`), so the names in the
   schema and in `tables.py` agree.
3. Extend the `tests/test_migration_string_defaults.py` family with a check
   that no `Table` in `tables.py` has an unnamed `ForeignKeyConstraint`,
   `CheckConstraint` or `UniqueConstraint` — a ratchet, so this cannot
   regress.
4. Then assert the empty-autogenerate property as a test, which T5 inherits.

Independent of the SQLite removal: this is a live correctness bug for anyone
running Postgres today, and it makes every autogenerate output untrustworthy.
Worth landing on its own even if the removal is deferred.

## T1 — Config and backend selection

| File | Change |
|---|---|
| `src/config.py:773-808` | Delete `DatabaseConfig.backend`. `is_postgres_url` becomes a *validator*, not a selector. |
| `src/config.py:810` (`validate`) | A non-Postgres `database.url` is a hard `ConfigError` naming `aq db import-sqlite`. This replaces the fail-silent hazard documented at `config.py:748-752`. |
| `src/config.py:2103` | Delete the legacy `database_path` field (31 `src/` refs, 221 in tests — the tests go in T6). |
| `src/config.py:2289-2292` | Delete the `backend == "sqlite"` validation branch. |
| `src/database/__init__.py` | `create_database` constructs `PostgreSQLDatabaseAdapter` unconditionally and raises on a non-PG URL. `Database` alias → `PostgreSQLDatabaseAdapter`. Delete the `__getattr__` lazy-import shim and the "Adding a New Backend" docstring. |

Config migration for operators: `aq doctor --check db.backend_is_postgres`
detects a SQLite URL still in `~/.agent-queue/config.yaml`, explains the
one-way import, and `--fix` rewrites the URL after a successful import.

---

## T2 — SQLite import escape hatch (then freeze it)

The one place SQLite code legitimately survives.

- Move `src/database/legacy_sqlite_import.py` → `src/database/legacy_sqlite_import.py`.
- Sever its dependency on the SQLite *adapter*: it needs only a raw
  `create_async_engine("sqlite+aiosqlite://…")` and `tables.py`. Inline the
  three lines of `create_sqlite_engine` it uses so T4 can delete that function.
- Demote `aiosqlite` from a core dependency to an optional
  `[sqlite-import]` extra in `pyproject.toml`. The module imports it lazily and
  raises a clear "pip install agent-queue[sqlite-import]" on absence.
- Surface it as `aq db import-sqlite <path>` (`src/cli/db.py`). Delete
  `scripts/legacy_sqlite_import.py` (duplicate).
- Reachable from the setup wizard when it finds a legacy `.db` file.

**Deprecation:** keep for two minor releases, then delete the module and the
extra. Track it with a dated `# TODO(sqlite-import): remove after vX.Y` marker
that the ratchet test in T8 asserts is still dated.

---

## T3 — Query-layer debranching

25 modules, ~40 sites. Mechanical and behavior-preserving.

**Pattern A — upsert dispatch (most common).** In `hierarchy_queries.py`,
`playbook_artifact_queries.py`, `integration_control_queries.py`,
`plugin_queries.py` and others:

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert   # delete
...
insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert   # delete
```
→ import `pg_insert` only, use it directly.

**Pattern B — degraded SQLite paths.** Delete the slower branch, keep the PG one:
- `task_queries.py:63` — the two-statement claim fallback "for older SQLite".
- `playbook_artifact_queries.py:311` — the generic update-then-insert fallback
  for "any other dialect". There are no other dialects.
- `project_queries.py:154` — the dummy write that exists only to take SQLite's
  database write lock.
- `token_queries.py:84,320` — grouping in Python "to avoid dialect-specific date
  functions"; leave the behavior, delete the *comments* (moving it into SQL is
  T3-adjacent optimization, defer to §9).

**Pattern C — feature gates.** `hierarchy_queries.py:286` (`!= "postgresql"` →
early return) and `:1104`, `:1147`; `agent_queries.py:45`;
`agent_question_queries.py:175`; `archive_queries.py`; `claim_queries.py`;
`layout_queries.py`; `metrics_queries.py`; `gate_queries.py`;
`merge_slot_queries.py`; `workspace_kinds_queries.py`;
`assignment_route_queries.py`; `dependency_queries.py`;
`integration_schedule_queries.py`; `onboarding_queries.py`;
`transaction_queries.py`; `triage_queries.py`. Unwrap to the Postgres arm.

Also debranch the non-query consumers: `src/task_graph/creator.py`,
`src/integration/{status,outbox,cleanup,ownership}.py`,
`src/orchestrator/core.py`, `src/playbooks/retention.py`,
`src/commands/{task_commands,claim_commands}.py`, `src/prime/sections.py`,
`src/sessions/env.py`, `src/vault.py`, `src/facts_parser.py`,
`src/task_names.py`, `src/main.py`.

**Review rule for this phase:** every deleted branch must be the *SQLite* arm.
A reviewer should be able to confirm the diff changes no Postgres SQL at all.

---

## T4 — Engine, adapters, doctor

### `src/database/engine.py` (605 → ~330 lines)

Delete: `import sqlite3`; `_sqlite_database_path`; `_schema_cache_is_enabled`;
`_cached_template_is_valid`; `_remove_sqlite_sidecars`; `_copy_sqlite_database`;
`_build_schema_template`; `_restore_schema_from_cache`; `create_sqlite_engine`
and its `_set_sqlite_pragmas` event hook.

Keep and relocate: `_alembic_head_revisions`, `_schema_cache_directory`,
`_schema_cache_inputs`, `_schema_cache_key` — **T0's Postgres template builder
consumes these**. Move them to `src/database/schema_key.py` so the test
substrate does not import a production engine module.

`run_schema_setup` collapses to today's `_run_schema_setup_without_cache`; the
`migration_decision(...) == VERIFY` guard at the top is unchanged and stays.

### Adapters

- Delete `src/database/adapters/sqlite.py`. Move its mixin composition list into
  `adapters/postgresql.py` (they are identical; `postgresql.py` currently
  inherits the same set).
- `adapters/__init__.py` exports `PostgreSQLDatabaseAdapter` only.
- Delete `src/database/connection.py` (19-line legacy `create_sqlite_connection`
  shim) and fix its importers.
- `src/database/base.py` — strip the SQLite mention from the protocol docstring;
  the protocol itself is unchanged and stays (it is the test seam).

### Doctor

- `src/doctor/builtin.py:171-190` — delete `_sqlite_path` and the WAL-size check
  entirely. Deregister it from the check registry.
- `src/doctor/db_checks.py:348-356` — the DSN normalizer loses its SQLite arm.
- Add `db.backend_is_postgres` (T1).

### Guards

`src/database/migration_guard.py` and `src/database/hierarchy_migration.py` —
strip sqlite references. The scope model (`AQ_DB_SCOPE`, `VERIFY` on production)
is orthogonal and unchanged.

---

## T5 — Schema and Alembic

### `src/database/tables.py`

Delete the 14 `sqlite_where=text(...)` lines. **Verified: every one already has
a `postgresql_where=` twin on the same `Index`**, so this is a pure deletion
with no DDL change. Lines: 255, 440, 723, 1240, 1365, 1433, 1742, 1876, 2024,
2074, 2535, 2546, 2868, 3308. Strip the SQLite parity comments at 4, 293, 752.

### `migrations/env.py`

- `_get_url()` — drop the `sqlite+aiosqlite:///~/.agent-queue/agent-queue.db`
  default (line ~40) and raise when no URL resolves. A default that silently
  points at a file is exactly the failure mode being removed.
- `render_as_batch=True` → `False` at line 51 (offline) and `render_as_batch=is_sqlite`
  → removed at line 68 (online); delete the `is_sqlite` computation.
- Rewrite the module docstring (it currently opens "Supports both SQLite … and
  PostgreSQL").
- Keep `transaction_per_migration=True` — revision `b2c3d4e5f6a7` depends on it.

### Existing revisions — do not touch

113 revisions; 71 use `op.batch_alter_table` and 29 branch on dialect. They ran
against real databases and their checksums are load-bearing. `batch_alter_table`
degrades to a plain `ALTER` on Postgres, and the dialect branches simply take
the Postgres arm. Rewriting them buys nothing and risks everything.

### Verification

- **Autogenerate must be empty.** Against a fresh Postgres at head,
  `alembic revision --autogenerate` produces a no-op migration. This is the
  proof that stripping `sqlite_where` changed no actual DDL. Add it as a test.
- A new lint (T8 ratchet) rejects `batch_alter_table` and `dialect.name` in
  *newly added* revisions.

---

## T6 — Test suite

The bulk of the work: 194 files, 232 `Database(...)` call sites, 510 `.db"`
literals, 221 `database_path=` references.

### Codemod, not hand-editing

Write `scripts/codemod_sqlite_tests.py` (throwaway, deleted after the PR lands):

1. `Database(str(tmp_path / "<anything>.db"))` → the T0 `temp_db` fixture.
   Where the call is inside a factory closure (`tests/conftest.py:339,386`,
   `tests/session_dispatch_helpers.py:67`), rewrite the factory to take the
   fixture as a parameter — that covers a large share of the 194 files
   transitively.
2. Drop `database_path=...` from `AppConfig(...)` constructions.
3. `SQLiteDatabaseAdapter` → `PostgreSQLDatabaseAdapter` (15 files).

Run it, then hand-review the residue. Expect ~20–30 files that need real
thought; the rest should be pure mechanical.

### Files to delete or rewrite

| File | Action |
|---|---|
| `tests/test_legacy_sqlite_import.py` | Rewrite against `legacy_sqlite_import` |
| `tests/test_database_engine.py` (15 refs) | Delete SQLite-cache tests; keep PG engine tests |
| `tests/test_database_backend_selection.py` | Rewrite: the only assertion left is "non-PG URL raises" |
| `tests/test_migration_guard.py` (23 refs) | Re-point at PG scratch databases |
| `tests/test_doctor_db_checks.py` (7) | Drop WAL and SQLite-DSN cases |
| `tests/test_setup_wizard.py` (7) | Drop the SQLite selection flow |
| `tests/test_config.py` (5) | Drop `backend` and `database_path` cases |
| `tests/perf/test_claim_statements.py` (20) | Postgres-only statement budgets |
| `tests/test_facts_parser.py`, `test_facts_handler.py`, `test_facts_bidirectional_sync.py` | Re-point |
| The 20+ `tests/test_migration_*.py` | Move to `create_scratch_database` (tier 3) |

### Skips become failures

Today ~30 modules do `pytest.skip("POSTGRES_TEST_DSN not set")`. When Postgres
is the *only* backend, a skip is a silent hole in the only thing being tested.
Make `AQ_REQUIRE_POSTGRES_TESTS=1` the default and have the absence of a DSN
fail collection with instructions (`docker compose up -d postgres`, or the
non-Docker path from §5.2). This is a real ergonomics cost and the honest price
of the decision.

### `pyproject.toml`

- Remove `aiosqlite>=0.20.0` from `[project].dependencies`; re-add under a new
  `sqlite-import` extra.
- Promote `asyncpg>=0.29.0` from the `postgresql` extra into core dependencies;
  delete the now-empty extra (keep the name as an alias for one release so
  `pip install -e ".[dev,cli,postgresql]"` in the wild does not break).

---

## T7 — CI, packaging, operations

### `.github/workflows/tests.yml`

- Delete the SQLite migration-check step at line 252
  (`AGENT_QUEUE_DB_URL: sqlite+aiosqlite:///...ci-migration-check.db`). The
  Postgres equivalent at line 264 already exists and stays.
- Confirm the `postgres:18` service block (line 191) is job-level and therefore
  available to *every* matrix arm including `Tests (default)`; if it is
  step-scoped to some arms, hoist it.
- `pip install -e ".[dev,cli,postgresql]"` (line 246) → `".[dev,cli]"`.
- Add the T0 template-build step so each arm builds the schema template once
  rather than per worker.

### Operator-facing

- `docker-compose.yml` (`postgres:18-alpine`, port 5533) is unchanged and stays
  the documented happy path.
- `src/setup_wizard.py` — delete `_select_backend`'s SQLite arm and the
  `existing_sqlite_path` plumbing through `_select_postgresql` /
  `_build_pg_config`, except where it feeds `aq db import-sqlite`. **Add a
  non-Docker branch**: detect a system Postgres (`pg_isready`, brew/apt service)
  before offering the Compose boot, so Docker is a convenience and not a
  requirement.
- `scripts/e2e-env.sh` / `e2e-smoke.sh` already run on real Postgres — verify
  unchanged.

---

## T8 — Docs and the ratchet

### Ratchet test

`tests/test_sqlite_removal.py`, modelled on the existing
`tests/test_v1_removal.py`:

- No file under `src/` (except `legacy_sqlite_import.py`) matches
  `sqlite|aiosqlite|dialect\.name`.
- `src/database/adapters/sqlite.py` and `src/database/connection.py` do not exist.
- `aiosqlite` is not in core dependencies.
- No *new* Alembic revision uses `batch_alter_table` or `dialect.name`
  (compare against a committed allowlist of the 71 + 29 historical ones).
- The `legacy_sqlite_import` deprecation marker still carries a future date.

### Documentation

Rewrite (first-class, must be correct):
`docs/specs/database.md` (11 refs) · `docs/specs/config.md` (6) ·
`docs/specs/setup-wizard.md` · `docs/specs/packaging.md` ·
`docs/guides/migrations.md` · `docs/guides/architecture.md` (4) ·
`README.md` · `CLAUDE.md` (3) · `AGENTS.md` · `profile.md` (3).

`CLAUDE.md` specifically: the Testing section's "every fresh test database
replays 58 alembic migrations" paragraph is superseded by T0 and must be
rewritten with the measured numbers.

Leave alone (historical record, not instructions): `docs/analysis/*`,
`docs/superpowers/plans/*`, `docs/reviews/*`, and superseded specs. Add a
one-line note at the top of `docs/specs/database.md` pointing at this document
for the cutover date.

---

## 9. Follow-on work unlocked (explicitly out of scope)

Once the fallbacks are gone, these become available and should be filed as
separate tasks:

1. **`FOR UPDATE SKIP LOCKED` in the claim path.** `claim_queries.py`'s epoch
   fence and `pools.py` currently serialize where they need not.
2. **Advisory locks** for `merge_slot_queries.py` and the integration train,
   replacing row-lock-plus-retry.
3. **The ~20 `.with_for_update()` sites in `src/integration/`** (ci.py,
   ownership.py, recovery_controls.py) were silently no-ops under SQLite. They
   are now real. **Audit them** — some may now block where the SQLite-era tests
   never exercised contention.
4. Native JSONB for the `Text`-encoded JSON columns kept "for SQLite/PG parity"
   (`tables.py:752`, 805).
5. Date grouping pushed back into SQL (`token_queries.py:320`).

Item 3 is the one to schedule soon: it is the clearest example of the class of
bug dual-backend support was hiding.

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| T0 substrate is slower than the SQLite cache, suite becomes unrunnable locally | Medium | T0 is a hard gate with a measured 1.5× budget; abort the project rather than proceed |
| Merge conflicts — 25 query modules + 194 test files | **High** | Merge freeze around T6; land the codemod in one PR, not incrementally |
| A silently-no-op `with_for_update` becomes a real lock and deadlocks | Medium | §9 item 3 audit; run `scripts/e2e-smoke.sh` (real daemon, real PG) after T3/T4 |
| Contributor onboarding regresses — Postgres now mandatory | **High** | §T7 non-Docker wizard branch; collection-time failure message must include copy-pasteable setup |
| An operator's `config.yaml` still names a SQLite path after upgrade | High | T1 fail-fast + `aq doctor --fix` + `aq db import-sqlite` |
| Stripping `sqlite_where` accidentally alters DDL | Low | Verified paired `postgresql_where` on all 14; T5 empty-autogenerate test proves it |

## 11. Rollback

Each phase is a separate revert-able commit. The point of no return is **T6**
(the test codemod) — before it, reverting T1–T5 restores a working dual-backend
tree. After T6, roll forward only. Tag `pre-sqlite-removal` on the commit before
T1 lands.

---

## 12. Exit criteria

- `grep -ri sqlite src/` returns only `legacy_sqlite_import.py`.
- `tests/test_sqlite_removal.py` green.
- `alembic revision --autogenerate` against a fresh Postgres is empty.
- Full suite green on Postgres with **zero** `POSTGRES_TEST_DSN` skips.
- `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` green.
- Measured suite wall clock recorded in `CLAUDE.md`.
- A fresh box goes from `git clone` to a running daemon following only
  `README.md`, without Docker.
