# Task 9b2 fix 1a round 3 report

## Identity and scope

- Fix base: `177a02b009f404475b92ec4f13e25d67bf2e1c5d`.
- Runtime/tests commit: `2520efbc4c287c8a7a2d69fe3eba748dbe15cf7a`.
- Read `task-9b2-fix-1a-round2-review.md` and the binding
  `task-9b2-fix-1a-round3-brief.md` completely before editing.
- Scope is limited to the remaining aggregate Task 9a App-auth push deadline
  and RootPromotionService authority-horizon binding.
- No fix 1b schema, receipt/finalizer, trigger, event, Task 10/11, live network,
  credential, forge, operator-database, or project-enablement work was performed.
- No schema or migration changed; migration-cycle testing is not applicable.

## Correction and public contract

- `src.git.manager.APP_AUTH_PUSH_TIMEOUT_SECONDS` is the exported authoritative
  120-second aggregate App-auth push budget.
- `src.git.manager.APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS` is the exported
  five-second bounded process-group termination margin. Root promotion imports
  both values rather than duplicating literals.
- `GitManager.apush_oid_with_app_auth` establishes one absolute monotonic
  deadline at public entry. Input validation, isolated bare-repository setup,
  exact object import, `rev-parse`, `git --exec-path`, pinned topology
  validation, broker setup/settlement, remote child execution, and normal
  termination share that one remaining budget.
- The internal file-URL-only containment seam accepts an internal deadline for
  the public method, but there is no caller-controlled timeout parameter on the
  public API. Direct containment tests receive the same aggregate default.
- Credential-free local Git children run in isolated process groups under the
  aggregate deadline. On timeout/cancellation/error, they are killed and reaped;
  the remote child is not created if preparation has exhausted the budget.
- Once the remote child exists, timeout or cancellation retains the reviewed
  process-group kill/reap, broker cancellation, request-FD closure, and mutable
  token-buffer zeroization behavior. Termination is bounded by the exported
  cleanup margin so the root lease/claim horizon covers aggregate work plus
  cleanup.
- RootPromotionService's final hierarchy-locked prewrite CAS now requires both
  the project lease and exclusive claim to remain valid through the exported
  aggregate timeout plus cleanup margin. The prewrite remains immutable and is
  not cleared after any later preparation/push failure; reconciliation remains
  mandatory because that state is conservatively ambiguous.
- Existing candidate and repair callers retain their public call shape and gain
  the aggregate transport deadline without new timeout authority.

## TDD evidence

### Preparation exhaustion prevents remote start

The production-boundary regression uses the public
`GitManager.apush_oid_with_app_auth`, real isolated local Git preparation,
instrumented cumulative preparation delay, a dummy credential, and a local
sentinel executable at the remote-child boundary.

- RED against the per-step timeout implementation:
  `pytest -q tests/test_git_app_auth.py::test_app_push_aggregate_budget_exhaustion_during_prep_never_starts_remote`
  returned **1 failed, 2 warnings in 0.59s**. The assertion
  `not remote_started.exists()` failed, proving that cumulative local preparation
  could consume the declared authority horizon and still start the remote child.
- The positive partial-consumption characterization was also run before the
  implementation change:
  `pytest -q tests/test_git_app_auth.py::test_app_push_partial_prep_leaves_one_remaining_budget_for_remote`
  returned **1 passed, 2 warnings in 0.52s**.
- The first GREEN attempt exposed one implementation-wiring mistake: `_deadline`
  was initially placed on the public signature instead of the private containment
  seam, producing **2 failed** nodes (`TypeError` and `UnboundLocalError`). No
  protocol or test expectation was weakened; the parameter placement was fixed.
- GREEN for both production-boundary nodes:
  `pytest -q tests/test_git_app_auth.py -k 'aggregate_budget or partial_prep'`
  returned **2 passed, 2 warnings in 0.71s**.

The exhaustion case additionally proves bounded completion, no remote sentinel,
one zeroized mutable buffer, stable open-FD count, and no surviving broker task.
The positive case imports and pushes the exact tested SHA through real local Git,
counts exactly one remote push child, and completes inside the original aggregate
deadline after local preparation has consumed part of it.

### Cleanup/cancellation retention

- Focused aggregate and lifecycle gate:
  `pytest -q tests/test_git_app_auth.py -k 'aggregate_budget or partial_prep or source_import_failure or cancellation_during_source_import or app_push_timeout_or_cancellation or app_push_spawn_failure or broker_timeout'`
  returned **8 passed, 13 deselected, 2 warnings in 1.36s**.
- Full focused Git-auth file:
  `pytest -q tests/test_git_app_auth.py -x`
  returned **21 passed, 2 warnings in 2.20s**.
- Full focused root-promotion file:
  `pytest -q tests/test_integration_main_promotion.py -x`
  returned **36 passed, 3 warnings in 7.73s**.

These retain source-import failure and cancellation zeroization, remote timeout
and cancellation process-group cleanup, spawn failure, broker timeout, root
post-refresh horizon measurement, marked ambiguity, exact attestation, and normal
one-write behavior.

## Final affected-area and static verification

- Final affected-area gate:
  `aq test tests/test_git_app_auth.py tests/test_git_manager_async.py tests/test_integration_main_promotion.py tests/test_integration_promotion.py`
  acquired aq slot 1/2, used three workers, and returned
  **270 passed, 11 warnings in 29.12s**.
- Changed-file Ruff:
  `ruff check src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py`
  returned `All checks passed!`.
- Compile:
  `python3.12 -m py_compile src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py`
  completed with exit 0 and no output.
- Read-only migration-head check:
  `python3.12 -m alembic heads`
  returned `d4a81f0c9e72 (head)`.
- Scoped whitespace check:
  `git diff --check -- src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py`
  completed with exit 0 and no output.

## Files and self-review

- `src/git/manager.py`: exported aggregate and cleanup contract, one public-entry
  monotonic deadline, deadline-aware isolated preparation/topology, remaining
  remote/broker budget, and bounded process-group termination.
- `src/integration/main_promotion.py`: derives final locked prewrite authority
  horizon from the Git transport's exported timeout and cleanup margin.
- `tests/test_git_app_auth.py`: real-Git cumulative-exhaustion and
  partial-consumption regressions plus adjusted lifecycle fixtures for the
  aggregate contract.
- Verified the remote child is created only after a positive remaining-budget
  check and inside the same absolute timeout boundary as all preceding prep.
- Verified no network/provider operation was moved under a database lock.
- Verified root failure after the prewrite marker still leaves the mutation
  ambiguous for authenticated reconciliation rather than clearing or repushing.
- Verified no token is added to argv, environment, config, logs, or durable state;
  existing secret-containment tests remain in the final area gate.
- Verified public callers cannot choose or reset the aggregate timeout.
- Existing `pkg_resources`, namespace-package, and `audioop` dependency warnings
  remain; no unrelated warning suppression was added.
- Fix 1b remains independently responsible for receipt schema/finalizer/trigger
  and event findings.

## Commits

- Runtime/tests: `2520efbc4c287c8a7a2d69fe3eba748dbe15cf7a`.
- Report: recorded by the following documentation commit.
