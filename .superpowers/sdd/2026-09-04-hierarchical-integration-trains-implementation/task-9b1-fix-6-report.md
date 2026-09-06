# Task 9b1 fix round 6 report

## Result

Implemented the public-build observer correction from
`task-9b1-rereview-5.md` and `task-9b1-fix-6-brief.md`, with logical
`FIX_BASE=9fa29cf9`.

While the round-6 changes were uncommitted, the shared branch advanced through
merge commit `80034ced` (`origin/main` at `904181fd`). That merge changed only
dashboard, documentation, and E2E script paths; it did not overlap the candidate
runtime or tests. It was preserved without reset, rebase, or revert. The scoped
runtime review base is therefore `80034ced`.

Runtime/test commit: `c5ca1ea3` (`fix(integration): resume expired candidate
mutations`).

## Delivered correction

- The unresolved-mutation observer now returns a structured disposition with
  distinct blocker and recoverable claim IDs instead of an unconditional
  boolean.
- `build()` may continue only past an expired, exact-expected claim for the one
  normal phase matching current persisted progress. The existing mutation path
  rereads the row and performs the approved exact nonce/state/expiry takeover
  CAS; observation never adopts a nonce, deletes a claim, or grants executor
  authority.
- A recoverable `candidate_partial` must match the current revision cursor and
  exact persisted conflict evidence. A recoverable `candidate_final` must match
  a built current revision and its exact reserved publication identity.
- Eligibility is reread under the hierarchy lock and validates the current
  project lease window, operation episode/stage, repository, branch owner/fence,
  revision, claim identity, and phase state. A concurrent identity or authority
  change becomes a blocker or is fenced again by `_mutate_ref()`.
- Live expected-old, expired unexpected, insufficient-lease, failed-read, and
  out-of-order claims remain durable blockers. When any blocker is present it
  wins over recoverable rows. Rebuild continues treating every unresolved claim,
  including a recoverable build claim, as an invalidation blocker.
- Desired remote state is authenticated, reconciled to the canonical applied
  row, reread, and allowed to continue without a push. A failed reconciliation
  remains blocked.

## RED evidence

`pytest -q tests/test_integration_candidates.py::test_public_build_takes_over_expired_prepush_claim_once -x`

Before the correction the `candidate_partial` variant failed at the required
public retry assertion: `1 failed`, with `len(git.pushes) == 0` rather than one.
Both fresh `build()` calls returned at the observer even though the durable
claim had expired at 235, the remote was still exact expected-old, and the
project lease remained valid through 1000. This directly reproduced the review
finding without calling private mutation methods.

## Focused GREEN evidence

- Public partial/final takeover:

  `pytest -q tests/test_integration_candidates.py::test_public_build_takes_over_expired_prepush_claim_once -x`

  Initial GREEN: `2 passed, 3 warnings in 2.83s`. Each variant first crashed
  after its durable pre-push reservation with zero pushes, advanced time from
  100 to 236 past the 235 claim expiry while retaining sufficient project lease,
  then overlapped two fresh service instances. Exactly one instance won the
  takeover and pushed; the partial path converged to conflict and the final path
  converged through audit-PR completion.
- Final public observer matrix after self-review strengthening:

  `pytest -q tests/test_integration_candidates.py::test_public_build_takes_over_expired_prepush_claim_once tests/test_integration_candidates.py::test_public_build_observer_fails_closed_for_nonrecoverable_claims tests/test_integration_candidates.py::test_public_build_reconciles_desired_remote_without_repush -x`

  Result: `8 passed, 3 warnings in 6.08s`. The five blocker variants covered
  live expected-old, expired unexpected, insufficient current lease window,
  failed authenticated ref read, and out-of-order publication progress. Each
  returned wait with zero push and retained the original reserved nonce. The
  desired-remote case marked the claim applied and completed with zero service
  pushes.
- Public and retained executor/invalidation matrix:

  `pytest -q tests/test_integration_candidates.py::test_public_build_takes_over_expired_prepush_claim_once tests/test_integration_candidates.py::test_public_build_observer_fails_closed_for_nonrecoverable_claims tests/test_integration_candidates.py::test_public_build_reconciles_desired_remote_without_repush tests/test_integration_candidates.py::test_mutation_claim_executor_takeover_is_exclusive tests/test_integration_candidates.py::test_lost_force_with_lease_response_reconciles_without_second_push tests/test_integration_candidates.py::test_expired_ambiguous_claim_blocks_every_invalidator tests/test_integration_candidates.py::test_invalidator_commit_fences_later_mutation_reservation -x`

  Result: `21 passed, 3 warnings in 14.95s`.
- Final candidate file after the external branch merge:

  `pytest -q tests/test_integration_candidates.py -x`

  Result: `62 passed, 3 warnings in 47.11s`.

## Final verification

The final affected-area gate ran after both the self-review strengthening and
external merge `80034ced`:

`aq test tests/test_integration_candidates.py tests/test_integration_repair.py tests/test_integration_ownership.py`

Result: `111 passed, 11 warnings in 51.07s`, using aq slot 1 and three workers.

`ruff check src/integration/candidates.py tests/test_integration_candidates.py`

Result: `All checks passed!`.

`/usr/bin/python3.12 -m py_compile src/integration/candidates.py tests/test_integration_candidates.py`

Result: exit 0. `git diff --check` also completed with exit 0.

`/usr/bin/python3.12 -m alembic heads` returned exactly
`46f910d0dce6 (head)`. No schema or migration changed in this round.

`aq prime` was invoked once as required and returned the expected delegated
session diagnostic: `task_id is required (no task in scope)`. No queue mutation
was attempted.

## Changed files

- `src/integration/candidates.py`
- `tests/test_integration_candidates.py`

No migration, public API, generated client, repair/ownership runtime, or other
schema surface changed.

## Self-review and scope

- Observation performs authenticated reads outside database transactions. The
  exact eligibility reread is local and hierarchy-locked; no token, Git, forge,
  or other network await was moved under a database transaction.
- The observer's recoverable disposition is not authority. The existing
  exclusive takeover and pre-push checks remain the only path to a mutation
  nonce and external push.
- Phase matching prevents a later final mutation, an earlier partial mutation,
  another revision, or another operation episode/stage from being skipped.
- Current lease duration is reread in the eligibility transaction rather than
  trusted from the earlier state snapshot. Any later change is revalidated by
  takeover and pre-push fencing.
- Unexpected and ambiguous claims are neither deleted nor overwritten and
  continue blocking repair expiry, branch transfer, and rebuild.
- No Task9b2 promotion, main mutation, workflow/playbook, enablement, live forge
  call, operator database action, queue mutation, or schema change was added.

Residual concern: the pre-existing SQLite-to-PostgreSQL copy/cutover omission
for hierarchical integration tables remains Task12 scope and is unchanged.
