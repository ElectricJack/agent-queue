# Task 9b2 fix 1a round 2 scoped re-review

## Finding Verdicts

1. **Prewrite authority can expire during local Git/token work before the actual bounded main push.** — **NOT ADDRESSED.** The direct caller now completes its local ancestry check and installation-token acquisition before `_mark_prewrite`, and `_mark_prewrite` measures one lease/claim deadline after acquiring the hierarchy lock while revalidating the exact snapshot, intent authority, attestation, executor nonce, and null marker (`src/integration/main_promotion.py:423-459`, `src/integration/main_promotion.py:536-575`). However, the only method called after the marker is not a single 120-second remote operation. `GitManager.apush_oid_with_app_auth()` enters `_apush_oid_with_app_auth_to_url()`, which, after receiving the token, creates an isolated repository and runs `git init`, a local exact-object fetch, `rev-parse`, and `git --exec-path` before it even creates the remote-push process (`src/git/manager.py:3188-3250`, `src/git/manager.py:3302-3329`). Each preparation command independently receives the full 120-second timeout (`src/git/manager.py:3139-3165`), and `_app_git_credential_topology()` adds another such command (`src/git/manager.py:3167-3177`). Only the eventual remote child is then bounded by a fresh 120-second timeout (`src/git/manager.py:3329`). Consequently, the project lease and claim can expire hundreds of seconds before the authenticated main write starts even though `_mark_prewrite` required only `120 + 5` seconds (`src/integration/main_promotion.py:39-41`, `src/integration/main_promotion.py:547-568`). This still violates the binding requirement that only the bounded push follow the marker and that exact authority remain valid through the actual write. Stage and verify the isolated exact graph and pin credential topology before the final prewrite transaction, then expose a prepared push whose only remaining operation is the bounded remote mutation; alternatively enforce one aggregate deadline across the complete Task9a method and size the authority horizon to that aggregate (while accepting that prewrite makes any preparation failure ambiguous). Add a behavior test using the production GitManager boundary that consumes time in post-marker isolated preparation and proves the remote push cannot begin after the lease/claim deadline. The new clock tests replace only the service's `apush_oid_with_app_auth` with an immediate fake, so they do not cover this hidden production sequence (`tests/test_integration_main_promotion.py:302-322`, `tests/test_integration_main_promotion.py:692-788`).

## New Breakage in the Fix Diff

- None beyond the still-open aggregate Task9a transport-horizon defect above. The new false-return path leaves `prewrite_at` null and makes zero transport calls when current authority is insufficient; marked-claim ambiguity remains unchanged.

## Out-of-Scope Observations

- Fix 1b's root-member/finalizer structural binding and SQLite receipt-trigger work was not reviewed.

## Checks

- Read the prior fix1a review, round-two brief/report, and exact `f421e2e8..3573ee90` runtime/test diff. Inspected the unchanged Task9a push implementation only for the explicitly named post-marker bounded-work and cleanup risk.
- Cancellation inside the entered Task9a transport continues to propagate through its existing `BaseException` cleanup and mutable token-buffer zeroization (`src/git/manager.py:3212-3218`, `src/git/manager.py:3333-3346`). A failed prewrite never enters Task9a or creates its mutable buffer. No new token persistence/logging path was found.
- Did not rerun the reported suites. The report records 242 affected-area tests passing with 11 existing warnings, and the added service-level horizon tests were inspected rather than rerun.

## Verdict

**Fix round: Findings remain open — Critical 1, Important 0.** The caller-side ancestry/token ordering and post-lock timestamp are correct, but the nominal 120-second Task9a call contains multiple independently bounded preparation commands before the actual remote write, so the claimed 125-second authority horizon remains insufficient. Task quality remains **Needs fixes** within fix1a scope.
