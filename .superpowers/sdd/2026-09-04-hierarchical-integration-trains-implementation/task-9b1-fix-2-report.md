# Task 9b1 fix round 2 report

## Scope and revisions

- FIX_BASE: `2a26c5f8`
- Runtime/test/migration commit: `c5f0feda`
- Branch: `feat/hierarchical-integration-trains`
- No external branch advance was observed between FIX_BASE and the runtime commit.
- `aq prime` was run once and returned the expected scoped-worker error: `task_id is required (no task in scope — pass task_id explicitly or run inside a task session)`. No queue mutation was attempted.
- No live GitHub/forge call, operator database access, Task9b2 promotion, playbook, or enablement work was performed.

## Implementation

### 1. Construction/conflict authority

- Added generated Alembic revision `e1eab6dbc186` directly after current head `69416e65ee21`.
- Added normalized `integration_candidate_ref_mutations`, whose immutable identity freezes batch/revision/member/resolution, purpose, repository, authority branch, exact target branch, old/new OIDs, operation ID/episode/stage, project lease identity, branch owner/role/fence, nonce, TTL, and remote reconciliation.
- Candidate final and partial publication now use reserve-under-hierarchy-authority -> commit -> authenticated exact-ref read/push -> hierarchy-first reconciliation/CAS. No token, forge, authenticated ref, or Git network await is inside the authority transaction.
- `_validate_authority_on` now compares the operation episode and active stage in addition to project mode/repository, current batch/revision, lease owner/fence/expiry, operation state, and exact branch owner/role/fence.
- Conflict persistence is an affected-row-checked CAS over the current member and current batch revision/lifecycle. Existing conflict replay first reconciles the durable partial publication and only then dispatches.
- Concurrent `_pending` insertion is transactionally idempotent and converts a uniqueness race into canonical reread/stale authority rather than leaking `IntegrityError`.
- Live mutation claims block rebuild, repair-stage expiry, and branch transfer. Claim expiry is deterministic under the injected clock and bounded by the project lease expiry; takeover first authenticated-reads the remote tip.

Focused evidence:

```text
pytest -q tests/test_integration_candidates.py::test_same_owner_concurrent_builds_never_duplicate_external_mutation tests/test_integration_candidates.py::test_stage_change_before_conflict_cas_cannot_mark_new_stage_repairing tests/test_integration_candidates.py::test_partial_push_crash_reconciles_from_fresh_service_before_dispatch tests/test_integration_candidates.py::test_candidate_network_awaits_run_after_database_commit tests/test_integration_candidates.py::test_persisted_pr_never_hides_diverged_candidate_ref -x
5 passed, 3 warnings in 4.12s
```

The first candidate-file iteration exposed a real reconciliation RED after an applied claim:

```text
pytest -q tests/test_integration_candidates.py -x
1 failed, 21 passed in 18.11s
FAILED test_persisted_pr_never_hides_diverged_candidate_ref: returned already_built instead of wait
```

GREEN changed applied-claim replay to authenticated-read and compare the current remote OID rather than trusting local durable state.

### 2. Exact repair reservation, publication, and replay

- `reserve_repair` now resolves the project ID read-only, then atomically validates hierarchy/project train mode/designated repo, batch/current revision/current conflict, project lease, operation ID/episode/current active stage, stage deadline, exact repair task/session/instance/workspace, repository/integration branch, and attached repair fence.
- Resolution rows now persist `operation_episode_id`, `stage_deadline_at`, `project_id`, and a reservation-qualified immutable `target_branch` (`refs/heads/aq/integration-repairs/<reservation-id>`).
- Repair publication uses the shared durable mutation protocol. Crash after the exact remote repair ref reaches `resolved_head_sha` is reconciled from a fresh service without another push.
- Acceptance fetches the exact qualified ref through App-auth transport, repeats exact lineage/tree/reserved-path/intended-path validation, transfers only after stopped/detached confirmation, then durably publishes the accepted head onto the integration ref under the collector claim before the single member/cursor/reservation CAS.
- Replacement instance and workspace identities and a same-number fence for another target fail closed.

Focused evidence:

```text
pytest -q tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once -x
4 passed, 3 warnings in 8.34s
```

Those four arms cover primary/debug stage, stale instance, wrong exact target, replacement workspace, crash after repair push before persistence with a fresh service, exact accept/replay, exact reserved addition rejection, and extra-path/content rejection.

### 3. Accepted repair carry-forward

- `_accepted_parent_repair` now resolves `accepted_reservation_id` from the normalized resolution table, requires its accepted exact batch/revision/ordinal identity, App-auth imports the accepted head, reconstructs the frozen lineage, and revalidates it before N+1 construction.
- The stage-0 exact repair regression advances main, rebuilds N+1 using the accepted repair in ordinal order, asserts repaired file content, and asserts the sealed source ref did not move.

### 4. Durable PR identity

- Publication guards now allow only exact adjacent state transitions/replay and make repository ID/full name, base ref, head ref/SHA, idempotency key, PR number, and canonical URL immutable after publication on SQLite and PostgreSQL.
- Provider output must be the canonical `https://github.com/<full_name>/pull/<number>` and match every structured repository/base/head/idempotency field.
- A zero-row final CAS rereads and compares the canonical persisted PR; it never returns an unpersisted provider response.
- Create-before-persist replay constructs a fresh provider over the same durable fake-forge backing store and performs lookup-before-create, yielding one PR.

Focused evidence:

```text
pytest -q tests/test_integration_candidates.py::test_published_pr_identity_is_immutable_and_replay_is_canonical tests/test_integration_candidates.py::test_build_restarts_at_every_persisted_external_boundary -x
6 candidate nodes passed (included in the 7-node focused run below)
```

### 5. Cancellation

- Broker settlement distinguishes cancellation of the broker child from cancellation of the caller. Caller cancellation cancels/reaps the broker child and is re-raised unchanged.

```text
pytest -q tests/test_git_app_auth.py::test_exact_fetch_broker_settlement_preserves_caller_cancellation
1 passed, 2 warnings in 0.39s
```

### 6. Transaction/network invariant

- An instrumented database context counter asserts installation-token, exact-ref, authenticated push, and candidate publication work run at transaction depth zero.
- Static self-review found network boundaries only between the claim reservation commit and hierarchy-first reconciliation transaction. The only transaction-spanning callbacks remaining are existing branch handoff confirmation flows, which explicitly release the first transaction before awaiting confirmation.

## Migration evidence

The generated revision is a single Alembic head:

```text
.venv/bin/python -m alembic heads
e1eab6dbc186 (head)
```

The migration:

- adds the normalized claim table with RESTRICT FKs to candidate revision and optional resolution;
- adds exact resolution project/episode/deadline/target binding with compatibility backfill;
- installs identity/monotonic/applied-state guards for claims and exact adjacent/immutable guards for publication and resolution on both dialects;
- refuses downgrade while claim rows exist;
- restores the prior revision's publication/resolution guards on downgrade.

RED while developing the SQLite guard replacement:

```text
pytest -q tests/test_migration_candidate_mutation_claims.py::test_sqlite_candidate_mutation_claim_upgrade_downgrade_upgrade -m migration -x
1 failed: sqlite3.OperationalError: no such trigger: trg_candidate_resolution_identity
```

GREEN after accounting for SQLite batch-table trigger recreation, followed by the final dual-dialect cycle after all schema edits:

```text
POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' pytest -q tests/test_migration_candidate_mutation_claims.py -m migration -x
2 passed, 2 warnings in 5.76s
```

The PostgreSQL test created only its uniquely named scratch database and dropped it in `finally`. A read-only post-check returned `0` databases matching `postgres_master_task9b1_candidate_mutations%`.

## Final verification

```text
aq test tests/test_integration_candidates.py tests/test_integration_ownership.py tests/test_integration_repair.py tests/test_git_app_auth.py
96 passed, 11 warnings in 23.91s

ruff check src/database/tables.py src/git/manager.py src/integration/candidates.py src/integration/ownership.py src/integration/repair.py migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py tests/test_git_app_auth.py tests/test_integration_candidates.py tests/test_migration_candidate_mutation_claims.py
All checks passed!

.venv/bin/python -m py_compile src/integration/candidates.py src/integration/ownership.py src/integration/repair.py src/git/manager.py migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py
exit 0

git diff --check
exit 0
```

An earlier affected gate returned 29 failures because an accidental metadata-only `target_branch` column had been inserted on `task_integration_checkpoints` without a migration. The exact diff was removed; the two subsequent affected gates were fully green (96 passed). One focused two-file invocation was mistakenly run through bare pytest (`7 passed`) before the required final multi-file `aq test`; it did not change the final evidence above.

## Changed files

- `src/database/tables.py`
- `src/git/manager.py`
- `src/integration/candidates.py`
- `src/integration/ownership.py`
- `src/integration/repair.py`
- `migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py`
- `tests/test_git_app_auth.py`
- `tests/test_integration_candidates.py`
- `tests/test_migration_candidate_mutation_claims.py`

## Self-review and residual concerns

- Every candidate/partial/repair/handoff ref mutation has a deterministic operation identity plus per-activation nonce and bounded TTL. Authorized invalidators honor live claims; expired recovery begins with an authenticated exact-ref read.
- All mutation progress writes are affected-row checked or canonical-reread checked. Current revision, operation episode/stage, lease, and branch fence are revalidated after external I/O.
- Repair acceptance consumes normalized server-owned authority only and advances a member ordinal once.
- No caller URL, wildcard fetch, ambient credential/config path, live provider, main push, CI execution, or Task9b2 path was added.
- The pre-existing SQLite-to-PostgreSQL copy-order test still reports all 28 hierarchical-integration tables as missing from `_ORDERED_TABLES` (not only this phase's new table). This is the previously deferred cross-phase migration-adapter gap and was not expanded into Task9b1 fix scope.
- This report does not claim Task9b1 complete; an independent controller review remains required.
