# Knowledge deprecation and restore

Reconcile every legacy source and prove a disposable restore before deciding G7.

## Why it exists

New search results do not prove that old originals, temporal versions, ownership
fences or erasure evidence survived migration. Deprecation requires explicit
evidence and an operator decision. G7 authorizes beginning deprecation; uninstall
and source deletion need their own separate authorization. This increment performs
neither. Mechanisms live in
[src/knowledge/deprecation.py](../../src/knowledge/deprecation.py).
The [migration and restore runbook](knowledge-migration-restore.md) gives the
full operator backup and recovery procedure; this page adds K14's evidence checks.

## Vocabulary

A **sealed manifest** retains exact inventory bytes and their hash. A **receipt**
names retained evidence; its presence is an attestation, not signature validation
or permission. **Compatibility attempts** count managed-source reads, refused
legacy writes and guarded translations before dispatch. **Restore rehearsal**
means restoring a backup into an exclusively disposable database and vault,
then reading and comparing the retained evidence.

## A realistic example

Prerequisites: this checkout, development dependencies, and the separate
disposable PostgreSQL test service. Set `POSTGRES_TEST_DSN` as described in
[testing](../contributing/testing.md); retain the worker's database refusal
sentinels. The following test uses synthetic sources, a real dump/restore, and
test-owned database names. The suite removes its own scratch databases and pytest
cleans its temporary vault. It never connects to the operator database:

```bash
aq test tests/test_knowledge_deprecation.py tests/test_knowledge_compatibility.py \
  tests/test_record_doctor.py -x
```

Observed on 2026-10-03 against the disposable service:

```text
32 passed, 10 warnings in 18.96s
```

The proof compares every `record_*`, `knowledge_*` and `records` row before and
after restore, including installation ID, revision envelopes, payloads, receipts,
mappings and compatibility counters. It also compares every vault file byte,
recomputes an exact historical content hash, verifies complete reconciliation,
replays the import without duplicate creation, and reapplies a post-backup erasure
before checking that the erased revision is unavailable. This is synthetic
functional evidence. It does not certify a live inventory, real adapter coverage,
an actual-model quality gate or a production backup.

## Inputs and outputs

Collect these artifacts per installation and intended deprecation scope:

1. An explicit, current inventory of all selected legacy roots and an offline
   vector export, including file-only/vector-only originals and temporal history.
   A missing vector inventory keeps reconciliation incomplete. An observed empty
   export can establish an empty synthetic vector inventory; an absent export cannot.
2. The sealed manifest bytes and hash, selected item IDs, apply/resume receipts,
   per-source hashes and explicit exclusion reasons. See
   [resumable import](knowledge-import.md). Unselected, changed, missing, redacted,
   quarantined or unavailable sources remain unresolved for deprecation.
3. Database and vault backup archives, verified checksums, locations, schema head,
   installation ID, pre-backup counts and exact revision/export hashes. A backup
   receipt must name these artifacts. Import stores the receipt string; it does not
   validate the archive.
4. A restore rehearsal log, before/after comparisons, an interrupted-import replay
   proof and the complete post-backup redaction ledger reapplied before delivery.
5. Replacement acceptance and coverage receipts from every surviving compatibility
   writer, watcher and adapter, plus zero unsafe compatibility writes. Core counters
   alone do not cover an external plugin.
6. The announcement timestamp and receipts for at least two distinct releases,
   with at least 30 elapsed days; a named approver and explicit G7 decision receipt.

The internal `reconcile_manifest` reader verifies the seal and accounts for every
source against exact descriptor hashes, matching import receipts, retained
revisions and permanent mappings. It returns counts and a hash, never source
content. Its result covers the supplied manifest; the operator must attest that
the roots/export cover the complete intended inventory. `adapter_removal_guard`
lists missing G7 conditions. Even complete evidence returns
`removal_authorized: false` and requires separate operator uninstall/deletion
authorization. These are internal helpers, not new CLI commands.

### Operator backup and restore procedure

These are **operator steps**, not commands run against a live install by this
worker. Keep production reads and consumers disabled during any recovery until
the full post-backup erasure ledger has been reapplied.

1. Freeze legacy writers for the intended scope. Record flags/grants with
   `aq record capabilities --project-id <project> --json`, schema state with
   `aq db current`, and integrity with `aq doctor --check records.*`.
2. Take a consistent PostgreSQL backup and archive the matching vault, including
   managed exports, `record-artifacts/` and sealed manifests. Use the matching
   PostgreSQL client version, keep credentials out of logs, and verify both archive
   checksums and archive listings. Retain the complete erasure ledger separately
   so later erasures can be reapplied to an older backup.
3. Restore those two archives into an isolated disposable environment. Re-enable
   no prompt, export or provider delivery path. Workers use the test above;
   production migration/recovery remains operator-only.
4. Compare installation identity, retained table counts, exact revision hashes,
   history, source artifacts and permanent ownership mappings. A successful
   `pg_restore` exit alone is insufficient. Resume an interrupted selected import
   using its retained run ID and manifest hash; verify counts close and replay
   creates no extra records.
5. Reapply every post-backup erasure and drain its purge intents before any
   delivery path is enabled. Verify exact reads return typed redacted results,
   not old originals. Inspect `records.redaction_cleanup` and `records.outbox`.
   A missing/incomplete post-backup ledger makes recovery unsupported.
6. Store the comparisons and checksums as the restore receipt. Destroy only the
   disposable rehearsal environment through its owning test/operator workflow.
   A production recovery or legacy deletion is a separate decision.

### Operator rollout gates

The approved `rev-quick-orbit` r1 plan, sections 11–13, requires separate decisions:

| Gate | Evidence required before activation |
|---|---|
| G1 inert deployment | Disposable migration and feature-off regression checks; operator upgrades production schema with features disabled |
| G2 core pilot | Core acceptance, scoped grants, backup and outbox crash drills; one project with memory disabled |
| G3 selected import and UI | Protection checks, explicit sealed selection, backup receipt and UI acceptance; retain legacy sources |
| G4 shared context | Cross-harness security, deterministic selection and budget checks; separately approve memory/context activation with legacy frozen |
| G5 retrieval pilot | Lexical comparison, privacy, quality and operational-cost evidence for the selected provider/scope |
| G6 extraction pilot | Durable replay, source permissions, proposal protections, budgets and explicit outbound-provider/cost approval |
| G7 deprecation | Complete reconciliation, replacement and restore proof, coverage with zero unsafe writes, two-release/30-day window and explicit approval |

K13/context delivery and real-provider evaluations are independent prerequisites,
not evidence established by this synthetic K14 drill. Keep their controls off until
their own delivery and acceptance are evidenced. No paid calls are needed for the
core reconciliation and restore test.

## State ownership

[src/database/tables.py](../../src/database/tables.py) owns
`record_compatibility_usage`: atomic aggregate counters keyed by scope, operation
and outcome, with first/last timestamps. It retains no paths, source keys, bodies,
titles or actor IDs. These are attempts, including failed canonical dispatches,
not successful mutations. Managed notes use the same transaction and path lock
as ownership lookup. A rollback or flags being off does not remove the fence or
the counters. Retained telemetry makes its migration downgrade refuse; use
read-only rollback.

The operator-only doctor surface reports:

```bash
aq doctor --check records.deprecation --json
```

Read `attempts`, ownership counts and the explicit coverage limit. `unsafe_writes`
is `null`, because core cannot attest an external adapter's writes.
`g7_decision: required` and `removal_authorized: false` are deliberate. No calls,
no unresolved mapping rows, or clean bounded doctor samples prove neither a
complete inventory nor approval. The report is informational and has no fix.
See [src/doctor/record_checks.py](../../src/doctor/record_checks.py).

## Common failures and recovery

| Finding | Recovery |
|---|---|
| No vector export or incomplete inventory roots | Obtain an authorized offline export and rescan; keep G7 closed |
| Changed source descriptor or unmatched import receipt | Review the new sealed snapshot and exact source preconditions; do not relabel the old receipt |
| Quarantined or unavailable original | Retain raw evidence and resolve the original/scope issue before deprecation |
| Guarded translation fails because flags are off | Preserve the refusal and expected revision; flags do not authorize fallback to a legacy writer |
| Restore comparison differs | Keep consumers disabled, retain the mismatch and fix the backup/restore procedure |
| Erasure ledger missing | Do not enable delivery from the restored backup |
| Missing release/coverage/G7 receipt | Obtain the explicit evidence/decision; absence is not approval |

Rollback disables context, extraction, consolidation, semantic and UI first, then
writes. Leave authorized reads/export/repair available. Preserve records, receipts,
task mappings, pending intents and source ownership fences. Do not down-migrate
over retained data or deploy old code that can restore legacy write authority;
remain read-only and fix forward. See
[src/knowledge/imports/compatibility.py](../../src/knowledge/imports/compatibility.py).

## Related pages

- [Knowledge records](knowledge-records.md) — user-facing identity, history and grants.
- [Resumable knowledge import](knowledge-import.md) — actual apply/resume commands.
- [Database migrations](migrations.md) — production upgrade ownership and rollback.
- [Resource gating](resource-gating.md) — test slots and the explicit migration arm.

## Source and tests

[tests/test_knowledge_deprecation.py](../../tests/test_knowledge_deprecation.py)
covers full accounting, incomplete evidence, concurrency, disabled flags,
content-free telemetry, restore and erasure reapplication. Run its focused checks
and the related record/knowledge area tests under `aq test`. Exercise the new
migration separately because the default test markers exclude migrations:

```bash
aq test tests/test_knowledge_deprecation.py -m migration
aq test tests/test_record*.py tests/test_knowledge*.py
```
