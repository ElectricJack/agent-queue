# Knowledge records: migration, restore rehearsal and rollout runbook

This runbook is the operator procedure for the durable knowledge and records
domain: taking a backup that is actually complete, rehearsing a restore in a
disposable environment, reconciling legacy sources against the inventory, and
turning the domain on and off behind explicit human gates.

> **Warning.** Nothing in this page has been executed. Every command block is a
> template for an operator to run, and none of them was run against a real
> install while this page was written. No production restore rehearsal, performance
> benchmark, import apply or live import has been performed, and this page
> reports no measured result. K14's separate [synthetic rehearsal report](../reports/2026-10-03-knowledge-deprecation-rehearsal.md)
> records disposable test evidence; it does not execute these operator templates.
> Where a block would produce evidence, the
> verification command that reads it back is named instead of pasted output.
> Activation, schema upgrade, paid-provider enablement, live import and deletion
> are operator decisions and are not authorised by this document.

Read the mechanism first:
[knowledge protection (K05)](../specs/design/knowledge-protection.md), which
owns the gates this page defers to, and the schema catalog in
[docs/specs/database.md](../specs/database.md#durable-knowledge--records) for
every table named here.

## What this runbook is for

The knowledge domain is **inert by default**. `knowledge.enabled` ships `false`,
so a fresh install has the tables and no writes. That is deliberate: rollout is
a sequence of human decisions, each with recorded evidence, and the code never
turns a flag on for you.

Four operations need a written procedure, and this page gives one each:

| Operation | Who runs it | What proves it worked |
|---|---|---|
| [Back up](#1-back-up-the-two-places-knowledge-lives) | Local operator | Two archives with verified checksums, and a recorded pre-backup state |
| [Rehearse a restore](#2-rehearse-a-restore-in-a-disposable-environment) | Local operator | Rehearsal proof: hash-identical reads in an environment that cannot reach production |
| [Reconcile legacy sources](#3-reconcile-legacy-sources-against-the-sealed-manifest) | Local operator | A sealed manifest whose accounting closes, with every disposition accounted for |
| [Roll out or roll back](#4-roll-out-one-gate-at-a-time) | Local operator, per gate | The gate's own evidence, recorded outside this page |

The final deprecation decision (G7) additionally requires compatibility-call
telemetry and explicit removal evidence. See [knowledge deprecation and
restore](knowledge-deprecation.md) for the implemented diagnostic, evidence
helper and synthetic proof. G7 remains closed pending operator evidence and a
decision; the helper grants no uninstall or deletion permission.

## Where knowledge state lives

Three places hold knowledge state, and a backup that covers only two of them is
not a backup.

| State | Where | Authority |
|---|---|---|
| Records, revisions, payloads, links, proposals, authority grants, shares, redaction ledger, import receipts and mappings | PostgreSQL, the daemon's database | Authoritative. A revision's stored `content_sha256` is its identity; `records.revision_hashes` re-derives it for a sample and `knowledge export` re-derives it on demand |
| Managed Markdown exports of a record revision | `<data_dir>/vault/projects/<project>/knowledge/records/<kn-alias>.md`, and `.../system/knowledge/records/<kn-alias>.md` for global scope | A projection. Rebuilt from the database; a human edit marks the export `diverged` and is never overwritten |
| Retained source artifacts | Under `<data_dir>/vault/record-artifacts/`, a namespace reserved for imports | Evidence. Erasure reaches only beneath that namespace |

The export paths and the confinement rules are in
[src/records/export.py](../../src/records/export.py) (`managed_parts`,
`render_export`); the artifact namespace check is in
[src/knowledge/redaction.py](../../src/knowledge/redaction.py) (`purge_event`).

Two consequences for backup and restore:

- **The database is the only state you cannot rebuild from the vault.** A vault
  copy with no matching `pg_dump` restores Markdown projections and no history.
- **A restore is never a production operation.** The complete post-backup
  redaction ledger must be reapplied before any read, export, context or
  provider delivery path is re-enabled; otherwise a backup taken before an
  erasure can resurrect the erased payload. Restoring an older backup *without*
  the complete ledger is unsupported, and already-exported human copies cannot
  be recalled by this system at all.

> **Note.** `aq knowledge restore` is not a database restore. It is the
> record-level command that returns a retired finding to a retained revision
> (`--identity`, `--revision-id`, `--if-revision`, `--reason`,
> `--idempotency-key`). It creates a new revision and clears verification. It
> never touches a backup.

## Permission boundaries

Knowledge authorisation is fail-closed and resolves from the principal, not
from the profile alone. A worker can propose a correction and cannot decide it,
verify it, grant authority over it, erase it, share it globally or run an
import. The table below is what
[src/records/auth.py](../../src/records/auth.py) (`authorize_on`) enforces.

| Action | Worker (session token) | Elevated supervisor | Local operator (`aq` on loopback) |
|---|---|---|---|
| Read records the principal can see | Yes, in its own task's project | Yes | Yes |
| Create a finding (always unverified) | Yes | Yes | Yes |
| Propose a correction to a readable record | Yes | Yes | Yes |
| Accept or reject a proposal | No | Yes, with the grant | Yes |
| Verify or dispute a revision | No | Yes, with the grant | Yes |
| Grant or revoke policy authority | No | Yes, with the grant | Yes |
| Share a record with another project | No | Yes, with the grant | Yes |
| Share a **global** record | No | Needs elevation, **no** project pin, and the `knowledge_share` grant | Yes |
| **Erase** (`knowledge redact`) | No | Yes, with the grant | Yes |
| Legacy import inventory (`knowledge import`) | No | No | Yes |
| Task-mapping backfill or outbox replay (`record repair`) | No | No | Yes |

Six operations are refused to a non-elevated principal whatever its profile
says: `knowledge_proposal_decide`, `knowledge_verify`,
`knowledge_authority_grant`, `knowledge_authority_revoke`, `knowledge_share`
and `knowledge_redact`. Elevation alone is not enough — the principal's policy
must also allow the operation, so a supervisor profile that accidentally
carries the command name still cannot decide, verify, grant, share or erase.

The last two rows are stronger than elevation: `knowledge_import` and
`record repair` both check `PrincipalKind.LOCAL` outright
([src/commands/knowledge_commands.py](../../src/commands/knowledge_commands.py),
`_cmd_knowledge_import` and `_cmd_record_repair`). `record repair` additionally
refuses to apply unless `knowledge.enabled` is on.

> **Note.** The K05 design describes `knowledge redact` as local-operator only.
> The shipped authorisation code admits an elevated supervisor holding the
> grant. Treat the design as the intent and the code as the behaviour: in a
> rehearsal, prefer the local operator.

Four further boundaries are worth stating because they surprise operators:

- **Project elevation never grants global access.** A global command needs
  `knowledge.global_enabled` *and*, for a non-local principal, elevation, no
  project pin, and `knowledge_share`. A scope mismatch is refused as
  `record.not_found`, not as a permission error, so it reveals nothing.
- **Every transport selects exactly one scope.** A command carries either
  `project_id` or `--global-scope`; a missing or wildcard scope is an error,
  never a default. Global scope additionally requires `knowledge.global_enabled`
  and a project scope requires the project to be in `enabled_projects`.
- **Writes have two more gates.** A write needs `knowledge.writes_enabled` *and*
  `legacy_memory_mode: disabled`; otherwise it is `knowledge.read_only` or
  `knowledge.legacy_writer_conflict`. A worker write also has to present its
  claim epoch and own the task.
- **Installed profile copies are not reseeded.** Knowledge grants reach a
  worker through the shipped `worker-<harness>` template and
  `aq agent profile-reseed --grants-only`, never silently by deploying code.
  `aq doctor --check profiles.system_drift` names the gap.

## 1. Back up the two places knowledge lives

Operator only, and never from a worker slot. Assumes the daemon is running and
you have shell access to the PostgreSQL instance and to `data_dir`.

### Record the pre-backup state first

Evidence before data: without these reads, a rehearsal cannot be compared
against anything. `--save-output` writes a mode-`0600` file and **refuses to
overwrite an existing one**, so give each run a fresh name.

```bash
install -d -m 700 ~/aq-evidence

# Which schema this install is stamped at, and what this checkout expects.
aq db current

# Which knowledge flags and grants are live right now. Retain the JSON.
aq record capabilities --project-id <project-id> \
  --save-output ~/aq-evidence/capabilities-before.json

# The integrity checks, as a baseline rather than as a verdict.
aq doctor --check records.revision_hashes --check records.redaction_cleanup \
  --check records.links --check records.exports \
  --save-output ~/aq-evidence/doctor-records-before.json
```

The `records.*` family comes from
[src/doctor/record_checks.py](../../src/doctor/record_checks.py) and is
`task_mappings`, `domains`, `revision_hashes`, `exports`, `links`, `index_lag`,
`redaction_cleanup`, `outbox`, `deprecation`. These checks have limits worth knowing before you
quote them:

- `records.revision_hashes` re-derives `content_sha256` for a **bounded sample
  of 100 revisions**, not the whole corpus. Its payload reports `sampled` and
  `truncated`, and when the sample is clean but the corpus is larger the result
  is downgraded to informational with the detail "bounded hash sample clean;
  full corpus not inspected". A clean sample is a reason to keep going, not a
  reason to stop.
- `records.redaction_cleanup` counts export checkpoints
  whose revision payload is gone, lexical search rows whose revision payload is
  gone, ledger rows whose `completed_at` is still null, and unacknowledged
  provider erasure receipts. Record every component;
  after a restore, the delta against your pre-backup numbers is the signal, and
  a nonzero `pending_redactions` is a redaction whose file purge never landed.
- `records.deprecation` reports aggregate managed compatibility attempts.
  External adapter coverage and unsafe writes remain unattested; zero counters
  alone do not establish G7 readiness.

Keep the receipts for both. They are the baseline the restore is compared
against, so a rehearsal with no "before" file has nothing to prove.

### Quiesce writers, then take the two archives

Stop the daemon before the copy so the database dump and the vault copy come
from one instant. `aq stop --keep-sessions` preserves agent sessions; plain
`aq stop` does not, which is why `--keep-sessions` is the normal form — see
[daemon updates](operations.md#daemon-updates) in
[operations](operations.md).

```bash
aq stop --keep-sessions

# PostgreSQL. $AQ_DB_URL must be THIS install's database.url from
# ~/.agent-queue/config.yaml — not agent_queue_e2e, and not one of the worker
# override variables. See the warning below.
pg_dump --format=custom --file=/secure/backup/aq-knowledge.dump "$AQ_DB_URL"
sha256sum /secure/backup/aq-knowledge.dump | tee /secure/backup/aq-knowledge.dump.sha256
sha256sum -c /secure/backup/aq-knowledge.dump.sha256

# The vault: managed exports and retained artifacts.
tar -C ~/.agent-queue -cpf /secure/backup/aq-vault.tar vault
sha256sum /secure/backup/aq-vault.tar | tee /secure/backup/aq-vault.tar.sha256
sha256sum -c /secure/backup/aq-vault.tar.sha256
```

> **Warning.** Resolve `$AQ_DB_URL` from `database.url` in
> `~/.agent-queue/config.yaml`, not from `AQ_DATABASE_URL` or
> `AGENT_QUEUE_DB`. Those are the overrides a worker session is given to point
> somewhere harmless, and
> [src/database/migration_guard.py](../../src/database/migration_guard.py)
> deliberately ignores them when deciding what "production" is. A shell that
> inherited a worker's environment is exactly the shell that will dump the
> wrong database.

Then bring the daemon back with `aq restart --no-dashboard`, which re-adopts the
preserved sessions.

Notes on the archives:

- Both contain knowledge payloads and are operator artifacts with the same
  handling as the database itself. Store them mode `0700`, outside any volume
  the daemon mounts, and record where they are.
- **Write down a backup receipt, and make it specific.** Applying a knowledge
  import refuses without a non-empty `--backup-receipt` and pins it to the run
  ([src/knowledge/imports/apply.py](../../src/knowledge/imports/apply.py),
  `apply`), but the service records the string without verifying it. A receipt
  naming the archive path, both checksums, the timestamp and the `aq db current`
  head is checkable by a later reader; "backup taken" is not.
- `tar -tf` the vault archive and confirm it actually holds the managed export
  paths named above — `vault/projects/…/knowledge/records/` or
  `vault/system/knowledge/records/` — plus the `record-artifacts/` directory
  that retained import evidence lives under. A truncated archive is caught now
  and not during a restore, and an empty archive with a valid checksum is still
  empty.
- `pg_dump --format=custom` is self-describing. Verify the archive lists the
  knowledge tables before you rely on it:
  `pg_restore --list /secure/backup/aq-knowledge.dump | grep knowledge_records`.
- There is a second, smaller safety net worth knowing about: when the stamped
  schema is behind the checkout's Alembic head, `aq start` dumps the database to
  `~/.agent-queue/backups/pre-deploy-<UTC>.sql` before it proceeds. That dump is
  a schema-migration guard, not a knowledge backup — it is only taken when the
  schema is behind, it is a plain SQL dump rather than a custom-format archive,
  and it never contains the vault. Do not treat it as satisfying this section.

### What this backup deliberately does not do

There is no `aq db backup`, and adding one is not this runbook's business. The
backups are access-controlled operator artifacts; the domain does not manage
their retention, and it does not expose a restore or import bypass either
([knowledge protection (K05)](../specs/design/knowledge-protection.md)).

## 2. Rehearse a restore in a disposable environment

The rule from the design is absolute: **a restore stays in a separate
disposable environment**, with every knowledge, export, context and provider
read/delivery path disabled until the complete post-backup redaction ledger has
been reapplied and cleanup verified
([knowledge protection (K05)](../specs/design/knowledge-protection.md)).
Nothing in this section restores anything onto a production database.

Read that rule operationally, because "every read path disabled" and "verify
the restore" pull against each other. What the shipped gates actually require:

| Gate | Rehearsal setting | Why |
|---|---|---|
| `knowledge.enabled` + the project in `knowledge.enabled_projects` | **On** | Every record read is refused with `knowledge.disabled` otherwise ([src/records/auth.py](../../src/records/auth.py), `authorize_on`). You cannot verify a restore through a closed door |
| `knowledge.writes_enabled` | **On only for the ledger-reapply step**, then back off | `knowledge redact` authorises as a write, so replaying a post-backup erasure is refused without it ([src/knowledge/redaction.py](../../src/knowledge/redaction.py), `write=True`). No knowledge content is written; erasure is the one-way guarded transition. Turn it off again the moment the ledger has closed |
| `knowledge.export.enabled` | Off | This gates the outbox exporter that writes managed files into the vault. A rehearsal must not repopulate a vault it just restored |
| `context`, `semantic`, `extraction`, `consolidation` | Off | Delivery, retrieval and paid calls are the paths through which a resurrected payload escapes |
| `legacy_memory_mode` | `disabled` | Anything else makes a write fail with `knowledge.legacy_writer_conflict`; leave the legacy writer frozen |

> **Note.** While a `knowledge redact` is computing its closure it takes an
> exclusive fence that ordinary knowledge writes share. That is deliberate and
> it is why a reapply belongs in a quiet disposable environment rather than
> alongside live traffic — and it is also why a reapply in production is an
> operator action taken deliberately, not a background job.

`aq knowledge export` is the read-only manual render and is **not** gated by
`knowledge.export.enabled` — that flag governs the automatic exporter
([src/records/export.py](../../src/records/export.py), `_manual` versus `_load`).
So a rehearsal can hash-verify a revision with the automatic exporter off, which
is what you want: bytes to your terminal, nothing written into the restored
vault.

The repository already has an isolated world for exactly this:
[scripts/e2e-env.sh](../../scripts/e2e-env.sh) builds one with its own
`data_dir`, its own vault, its own workspace tree and its own database
(`agent_queue_e2e`), and it never touches `~/.agent-queue` or the real
`agent_queue` database. [docs/guides/e2e-swarm.md](e2e-swarm.md) is its guide.

> **Note.** Run this from a checkout, on a quiet box, as the operator. A worker
> slot must not build a restore environment, and the kit's own guard refuses to
> run where it could reach the operator's install: the database helper
> [scripts/e2e/dbsetup.py](../../scripts/e2e/dbsetup.py) refuses the name
> `agent_queue` outright and refuses any name that does not contain the `e2e`
> ownership marker. That refusal is the guard, not an inconvenience to work
> around.

Every command in this section goes through the kit's own launcher, so it hits
this worktree's code and the e2e endpoint rather than whatever `aq` is on
`PATH`:

```bash
source scripts/e2e-common.sh   # exports AQ_API_URL at $AQ_E2E_API_URL
```

### Build the disposable world

```bash
scripts/e2e-env.sh --reset
```

Everything lives under `$AQ_E2E_HOME` (default `~/.agent-queue-e2e`) on its own
API port. Override `AQ_E2E_HOME`, `AQ_E2E_PORT` and `E2E_PG_*` from
[scripts/e2e-common.sh](../../scripts/e2e-common.sh) to run two rehearsals
side by side. `--reset` also drops and recreates the kit's own database, so it
is the safe form after an earlier rehearsal; `--register` cannot be combined
with it, because the reset drops the database the registration needs.

### Load the dump into a disposable database

Use a **dedicated** database for the rehearsal rather than the kit's own, so the
answer to "which database did I just write to" is unambiguous. Its name must
contain `e2e`, or `dbsetup.py` refuses:

```bash
RESTORE_DB=agent_queue_e2e_restore
# Two forms of one DSN: libpq for pg_restore/psql, SQLAlchemy+asyncpg for the
# daemon's config. pg_restore will not accept the +asyncpg form.
RESTORE_DSN="postgresql://${E2E_PG_BASE}/${RESTORE_DB}"
RESTORE_URL="postgresql+asyncpg://${E2E_PG_BASE}/${RESTORE_DB}"

python3 scripts/e2e/dbsetup.py "$E2E_ADMIN_DSN" "$RESTORE_DB"
pg_restore --dbname="$RESTORE_DSN" --no-owner --no-privileges \
    /secure/backup/aq-knowledge.dump

# The vault copy goes where this daemon's data_dir expects it.
tar -C "$AQ_E2E_HOME" -xpf /secure/backup/aq-vault.tar
```

This is the same two-form split the kit itself uses: `E2E_ADMIN_DSN` is
`postgresql://…` for `psql`-style tools while `E2E_DB_URL` is
`postgresql+asyncpg://…` for the daemon.

`pg_restore` brings the schema with it. If it reports errors on objects that
already exist, the target was not empty — drop and recreate it with
`dbsetup.py "$E2E_ADMIN_DSN" "$RESTORE_DB" --reset`, then restore again. Never
re-run the restore on top of a partially loaded database.

Point the disposable daemon at that database. Edit `database.url` in the e2e
config in place — that file is disposable by construction and `--reset`
regenerates it — and validate before starting, so a bad edit cannot reach a
running daemon:

```bash
scripts/e2e-daemon.sh stop      # one daemon owns $AQ_E2E_PORT at a time
cp "$E2E_CONFIG" "$E2E_CONFIG.restore"   # keep the kit's original config

# Edit $E2E_CONFIG. Set database.url to $RESTORE_URL, put the rehearsal's
# projects in knowledge.enabled_projects, and leave every other knowledge flag
# as the table above requires: writes and the automatic exporter off.
python3 -m src.main "$E2E_CONFIG" --validate-config

scripts/e2e-daemon.sh start
scripts/e2e-daemon.sh status
```

If the restore needs the original kit config afterwards, move it back:
`mv "$E2E_CONFIG.restore" "$E2E_CONFIG"`.

Read the restored schema head before anything else. If it does not match the
pre-backup `aq db current`, the dump and the vault copy are not a matched pair
and the rehearsal is void:

```bash
e2e_aq db current
```

### Verify the redaction ledger before enabling anything

This is the step that makes a restore safe, and it is the step a rehearsal
exists to prove. The ledger is `knowledge_redactions` plus its
`knowledge_redaction_targets` rows; a row is complete only when
`completed_at` is set, which happens only after its purge intent drains
([src/records/outbox.py](../../src/records/outbox.py), `acknowledge`), at which
point `cleanup_state` reads `{"export": "purged", "artifact": "purged",
"index": "absent"}`.

Compare three things, and treat any mismatch as a failed rehearsal:

1. `completed_at IS NULL` rows in the restored database — these are
   redactions whose file purge never landed.
2. The count of redactions *requested after* the backup timestamp on the
   production install. Those erasures exist only in production, so they are not
   in the dump at all.
3. Every managed export and retained artifact for the affected records in the
   restored vault, against what production had removed.

Read the ledger directly in the **disposable** database — there is no CLI that
lists it, and this page will not invent one:

```bash
psql "$RESTORE_DSN" -c \
  'SELECT redaction_id, record_id, requested_at, completed_at, cleanup_state
   FROM knowledge_redactions ORDER BY requested_at'
```

`psql` is the usual tool, but it is not assumed to be installed on every box —
that is the reason [scripts/e2e/dbsetup.py](../../scripts/e2e/dbsetup.py) talks
to PostgreSQL through `asyncpg` instead. A short `asyncpg` snippet against
`$RESTORE_DSN` running the same `SELECT` is an equally good way to read it.

Reapply each post-backup erasure in the disposable environment with the same
three-step the operator uses in production — preview, then apply, then confirm
the cleanup closed. `knowledge redact` defaults to dry run, and the closure it
reports can be wider than the selected revision, so preview first every time:

```bash
# 1. Preview: note affected_revisions and affected_records before applying.
e2e_aq knowledge redact --identity knowledge:<kn-alias> \
  --reason-code operator_erasure --idempotency-key <new-key> --dry-run

# 2. Apply, with a NEW idempotency key and the exact current revision.
e2e_aq knowledge redact --identity knowledge:<kn-alias> \
  --if-revision <current-revision-id> --reason-code operator_erasure \
  --idempotency-key <new-key> --no-dry-run

# 3. Confirm the ledger closed rather than assuming it did.
e2e_aq doctor --check records.redaction_cleanup --json
e2e_aq doctor --check records.outbox --json
```

`records.outbox` reporting exhausted intents, or `redaction_cleanup` still
reporting `pending_redactions`, means the purge never completed. Purge intents
run even when the knowledge and export flags are disabled, so a rehearsal must
not "fix" a stuck purge by enabling more features.

### Prove the restored records read back identically

Only now, and still without writes, context or any provider.

```bash
# Exact-revision read; record_id, revision_id, sequence and content_sha256
# must match what production returned before the backup.
e2e_aq knowledge show --identity knowledge:<kn-alias> \
  --revision-id <revision-id> --json

# History is intact and the head is the revision you backed up.
e2e_aq knowledge history --identity knowledge:<kn-alias> --json

# A re-derivation of the stored hash. record.hash_divergence means the
# restored payload does not match its recorded content_sha256.
e2e_aq knowledge export --identity knowledge:<kn-alias> \
  --revision-id <revision-id> --json
```

`knowledge export` re-hashes the snapshot and refuses with
`record.hash_divergence` on a mismatch; on success it returns the rendered
Markdown and an `export_sha256` for the bytes. Compare three hashes for the same
revision:

| Hash | Source | What it proves |
|---|---|---|
| `content_sha256` | `aq knowledge show --json` before the backup | The snapshot the record held |
| `content_sha256` | `aq knowledge show --json` after the restore | The restored snapshot is byte-identical |
| `content_sha256` | The `content_sha256:` frontmatter of the managed `.md` | The vault projection matches the database |

If the vault frontmatter and the database disagree, the export is stale or
diverged. `records.exports` names it; the exporter will not overwrite a
human-edited file, and that is correct behaviour, not a restore failure.

### Then, and only then, prove an import replays

A backup taken *after* an import has to be able to replay that import, because
the alternative is re-running an operator selection you may no longer be able to
justify. This is a rehearsal of the replay, not a production recovery:
everything here happens in the disposable database.

Per [resumable knowledge import](knowledge-import.md), a terminal replay of a
completed run creates no revisions and reports its successful receipts as
`reused`. A restored database is where you find out whether that still holds.
Because apply authorises as a write, this step needs `knowledge.writes_enabled`
and `knowledge.import_apply.enabled` — the same two flags the ledger reapply
needed — and the selection, `manifest_sha256`, `--backup-receipt`,
`expected_revisions` and `expected_source_hashes` from the original run:

```bash
e2e_aq knowledge import --operation resume \
  --project-id <project-id> \
  --manifest-content-base64 "<exact sealed bytes from the retained manifest>" \
  --manifest-sha256 "<sealed SHA-256>" \
  --run-id "<run_id from the apply receipt>" \
  --idempotency-key <original-key> \
  --backup-receipt "<the receipt from step 1>" \
  --limit 100
```

What to check in the result, and what each failure means:

| Signal | Meaning |
|---|---|
| `state` reaching a terminal value with `pending` at zero | The replay finished and the accounting closed |
| Reused receipts and zero new revisions | Idempotence survived the restore — this is the property worth proving |
| `knowledge_import.source_conflict` | An `expected_source_hashes` entry no longer matches. The hash is the SHA-256 of the canonical JSON array `[source_installation_id, source_kind, source_scope, source_key]`, so this means a source identity moved, not that content drifted |
| A revision conflict | A concurrent canonical edit. Never retry against a new head; take the new token deliberately |
| `knowledge_import.invalid_input` | The manifest bytes, the seal, the selection, the backup receipt or a hash is malformed. Fix the input; do not loosen the check |

Keep the retained snapshot artifacts until the run completes. A restart uses
those artifacts rather than live legacy files, so losing them turns a resumable
run into a failed one.

### What a rehearsal deliberately does not do

- It does not enable the `context`, `semantic`, `extraction`, `consolidation`
  or `export` features, and it leaves `knowledge.writes_enabled` off except
  while replaying the ledger. Reads plus one manual `knowledge export` are
  enough to prove a restore, and each additional path is a way for a resurrected
  payload to escape.
- It does not run against a provider. There is no paid call in a rehearsal.
- It does not touch production. If you cannot state which database a command
  will hit, stop.
- It does not restore the *ledger* for you. A dump older than the last erasure
  is unsupported until the ledger is reapplied and cleanup verified.
- It does not write a redaction closure wider than the one you previewed. The
  dry run is the record of what you agreed to erase; if the closure grew
  between preview and apply, start again from the preview.

## 3. Reconcile legacy sources against the sealed manifest

Legacy knowledge predates the records domain, and reconciliation is a
read-only, operator-only, hash-pinned measurement. It answers "what would an
import create, revise, reuse, exclude or quarantine" — and it writes nothing.

```bash
aq knowledge import \
  --roots "[{\"root_id\":\"notes\",\"path\":\"$HOME/.agent-queue/vault/projects/demo\",\"source_scope\":\"project:<project-uuid>\"}]" \
  --vector-export /secure/legacy/milvus-export.json \
  --scope-aliases '{"demo":["project:<project-uuid>"]}' \
  --source-installation-id <legacy-installation-id> \
  --snapshot-id <snapshot-id> \
  --snapshot-timestamp <ISO-8601>
```

Three details in that invocation are load-bearing, and each has bitten an
operator:

- **`path` is not tilde-expanded.** The scanner calls `Path.absolute()`, which
  resolves `~` against the working directory rather than your home, so a literal
  `~/.agent-queue/...` silently scans the wrong tree. Let the shell expand
  `$HOME` instead, as above. The manifest records the resolved `real_root`, so
  read it back and confirm it is the tree you meant.
- **`source_scope` is the explicit scope**, `project:<uuid>` or `global`. A
  readable alias is not a scope.
- **`--scope-aliases` maps a vector collection's `scope_alias` to exactly one
  scope.** An alias that is missing from the map gets `unknown_scope_alias`; one
  that maps to more than one scope becomes `unresolved:<alias>` with
  `scope_alias_collision`. Both are quarantined rather than guessed, which is
  the intended behaviour: an ambiguous scope must not be resolved by guessing.

Requirements and refusal codes, all enforced in
[src/commands/knowledge_commands.py](../../src/commands/knowledge_commands.py)
(`_cmd_knowledge_import`): the caller must be the local operator
(`knowledge_import.forbidden`), and both `knowledge.enabled` and
`knowledge.import_inventory.enabled` must be on
(`knowledge_import.disabled`). A malformed root is
`knowledge_import.invalid_input`; an unreadable path is
`knowledge_import.source_unavailable`. `root_id` must be a unique, path-free
name. Nothing is applied — the handler's docstring is "Scan, seal and verify a
legacy import inventory (read-only, operator)".

Read these five fields out of the JSON:

| Field | Meaning | How to treat it |
|---|---|---|
| `manifest_sha256` | The seal over the canonical manifest bytes | The identity of this measurement. A changed source file means a new snapshot, never a late mutation to this one |
| `manifest_content_base64` | The sealed manifest itself, lossless | The artifact to archive alongside the report. The hash above is the seal on exactly these bytes |
| `vector_observation` | `observed`, `not_observed` or `unavailable` | `not_observed` means "not looked at". It never means "zero vector-only records", and no live store count is asserted |
| `counts` | `items`, `sources`, `classifications`, `dispositions` | The accounting the whole decision closes on |
| `items` / `mappings` / `identities` | One row per item, per source identity, per source occurrence | Every selected source identity appears exactly once; several identities may map to one record |

The accounting closes on the manifest, which `verify_manifest` re-derives
before the report is built
([src/knowledge/imports/manifest.py](../../src/knowledge/imports/manifest.py)):
`counts.items` equals the sum of `counts.dispositions`, and `counts.sources`
equals the number of source identities. A mismatch raises
`manifest accounting mismatch` and no report is produced.

The classifications tell you what kind of evidence each source is, and they are
the reason to read the report rather than glance at the totals:

| Classification | What it means for an import decision |
|---|---|
| `matched` | A file and a vector document pair to the same `(source_scope, source_key)` with no issues between them, producing one candidate with two legacy mapping aliases and both provenance descriptors. Whether an exact hash match may dedup is a later, per-scope decision — it is never taken here |
| `file_only` | Import the exact body and frontmatter plus the artifact. Mark the vector absent, not failed. No embedding is required |
| `vector_only` | Retain the indexed `original`; preserve the indexed `content` as a summary with provenance. If `original` is absent, the item is summary-only and the evidence is incomplete. A lost file is never fabricated |
| `summary_only` | Do not substitute the summary for the original |
| `conflicting_originals` | File and vector disagree: both artifacts are preserved and the item is quarantined until an explicit decision picks the authoritative text. Modification time is never the winner rule |
| `paired_incomplete` | A file and a vector document pair where one side has no original. Both artifacts are kept and the item is quarantined |
| `kv` | Stable key is (source scope, namespace, key). Ordered imported revisions, with imported effective validity. Current approval is never inferred from historical fact status |
| `temporal_history` | As `kv`, with every available time-version hash retained |
| `unavailable_root` | The root could not be opened. The whole root is `unavailable`, never empty |
| `unavailable_export` | The vector export is missing. `vector_observation` becomes `not_observed`; nothing is claimed about vector records |
| `malformed_export` | The export parsed but a row, its original or its content is malformed. The raw export artifact is retained and the item is `quarantined` |

Read `items[].issues` before you read the totals. A `quarantined`, `excluded` or
`unavailable` item carries its reason there, and each disposition appears in
`counts.dispositions`. Most issues imply a non-candidate disposition — an
ambiguous scope, a conflicting original, a missing original, a malformed row —
but not all: a vector document whose declared source path is outside the
scanned roots keeps its disposition and gains a `missing_file` issue, so scan
the issues column as well as the disposition column. A report where the
interesting rows are quarantined is the report working, not the report
failing.

The import plan is explicit per stable item key and hash — never "all matching
filenames" — and task-shaped reference material is an inventory classification
only. Nothing here rewrites, archives or reclassifies a task.

Symlinks and path traversal are refused, files, archive expansion and metadata
sizes are bounded, and invalid UTF-8 preserves the raw artifact and quarantines
parsing. The scanner is read-only: it does not initialise the legacy store and
does not call its search or get paths, because those can update retrieval
counters. `tests/test_knowledge_inventory.py` asserts exactly that, with a
socket spy that fails the test on any network access.

### Apply is a separate gate, and a separate page

The sealed manifest above is a measurement. Turning it into revisions is a
different command with a different flag, and it has its own operator guide:
[resumable knowledge import](knowledge-import.md). Do not reconstruct apply from
this page — read that one. What belongs here is only the boundary between the
two, because it decides what your backup has to be good for.

| | Reconcile (this section) | Apply (`knowledge-import.md`) |
|---|---|---|
| Flag | `knowledge.import_inventory.enabled` | `knowledge.import_apply.enabled` |
| Also needs | — | `knowledge.writes_enabled`, and the project's enablement |
| Principal | Local operator | Local operator |
| Input | Explicit roots and an offline vector export | The exact `manifest_content_base64` and `manifest_sha256` from the dry run, plus explicit `selected_item_ids` |
| Writes | Nothing | Revisions, source mappings, per-item receipts and a cursor |
| Idempotency | Each run seals a new snapshot | `--idempotency-key` pins the run; reusing it with different inputs is a conflict |

The line that connects this page to that one is **`--backup-receipt`**. Apply
refuses without a non-empty one, pins it to the run, and stores it
([src/knowledge/imports/apply.py](../../src/knowledge/imports/apply.py), `apply`).
The service records the reference; it does not verify it. So the backup you took
in [step 1](#1-back-up-the-two-places-knowledge-lives) is the artifact the receipt
has to describe, which is why the checksummed archives and their locations belong
in the receipt rather than in a chat message.

Two properties of apply matter to a rollback, and are covered in full on that
page:

- **A cancelled run does not undo imported records.** `operation: cancel` stops
  progress; it is not a revert. Unselected sources stay under legacy ownership.
- **The per-source ownership fence survives rollback.** Managed note reads
  resolve the canonical record and report `source_diverged` when the retained
  source no longer matches the file, and legacy write, append, delete and
  promote keep refusing with `memory.migrated_read_only` even after rollout is
  disabled or the original file is deleted.

Full sealed manifests are operator evidence and never become public record
sources, and redaction denies and queues purging of the retained manifest so a
resume or a fresh replay cannot restore erased evidence. Both matter when you
are deciding what to archive alongside a backup and what to keep out of one.

## 4. Roll out one gate at a time

The `knowledge` config section is hot-reloadable, so a flag edit bites on the
next access without a daemon restart ([src/config.py](../../src/config.py),
`HOT_RELOADABLE_SECTIONS`). Confirm which is which before you rely on it:

```bash
aq system config get knowledge
aq system reload-config --json
```

`memory` is in `RESTART_REQUIRED_SECTIONS`, so enabling the memory master is a
restart decision, not a flag edit.

Set one key at a time and validate before writing:

```bash
aq system config set knowledge.enabled=true --dry-run
aq system config set knowledge.enabled=true
```

The keys are `knowledge.enabled`, `knowledge.enabled_projects`,
`knowledge.global_enabled`, `knowledge.writes_enabled`,
`knowledge.authority_review_required`, `knowledge.ui_enabled`,
`knowledge.legacy_memory_mode` (`disabled`, `read_only` or `compatibility`),
and one `{enabled}` sub-object each for `context`, `semantic`, `extraction`,
`consolidation`, `export`, `import_inventory` and `import_apply`.
`enabled_projects` takes explicit project IDs; a wildcard is refused by config
validation.

Only the local operator can change these. Config commands are outside the
worker command set ([src/api/scope.py](../../src/api/scope.py),
`AGENT_COMMAND_SET`), so an agent token asking is refused by scope rather than
by policy — which is the right layer for it.

### The gates

Each gate is an operator decision. None of them activates itself, none is a
dependency that flips a flag, and crossing one requires its own recorded
evidence. The gate table is §11 of the approved `rev-quick-orbit` plan; the
in-repo design that implements sections 4–7 and 11–13 of it is
[knowledge protection (K05)](../specs/design/knowledge-protection.md). The plan
itself is an approved review artefact, not a page in this tree — if you do not
have it, the in-repo spec and this table are the authority, and a gate you
cannot evidence stays closed. In summary:

| Gate | What it authorises | What must be recorded first |
|---|---|---|
| **G0** plan approval | Nothing operational | The approved implementation revision and hash |
| **G1** inert deployment | Schema only; all features stay disabled | Migrations exercised on a disposable database; the schema guard validated; feature-off task, claim and integration regression tests green. **Only the operator upgrades the production schema** |
| **G2** core pilot | Core read/write/export for one project; memory stays `false` | Core acceptance evidence, scoped capability grants, a restore backup and an outbox crash drill |
| **G3** selected import and UI | Explicitly selected manifest items under `knowledge.import_apply.enabled`, and the knowledge UI | An explicit manifest selection, the backup receipt from step 1, and the UI checks — see [resumable knowledge import](knowledge-import.md). **No legacy source deletion** |
| **G4** shared context | The context master and injection, only after the legacy plugin is confirmed frozen | Cross-harness determinism, security and budget gates. The operator enables the memory master separately and deliberately |
| **G5** retrieval pilot | One selected retrieval provider and scope | Lexical-versus-retrieval privacy, cost and operational results, approved. Quality no worse than lexical is the bar; a more expensive backend is not selected for being faster. See [the K12 row](#what-is-not-in-this-runbook) for the implementation boundary |
| **G6** extraction pilot | One project, unverified proposals only | Replay, budget, source-permission and proposal protections, plus explicit outbound-provider and cost approval. This is the gate that can spend money |
| **G7** deprecation | The decision to begin removing legacy writers | Complete reconciliation, zero unsafe compatibility writes, the announced minimum two-release and 30-day window, and a backup/restore exercise. **Uninstall and deletion need their own separate authorisation** |

Two things no gate authorises, stated once so nobody has to infer them:
**paid provider calls need explicit cost approval at G6**, and **no live import,
legacy deletion or adapter removal is authorised by anything in this page.**

### Confirm what is actually live

```bash
aq record capabilities --project-id <project-id> --json
```

Read `enabled`, `global_enabled`, `writes_enabled`, `ui_enabled`,
`enabled_projects`, `legacy_memory_mode`, the `features` map and
`granted_operations`. `granted_operations` is what the calling principal may
actually invoke, which is the honest answer to "did the grants land" —
`profiles.system_drift` answers the separate question of whether an installed
vault copy still matches what shipped.

## 5. Roll back without destroying evidence

Rollback is a sequence of flag changes, not a migration. There is no
destructive downgrade path once knowledge or protection data exists.

**Order matters.** Disable in this order, so nothing is still trying to write
into a subsystem you just turned off:

```bash
aq system config set knowledge.context.enabled=false
aq system config set knowledge.extraction.enabled=false
aq system config set knowledge.consolidation.enabled=false
aq system config set knowledge.semantic.enabled=false
aq system config set knowledge.ui_enabled=false
aq system config set knowledge.writes_enabled=false
```

What is deliberately left running: authorised reads, export, and repair. An
investigation needs them, and none of them can write knowledge.

What rollback preserves:

- Database records, revisions, payloads and links.
- Request receipts and the idempotency record.
- Task record mappings and per-source ownership fences. Managed note reads keep
  resolving the canonical record — with `source_diverged` when the retained
  source no longer matches the file — and legacy write, append, delete and
  promote keep refusing with `memory.migrated_read_only` even after rollout is
  disabled or the original file is deleted. The fence stays in place; rollback
  does not restore two write authorities over the same source.
- The redaction ledger and its evidence, including purge intents that have not
  drained yet.

What rollback does not do:

- It does not run a down-migration. Downgrade functions refuse when user
  records, citations, mappings or import receipts exist and point the operator
  at read-only rollback instead. Only an empty disposable or brand-new
  installation may drop these tables, in dependency order.
- It does not un-export anything. Already exported human copies are outside the
  system's reach.
- It does not un-verify or un-grant. `knowledge verify` and `knowledge
  authority-grant` are the only commands that set verified status or an
  authority annotation, and a grant is revoked by
  `aq knowledge authority-revoke` with a reason, not by a flag.

If the previous core cannot tolerate the additive tables, stay on the new code
in read-only mode and fix forward. Redeploying old core over live knowledge is
the one rollback step that can lose the ownership fence.

## What is not in this runbook

This checkout contains the following implementations. Their presence and passing
synthetic tests do not activate features or establish production delivery. This
runbook leaves feature-specific setup and acceptance to the linked contracts.

| Not here | Why | What exists today instead |
|---|---|---|
| **Context assembly, budgets, citations** (K08) | Operator G4 acceptance remains required | [Knowledge context](../specs/knowledge-context.md) owns prepared bundles, observed delivery, citations and aggregate budgets; the command surface is under `aq knowledge` |
| **Harness delivery adapters** (K09) | Cross-harness release evidence remains required at G4 | [Session runtime](../specs/design/session-runtime.md) owns guarded delivery and refresh; feature flags stay disabled |
| **Durable extraction and consolidation** (K13) | G6 requires explicit provider and cost approval | [Extraction provider contract](../reference/knowledge-extraction-provider.md) describes durable jobs, separate budgets and unverified proposals. The absent-provider path remains local |
| **Semantic provider** (K12) | G5 requires a selected provider and approved privacy, quality and cost evidence | [Retrieval provider contract](../reference/knowledge-retrieval-provider.md) describes optional registration and derived index receipts. `aq record search --kind task\|knowledge\|all` remains lexical by default |
| **Deprecation reports and adapter removal** (K14) | G7 remains closed pending complete operator evidence and decision; uninstall/deletion need separate approval | [Deprecation and restore](knowledge-deprecation.md) documents compatibility counters, sealed reconciliation and the evidence-only guard |
| **Benchmark and quality numbers** | Actual-model quality and production recovery are separate release evidence | The [synthetic rehearsal report](../reports/2026-10-03-knowledge-deprecation-rehearsal.md) covers functional restore checks; it claims no live quality or performance result |

## Evidence checklist

What a complete record of a rollout or rehearsal keeps. Each row is a file or
command output, not a claim.

| # | Artifact | Produced by | What it proves |
|---|---|---|---|
| 1 | Pre-backup state | `aq db current`; `aq record capabilities --json`; `aq doctor --check records.*` | The schema head, the live flags and grants, and the integrity baseline the restore is compared against |
| 2 | Database archive + checksum | `pg_dump` + `sha256sum -c` | Every record, revision, payload, link, proposal, grant, share, ledger row, import receipt, mapping, context/citation row, extraction job/budget, index receipt and compatibility counter |
| 3 | Vault archive + checksum | `tar` + `sha256sum -c`, plus `tar -tf` | Managed exports, retained `record-artifacts/`, and the retained sealed manifest |
| 3b | Backup receipt | The text you will pass to `--backup-receipt` | That the apply step is pinned to *this* archive. The service stores the string; only you can make it checkable |
| 4 | Restore rehearsal log | `scripts/e2e-env.sh --reset`, `pg_restore` into `$RESTORE_DB`, the reads in step 2 | That the archive actually restores, in an environment that cannot reach production |
| 5 | Hash comparison | `content_sha256` from `aq knowledge show --json` before and after; `export_sha256` from `aq knowledge export --json` | Byte-identical restoration, not merely a successful exit code |
| 6 | Ledger proof | `records.redaction_cleanup` and `records.outbox` after each reapplied erasure | Cleanup closed; no purge intent stranded |
| 7 | Sealed manifest | `aq knowledge import` report with `manifest_sha256`, `manifest_content_base64`, `counts` | Reconciliation accounting that closes, with every disposition visible |
| 7b | Replay proof | `aq knowledge import --operation resume` in the disposable database | That a restore can finish an interrupted import rather than forcing a fresh operator selection |
| 8 | Gate decision record | The gate's own evidence, outside this document | That a human approved a specific step, with the specifics of what was approved |
| 9 | Post-rollback state | `aq record capabilities --json` again | The flags you believe you changed are the flags that are live |

Do not substitute a successful exit code for any row above. `success: true` on
a read means the read was authorised; only the hash comparison means the bytes
are the same.

## Related pages

- [Knowledge protection (K05)](../specs/design/knowledge-protection.md) — the
  design that owns the authority, sharing, erasure and backup rules this
  runbook defers to.
- [Resumable knowledge import](knowledge-import.md) — the apply, resume and
  cancel contract behind the backup receipt step 1 produces, which is why this
  page stops at the reconcile/apply boundary.
- [Database migrations](migrations.md) — who may run Alembic, against which
  database, and what to do when the schema guard refuses.
- [End-to-end testing the swarm](e2e-swarm.md) — the disposable environment
  step 2 builds on, and what its tiers do and do not cover.
- [A PostgreSQL 18 Compose volume migration](postgres18-volume-migration.md) —
  the cluster-level backup, checksum and clean-shutdown precedent this page
  follows for the database archive.
- [Agent collaboration threads](agent-collaboration.md) — if a rehearsal
  finding needs adjudication across two to four held tasks rather than a single
  operator decision.

## Source and tests

Implementation read while writing this page:

- [src/knowledge/imports/dry_run.py](../../src/knowledge/imports/dry_run.py),
  [manifest.py](../../src/knowledge/imports/manifest.py),
  [inventory.py](../../src/knowledge/imports/inventory.py) — the sealed
  inventory, its accounting and its refusals.
- [src/records/export.py](../../src/records/export.py),
  [src/records/outbox.py](../../src/records/outbox.py) — managed export paths,
  the purge that closes the redaction ledger, and export-state checks.
- [src/knowledge/redaction.py](../../src/knowledge/redaction.py),
  [src/knowledge/protection_schema.py](../../src/knowledge/protection_schema.py)
  — erasure closure, the confined artifact namespace, and the database guard
  that permits only the one-way audited transition.
- [src/knowledge/imports/apply.py](../../src/knowledge/imports/apply.py) and
  [compatibility.py](../../src/knowledge/imports/compatibility.py) — the backup
  receipt the reconcile/apply boundary depends on, and the per-source ownership
  fence that survives a rollback. The operator contract for both is
  [knowledge-import.md](knowledge-import.md); this page links it rather than
  restating it.
- [Knowledge deprecation and restore](knowledge-deprecation.md) — aggregate
  telemetry, reconciliation and the explicit G7 evidence boundary.
- [src/commands/knowledge_commands.py](../../src/commands/knowledge_commands.py),
  [src/doctor/record_checks.py](../../src/doctor/record_checks.py) and
  [src/config.py](../../src/config.py) — the operator-only refusals, the
  `records.*` check family, and the hot-reload boundary.

Focused checks for the areas this page describes:

```bash
aq test tests/test_knowledge_inventory.py \
        tests/test_knowledge_import_inventory.py
aq test tests/test_record_exports.py tests/test_record_outbox.py \
        tests/test_record_doctor.py tests/test_knowledge_redaction.py \
        tests/test_knowledge_authorization.py
python3 scripts/check-docs.py docs/guides/knowledge-migration-restore.md
```

The documentation-coverage manifest assigns every page under `docs/` an owner,
and a new guide is covered by that generic rule — no manifest change is needed.
See
[documentation style and runnable-example
rules](../contributing/documentation-style.md) before editing this page
further.
