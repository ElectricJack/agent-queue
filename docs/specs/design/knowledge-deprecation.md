# Knowledge deprecation evidence (K14)

Implementation contract for approved `rev-quick-orbit` r1, sections 11–13, K14.
This contract adds diagnostics and a synthetic restore drill. G7 remains an
explicit operator decision; the implementation performs no activation, live
inventory/import, paid provider call, uninstall or source deletion.

## Compatibility telemetry

`record_compatibility_usage` retains aggregate managed-source attempts keyed by
`(scope_key, operation, outcome)`, with a positive `calls` counter and server-set
`first_seen_at`/`last_seen_at`. Operations are read, list, write, append, delete
and promote. Outcomes are canonical_read, refused_write and guarded_write.
Atomic PostgreSQL upsert increments attempts before canonical dispatch and keeps
last-seen time monotonic when older transactions arrive after newer callers. A failed
dispatch is still an attempt, never reported as a successful mutation.

Notes count under the existing source-path transaction/lock through the database
service interface. Compatibility adapters count in their ownership-lookup
transaction. Unmapped legacy notes remain unchanged; every managed unsafe write
continues to refuse even with rollout disabled. The table retains no source keys,
paths, content, actor IDs or record IDs. Scope is a soft label so deleted projects
do not erase the audit aggregate. Named checks constrain scope/operation/outcome
and positive counts. Its additive inspector-guarded migration preserves nonempty
evidence on downgrade and directs operators to read-only rollback.

The operator-only doctor surface `records.deprecation` is read-only and
informational, with no fix. Its aggregate report explicitly says coverage is
limited to managed core compatibility paths and reports unsafe writes as unknown.
It never initializes a plugin or reads live vector search/get paths. Neither an
empty report nor zero mapped-source issues establishes complete adapter coverage.

## Reconciliation and removal evidence

The internal `reconcile_manifest` reader verifies supplied sealed manifest bytes
and their exact SHA256. For every source, candidates require a matching import
receipt, exact source descriptor hash, managed ownership and a retained revision
in the same scope. Exclusions require a matching receipt and stored exclusion
reason. Changed, missing, redacted, quarantined and unavailable sources remain
unresolved. Unobserved/unavailable vector inventory prevents completeness. The
output retains only manifest hash and counts; no source identity or body leaves
the reader. Manifest integrity does not establish authorization or that inventory
roots cover the whole installation; that remains explicit operator evidence.

`DeprecationEvidence` carries operator attestations: announcement timestamp,
distinct release receipts, backup/restore proof, replacement acceptance, complete
adapter coverage, zero unsafe writes, named G7 approver and decision receipt.
`adapter_removal_guard` requires at least two distinct releases and 30 elapsed
days, plus complete reconciliation and all attestations. Missing evidence closes
the guard. Receipt strings are not signature checks or permission tokens. Even a
complete result grants no removal permission: uninstall and deletion require
separate operator authorization. These helpers are internal mechanism, with no
new mutation command or automatic release policy.

## Restore acceptance

`tests/test_knowledge_deprecation.py` uses synthetic complete file inventory and
an observed vector inventory, real PostgreSQL dump/restore into test-owned scratch
databases, and a separate temporary vault. It compares all retained domain rows
and vault bytes, recomputes exact historical hashes, verifies ownership/counters,
and checks import replay without duplicate creation. An erasure performed after
backup is reapplied before any delivery is enabled, and the retained revision
must return typed redacted unavailability. The test harness owns cleanup.

This proof covers functional preservation of synthetic data. It does not attest
live inventory coverage, provider/model quality, performance, a real production
backup, or a G7 decision. K11/K13 and their release evidence remain prerequisites;
an organizational container or completed implementation task activates nothing.

Operator procedures and gate evidence are in
[knowledge deprecation and restore](../../guides/knowledge-deprecation.md).
