# Task 9b2 implementation report

## Status and scope

- Starting base: `0694526f00d5b79bb4292c2990007ea211d6e2b0`.
- Runtime/tests commit: `c84d367df0898428e7626a10deb46bf3a4270c7f`.
- `aq prime` was run exactly once. It returned `task_id is required (no task in scope...)`; no queue record was read or mutated afterward.
- No external shared-branch change appeared during implementation. No operator database, live credential, network, forge, project-enablement, playbook, cleanup worker, or Task10/Task11 behavior was touched.
- The migration is the single new head `d4a81f0c9e72`, based on reviewed Task9b1 head `46f910d0dce6`.

## Delivered contracts

### Durable root intent and receipts

- Promotion intents now have a historical-default `child` versus `root` discriminator. Root identity requires the immutable paired batch/revision, exact aggregate CI evidence, and separately named project-lease and integration-branch fence provenance. Root `superseded` is terminal and removed from the unresolved-target uniqueness domain without changing child conflict states.
- `integration_root_intent_members` is normalized and append-only. It freezes every ordinal's deterministic receipt ID, task/repository/reviewed head/tree, generated squash SHA, accepted result evidence, and review evidence ID before any main write. The root intent's receipt is exactly ordinal zero; there is no anonymous extra receipt.
- Root receipt batch/revision/member fields are all-or-none and reference the exact sealed member and candidate result. Existing child finalizers reject root intents in both the service and database mixin.
- Candidate main-mutation claims add only the reviewed `root_main`, `prewrite_at`, and terminal `superseded` shapes. Existing Task9b1 reserved-claim consumers therefore block rebuild, repair-stage expiry, and ownership transfer even after the originating lease expires.
- Downgrade refuses to erase any root intent, normalized reservation, root receipt, or root-main mutation claim. The refusal occurs before destructive DDL; draining the fixture permits downgrade and re-upgrade.

### Exact trusted green and prepare

- `CIService.observe_candidate` still performs provider observation outside SQL locks, then re-locks/revalidates the exact current subject. An exact trusted success appends one deterministic aggregate evidence row and atomically projects its ID, tested candidate SHA, candidate `green` state, and awaiting-completion stage. Failed or stale observations cannot project green.
- `RootPromotionService.prepare(batch_id, revision)` accepts no caller-supplied SHA, evidence, repository, lease, fence, operation, receipt, or publication identity. It derives all of them under the hierarchy-first project lock from the complete current candidate, exact aggregate evidence and ordered configured checks, current root operation/stage, project lease, integration-branch collector, and authenticated publication binding.
- Empty batches return `already_promoted` without an intent, receipt, operation, CI request, ref, or push. Nonempty prepare pins `refs/aq/root-promotions/<intent-id>` before atomically writing the intent, every member reservation, the root-main mutation claim, and `testing -> promoting` lifecycle.
- Stable identities are UUIDv5-derived from `(batch, revision)` and `(batch, revision, ordinal)`. Exact replay returns the full ordered receipt list.

### Main write, reconciliation, and finalization

- Main is read through the authenticated Task9a App client before any push. Only the exact tested candidate, frozen literal GitHub repository binding, default branch, construction base, and Task9a isolated `apush_oid_with_app_auth` expected-old primitive are used.
- The durable claim is reserved and committed before token/Git work. A prewrite marker is committed only after a hierarchy-locked recheck of current revision, root operation/stage, lease namespace, branch-fence namespace, and claim nonce/deadline. Local Git must prove `construction_base <= tested_candidate` before the write. No network or Git operation occurs while a database transaction is held.
- Live claims wait. After expiry, a successor can CAS-take the exact existing claim; a stale observed nonce cannot supersede that successor. Crossed/equal-looking lease and branch fence tokens do not authorize takeover.
- Lost push responses and restarts first re-read authenticated main. Exact candidate or an authenticated descendant finalizes the original intent without another push. Missing/unavailable/indeterminate ancestry, or an obsolete claim with a prewrite marker, remains `reconciliation_blocked`. Only an obsolete, expired, exact, provably unattempted claim can atomically become terminal `superseded`.
- Root finalization is one hierarchy transaction. It requires the applied exact mutation proof and the complete ordered append-only reservations; inserts every receipt, emits one `integration.root_delivered` event per ordinal plus `integration.batch_promoted` and `integration.cleanup_requested`, marks the candidate promoted, batch cleanup-pending with authenticated final main SHA, stage passed, operation completed, and intent committed. A mid-receipt crash rolls back all receipts/events/state; replay completes all members.
- The production push path makes no forge merge/rebase/squash call, creates no commit, and performs no post-main CI/audit run.

### Public command

- Added strict `integration_promote_main {batch_id, revision}` with stable typed outcomes and the full ordered `receipt_ids`. Only local/service or correctly scoped capable playbook authority can enter the service; a session caller is rejected before any service call.
- Existing `integration_reconcile_promotion` remains child-only and unchanged.

## TDD evidence

### Milestone 1 — schema and guards

- RED: `pytest -q tests/test_migration_root_main_promotion.py::test_sqlite_root_promotion_schema_and_guarded_round_trip -m 'migration and perf'`
  - `1 failed`; `integration_root_intent_members` was absent.
- GREEN SQLite: same node after the migration
  - `1 passed, 2 warnings in 3.75s`.
- GREEN PostgreSQL: `POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' pytest -q tests/test_migration_root_main_promotion.py::test_postgres_root_promotion_schema_and_guarded_round_trip -m 'migration and perf'`
  - `1 passed, 2 warnings in 2.50s`.
- Combined schema refinement gate:
  - `2 passed, 2 warnings in 6.09s`.

### Milestone 2 — aggregate green and root prepare

- RED aggregate projection: `pytest -q tests/test_integration_ci.py::test_ci_service_binds_root_evidence_to_exact_batch_revision_and_candidate`
  - `1 failed`; result had no `aggregate_evidence_id`.
- GREEN same node:
  - `1 passed, 2 warnings in 0.43s`.
- CI file gate: `pytest -q tests/test_integration_ci.py`
  - `34 passed, 2 warnings in 1.89s`.
- RED prepare: focused root-prepare collection
  - collection failed with `ModuleNotFoundError: No module named 'src.integration.main_promotion'`.
- GREEN root preparation/empty/stale cases:
  - `3 passed, 3 warnings in 0.78s`.
- RED child-finalizer boundary:
  - `1 failed`; the child finalizer accepted a root-shaped intent.
- GREEN root prepare plus child boundary:
  - `4 passed, 3 warnings in 1.22s`.

### Milestone 3 — claim, exact push, and crash reconciliation

- RED exact main path:
  - focused test failed because `RootPromotionService` had no `promote` method.
- GREEN exact main node:
  - `1 passed, 3 warnings in 1.19s`.
- Crash/restart matrix initially exposed the immutable-prewrite transition and swallowed-crash fixture issues; after fixing the durable state transition and fixture, the exact three-node command completed:
  - `3 passed, 3 warnings in 1.80s`.
- Strengthening nodes for crossed fences, obsolete live/unattempted/ambiguous state, authenticated descendant, and real Git ancestry:
  - `5 passed, 3 warnings in 1.86s`.
- RED successor supersession race: `pytest -q tests/test_integration_main_promotion.py::test_obsolete_supersession_cannot_erase_a_successor_claim`
  - `1 failed, 3 warnings in 4.33s`; stale observed claim incorrectly superseded the fresh nonce/deadline.
- GREEN same node after hierarchy-locked batch/intent plus exact expired-claim CAS validation:
  - `1 passed, 3 warnings in 1.10s`.

### Milestone 4 — finalizer, invalidators, and command

- RED lifecycle/authority group: `pytest -q tests/test_integration_main_promotion.py::test_root_prepare_derives_exact_green_authority_and_reserves_all_members tests/test_integration_main_promotion.py::test_live_root_claim_blocks_owner_handoff_and_repair_expiry tests/test_integration_main_promotion.py::test_root_promotion_command_denies_session_and_allows_service`
  - `1 failed, 2 passed, 3 warnings in 1.47s`; the reserved batch remained `testing` rather than atomically entering `promoting`.
- GREEN same command:
  - `3 passed, 3 warnings in 1.30s`.
- Root crash/replay/finalizer file:
  - `pytest -q tests/test_integration_main_promotion.py -x`
  - `16 passed, 3 warnings in 3.60s`.
- Final explicit two-activation concurrency node, added without changing runtime after the
  combined area gate: `pytest -q tests/test_integration_main_promotion.py::test_two_concurrent_root_activations_make_one_main_write`
  - `1 passed, 3 warnings in 1.23s`; two fresh service instances produced exactly one push.
- Contract gate:
  - `pytest -q tests/test_integration_contracts.py -x`
  - `10 passed, 3 warnings in 0.80s` before the final stale-outcome alignment, which is included in the final affected-area gate below.

## Final verification

- Final affected-area gate:
  - `aq test tests/test_integration_main_promotion.py tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_integration_ownership.py tests/test_integration_repair.py -x`
  - aq slot 1/2, three workers; `222 passed, 11 warnings in 54.03s`.
- A first final migration invocation without overriding the repository's marker filter was intentionally not counted: `POSTGRES_TEST_DSN=... pytest -q tests/test_migration_root_main_promotion.py` returned `2 deselected, 2 warnings in 0.32s`.
- Final dual-dialect migration cycle:
  - `POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' pytest -q tests/test_migration_root_main_promotion.py -m 'migration and perf'`
  - `2 passed, 2 warnings in 2.90s`.
  - The SQLite arm proves U-D-U and pre-DDL refusal/data retention. The PostgreSQL arm creates a unique `root_promotion_d4_*` scratch DB, proves the same cycle/refusal, and drops it. A read-only admin query afterward returned `[]` for that prefix. An attempted `psql` survivor check failed because `psql` is not installed; a read-only `asyncpg` check supplied the evidence. One malformed Python one-liner also produced a syntax error before that corrected read-only check; neither touched data.
- Changed-file Ruff:
  - `ruff check migrations/versions/d4a81f0c9e72_root_main_promotion_intents.py src/database/tables.py src/database/queries/integration_delivery_queries.py src/integration/ci.py src/integration/main_promotion.py src/integration/promotion.py src/commands/contracts/integration.py src/commands/integration_commands.py tests/test_migration_root_main_promotion.py tests/test_integration_main_promotion.py tests/test_integration_ci.py tests/test_integration_contracts.py`
  - First run found one unused `json` import in the new root service; after removal the exact rerun returned `All checks passed!`.
- Compile/head/diff:
  - `python3.12 -m py_compile` over the same 12 changed Python paths completed with exit 0.
  - `python3.12 -m alembic heads` returned `d4a81f0c9e72 (head)`.
  - `git diff --check` completed with exit 0.

## Self-review and deliverable reconciliation

- Reservation precedes every write; the recovery ref precedes reservation; prewrite proof precedes the exact expected-old push; authenticated observation precedes applied proof; all receipts and outbox rows share one final transaction.
- Project lock order is hierarchy-first. No token acquisition, Git subprocess, authenticated provider request, or crash-reconciliation read is performed under a long database transaction.
- Root project-lease and integration-branch token fields are never interchanged. Current authority is revalidated before claim takeover and again before the prewrite marker. The shared reserved mutation table makes existing rebuild, repair expiration, and owner-handoff invalidators purpose-agnostic.
- All Git inputs are immutable durable values. Replay does not read source branches or caller prose/dicts. The real temporary-Git test proves both positive and negative ancestry results.
- Aggregate green requires the exact trusted typed observation, configured ordered checks, producer, version, operation, batch, revision, and tested SHA. Generic check evidence cannot qualify.
- Exact-success, lost-response, descendant-applied, unattempted supersession, ambiguous write, crossed-fence, stale CI, empty batch, concurrent claim takeover, mid-finalization rollback, child-finalizer rejection, and command-authority paths are covered.
- The implementation intentionally leaves Task10 routing/periodic execution/cleanup, Task11 enablement/probes, live branch protection, live GitHub calls, forge merge APIs, synthetic/post-main CI, and project configuration changes out of scope.

## Concerns

- `RootPromotionService` is a large but single-purpose durable protocol module: preparation, claim/reconciliation, and atomic finalization are separated into private helpers but kept together so immutable root intent state and lock order remain locally auditable. A later structural split should preserve those transaction boundaries rather than refactor during this security-sensitive phase.
- Existing unrelated `pkg_resources` and `audioop` deprecation warnings remain. No warning suppression was added.
