# Migrations

The schema changes over time. This page is how it changes, who is allowed to
change it, and what to do when a database and a checkout disagree.

Two readers are served here. If you run an AQ install, read
[Who may migrate what](#who-may-migrate-what), [Operator commands](#operator-commands),
[Backup and restore](#backup-and-restore) and [When something is wrong](#when-something-is-wrong).
If you are changing the schema, also read [Adding a revision](#adding-a-revision).

## Vocabulary

* **Alembic** — the migration tool. It keeps an ordered chain of revisions and
  records which one a database is at.
* **Revision** — one migration file in
  [`migrations/versions/`](../../../migrations/versions/), with an `upgrade()`
  and a `downgrade()`, naming its parent as `down_revision`.
* **Head** — the newest revision in the chain. This checkout's head is
  `a0000000000c`; there is exactly one, and
  `tests/test_migration_single_head.py` fails if a branch ever creates a second.
* **Stamped** — the revision id recorded in the database's `alembic_version`
  table. "Stamped at head" means the schema matches this checkout.
* **Production database** — whatever `database.url` in
  `~/.agent-queue/config.yaml` points at. This definition deliberately ignores
  `AGENT_QUEUE_DB` and `AQ_DATABASE_URL`, because those are the overrides worker
  sessions get, and letting an override redefine "production" would disarm the
  guard exactly where it matters.

## What the chain looks like today

On 2026-09-07 the 117 revisions that preceded it were collapsed into one
squashed baseline, at the same time SQLite support was removed. Most of their
weight was dual-backend tax — 71 used `op.batch_alter_table`, which exists only
because SQLite cannot `ALTER TABLE`, and 29 branched on the dialect name.
Replaying them cost about eight seconds per fresh database.

What is left is a baseline plus eleven revisions:

| Revision | What it does |
|---|---|
| [`a00000000001`](../../../migrations/versions/a00000000001_squashed_baseline.py) | The squashed baseline. `metadata.create_all` from [`tables.py`](../../../src/database/tables.py), then installs the PL/pgSQL guards, then seeds the three built-in workspace kinds — `create_all` builds tables, not rows, and the orchestrator cannot acquire a workspace without them. |
| [`a00000000002`](../../../migrations/versions/a00000000002_restore_integration_guards.py) | Reinstalls the procedural guards the first cut of the baseline omitted. Idempotent, so a fresh database and a database built by the original squash both end up correct. |
| [`a00000000003`](../../../migrations/versions/a00000000003_add_min_per_project.py) | `agent_profiles.min_per_project` — the per-project warm floor, once pool sizing became fleet-wide. |
| [`a00000000004`](../../../migrations/versions/a00000000004_task_branch_origin_discard.py) | Branch-discard intent on `task_branch_origins`, and narrows the immutability trigger so a materialised origin can be retired at all. |
| [`a00000000005`](../../../migrations/versions/a00000000005_resolution_push_start_fence.py) | Marks pre-marker `resolution_reserved` intents with a `0.0` "push-start time unknown" sentinel — *not* "the push started at the epoch". |
| [`a00000000006`](../../../migrations/versions/a00000000006_legacy_resolution_recovery_evidence.py) | Keeps an audited remote observation so a legacy resolution can be recovered. |
| [`a00000000007`](../../../migrations/versions/a00000000007_candidate_rejection_recovery.py) | Retains rejected candidate-repair pushes as immutable audit and permits exactly one replacement reservation. |
| [`a00000000008`](../../../migrations/versions/a00000000008_recover_unwritten_resolution.py) | A durable pre-write marker plus the links needed to retain a malformed reservation while a fresh one is reserved. |
| [`a00000000009`](../../../migrations/versions/a00000000009_durable_escalation_state.py) | The four escalation and digest tables. |
| [`a0000000000a`](../../../migrations/versions/a0000000000a_add_task_comment_kind.py) | `task_comments.kind` — `note` vs `progress`, so the digest cannot treat chatter as progress. |
| [`a0000000000b`](../../../migrations/versions/a0000000000b_escalation_actions.py) | Durable verified-reply action reservations. |
| [`a0000000000c`](../../../migrations/versions/a0000000000c_development_integration.py) | The development delivery journal and its rollout mode. **Current head.** |

Supporting files in the same directory:

| File | Purpose |
|---|---|
| [`alembic.ini`](../../../alembic.ini) | Alembic configuration. `sqlalchemy.url` is deliberately empty — the URL is supplied programmatically. A `ruff check --fix` post-write hook formats generated revisions. |
| [`migrations/env.py`](../../../migrations/env.py) | The environment. Two things matter: `target_metadata` is `src.database.tables.metadata`, and `transaction_per_migration=True`, because one revision opens a *second* connection to inspect what the previous revision created. Its docstring and a `render_as_batch` branch still mention SQLite; PostgreSQL is the only supported backend. |
| [`migrations/integration_guards.py`](../../../migrations/integration_guards.py) | 29 PL/pgSQL functions and 50 triggers over 31 integration tables, snapshotted at the pre-squash head. Table metadata cannot express a trigger, so these are carried separately. **Immutable** — a behaviour change is a new revision, not an edit here. |
| [`migrations/script.py.mako`](../../../migrations/script.py.mako) | The template new revisions are generated from. |

## Who may migrate what

Only the daemon, or an operator who typed `aq db upgrade`, may migrate the
production database. Everything else — the CLI, pytest, worker sessions — may
migrate its own scratch databases freely and must never touch production's
schema.

[`migration_guard.py`](../../../src/database/migration_guard.py) makes this two
independent facts rather than a privilege ladder:

| | |
|---|---|
| **Who is asking** | `current_scope()`: `AQ_DB_SCOPE` in the environment → a scope declared with `set_process_scope()` → an `AQ_SESSION_ID` in the environment (⇒ `worker`) → pytest (⇒ `test`) → otherwise `cli`. |
| **What they point at** | `is_production_database(url)`: read straight from `config.yaml`, ignoring the override variables. |

```mermaid
flowchart TD
    S["run_schema_setup(engine)"] --> P{"Is this the<br/>production database?"}
    P -- no --> M["MIGRATE — alembic upgrade head"]
    P -- yes --> W{"Scope is daemon<br/>or operator?"}
    W -- yes --> M
    W -- no --> V["VERIFY — read only"]
    V --> V1["stamped at head → proceed"]
    V --> V2["unknown revision → orphan diagnostic"]
    V --> V3["behind head → SchemaBehindCode:<br/>'ask the operator to upgrade'"]
```

The environment variable outranks the process scope on purpose. A worker
session carries `AQ_DB_SCOPE=worker`, so an `aq start` launched *inside a
worktree slot* still resolves to `worker` and still refuses, even though the
daemon it would boot declares itself the daemon. An operator who genuinely
needs to migrate from an unusual place sets `AQ_DB_SCOPE=operator` explicitly —
a decision they can be held to, rather than an accident.

> **If you are a worker session and you see "schema behind code; ask the
> operator to upgrade", that is the guard working.** Report it. Do not run
> `alembic upgrade`, `alembic stamp` or `aq start` to get around it. On
> 2026-09-02 a worker did exactly that: production ended up stamped with
> revisions that existed only on unmerged branches, the daemon refused to boot
> with "Alembic preflight failed", and an operator had to hand-write a merge
> revision to recover. `aq db current` is the read-only "am I behind?" answer
> and is always safe.

## Worker and test databases are not the production database

Everything that is not the configured production URL migrates exactly as it
always did. Three separate mechanisms rely on that:

**Worker sessions.** A session launched into a worktree slot carries
`AQ_DB_SCOPE=worker` plus `AQ_DATABASE_URL` / `AGENT_QUEUE_DB` set to a sentinel
the CLI refuses with an explanation. Leave all three alone. The worker can still
*read* production through the daemon's API; it simply cannot change its schema.

**The test suite.** `tests/conftest.py` calls
`assert_not_production_database(...)`, so the suite can never be pointed at the
daemon's real database whatever the environment says. Each run gets an ownership
token; workers and migration tests create uniquely named `aq_test_*` databases,
and a graceful teardown drops only names that process created. An unexpected
name collision is inspected read-only and then refused — the harness never
stamps, migrates, drops or repairs a database it did not create.

**The schema template.** [`schema_key.py`](../../../src/database/schema_key.py)
hashes every file that decides what a migrated schema looks like — `tables.py`,
`hierarchy_migration.py`, `env.py`, `integration_guards.py` and every revision,
by path *and* content. That key names a PostgreSQL template database the suite
clones with `CREATE DATABASE … TEMPLATE`, which is why a fresh test database
costs milliseconds. Touch any of those files and the next run builds a new
template by itself.

To run the PostgreSQL-backed and migration-marked tests locally:

```bash
export POSTGRES_TEST_DSN=postgresql://postgres:postgres@localhost:5533/postgres
aq test tests/test_database_postgresql.py
aq test -m migration tests/test_migration_postgres_upgrade_head.py
```

`aq test` deselects the `migration` marker by default, so pass `-m migration`
(or `--aq-all-markers`) when the change *is* about migrations. Without
`POSTGRES_TEST_DSN` the PostgreSQL half of the suite skips rather than failing.

## Operator commands

```bash
aq db current    # read-only: stamped revision(s) vs. this checkout's head
aq db upgrade    # the one sanctioned migration path (refuses inside a slot)
```

`aq db current` prints the database (with its password redacted), what it is
stamped at, and this checkout's head, and exits non-zero when they differ. It
opens a connection and runs no migration.

`aq db upgrade` is the single exception the migration policy names: it claims
`operator` scope for the duration of the upgrade, and nothing else in the CLI
ever does. It refuses outright inside a worker session, names the target
database, and asks for confirmation unless you pass `--yes`. Run it from a
normal shell, not from a worktree slot.

Two more commands touch the schema:

```bash
aq db import-sqlite <path>          # one-way, one-time carry-over from a pre-PostgreSQL database
aq system db-preflight-hierarchy    # dry-run the hierarchy canonicalisation and see its rejects
```

`import-sqlite` requires the target PostgreSQL database to be **empty** —
importing over a live database would interleave two histories — and needs the
optional reader (`pip install "agent-queue[sqlite-import]"`). It copies tables
in foreign-key-safe order, restores deferred columns, and resets sequences.

## Backup and restore

AQ ships no backup command. The database is an ordinary PostgreSQL database and
the ordinary tools are the right ones. Take a backup **before** any upgrade you
have not run elsewhere.

```bash
# Stop the daemon first so nothing writes mid-dump.
aq stop

# Custom-format dump: compressed, and restorable table-by-table.
pg_dump --format=custom --no-owner --file agent-queue-$(date +%F).dump \
        "postgresql://<user>@<host>:<port>/<dbname>"

aq start
```

Restoring into a *new*, empty database is the safe shape — it leaves the
original untouched while you check the copy:

```bash
createdb agent_queue_restore
pg_restore --no-owner --dbname \
    "postgresql://<user>@<host>:<port>/agent_queue_restore" agent-queue-2026-09-09.dump

# What revision did the dump carry?
psql "postgresql://<user>@<host>:<port>/agent_queue_restore" \
     -c "SELECT version_num FROM alembic_version"
```

`aq db current` always reads `~/.agent-queue/config.yaml`, so it reports on the
*live* database, not the restored copy — read `alembic_version` directly, as
above, to see where the copy stands.

Notes that matter:

* A dump captures `alembic_version` too, so a restored database comes back
  stamped at whatever revision it was taken at. If that is behind your
  checkout, `aq db upgrade` brings it forward — after you have confirmed it is
  the database you meant.
* The dump contains **secrets**: API session token hashes, DSNs recorded in
  config-derived rows, repository URLs. Treat it as sensitive.
* `pg_dump --data-only` and hand-edited restores are not supported: the
  procedural guards reject `UPDATE` and `DELETE` on evidence tables, and a
  partial restore will trip them in ways that are hard to reason about. Restore
  whole databases.
* Never restore *over* a running install's database. Restore beside it, verify,
  then repoint `database.url`.

## When something is wrong

### "schema behind code; ask the operator to upgrade"

Your process may read this database but not migrate it. Ask the operator to run
`aq db upgrade` outside a worktree slot, or to restart the daemon (which
migrates as `daemon` scope). Nothing is broken.

### "Alembic preflight failed: alembic_version references unknown revision(s)"

The database is stamped with a revision this checkout does not contain. AQ never
auto-repairs this — clobbering the row loses history and can silently skip data
migrations — so it raises a diagnostic naming the resolved URL instead.

```bash
aq doctor --check db.alembic_orphan          # which branch and file define it
aq doctor --check db.alembic_orphan --fix    # run that revision's own downgrade()
```

The check searches every `refs/remotes` and `refs/heads` for the file declaring
the unknown revision, so the answer is a branch name and a path rather than a
bare hex id. `--fix` borrows that file into a private temporary directory that
Alembic reads alongside `migrations/versions/` for exactly one `alembic
downgrade`, leaving the database at the orphan's parent. It never writes into
the checkout: a file that appears in and vanishes from the real `versions/`
makes any concurrent Alembic scan fail. Stamping *past* an orphan whose file
cannot be found anywhere is a second, separate opt-in
(`AQ_DOCTOR_ALEMBIC_STAMP=1`), because it leaves whatever DDL the orphan applied
in place.

### "Existing database has tables but no alembic_version"

A pre-Alembic database. Its migration history cannot be verified, and the squash
cannot infer which migrations it needs. Bring it to the pre-squash head
`6ad7aebb8c7c` using the previous release first, then retry. Nothing has been
changed.

### A database stamped at the pre-squash head

Handled automatically. A database at `6ad7aebb8c7c` already *has* the baseline's
exact schema, so the engine re-stamps it forward to `a00000000001` rather than
replaying a `create_all` over live tables. A database stamped anywhere *earlier*
does not qualify: it has an incomplete schema, and the engine raises the ordinary
unknown-revision diagnostic rather than marking a half-migrated database current.

## Adding a revision

The schema's source of truth is
[`tables.py`](../../../src/database/tables.py). Edit it first, then generate the
migration that makes a database match it.

```bash
alembic revision --autogenerate -m "description of change"
# Review the generated file in migrations/versions/ — always.
```

**Never run `alembic upgrade` from a worktree slot.** Test the revision against
a scratch database, or through the test suite, which builds its own.

Rules learned the hard way:

* **Name every `CheckConstraint`** (`name="ck_<table>_<what>"`). Autogenerate
  matches check constraints by name only, so an unnamed one in `tables.py` is
  invisible to the comparison and every later autogenerate emits a spurious
  `drop_constraint` for the auto-named constraint the database actually carries.
  `tests/test_migration_metadata.py` is where autogenerate-drift regressions of
  this shape are pinned.
* **`server_default` takes the bare value.** `server_default="system"`, never
  `"'system'"` — SQLAlchemy quotes it again and the DDL becomes
  `DEFAULT '''system'''`. Booleans use `sa.false()` / `sa.true()`, never `"0"` /
  `"1"`. Guarded by `tests/test_migration_string_defaults.py` and
  `tests/test_migration_boolean_defaults.py`.
* **PostgreSQL only.** No `batch_alter_table`, no `dialect.name` branches;
  `tests/test_sqlite_removal.py` is the ratchet.
* **Be idempotent.** The squashed baseline builds from live metadata, so a fresh
  database may already have your new column. Guard a `add_column` with an
  inspector check rather than assuming.
* **Autogenerate misses renames.** It sees a drop plus an add. Review and fix by
  hand, or you will lose the column's data.
* **One head.** Base your revision on the current head, and re-check
  `alembic heads` after rebasing — two branches that both picked the next
  `a0000000000N` collide, and the fix is to re-chain the later file.

Then check what you produced:

```bash
aq test tests/test_migration_single_head.py tests/test_migration_metadata.py
aq test tests/test_migration_string_defaults.py tests/test_migration_boolean_defaults.py
aq test tests/test_database.py
POSTGRES_TEST_DSN=... aq test -m migration tests/test_migration_postgres_upgrade_head.py
```

### Startup data normalisations

Separately from Alembic, `db.initialize()` runs a handful of idempotent row
normalisations every start, in
[`engine.py`](../../../src/database/engine.py): resolve relative workspace paths
to absolute, remove link workspaces whose path belongs to another project, drop
the long-dead `agent_workspaces` and `workspace_locks` tables, and copy a legacy
`repos` row's URL into its project's columns. They are safe to re-run and are
not part of the revision chain.

### The hierarchy canonicalisation

[`hierarchy_migration.py`](../../../src/database/hierarchy_migration.py) is the
one data migration complex enough to have its own module and its own dry run. It
snapshots parent pointers and `parent-child` edges, chooses one canonical parent
per task, validates the whole candidate graph (parent exists, same project, no
cycle, depth ≤ 3), drops offenders into `hierarchy_migration_rejects`, and only
then rewrites edges and pointers. Preview it without changing anything:

```bash
aq system db-preflight-hierarchy
```

## Related pages

* [The database](README.md) · [Table families](tables.md) ·
  [Query modules](queries.md) · [Data lifecycle](data-lifecycle.md)
* [Migration policy guide](../../guides/migrations.md) — the same policy, and the incident behind it
* [Module catalog — database](../modules/database.md)
