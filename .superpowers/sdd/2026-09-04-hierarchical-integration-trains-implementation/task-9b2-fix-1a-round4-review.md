# Task 9b2 fix 1a round 4 scoped re-review

## Finding Verdict

1. **The exact locked root-main authority deadline must survive prewrite commit and Git dispatch without resetting the transport budget.** — **ADDRESSED.** `_mark_prewrite` samples monotonic time inside the hierarchy-locked transaction before the wall-clock authority sample, validates the exact snapshot/intent/proof/executor nonce and `120 + 5` second lease/claim horizon, and returns the corresponding monotonic push deadline only after the one-row prewrite CAS commits (`src/integration/main_promotion.py:544-593`). Reconcile carries that exact deadline across the post-marker boundary into the production App-auth push (`src/integration/main_promotion.py:447-467`). `GitManager.apush_oid_with_app_auth` rejects invalid or exhausted values and caps any supplied deadline to the existing entry-time maximum, so the internal parameter can tighten but never extend transport authority (`src/git/manager.py:2892-2935`). The same deadline continues through all local preparation and the remote child; if commit/dispatch/preparation consumes it, no remote starts. If a remote child starts while budget remains, it may run only until that inherited push deadline, after which process-group termination/reaping uses the separately reserved five-second cleanup margin (`src/git/manager.py:3106-3137`, `src/git/manager.py:3161-3240`, `src/git/manager.py:3273-3429`). Thus the fix covers both cases precisely: expiry before remote start prevents the start, while expiry after a valid start terminates the in-flight child and leaves the immutable prewrite marker for authenticated reconciliation.

## New Breakage in the Fix Diff

- None. Candidate and repair callers that omit the internal deadline retain the bounded default; no command/API timeout parameter was added. Prewrite failure still leaves the marker null, while post-marker exhaustion leaves it non-null and ambiguous as required.

## Out-of-Scope Observations

- Fix 1b schema, finalizer, receipt-trigger, and event work was not reviewed.

## Checks

- Read the round-four brief/report, prior round-three review, and supplied `review-ffef13b3..f3facfb9.diff` package. Inspected current root prewrite/dispatch and App-auth transport code only for the named deadline and cleanup boundaries.
- The production-boundary tests use the root service, real isolated Git preparation, delayed post-marker dispatch, and a local bare remote. They prove partial delay consumes the inherited deadline and full exhaustion starts zero remote push children while retaining the marked claim (`tests/test_integration_main_promotion.py:862-953`). Public transport tests cover tightening-only, late-deadline capping, nonfinite values, and already-expired rejection (`tests/test_git_app_auth.py:203-280`).
- Did not rerun the reported suites. The report records 26 Git-auth tests, 38 root-promotion tests, and the 277-test affected-area gate passing with existing dependency warnings.

## Verdict

- **Spec verdict: PASS — Critical 0, Important 0.** The sole deadline-handoff finding is addressed with one inherited, tightening-only absolute budget and reserved bounded cleanup.
- **Quality verdict: Ready for fix 1b.** The implementation is narrowly scoped, the authority/transport ownership boundary is explicit, and the behavior tests exercise the real production orchestration rather than only a fake push method.
