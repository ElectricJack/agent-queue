# Resumable knowledge import

`knowledge_import` defaults to an offline dry-run. Applying a snapshot requires
the local operator, `knowledge.enabled`, the selected project's enablement,
`knowledge.writes_enabled`, and `knowledge.import_apply.enabled`. All feature
flags remain off by default. The G3 import gate must approve the pilot selection
before an operator enables apply. Source labels and frontmatter grant no
authority; imported records start unverified.

Retain the exact canonical manifest bytes and SHA-256 from the dry-run. Back up
PostgreSQL and the vault, including `record-artifacts/`, and retain a backup
receipt. The service records that receipt; the operator remains responsible for
ensuring it refers to a usable backup.

The `knowledge_import` command and `POST /api/knowledge/import` accept an explicit
apply request:

```json
{
  "operation": "apply",
  "project_id": "pilot-project",
  "manifest_content_base64": "<base64 of exact sealed bytes>",
  "manifest_sha256": "<sealed SHA-256>",
  "selected_item_ids": ["<item_key from the manifest>"],
  "idempotency_key": "pilot-snapshot-1",
  "backup_receipt": "<verified backup reference>",
  "limit": 100
}
```

The selection, manifest hash, backup receipt and expected revisions are pinned
to the run. Reusing its key with different inputs is a conflict. Each transaction
processes at most 100 selected items and commits the revisions, source mappings,
per-item receipts and cursor together. Failed items receive durable error
receipts; fixing an input or base revision requires a new explicit run.

If the response has `state: applying`, use `operation: resume` with the same
project, returned `run_id`, original `manifest_sha256`, and a batch `limit`.
`operation: cancel` stops progress without undoing imported records. The same
resume request can continue a cancelled run. A restarted process uses retained
snapshot artifacts rather than live legacy files. Keep those artifacts until
the run completes. A terminal replay creates no revisions and reports successful
receipts as reused. The selected count reconciles with created, revised, reused,
excluded, quarantined and failed counts when pending reaches zero.

Changed source content or paths require a new sealed snapshot, a new key and
`expected_revisions: {"<item_key>": "<current revision UUID>"}`. A concurrent
canonical edit causes a revision conflict. Also supply `expected_source_hashes`
by copying each prior receipt's `source_hashes`: its keys are SHA-256 of the
canonical JSON array `[source_installation_id, source_kind, source_scope,
source_key]`, and values pin that alias's previous descriptor hash. Every already
mapped alias must have its own expected old hash. A stale hash returns
`knowledge_import.source_conflict` even with the current revision token. Both
preconditions remain immutable across resume. Source identity retains its canonical
record; old physical paths remain fenced after relocation.

Original files are preserved. Managed note reads return the canonical body,
record/revision IDs and deprecation metadata, with `source_diverged` when the
retained source differs from the current file. Legacy write, append, delete and
promote refuse with `memory.migrated_read_only`, even after rollout is disabled
or the original file is deleted. Unselected sources remain under legacy
ownership. The core `CompatibilityFence` adapter port translates guarded writes
through `knowledge_update`; external adapters must use it for mapped sources
and exclude `knowledge/records/` from their watchers. External legacy plugins
remain denied by default; their optional implementation is separate from core
import activation.

Full sealed manifests are operator evidence and cannot become public record
sources. Vector records retain scoped original rows rather than exposing a
whole export containing other projects. Redaction denies and queues purging of
both record evidence and the retained import manifest, preventing resume or
fresh replay from restoring erased evidence.

Restore into a disposable PostgreSQL database from `pg_dump`, restore the vault
artifact directory, and verify history, mapping ownership, receipts and replay
before recovering a real installation. Revision `a00000000061` pins validator
helper lookup so PostgreSQL's empty restore search path accepts valid retained
snapshots while continuing to reject invalid ones. Schema downgrade with import
data is refused; use a read-only rollback with ownership mappings retained.
