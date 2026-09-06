# Task 9b2 fix 1a round 2 report

## Identity and scope

- Fix base: `f421e2e899a6de6668bb60f51cc5e42f1c618b8c`.
- Runtime/tests commit: `3573ee908be478ce3def609c2f63f30784069791`.
- Read `task-9b2-fix-1a-review.md` and the binding
  `task-9b2-fix-1a-round2-brief.md` completely before editing.
- Scope is limited to the remaining Critical authority-horizon ordering issue in
  root-main promotion. The accepted purpose isolation, moved-main rebuild,
  descendant import, and attestation contracts are preserved.
- No fix 1b receipt/member schema, finalizer completeness, SQLite trigger, event,
  Task 10/11, live network, credential, forge, operator database, or project
  enablement work was performed.
- No schema or migration changed; migration-cycle testing is therefore not
  applicable.

## Correction

- On the expected-old write path, root reconciliation now finishes the retained
  store's local fast-forward proof and acquires the installation token before
  attempting the final hierarchy-locked prewrite CAS. Both operations remain
  outside database transactions.
- `_mark_prewrite` acquires the hierarchy project lock, loads the exact current
  snapshot, then measures one absolute deadline of current time plus the
  120-second Task 9a push timeout and five-second margin. That same deadline is
  used to validate the current project lease and exclusive root-main claim.
- The locked recheck still binds the current batch/revision, active root repair
  operation and stage, immutable intent lease/fence authority, exact frozen
  attestation proof, claim purpose/state/nonce, and null prewrite marker.
- A lost authority or insufficient lease/claim horizon returns `False` from the
  CAS helper. Public reconciliation returns typed `wait`, performs zero pushes,
  and leaves the reservation unmarked so a later exact retry/takeover is safe.
- Only the crash-injection boundary and Task 9a authenticated expected-old push
  remain between a successful marker and possible remote mutation. Task 9a
  continues to own mutable credential-buffer zeroization and subprocess/broker
  cleanup for every push result or cancellation.
- When the final CAS fails, Task 9a transport is never entered, so it allocates no
  mutable credential buffer, broker, subprocess, or inherited FD. The token is
  neither persisted nor logged; the App client's existing bounded token cache
  remains the sole intended owner of the returned immutable token string.

## TDD evidence

### Ancestry and cache-miss refresh consume authority

The regression uses a deterministic clock. Local ancestry advances it from 10
to 15, and the fake cache-miss installation-token refresh advances it to 21.
The test isolates both possible limiting authorities: the original claim with a
long lease, and an exact lease with a long claim.

- RED:
  `pytest -q tests/test_integration_main_promotion.py -k 'prewrite_rechecks_horizon_after_ancestry_and_token_refresh or prewrite_accepts_exact_post_refresh_push_horizon' -x`
  returned **1 failed, 32 deselected, 3 warnings in 1.23s**. The claim-limited
  case promoted instead of waiting because prewrite had already been recorded
  before ancestry/token work.
- The positive exact-boundary node was also run independently against the old
  ordering:
  `pytest -q tests/test_integration_main_promotion.py::test_prewrite_accepts_exact_post_refresh_push_horizon`
  returned **1 failed, 3 warnings in 1.21s**. The push succeeded, but
  `prewrite_at` was 10 rather than the required post-refresh time 20.
- GREEN after moving ancestry/token before the CAS:
  `pytest -q tests/test_integration_main_promotion.py -k 'prewrite_rechecks_horizon_after_ancestry_and_token_refresh or prewrite_accepts_exact_post_refresh_push_horizon'`
  returned **3 passed, 32 deselected, 3 warnings in 1.54s**.

The two insufficient cases prove `wait`, zero push, one completed token request,
reserved claim, null prewrite marker, and no dummy token in durable intent/claim
state. The positive case sets both the claim and lease to expire exactly 125
seconds after the final timestamp, records `prewrite_at=20`, and pushes exactly
once.

### Lock-wait horizon measurement

Self-review after the first area gate found that the first implementation
measured the absolute horizon just before acquiring the hierarchy lock. A
contended lock could therefore consume claim time. A new deterministic test
advances time from 20 to 21 as the final hierarchy lock is acquired.

- RED:
  `pytest -q tests/test_integration_main_promotion.py::test_prewrite_horizon_is_measured_after_hierarchy_lock`
  returned **1 failed, 3 warnings in 1.32s**; it promoted instead of waiting.
- GREEN after moving the single absolute deadline measurement inside the acquired
  lock:
  the same command returned **1 passed, 3 warnings in 1.14s**.
- Final focused boundary:
  `pytest -q tests/test_integration_main_promotion.py -k 'prewrite_rechecks_horizon_after_ancestry_and_token_refresh or prewrite_accepts_exact_post_refresh_push_horizon or prewrite_horizon_is_measured_after_hierarchy_lock or expired_inflight_prewrite_is_blocked_until_remote_proves_result or attestation_is_frozen_and_revalidated_before_prewrite or owned_unmarked_claim_renews_full_horizon_before_prewrite or exact_tested_sha_main_push_finalizes_every_member_without_post_ci'`
  returned **8 passed, 28 deselected, 3 warnings in 2.82s**.

This focused gate retains the existing marked-ambiguity behavior, attestation
revalidation, owned-claim renewal, normal exact push, both insufficient-horizon
cases, exact positive boundary, and lock-delay measurement.

## Affected-area and static verification

- Before the lock-wait self-review improvement, the prescribed area command
  returned **241 passed, 11 warnings in 52.24s**. Because production changed
  afterward, that run is not used as final completion evidence.
- Fresh final affected-area gate:
  `aq test tests/test_integration_main_promotion.py tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_integration_ownership.py tests/test_integration_repair.py -x`
  used aq slot 1/2 and three workers, returning
  **242 passed, 11 warnings in 53.82s**.
- Changed-file Ruff:
  `ruff check src/integration/main_promotion.py tests/test_integration_main_promotion.py`
  returned `All checks passed!`.
- Compile:
  `python3.12 -m py_compile src/integration/main_promotion.py tests/test_integration_main_promotion.py`
  completed with exit 0 and no output.
- `python3.12 -m alembic heads` returned `d4a81f0c9e72 (head)`.
- `git diff --check` completed with exit 0 and no output.

## Files and self-review

- `src/integration/main_promotion.py`: pre-CAS ancestry/token ordering, typed
  prewrite CAS failure, exact post-lock horizon measurement, and absolute lease
  deadline validation.
- `tests/test_integration_main_promotion.py`: deterministic ancestry, token
  refresh, claim-limited, lease-limited, exact-boundary, and hierarchy-lock-delay
  regressions.
- Verified there is no provider call, token acquisition, Git operation, or push
  inside the hierarchy transaction. After the marker, no local proof or token
  refresh remains before push.
- Verified both claim and lease compare against the same absolute final deadline;
  equality is accepted and one-second insufficiency fails closed.
- Verified marked claims still block without takeover or repeat push, while a
  failed unmarked final CAS remains retryable.
- Existing `pkg_resources`, namespace-package, and `audioop` dependency warnings
  remain; no unrelated suppression or cleanup was added.
- Fix 1b remains independently responsible for the previously accepted receipt
  schema/finalizer/SQLite-trigger issues.

## Commits

- Runtime/tests: `3573ee908be478ce3def609c2f63f30784069791`.
- Report: recorded by the following documentation commit.
