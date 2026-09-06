# Task 9b2 fix 1a round 4 report

## Identity and scope

- Assigned base: `b83b1f430ffdbc5feef2644821736c41d268998c`.
- Before the runtime commit, the shared branch advanced through unrelated documentation commits
  `38c58e18` and `ffef13b3`; both were preserved. Runtime review base is therefore `ffef13b3`.
- Runtime/tests commit: `661fde08`.
- Read the binding round-4 brief, round-3 review, and round-3 report completely before editing.
- Scope is limited to `src/git/manager.py`, `src/integration/main_promotion.py`, and focused tests
  for the absolute root-main authority deadline across prewrite commit and Git dispatch.
- No schema, migration, Task 9b2 fix 1b, Task 10 runtime, live network, forge/operator mutation,
  new workspace, push, or PR work was performed.
- Repository-required `aq prime` was attempted once and returned:
  `Error: Command 'prime' failed: task_id is required (no task in scope ...)`. No queue mutation or
  retry was attempted.

## Correction

- `RootPromotionService._mark_prewrite` now samples the event loop monotonic clock immediately
  before its wall-clock authority sample, after hierarchy locking and exact snapshot loading.
- A successful one-row prewrite CAS returns the absolute monotonic transport deadline established
  by that sample. The transaction commit occurs before the value reaches the caller, so commit,
  crash-hook, scheduling, and dispatch latency consume rather than reset its budget.
- Root reconciliation passes the exact deadline to `GitManager.apush_oid_with_app_auth`; failure
  after the immutable prewrite marker retains the existing ambiguous authenticated-reconciliation
  behavior.
- The App-auth push accepts one optional daemon-internal `authority_deadline: float | None`. It
  rejects booleans and nonfinite values, rejects an already exhausted deadline before entering the
  transport, and caps any later supplied value at the existing 120-second entry maximum. Callers
  that omit it, including candidate and repair publication, retain the existing capped default.
- No command contract or caller-controlled timeout was added. No provider/Git await moved under a
  database transaction. The existing five-second cleanup margin remains separately reserved by
  the locked lease/claim horizon.

## TDD evidence

### Public tightening-only deadline

RED before production:

```text
pytest -q tests/test_git_app_auth.py::test_app_push_authority_deadline_can_only_tighten_public_transport_budget
FAILED ... TypeError: GitManager.apush_oid_with_app_auth() got an unexpected keyword argument
'authority_deadline'
1 failed, 2 warnings in 0.38s
```

The first GREEN run exposed a test-clock comparison that ignored call-entry time by four
microseconds (`1 failed`); the assertion was corrected to compare against the post-call loop sample,
without weakening the maximum-duration contract. Final GREEN:

```text
pytest -q tests/test_git_app_auth.py::test_app_push_authority_deadline_can_only_tighten_public_transport_budget
1 passed, 2 warnings in 0.37s
```

Additional finite/exhausted contract gate:

```text
pytest -q tests/test_git_app_auth.py -k 'authority_deadline' -x
5 passed, 21 deselected, 2 warnings in 0.36s
```

This covers inherited deadline propagation, cap-at-entry, NaN/positive infinity/negative infinity,
and an expired deadline that never enters the isolated transport.

### Root prewrite handoff and real Git boundary

RED before root production change:

```text
pytest -q tests/test_integration_main_promotion.py::test_prewrite_accepts_exact_post_refresh_push_horizon
FAILED ... KeyError: 'authority_deadline'
1 failed, 3 warnings in 1.41s
```

GREEN after returning/passing the prewrite monotonic deadline:

```text
pytest -q tests/test_integration_main_promotion.py::test_prewrite_accepts_exact_post_refresh_push_horizon
1 passed, 3 warnings in 1.20s
```

Production root-to-Git boundary gate:

```text
pytest -q tests/test_integration_main_promotion.py -k 'prewrite_deadline_consumes or exhausted_prewrite_deadline' -x
2 passed, 36 deselected, 3 warnings in 1.90s
```

The positive case uses the root service, a 100 ms post-marker dispatch delay, the public App-auth
transport, real isolated Git preparation, and a real local bare remote. It proves only the remaining
portion of the original 400 ms deadline reaches the transport and the exact tip lands. The negative
case exhausts the original 80 ms deadline before dispatch, observes zero remote push children, keeps
the remote at expected-old, and retains the durable `reserved` claim with non-null `prewrite_at`.

## Focused and final verification

```text
pytest -q tests/test_git_app_auth.py -x
26 passed, 2 warnings in 2.32s

pytest -q tests/test_integration_main_promotion.py -x
38 passed, 3 warnings in 9.18s

aq test tests/test_git_app_auth.py tests/test_git_manager_async.py tests/test_integration_main_promotion.py tests/test_integration_promotion.py
aq test: slot 1 of 2, -n 3
277 passed, 11 warnings in 31.38s

ruff check src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py tests/test_integration_main_promotion.py
All checks passed!

python3.12 -m py_compile src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py tests/test_integration_main_promotion.py
exit 0, no output

git diff --check -- src/git/manager.py src/integration/main_promotion.py tests/test_git_app_auth.py tests/test_integration_main_promotion.py
exit 0, no output
```

Warnings are the pre-existing `pkg_resources`, namespace-package, and `audioop` deprecations.

## Files and self-review

- `src/git/manager.py`: optional finite tightening-only authority deadline at the existing public
  daemon App-auth push boundary.
- `src/integration/main_promotion.py`: prewrite monotonic sample/return and exact handoff to Git.
- `tests/test_git_app_auth.py`: public cap, invalid-value, and expired-before-transport tests.
- `tests/test_integration_main_promotion.py`: root handoff assertion plus partial/exhausted real-Git
  delayed-dispatch coverage.
- Verified the absolute deadline is produced only after exact locked validation and a successful
  prewrite CAS, while transaction exit/commit occurs before external dispatch.
- Verified no late deadline can extend the transport's existing default.
- Verified timeout after prewrite does not clear or supersede the irreversible ambiguity marker.
- Verified normal promotion, concurrency/replay, cancellation, process-group cleanup, broker
  settlement, candidate/repair promotion compatibility, and the prior authority checks remain in
  the 277-test area gate.
- No concern remains within round-4 scope. Task 9b2 fix 1b remains an independent dependency.
