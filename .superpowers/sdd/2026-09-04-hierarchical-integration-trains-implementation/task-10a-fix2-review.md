# Task 10a fix round 2 re-review

## Finding Verdict

1. **Post-promotion triggers are coalesced without recording the required first catch-up provenance when cleanup finishes before release.** — **ADDRESSED.** Catch-up eligibility now accepts an exact matching `promoted` batch without consulting its independent cleanup state, while still requiring `project_id` and `request_id == outstanding_request_id` (`src/integration/scheduler.py:100-138`). Once release clears/converts the outstanding request, this query is no longer reached for the old request, so a later cleanup transition cannot revive its authority. The focused tests cover manual-first and periodic-first provenance across `pending`, `complete`, and `conflict`, retain the first tuple and single outbox event, and separately prove both cleanup-after-release terminal orderings create a fresh sequence-2 request (`tests/test_integration_schedule.py:126-179`, `tests/test_integration_schedule.py:182-242`).

## New Breakage in the Fix Diff

- None.

## Out-of-Scope Observations

- The previously approved service cursor and failure-resilience implementation was untouched and was not reopened.
- The three reported warnings remain the already identified inherited dependency deprecations; no new warning or suppression was introduced.

## Spec and Quality Verdicts

- **Spec compliance: PASS — Critical 0, Important 0, Minor 0.** Catch-up lifetime is now bound to the exact outstanding request and is independent of cleanup ordering.
- **Code quality: Approved.** The fix removes the incorrect cross-state predicate, retains the exact project/request fence, and adds behavior-based coverage for both event orders and all current cleanup outcomes.

## Checks

- Read the prior re-review, Task 10a brief, appended fix report, and supplied `review-f7e0594d..b66fd373.diff` once. Inspected only the changed scheduler/test lines needed for exact references.
- Did not rerun the reported 15 focused tests; the diff directly resolves the sole concern and the evidence covers the relevant state matrix.

## Verdict

**Fix round: All findings addressed, no new Critical/Important breakage.**
