# Task 9b1 fix round 5 report

## Result

Implemented the exclusive candidate ref-mutation executor correction from
`task-9b1-rereview-4.md` and `task-9b1-fix-5-brief.md`, starting from
`FIX_BASE=53964e55`.

Runtime/test commit: `795db5b6` (`fix(integration): fence candidate mutation
executors`). No external branch advance occurred before that commit.

## Delivered correction

- Only the invocation that inserts a mutation claim owns its nonce. Canonical
  readers no longer adopt a live nonce merely because the deterministic
  mutation identity matches.
- A live claim over the exact expected remote OID is observation-only and
  waits. An expired claim may be taken over only through an affected-row CAS
  over its exact ID, `reserved` state, nonce, expiry value, and expired
  predicate while current hierarchy, operation episode/stage, project lease,
  revision, and branch authority still validate.
- An expired claim whose remote OID is unexpected remains reserved and blocked;
  no reader pushes over it or deletes it.
- Authenticated observation of the desired remote OID marks the canonical claim
  applied without pushing. A successful push whose response is lost performs
  the same authoritative read/reconcile path and does not push a second time.
- Post-push authority loss still records the authenticated desired remote fact
  as applied but returns wait rather than allowing stale lifecycle progress.
- Repair handoff acceptance now converts a losing final member/cursor/resolution
  CAS into the canonical `already_accepted` or `wait` result. Concurrent fresh
  services therefore do not leak raw stale-authority or uniqueness errors.

## RED evidence

1. Concurrent live handoff execution:

   `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0-overlap]' -x`

   Result before the correction: `1 failed`; two fresh services reached final
   acceptance and the loser leaked
   `CandidateStaleAuthority('candidate repair acceptance CAS lost')` instead of
   returning the canonical persisted result.

2. Expired expected-old takeover:

   `pytest -q tests/test_integration_candidates.py::test_expired_expected_old_claim_has_one_fresh_executor -x`

   Result before the correction: `1 failed`; both services returned false and
   zero authenticated pushes occurred (`[False, False]`) because expiry was
   observed but no exact-nonce takeover CAS existed.

3. Lost push response with expired authority:

   `pytest -q tests/test_integration_candidates.py::test_lost_force_with_lease_response_reconciles_without_second_push -x`

   Result before the correction: the expired-authority variant leaked
   `CandidateStaleAuthority` after the remote had accepted the one push. During
   full-file verification this also exposed a reserved, rather than applied,
   canonical claim after post-push project-lease loss.

4. Retained pre-push crash behavior initially showed the intentional behavior
   change:

   `pytest -q tests/test_integration_candidates.py -k 'instance_bound_repair_reservation_push_and_accept_once and exact and accepted and 0' -x`

   The fresh service correctly returned wait at a still-live claim rather than
   adopting its nonce. The crash fixture was corrected to advance that exact
   durable claim past expiry before asserting takeover/recovery.

## Focused GREEN evidence

- Exclusive claim matrix:

  `pytest -q tests/test_integration_candidates.py::test_mutation_claim_executor_takeover_is_exclusive -x`

  Result: `3 passed`. Two fresh services produced exactly one CAS winner and
  one push for expired expected-old; live expected-old produced two waits and
  zero pushes; expired unexpected produced two waits, zero pushes, and retained
  the original reserved nonce.
- Lost-response reconciliation:

  `pytest -q tests/test_integration_candidates.py::test_lost_force_with_lease_response_reconciles_without_second_push -x`

  Result: `2 passed` for retained and expired project authority. Both variants
  issued one push, persisted the claim as applied after an authoritative ref
  read, and a fresh service issued zero pushes. The expired-authority variant
  returned wait as fenced.
- Overlapping repair acceptance:

  `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0-overlap]' -x`

  Result: `1 passed`. Two fresh services used the same deterministic handoff
  identity, exactly one pushed and accepted, and the loser returned wait or the
  canonical accepted replay without an exception.
- Retained stage-0 repair crash/replay matrix:

  `pytest -q tests/test_integration_candidates.py -k 'instance_bound_repair_reservation_push_and_accept_once and exact and accepted and 0' -x`

  Result: `6 passed, 47 deselected`, covering no-crash, overlap,
  reservation-before-push, transfer-before-push, push-before-acceptance, and
  acceptance-boundary recovery.
- Retained invalidator orderings:

  `pytest -q tests/test_integration_candidates.py::test_expired_ambiguous_claim_blocks_every_invalidator tests/test_integration_candidates.py::test_invalidator_commit_fences_later_mutation_reservation -x`

  Result: `8 passed`.
- Final candidate file after the post-push lease-loss reconciliation correction:

  `pytest -q tests/test_integration_candidates.py -x`

  Result: `54 passed, 3 warnings in 41.61s`.

## Final verification

`aq test tests/test_integration_candidates.py tests/test_integration_repair.py tests/test_integration_ownership.py`

Result: `103 passed, 11 warnings in 44.85s`, using aq slot 1 and three workers.

`ruff check src/integration/candidates.py tests/test_integration_candidates.py`

Result: `All checks passed!`.

`/usr/bin/python3.12 -m py_compile src/integration/candidates.py tests/test_integration_candidates.py`

Result: exit 0. `git diff --check` also completed with exit 0.

`/usr/bin/python3.12 -m alembic heads` returned exactly
`46f910d0dce6 (head)`. No schema or migration changed in this round. The shell's
bare `python` command was absent and the bare `alembic` launcher referenced a
missing interpreter, so the successful checks used the repository's working
Python 3.12 module entrypoints.

## Changed files

- `src/integration/candidates.py`
- `tests/test_integration_candidates.py`

No migration, public API, generated client, repair/ownership runtime, or other
schema surface changed.

## Self-review and scope

- The executor token remains a durable random nonce owned only by its successful
  inserter or exact expired-row CAS winner. No canonical-read adoption path
  remains.
- Remote inspection and authenticated push remain outside database
  transactions. Pre-push authorization retains the bounded mutation-exclusion
  transaction established in the prior approved rounds.
- Takeover requires both enough project-lease lifetime for the mutation bound
  and the exact old claim tuple; two readers cannot both win.
- Desired-remote observation is monotone and push-free. Unexpected remote state
  is never overwritten, and ambiguous reserved state continues blocking all
  invalidators.
- A stale post-push authority snapshot cannot advance batch lifecycle even when
  the authenticated remote fact is reconciled as applied.
- No Task9b2 promotion, main mutation, workflow/playbook, enablement, live forge
  call, operator database action, queue mutation, or schema change was added.

Residual concern: the pre-existing SQLite-to-PostgreSQL copy/cutover omission
for hierarchical integration tables remains Task12 scope and is unchanged.
