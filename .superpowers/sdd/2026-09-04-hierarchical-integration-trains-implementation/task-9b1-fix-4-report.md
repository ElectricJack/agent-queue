# Task 9b1 fix round 4 report

## Result

Implemented the remaining two Critical and two Important lifecycle corrections
from `task-9b1-rereview-3.md` and `task-9b1-fix-4-brief.md`, starting from
`FIX_BASE=0d9f7b98`.

Runtime/test/migration commit: `a55c8085` (`fix(integration): block unresolved
candidate handoffs`). No external branch advance occurred before that commit.

## Delivered corrections

- Repair-stage expiry, ordinary ownership transfer, and confirmed ownership
  transfer now treat every exact-scope `state='reserved'` candidate ref mutation
  as unresolved and blocking, regardless of its executor lease expiry. Rebuild's
  existing hierarchy-transaction blocker already used the same unresolved
  definition and remains covered.
- Candidate mutation reservation is now a conn-owned helper. Repair acceptance
  inserts or canonical-rereads its deterministic `repair_handoff` mutation in
  the same hierarchy transaction that transfers the repair fence to the
  collector and persists the collector owner/token on the resolution.
- The post-commit mutation path adopts the exact canonical mutation nonce. A
  fresh service after transfer-before-push therefore resumes the durable claim
  rather than reconstructing or retrying the obsolete repair fence.
- `confirm_transfer()` canonical-rereads ownership after the external confirmer.
  It accepts the production-shaped released row only when session/workspace are
  cleared and `confirmed_workspace_id` matches the pre-release workspace. An
  already canonical released row also replays safely. `transfer_confirmed_on()`
  consumes an exact canonical confirmation snapshot.
- Migration `46f910d0dce6` now performs a pre-DDL downgrade guard and deliberately
  refuses when any resolution retains non-null handoff owner/fence provenance.

## RED evidence

1. Expired ambiguous invalidation:

   `pytest -q tests/test_integration_candidates.py::test_expired_ambiguous_claim_blocks_every_invalidator -x`

   The stage case failed because `RepairService.expire()` returned `expired`
   instead of the required `not_due`; it had filtered out the surviving
   reserved row solely because `expires_at` had passed.

2. Transfer-to-mutation atomicity:

   `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0-after_handoff_reservation]' -x`

   The interleaved rebuild returned `conflict` instead of `wait`: collector
   transfer had committed without a durable handoff mutation for rebuild to
   observe.

3. Production-shaped confirmation:

   `pytest -q tests/test_integration_ownership.py::test_confirmed_transfer_consumes_production_shaped_release_first_try -x`

   `1 failed`; the method returned its stale `handoff_pending` row after the
   callback had atomically written canonical `released` and cleared its writer
   fields.

4. Already-released replay found during self-review:

   `pytest -q tests/test_integration_ownership.py::test_confirm_transfer_replays_existing_canonical_release -x`

   `1 failed`; validation compared the persisted confirmed workspace against
   the intentionally cleared `workspace_id`. The correction now distinguishes
   a newly released callback result from an already canonical released replay.

5. Downgrade safety:

   `pytest -q -m migration tests/test_migration_candidate_mutation_claims.py::test_sqlite_candidate_mutation_claim_upgrade_downgrade_upgrade -x`

   `1 failed`; `pytest.raises(RuntimeError)` reported `DID NOT RAISE`, proving
   the prior downgrade silently dropped live collector-fence provenance.

## Focused GREEN evidence

- Writer-before-invalidator expired-ambiguous matrix:

  `pytest -q tests/test_integration_candidates.py::test_expired_ambiguous_claim_blocks_every_invalidator -x`

  Result: `4 passed` for stage expiry, ordinary transfer, confirmed transfer,
  and rebuild. The reserved row survived every attempted invalidation.
- Invalidator-before-writer ordering:

  `pytest -q tests/test_integration_candidates.py::test_invalidator_commit_fences_later_mutation_reservation -x`

  Result: `4 passed`. Each committed stage/revision/owner invalidation fenced a
  stale writer before claim insertion; no late mutation row was created.
- Atomic repair handoff and fresh replay:

  `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0-after_handoff_reservation]' -x`

  Result: `1 passed`. After the transfer transaction and before external
  mutation, stage expiry returned wait, ordinary transfer was busy, rebuild
  returned wait, the handoff claim remained reserved, and a fresh service
  reconciled and accepted revision N exactly once.
- Canonical confirmer contract:

  `pytest -q tests/test_integration_ownership.py::test_confirmed_transfer_consumes_production_shaped_release_first_try tests/test_integration_ownership.py::test_confirmed_transfer_rejects_mismatched_released_workspace tests/test_integration_ownership.py::test_confirm_transfer_replays_existing_canonical_release -x`

  Result: `3 passed`, covering first-attempt production mutation, mismatched
  workspace rejection, and already-released replay.

## Migration evidence

Fresh dual-backend command:

`POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' pytest -q -m migration tests/test_migration_candidate_mutation_claims.py`

Result: `2 passed, 2 warnings in 3.91s`. SQLite and a uniquely named disposable
PostgreSQL scratch database each exercised safe handoff-null
upgrade/downgrade/upgrade, then seeded a non-null handoff pair and proved the
downgrade refused before DDL while preserving both columns and values. The
PostgreSQL fixture dropped its scratch database in `finally`; neither the
`postgres` database, the `integration_test` database, nor an operator database
was migrated.

`python3.12 -m alembic heads` returned exactly `46f910d0dce6 (head)`.

## Final verification

`aq test tests/test_integration_candidates.py tests/test_integration_repair.py tests/test_integration_ownership.py`

Fresh post-self-review result: `97 passed, 11 warnings in 41.48s`, using aq slot
1 and three workers.

`ruff check src/integration/candidates.py src/integration/ownership.py src/integration/repair.py tests/test_integration_candidates.py tests/test_integration_ownership.py tests/test_migration_candidate_mutation_claims.py migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py`

Result: `All checks passed!`.

`python3.12 -m py_compile` on all seven changed Python files completed with exit
0. `git diff --check` completed with exit 0. The shell's bare `python` command
was absent and the bare `alembic` script had a missing interpreter, so the
successful compile and Alembic-head checks used the repository's working
Python 3.12 module entrypoints.

## Changed files

- `src/integration/candidates.py`
- `src/integration/ownership.py`
- `src/integration/repair.py`
- `migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py`
- `tests/test_integration_candidates.py`
- `tests/test_integration_ownership.py`
- `tests/test_migration_candidate_mutation_claims.py`

## Self-review and scope

- The repair-to-collector fence transfer, immutable resolution handoff binding,
  and exact ref-mutation reservation are one local hierarchy transaction. No
  token, Git, forge, or other network await was moved inside it.
- Claim expiry remains executor/reconciliation metadata only. An ambiguous
  remote result remains durably reserved and blocks every invalidator until an
  authenticated observation atomically proves applied or safely unapplied.
- Mutation IDs and identities remain deterministic. Canonical replay adopts
  only the stored nonce and exact operation episode/stage, project lease, branch
  owner/fence, revision, ordinal, and OIDs.
- Confirmation cannot legitimize a changed owner or mismatched workspace; the
  consumer compares the exact canonical row after callback mutation.
- Downgrade refusal occurs before any schema mutation and preserves replay-
  critical handoff authority.
- No Task9b2 main promotion, workflow/playbook, enablement, live forge call,
  caller-controlled repository transport, operator DB action, or queue mutation
  was added.

Residual concern: the pre-existing SQLite-to-PostgreSQL copy/cutover omission
for hierarchical integration tables remains Task12 scope and is unchanged.
