# Task 9b2 fix 1a round 3 scoped re-review

## Finding Verdict

1. **The root-main authority horizon must cover one aggregate Task9a App-auth push deadline, including preparation and bounded cleanup.** — **NOT ADDRESSED.** The transport half is corrected: the public production method establishes one monotonic deadline and passes it through isolated initialization, exact-object import, verification, topology pinning, broker setup, and the remote child (`src/git/manager.py:2891-2918`, `src/git/manager.py:3161-3223`, `src/git/manager.py:3225-3413`). Each local child and the remote child uses the same deadline, preparation exhaustion is checked before remote spawn, and process-group cleanup has a separate exported five-second bound (`src/git/manager.py:3106-3137`, `src/git/manager.py:3273-3304`, `src/git/manager.py:3359-3408`). However, root authority and transport do not share the same absolute deadline. `_mark_prewrite` measures the lease/claim minimum before its transaction commits and requires exactly `120 + 5` seconds from that timestamp (`src/integration/main_promotion.py:539-582`). Only after the commit returns (and the crash-hook boundary) does `apush_oid_with_app_auth` establish a new full 120-second deadline (`src/integration/main_promotion.py:447-462`, `src/git/manager.py:2901-2918`). Database commit latency, event-loop scheduling, and call-entry latency therefore reset rather than consume the reserved budget; after any positive delay, the remote child plus allowed cleanup can outlive the exact lease/claim horizon. This still violates the requirement that current authority remain valid through the actual write. Return an opaque monotonic transport deadline from the successful prewrite transaction (computed alongside the persisted wall-clock minimum) and pass that exact deadline to a non-caller-configurable privileged push entry, or revalidate the remaining authority immediately before transport entry and shorten the aggregate transport deadline by all elapsed time. Add a deterministic delayed-commit/pre-entry regression proving the production remote child cannot run beyond the original prewrite authority deadline. The new Git tests start their deadline at transport entry and the root tests use an immediate fake push, so neither covers this boundary (`tests/test_git_app_auth.py:297-383`).

## New Breakage in the Fix Diff

- None beyond the still-open DB-to-transport deadline handoff. The aggregate transport now prevents remote start after cumulative preparation exhausts its own deadline, keeps the prewrite marker immutable on failure, and retains token/FD/broker/process-group cleanup.

## Out-of-Scope Observations

- Fix 1b schema, finalizer, receipt-trigger, and event work was not reviewed.

## Checks

- Read the round-three brief/report, prior round-two review, and the supplied `review-177a02b0..cd8fe1fc.diff` package. Inspected the current transport and root prewrite functions only to resolve the named deadline, cancellation, cleanup, and ambiguity risks.
- Cancellation propagates through the entered transport's `BaseException` paths after process-group and broker cleanup, while the credential buffer is zeroized by its enclosing context (`src/git/manager.py:3261-3267`, `src/git/manager.py:3389-3408`). No new secret/FD leak was found.
- Did not rerun the reported suites. The report records 21 Git-auth tests, 36 root-promotion tests, and a 270-test affected-area gate passing with existing warnings; the new production-boundary tests were inspected and do not exercise elapsed time between the prewrite timestamp and public transport entry.

## Verdict

- **Spec verdict: FAIL — Critical 1, Important 0.** The transport has one aggregate deadline, but that deadline is freshly reset after the prewrite authority timestamp and transaction commit.
- **Quality verdict: Needs fixes.** The transport implementation and cleanup coverage are materially improved, but the safety contract remains incomplete at the database-to-Git boundary.
