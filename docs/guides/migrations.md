---
tags: [database, migrations, operations]
---

# Migration policy

Who may run Alembic, against which database, what happens when they may not,
and the incident that made it a rule.

For everything else about the schema — what the revision chain contains, how to
add a revision, how to back up and restore, and what each diagnostic means —
see the [database migrations reference](../reference/database/migrations.md).

## Release compatibility and code rollback

Check the exact release range before promotion:

```bash
python scripts/check_additive_migrations.py --from-ref v0.1.0 --to-ref v0.2.0 --json
```

The checker reads pinned Git objects without executing migrations. It allows
new tables, indexes, constraints, and nullable or server-defaulted columns.
Upgrade drops, renames, type changes, non-null columns without server defaults,
raw SQL and imported helpers that cannot be proved additive fail with file and
line evidence. Drops in `downgrade()` do not affect code rollback. Historical
migration edits, missing parents, duplicate revision ids and multiple heads
also fail; resolve the graph before promoting.

An older checkout cannot infer a newer migration's compatibility from its id.
Retain the newer release's complete, unmodified `migrations/versions/` directory
outside the checkout before rolling back, and configure the database startup:

```yaml
database:
  schema_ahead_migrations: /path/to/retained-newer-release/migrations/versions
  schema_ahead_max_revisions: 1
```

The guard proves that the database stamp descends from the code head, that all
extra upgrades pass the additive checker, and that retained historical sources
match the running checkout. Accepted ahead schemas are left untouched by Alembic.
The limit counts extra **revisions**, including both sides of a merge, rather
than releases. Set it to the number of migrations in the supported rollback
window; zero disallows an ahead schema. Its default is `null`, so no distance
limit is enabled before cutover. With no retained directory, the existing
unknown-revision refusal remains in effect. Behind schemas retain the existing
operator/daemon migration rules, including multiple known heads awaiting a
merge revision. The direct plugin client uses this policy as well. With retained
migrations configured, `aq doctor --check db.alembic_orphan` applies the same
proof and limit and never offers or runs a downgrade or stamp, even when
retained evidence is rejected. Refusals explain how to deploy matching code or
supply evidence; downgrading or stamping to bypass the guard is not a rollback.

Run a drill on an explicitly provisioned, empty scratch PostgreSQL database:

```bash
AQ_ROLLBACK_DRILL_DSN=postgresql://user:password@localhost/scratch_rollback \
  python scripts/rollback_drill.py --previous-tag v0.1.0 --current-tag v0.2.0 \
  --max-revisions 1 --seed-sql /path/to/scratch-seed.sql
```

The script exports both pinned release trees, initializes A's database adapter,
upgrades with B, optionally inserts fixture rows, then initializes A's database
adapter against B's schema using B's retained migrations. This exercises
`run_schema_setup` and startup data migrations. Both tags must include schema-ahead
support. Every public table's contents and the Alembic stamp are hashed before
and after the rollback probe; changed rows fail the drill. JSON output records
the commit ids and per-table evidence. It launches no live daemon, modifies no
checkout, performs no downgrade or restore, and leaves the scratch database
available for inspection. It refuses the configured production database and
any nonempty database. The fixture check seeds actual task, event, durable-wait
and approval rows:

```bash
aq test -m migration tests/test_update_rollback.py
```

> **This page used to be a technical-debt inventory** of `ALTER TABLE`
> one-liners run from `Database.initialize()`. That is no longer how the schema
> changes: schema changes are Alembic revisions under `migrations/versions/`,
> which is a squashed baseline plus eleven revisions. The inventory has been
> removed; the policy below is current.

## Who may run migrations

Migrations against the database named in `~/.agent-queue/config.yaml` are
**daemon-only**. `src/database/migration_guard.py` enforces it, and
`run_schema_setup` consults it before touching Alembic.

The policy is two independent facts, not a privilege ladder:

| | |
|---|---|
| **who is asking** | `current_scope()` — `AQ_DB_SCOPE` env, then a scope declared with `set_process_scope()`, then `AQ_SESSION_ID` (⇒ `worker`), then pytest (⇒ `test`), else `cli`. |
| **what they point at** | `is_production_database(url)` — read straight from `config.yaml`, deliberately ignoring `AGENT_QUEUE_DB` / `AQ_DATABASE_URL` so an override cannot redefine what "production" means. |

`migration_decision()` combines them: **migrate**, unless the URL is
production and the scope is neither `daemon` nor `operator` — then **verify**.
Verifying reads `alembic_version`, and raises rather than repairing:

* stamped at this checkout's head → proceed (a worker may *read* production);
* stamped at a revision this checkout lacks → the unknown-revision diagnostic;
* anything else → `SchemaBehindCode`: *schema behind code; ask the operator to
  upgrade*.

Every other database — every leased test database, every per-xdist-worker
Postgres database, every e2e scratch DSN — is not production and migrates
exactly as it always did.

The pytest substrate treats `POSTGRES_TEST_DSN` as a maintenance connection,
not as a reusable test database. `aq test` assigns each run an ownership token;
workers and migration tests create unique `aq_test_*` databases and a graceful
session teardown drops only names created by that process. An unexpected name
collision is inspected read-only for stale/unknown Alembic revisions and then
refused. The harness never stamps, migrates, drops, or otherwise repairs a
database it did not create, with one exception: a background sweep drops
`aq_test_ownv2_*` databases whose owner's advisory lock proves the process
that created them is gone (see [resource gating](resource-gating.md)).

### Why the env var beats the process scope

`AQ_DB_SCOPE` in the environment outranks `set_process_scope(DAEMON)`. Worker
sessions carry `AQ_DB_SCOPE=worker`
(`src.sessions.env.session_db_isolation`), so an `aq start` launched *inside a
worktree slot* still resolves to `worker` and still refuses — which is one of
the three ways the 2026-09-02 incident actually happened. An operator who
needs to migrate from an unusual place sets `AQ_DB_SCOPE=operator` and owns
that decision.

### Operator commands

```bash
aq db current    # read-only: stamped revision(s) vs. this checkout's head
aq db upgrade    # the one sanctioned migration path (refuses inside a slot)
```

### Legacy conflict-resolution reservations

Revision `a00000000005` treats a pre-marker `resolution_reserved` intent as
an uncertain external write; the upgrade records a `0.0` unknown-start
sentinel rather than treating its missing marker as proof that it was never
pushed. For the known legacy receipt, first reconcile only the frozen remote
identity:

```bash
aq system integration-reconcile-promotion \
  --intent-id receipt-71755ac2-3bca-5bfe-8221-11fd243860fc
```

`applied` means the remote exactly matches the reserved head and the existing
receipt path can complete. If it instead reports `not_applied` because the
remote is still the receipt's immutable expected old target, revision
`a00000000006` permits this narrowly bounded public recovery:

```bash
aq integration resume operation81d0aaee-0c3a-482c-b04c-d3afe6631cbe
```

The command requires the same live delegate session, claim epoch, workspace,
branch owner and fence recorded by the receipt. It reads the exact remote tip
under the retained-repository lock and records that old-tip observation before
rearming the existing stage. The subsequent writer uses its ordinary
expected-old → frozen-head push fence and receipt path; no writer, receipt or
frozen resolution identity is replaced. A remote mismatch, unavailable remote,
manual hold, changed ownership, duplicate owner, or any second unresolved
intent returns `ambiguous` without changing the deadline or operation state.

### When production is already stamped with an orphan

With `database.schema_ahead_migrations` configured, this check uses the retained
migration proof described above. `--fix` leaves both schema and stamp untouched,
including when the proof fails. The repair procedure below applies only when
retained migrations are not configured.

```bash
aq doctor --check db.alembic_orphan          # which branch/file defines it
aq doctor --check db.alembic_orphan --fix    # run that revision's own downgrade()
```

The check searches every `refs/remotes` and `refs/heads` for the migration
file that declares the unknown revision, so the answer is a branch name and a
path rather than a bare hex id. `--fix` borrows that file into a private
temporary directory that Alembic reads alongside `migrations/versions/` (via
`version_locations`) for exactly one `alembic downgrade`, leaving the database
at the orphan's parent. It never writes into the checkout: a borrowed file
that appears in and vanishes from the real `versions/` makes any concurrent
Alembic scan (a second shell, a pytest-xdist worker) fail with `Can't find
Python file`. Stamping past an orphan
whose file cannot be found anywhere is a second, separate opt-in
(`AQ_DOCTOR_ALEMBIC_STAMP=1`), because it leaves whatever DDL the orphan
applied in place.

### The 2026-09-02 incident

A worker session in a worktree slot ran Alembic against the production URL —
directly, through a pytest fixture, and through an `aq start`. Production's
`alembic_version` ended up naming `e7a2b9c41d05` and then `f2a4c6e8b0d2`,
revisions that live only on unmerged branches. The daemon refused to start
("Alembic preflight failed: alembic_version references unknown revision(s)")
until the operator hand-wrote merge revision `23daf00e`.

## Where the rest of it went

| Question | Page |
|---|---|
| What is in the revision chain? | [Migrations reference → the chain](../reference/database/migrations.md#what-the-chain-looks-like-today) |
| How do I add a revision? | [Migrations reference → adding a revision](../reference/database/migrations.md#adding-a-revision) |
| How do I back up and restore? | [Migrations reference → backup and restore](../reference/database/migrations.md#backup-and-restore) |
| What do the tables mean? | [Table families](../reference/database/tables.md) |
| What happens when a task is deleted or archived? | [Data lifecycle](../reference/database/data-lifecycle.md) |
| Where does this row come from? | [Query modules](../reference/database/queries.md) |
