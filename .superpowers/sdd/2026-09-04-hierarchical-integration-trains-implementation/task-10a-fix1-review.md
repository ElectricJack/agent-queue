# Task 10a fix round 1 re-review

## Finding Verdicts

1. **Post-promotion triggers are coalesced without recording the required first catch-up provenance.** — **NOT ADDRESSED (Important).** The fix adds `promoted` only when `cleanup_state == 'pending'` (`src/integration/scheduler.py:100-143`). Cleanup completion is independent of release and may occur first; while the old request still remains outstanding, a trigger against `lifecycle='promoted', cleanup_state='complete'` is therefore returned as `coalesced` without capturing the catch-up tuple. The new test covers only `cleanup_state='pending'` (`tests/test_integration_schedule.py:126-178`) and cannot detect the allowed cleanup-first ordering. Root outstanding-request ownership, not cleanup state, is the release boundary: treat the exact promoted batch matching that outstanding request as eligible until release atomically clears/converts the request, and test both cleanup-before-release and release-before-cleanup orderings.

2. **Every service tick resets every selector cursor to `None`, so persistent rows beyond the first page starve indefinitely.** — **ADDRESSED.** Each source now owns an independent cursor; `_page` passes it to the selector, advances from the last scanned row before any handler runs, and wraps only after exhaustion (`src/integration/service.py:50-55`, `src/integration/service.py:73-141`). The production `tick` uses these pages for all four sources (`src/integration/service.py:57-123`), and the new multi-tick test proves more persistent rows than `page_size` are eventually scanned for both missing and declining handlers (`tests/test_integration_service.py:584-640`).

3. **A transient selector exception permanently terminates the sole background loop and is re-raised during shutdown.** — **ADDRESSED.** Each source, including outbox dispatch, now has a cancellation-preserving exception boundary so later sources continue (`src/integration/service.py:57-71`, `src/integration/service.py:143-151`), and the background loop logs/retries any remaining non-cancellation tick failure (`src/integration/service.py:192-205`). The regression test exercises a one-shot selector failure, same-tick later-source/outbox progress, a subsequent healthy tick, and clean `stop()` (`tests/test_integration_service.py:643-692`).

## New Breakage in the Fix Diff

- None beyond the still-open first finding.

## Out-of-Scope Observations

- The previously carried Task 12 receipt-test and true fresh-empty migration-chain debts are unchanged by this four-file runtime/test fix.
- The prior warning-evidence Minor is resolved for this round: the appended report identifies the 11 warnings as inherited `pkg_resources`, namespace-package, and `audioop` deprecations (`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-report.md:333-341`).

## Spec and Quality Verdicts

- **Spec compliance: FAIL — Critical 0, Important 1, Minor 0.** Catch-up remains lossy in the allowed cleanup-first/release-later ordering.
- **Code quality: Needs fixes.** The cursor and failure-resilience changes are clean, source-local, cancellation-safe, and well tested; the scheduler predicate still couples request lifetime to the wrong independent state machine.

## Checks

- Read the prior review, complete Task 10a brief, appended fix report, and supplied `review-f29ea277..82bd825a.diff` once. Inspected only the current changed functions/tests needed for precise line references.
- Did not rerun the reported 141-test gate; direct inspection resolves the remaining ordering defect and the other two findings' coverage.

## Verdict

**Fix round: Findings remain open** — finding 1 remains Important; findings 2 and 3 are addressed with no new Critical/Important breakage.
