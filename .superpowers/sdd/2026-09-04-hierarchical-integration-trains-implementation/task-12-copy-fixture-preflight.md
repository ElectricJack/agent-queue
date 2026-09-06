# Task 12 SQLite→PostgreSQL copy and fixture preflight

Date: 2026-09-06

Scope: bounded read-only inspection of the canonical and legacy copy adapters, the current
SQLAlchemy metadata graph, the Task 10a parent-completion fixture failures, and the Task 10c/11
briefs. No test, database, network, queue, runtime, or operator action was performed. Concurrent
Task 10c worktree edits were observed but not modified or treated as reviewed runtime.

## Findings

`src/database/migrate_sqlite_to_pg.py` is the canonical adapter used by
`src/setup_wizard.py`. Its `_ORDERED_TABLES` contains 54 of the 85 tables visible in the current
worktree metadata. The 31 omitted tables below would therefore be silently omitted by a copy. The
existing `tests/test_migrate_sqlite_to_pg.py::test_ordered_tables_covers_every_table` already
expresses the correct invariant and will fail until the list is complete.

The separate `scripts/migrate_sqlite_to_pg.py` is not a compatible second implementation. It has
an old 23-name list containing removed `playbook_runs`, `hooks`, and `hook_runs`; omits current
tables; skips nonempty target tables rather than enforcing an all-empty target; and performs no
single-transaction rollback, deferred-FK fixup, or complete metadata comparison. It must not
remain an independently maintained migration path.

The canonical adapter is substantially safer, but it currently commits the bulk copy before
deferred-FK fixups and sequence resets, and it opens a new SQLite connection for each table and
fixup. A crash can therefore leave a partial target, and concurrent source writes can yield a
cross-table snapshot that never existed. Task 12 should make the stopped-daemon/quiesced-source
precondition explicit and use one source snapshot plus one target transaction.

## A. Exact missing tables and dependency order

The following order is FK-safe relative to the current metadata when appended after the existing
legacy/base tables (so `projects`, `repos`, `tasks`, `sessions`, `workspaces`,
`playbook_artifacts`, and other non-integration dependencies already exist). It also preserves the
logical producer-before-consumer order where the metadata intentionally lacks an FK.

1. `integration_branch_owners`
2. `integration_review_evidence`
3. `integration_batches`
4. `integration_candidate_revisions`
5. `integration_parent_episodes`
6. `integration_repair_operations`
7. `integration_repair_stages`
8. `integration_check_evidence`
9. `project_integration_schedules`
10. `project_integration_leases`
11. `integration_outbox`
12. `task_branch_origins`
13. `integration_batch_members`
14. `integration_candidate_member_results`
15. `integration_candidate_publications`
16. `integration_child_dispositions`
17. `integration_candidate_resolutions`
18. `integration_candidate_ref_mutations`
19. `integration_promotion_intents`
20. `integration_root_intent_members`
21. `task_delivery_receipts`
22. `integration_attestation_publications`
23. `integration_repair_stage_evidence`
24. `integration_parent_verifications`
25. `integration_parent_operation_completions`
26. `integration_episode_receipt_acceptances`
27. `integration_parent_verification_evidence`
28. `integration_operation_artifact_pins`
29. `task_integration_checkpoints`
30. `integration_outbox_artifact_pins`
31. `integration_cleanup_items` — visible only in the concurrent, unreviewed Task 10c worktree at
    inspection time; include only after that migration passes review.

Important dependency edges behind that order:

- members follow review evidence; candidate member results follow members and revisions;
  candidate publications follow revisions;
- parent repair operations follow parent episodes; repair stages follow operations logically;
  candidate resolutions follow member results, stages, tasks, sessions, and workspaces;
- candidate ref mutations follow revisions and optional resolutions; promotion intents follow
  revisions; root intent members follow intents, members, results, and review evidence;
- delivery receipts follow root members/results and parent episodes/operations; episode receipt
  acceptances in turn follow receipts, episodes, operations, and parent verifications;
- attestation publications follow projects, candidate revisions, repair operations, and check
  evidence; repair-stage and parent-verification evidence follow check evidence;
- parent verifications follow episodes and operations; operation completions follow verifications;
  checkpoints follow episodes, verifications, and operation completions;
- artifact pins follow their outbox/operation owner and `playbook_artifacts`; cleanup items follow
  batches, projects, repos, and immutable delivery receipts.

The complete graph fixture must populate and compare the JSON-bearing columns, not merely row
counts. Current JSON fields are:

- `integration_review_evidence.evidence`;
- `integration_promotion_intents.review_evidence`, `authors`, `provenance`,
  `commit_metadata`, `conflict_diagnostics`, `resolution_commit_shas`,
  `resolution_push_evidence`, and `remote_evidence`;
- `task_delivery_receipts.review_evidence`, `verification_evidence`, and
  `resolution_evidence`;
- `integration_batches.policy_snapshot` and `artifact_snapshot`;
- `integration_batch_members.review_evidence`;
- `integration_candidate_member_results.conflict_evidence`;
- `integration_candidate_resolutions.repair_commit_shas` and `push_evidence`;
- `integration_root_intent_members.result_evidence`;
- `integration_repair_operations.policy_snapshot` and `artifact_snapshot`;
- `integration_repair_stages.policy`, `current_subject`, `success_subject`,
  `retained_handoff`, and `dossier`;
- `integration_check_evidence.checks`; and
- `integration_outbox.payload` and `destination_manifest`.

## B. Minimum safe canonical-adapter and delegation changes

1. Import every final metadata table into `src/database/migrate_sqlite_to_pg.py` and extend
   `_ORDERED_TABLES` in the dependency order above. Keep the existing exact metadata-set,
   duplicate, FK-order, deferred-nullability, and primary-key tests.
2. Require source and destination schemas to be the same reviewed final Alembic head before any
   data copy. Fail before target mutation when either revision or metadata coverage differs.
3. Open one SQLite connection and explicit read transaction for the complete read/fixup input.
   Document and enforce that the daemon is stopped before the snapshot begins; the transaction is
   defense in depth, not permission to copy a live writer.
4. Open one PostgreSQL transaction for the empty-target check, trigger suppression, every table
   insert, deferred-column fixups, sequence resets, and exact verification. Refactor the internal
   helpers to accept those owned connections rather than opening/committing independently. Restore
   `session_replication_role` in `finally`; any error must roll the transaction back to the
   original empty target.
5. Compare every copied row by primary-key tuple and typed column value before commit, including
   nested JSON values and nullable provenance—not only `actual >= expected` counts. Retain the
   post-commit count/progress result as operator output. Explicitly test composite keys, byte/text
   values, booleans, nulls, and all JSON columns above.
6. Seed one dependency-complete live graph: project/repo/tasks/workspaces/session; parent episode,
   operation/stages/checkpoint/verifications/completion/carry-forward; review evidence and child
   receipt; schedule/lease/outbox plus artifact pins; batch/members/revisions/results/publication/
   resolution/ref claim; root intent/member receipts; CI/attestation; and reviewed Task 10c/11
   records. Copy to a unique empty PostgreSQL database, then compare every row and relationship.
7. Add a crash/fault injection after bulk insertion and during deferred fixup to prove the target
   remains empty. Add a source-write/quiescence rejection test rather than accepting a mixed
   snapshot. Retain the existing nonempty-target and sequence-reset cases.
8. Replace `scripts/migrate_sqlite_to_pg.py` with a thin CLI wrapper over
   `src.database.migrate_sqlite_to_pg.migrate_sqlite_to_postgres`; it must contain no table list or
   copy algorithm. If dry-run remains supported, expose one canonical read-only count/validation
   helper and call it from the wrapper. Otherwise fail dry-run with a clear deprecation message;
   never preserve the stale best-effort/skip behavior.

The preferred safety contract is both stopped-daemon source quiescence and one atomic destination
transaction. If PostgreSQL privilege policy prevents `session_replication_role = replica`, fail
before copying rather than falling back to unordered or partial inserts.

## C. Exact immutable-receipt fixture corrections

The two inherited failures are:

- `test_arbitrary_resolution_json_cannot_satisfy_code_receipt_chain`, which currently calls
  `_code_receipt(...)` and then UPDATEs `squash_sha=NULL` plus arbitrary
  `resolution_evidence`; and
- `test_unbound_historic_receipts_do_not_satisfy_current_parent_episode`, which currently calls
  `_code_receipt(...)` and then UPDATEs `parent_operation_id` and `parent_episode_id` to NULL.

Both UPDATEs correctly fail under the reviewed append-only receipt trigger. The consumer behavior
can be tested without weakening or temporarily dropping that trigger because both intended rows
are legal at their initial INSERT under the current checks:

1. Extend the test-only `_code_receipt` helper with keyword-only `omit_squash: bool = False`,
   `resolution_evidence: dict | None = None`, and `bind_parent: bool = True` controls.
2. At the one INSERT, set `squash_sha=None if omit_squash else after_sha`, pass the supplied
   `resolution_evidence`, and set both parent-binding columns to their canonical values when
   `bind_parent` is true or both to NULL when false. Do not UPDATE afterward.
3. Change the arbitrary-JSON test to call `_code_receipt(..., omit_squash=True,
   resolution_evidence={"kind": "conflict_resolution", "trusted": True})`; retain its
   `waiting`, original-head, and `receipt_chain` assertions.
4. Change the unbound-history test to call `_code_receipt(..., bind_parent=False)`; retain its
   `waiting` and `receipt_missing` assertions.
5. Keep separate schema tests asserting UPDATE and DELETE raise the append-only error on both
   SQLite and PostgreSQL. Do not disable, drop, or relax production triggers in either fixture.

This preserves the original purpose: readiness must reject a malformed code proof and an unbound
historic receipt, while immutable receipt history remains impossible to rewrite.

## D. Explicit still-pending final inventory

Task 12 must refresh `metadata.tables` after reviewed Tasks 10c and 11 rather than freeze this
preflight list.

- Task 10c currently proposes/contains `integration_cleanup_items`. It is not yet reviewed at this
  snapshot. Release/catch-up reuses existing `project_integration_schedules`,
  `project_integration_leases`, batches, intents, receipts, and outbox rather than requiring a
  separate release table.
- Task 11 has not landed table names. Its brief requires separate desired/draining rollout state
  (possibly project columns) plus append-only cutover audit records, explicit-history waiver
  records, and durable scratch-probe run/receipt records binding transport, protection, trust,
  software, positive identity, and negative-control identity. Those final named tables/columns and
  every JSON field must be inserted into the canonical order and complete-graph fixture only after
  Task 11 review.
- Status, doctor, CLI, and read-only protection inspection do not themselves imply copy tables;
  do not invent persistence beyond the final Task 11 metadata.

At Task 12 start, rerun the metadata-set and FK-order inventory against the clean reviewed head.
The acceptance condition is exact equality between `_ORDERED_TABLES` and `metadata.tables`, not
merely the 31-item interim list above.

## Recommended focused acceptance grouping

1. Pure metadata tests: exact set, no duplicates, FK order/deferred columns, and legacy-wrapper
   delegation with no second table list.
2. SQLite-only fixture tests: legal-at-insert malformed/unbound receipts remain immutable and are
   rejected by readiness.
3. One unique PostgreSQL copy test with the complete graph and typed row-by-row/JSON comparison.
4. One atomicity group for fault rollback, nonempty target, schema-head mismatch, quiescence, and
   sequence reset.

No whole-repository suite is needed for this seam; Task 12's final affected-area and migration
groups can consume these focused tests later.
