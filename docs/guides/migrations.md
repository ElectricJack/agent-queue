---
tags: [database, migrations, operations]
---

# Migration policy

Who may run Alembic, against which database, what happens when they may not,
and the incident that made it a rule.

For everything else about the schema — what the revision chain contains, how to
add a revision, how to back up and restore, and what each diagnostic means —
see the [database migrations reference](../reference/database/migrations.md).

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
aq db retire     # read-only: what still uses each legacy integration table
```

### Retiring legacy integration tables

rev-agile-ridge §5.3 keeps a few integration tables and retires 37 legacy
ones, grouped into seven families (`FAMILIES` in
`src/integration/table_retirement.py`). No migration drops one. Revision
`a00000000064` only adds the append-only archive, `integration_table_retirements`
and `integration_retired_rows`. Dropping a family is this explicit operator
step, and it changes nothing unless every check passes:

```bash
aq db retire                       # every family: rows, source files, outside FKs
aq db retire writers               # one family; exit 0 only when it is ready
docker exec aq-postgres pg_dump -U agent_queue -d agent_queue -Fc \
  > ~/agent-queue-$(date +%Y%m%d-%H%M).dump
aq db retire writers --apply --backup ~/agent-queue-<stamp>.dump
aq db restore-retired integration_branch_owners   # replay one table if needed
```

A family is ready when no Python file under `src/` names any of its tables (so
its code and its `tables.py` entries are gone) and no foreign key reaches one
from outside the families being retired. `repair` and `parent` reference each
other, so they are named together: `aq db retire repair parent --apply …`.
The check reads this checkout's files, not the running daemon's, so restart
the daemon onto the code that removed the family (`aq restart --no-dashboard`)
before `--apply`.
`--apply` refuses inside a worker slot and needs a `pg_dump --format=custom`
backup from the last 24 hours. In one transaction it takes `ACCESS EXCLUSIVE`
locks with `NOWAIT` (a table in use refuses), re-checks readiness, copies every
row into the archive, records a receipt whose row count and digest the archive
must reproduce, and drops the tables. Running it again is a no-op. Downgrading
past `a00000000064` is refused once a receipt exists. `restore-retired`
recreates an absent table with its recorded columns only (constraints, defaults
and indexes come from the backup when they are needed) and refuses unless the
restored rows reproduce the receipt.

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
