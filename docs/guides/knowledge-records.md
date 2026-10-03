# Knowledge records

Keep durable findings as versioned knowledge, and cite them from executable tasks.

## Why it exists

A task tracks work that can be claimed, completed and delivered. Knowledge retains
what that work established: an incident, procedure, decision or reference. Knowledge
never enters task readiness, capacity or progress. Core stores and searches it with
every optional memory provider absent; see [src/knowledge/service.py](../../src/knowledge/service.py)
and [src/records/search.py](../../src/records/search.py).

## Vocabulary

A **record** identifies either a task or knowledge. A **revision** retains an exact
knowledge snapshot, including its sources and outgoing links. A **proposal** is an
unaccepted correction. Verification and authority require separate authorized
actions; a recent or frequently retrieved finding gains neither. A **managed source**
is a legacy item whose write ownership has moved to core through explicit import.
Its compatibility fence survives flag rollback and deletion of the original file.

## A realistic example

Prerequisites: a running daemon and access to the `agent-queue` project. This is a
read-only discovery command, run while writing this guide; it activates nothing:

```bash
aq record capabilities --project-id agent-queue --json
```

The observed capability response had `enabled: false`, `writes_enabled: false`,
`enabled_projects: []`, `legacy_memory_mode: disabled`, and every feature disabled.
Read `data.capabilities.granted_operations` as well: configuration and principal
grants are independent. Replace the project ID with your own when following this
example. There is no state to clean up.

After an operator approves the core pilot and grants project writes, the following
is an **operator template**, not an operation performed for this guide:

```bash
aq knowledge create --project-id demo --title 'Restore drill finding' \
  --body 'The disposable restore retained exact revision hashes.' \
  --category incident --idempotency-key restore-drill-finding-1 --json
```

The create receipt supplies the identity and revision. Reuse the key only to replay
the same request. Use `knowledge show --revision-id` for an exact historical read,
and `knowledge update --if-revision --idempotency-key` for a guarded edit. A stale
base returns a conflict; read it and reconcile the edit. Knowledge starts
unverified. A worker can propose a protected correction, while verification,
authority, proposal acceptance and erasure require stronger grants. See
[src/records/auth.py](../../src/records/auth.py) and
[src/commands/knowledge_commands.py](../../src/commands/knowledge_commands.py).

## Inputs and outputs

Knowledge accepts a title, original body, category, tags, optional summary,
sources and namespaced metadata. Bodies preserve Unicode and newline bytes.
Mutations use an expected revision and idempotency key; responses include stable
identity, revision and content hash. History retains the old snapshots. Retirement
requires a reason, and record-level restore creates a new revision from retained
history, clearing verification; it does not restore a database backup. Redacted
history returns a typed unavailable result. It never substitutes the current body.
See [src/knowledge/models.py](../../src/knowledge/models.py).

`aq record search --kind task|knowledge|all` performs local lexical discovery.
Informational links can cite an exact knowledge revision. They do not become task
dependencies or change the execution graph. The mixed graph is implemented by
[src/records/graph.py](../../src/records/graph.py).

## State ownership

PostgreSQL owns installation identity, records, immutable revision envelopes,
payloads, links, receipts, protections and ownership mappings. Vault exports are
derived Markdown; an edited export is marked diverged and is not overwritten.
Retained import artifacts live under the confined `record-artifacts/` namespace.
Back up the database and the vault together. See
[src/records/export.py](../../src/records/export.py) and
[src/knowledge/imports/apply.py](../../src/knowledge/imports/apply.py).

[src/config.py](../../src/config.py) ships core, writes, UI, import, export,
context, semantic retrieval, extraction and consolidation disabled. The project
allowlist is empty; global scope requires explicit enablement. Core storage
works with `memory.enabled: false`. Context and provider paths require the
memory master as well as their own feature flags and approved scope. An
operator crosses the applicable release gate before enabling each one. Plan
approval, completed tasks and a passing test never activate these controls.

## Common failures and recovery

| Symptom | Read or action | Meaning |
|---|---|---|
| `knowledge.disabled` | `aq record capabilities --project-id <project> --json` | Ask the operator to check the approved scope and flags; workers cannot activate them |
| `record.forbidden` | Inspect `granted_operations` and requested project | A grant does not confer another project's scope |
| `record.revision_conflict` | Read the exact current revision and propose a reconciled edit | Replaying with a new key does not resolve a stale base |
| `memory.migrated_read_only` | Use canonical knowledge update with an expected revision and key | The legacy source has one permanent core writer |
| `source_diverged` on a managed note | Read the canonical record and retain the differing source for operator review | The filesystem edit is not an automatic knowledge revision |
| `record.revision_redacted` | Retain the typed tombstone; consult the erasure ledger | Import replay or an old backup cannot authorize resurrection |
| An edited or missing export | Operator runs `aq doctor --check records.exports` | Repair the projection without discarding retained history |

The [deprecation and restore runbook](knowledge-deprecation.md) explains the
operator evidence behind these decisions. No live import, paid provider call,
adapter removal or source deletion was performed for this increment.

## Related pages

- [Resumable knowledge import](knowledge-import.md) — sealed selection, backups,
  replay receipts and ownership cutover.
- [Knowledge deprecation and restore](knowledge-deprecation.md) — compatibility
  telemetry, synthetic restore proof and the explicit G7 decision.
- [Migration and restore runbook](knowledge-migration-restore.md) — the complete
  operator backup, recovery and staged rollout procedure.
- [Knowledge context](../specs/knowledge-context.md) — common bundles, observed
  delivery, citations and shared budgets behind G4.
- [Extraction provider contract](../reference/knowledge-extraction-provider.md) —
  durable unverified proposals, independent budgets and G6 approval.
- [Database migrations](migrations.md) — disposable test databases and the
  operator-only production upgrade boundary.
- [Reviews](reviews.md) — approval evidence remains owned by the review system.

## Source and tests

[src/knowledge/deprecation.py](../../src/knowledge/deprecation.py) retains
content-free compatibility attempts and checks deprecation evidence.
[src/doctor/record_checks.py](../../src/doctor/record_checks.py) exposes the
informational `records.deprecation` report. The report never approves G7.

With the separate disposable PostgreSQL test service available and
`POSTGRES_TEST_DSN` configured, run:

```bash
aq test tests/test_knowledge_deprecation.py tests/test_knowledge_compatibility.py \
  tests/test_record_doctor.py
```
