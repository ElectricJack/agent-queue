# Task 10a implementation review

## Spec Compliance

- ❌ **Issues found — Critical 0, Important 3, Minor 1.** The migration, frozen policy defaults, bounded selectors, direct durable outbox acceptance, and orchestrator lifecycle wiring are substantially in scope, but the scheduler drops catch-up provenance during the reviewed promoted/pending-cleanup phase and the service does not provide the required fair, failure-resilient scan lifecycle.

## Strengths

- The four selectors validate positive limits and use complete stable keysets (`src/database/queries/integration_reconciliation_queries.py:20-176`); the repair compatibility wrapper now delegates to the bounded repair-stage page (`src/integration/repair.py:839-854`).
- `IntegrationCleanupPolicy` is frozen/closed, validates ordered retry bounds, and preserves the exact compatibility defaults; the enclosing frozen policy also defaults `on_main_moved` to `rebuild` (`src/integration/models.py:114-145`). Newly sealed batches serialize the resolved model rather than rereading mutable policy later (`src/integration/scheduler.py:316-370`).
- The service uses the existing scheduler, repair, and outbox authorities and does no provider/Git work. Orchestrator wiring directly invokes durable `V2PlaybookRuntime.accept_integration_event`, starts one named loop after playbook construction, and stops it before database shutdown (`src/orchestrator/core.py:1454-1479`, `src/orchestrator/core.py:2158-2164`).
- The generated migration adds an all-or-none catch-up tuple, preserves legacy rows as NULL, refuses downgrade while any tuple component is live, and otherwise leaves outstanding request identity untouched (`migrations/versions/ed46f4aec7be_integration_schedule_catchup_policy.py:20-57`).

## Issues

### Critical (Must Fix)

- None.

### Important (Should Fix)

1. **Post-promotion triggers are coalesced without recording the required first catch-up provenance.** `IntegrationScheduler.mark_due` recognizes `cleanup_pending` as a lifecycle but excludes the reviewed Task 9b2 terminal representation, `lifecycle='promoted'` with `cleanup_state='pending'` (`src/integration/scheduler.py:100-137`; the actual terminal write is `src/integration/main_promotion.py:1111-1124`). The outstanding request remains owned until Task 10c release, so a manual or due-periodic trigger in that interval returns `coalesced` but leaves the catch-up tuple NULL; release therefore has nothing to convert into the next request. The acceptance test only records catch-up while the batch is `sealing` and manually clears the request after changing it to `promoted`, so it misses this window (`tests/test_integration_schedule.py:65-119`). Treat an outstanding nonempty promoted/pending-cleanup batch as catch-up eligible (or define eligibility from the outstanding request plus nonempty batch state rather than an obsolete lifecycle list), and add manual-first and periodic-first tests after root promotion but before release.

2. **Every service tick resets every selector cursor to `None`, so persistent rows beyond the first page starve indefinitely.** Candidate and intent rows deliberately remain untouched when a later-phase handler is absent or declines them, yet `tick` always asks for the first page again (`src/integration/service.py:51-92`, `src/integration/service.py:100-111`). The query-only tests prove callers *can* page, but no service test proves cursor advancement across ticks (`tests/test_integration_service.py:61-122`, `tests/test_integration_service.py:320-405`). A focused in-memory diagnostic using the real `tick` observed `['a', 'b', 'a', 'b']` over two ticks with `page_size=2`; persistent row `c` was never scanned. Maintain a per-source fair scan cursor, advance it from the last selected row before invoking handlers (including declined/stale rows), and wrap to the beginning without making the cursor a correctness queue. Add an actual multi-tick test with more persistent rows than `page_size` and both missing and declining handlers.

3. **A transient selector exception permanently terminates the sole background loop and is re-raised during shutdown.** Selector awaits sit outside `_isolated`, and `_run` directly awaits `tick` without a non-cancellation error boundary (`src/integration/service.py:51-98`, `src/integration/service.py:131-147`). A focused diagnostic made the first selector raise once; the named service task became done with `RuntimeError`, no later source/outbox ran, and `stop()` re-raised the old failure. This violates the durable service lifecycle and the requirement that one source failure not prevent later work. Isolate selection/processing per source so later sources still run, retain `CancelledError` propagation, and add an outer loop safeguard so an unexpected non-cancellation failure is logged and retried on the next interval. Test a one-shot selector failure followed by successful later sources, another healthy tick, and clean deterministic stop.

### Minor (Nice to Have)

1. **The reported test output is not pristine and does not identify its warnings.** The required area gate reports 11 warnings and the migration gate reports two, but no warning classes/messages are retained (`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-report.md:142-169`). Record and resolve or narrowly justify each warning so regressions are distinguishable from inherited noise.

## Carried Integration Debt for Task 12

- The two exploratory parent-completion failures are not introduced by this diff: the historical tests mutate append-only delivery receipts directly (`tests/test_integration_parent_completion.py:569-580`, `tests/test_integration_parent_completion.py:591-599`), while Task 10a changes neither that authority nor that test file. Task 12 should update their setup to construct the intended immutable historic/malformed cases through valid persisted shapes while retaining the negative readiness assertions.
- The migration tests initialize an already-current database and then downgrade before exercising this revision (`tests/test_migration_integration_service.py:98-125`), so they do not retain the brief's true fresh-empty `upgrade head` proof. The report attributes the blocked fresh-empty attempt to the older hierarchy canonicalization migration, not to Task 10a's additive revision. Preserve this as explicit Task 12 migration-chain debt: run both dialects from a genuinely empty database and repair the pre-existing bootstrap seam rather than continuing to substitute current-head U-D-U for fresh-install evidence.

## Checks

- Read the complete Task 10a brief, implementation report, and supplied `review-cbec0753..c3892b41.diff` once. Focused external checks were limited to: the Task 9b2 final batch lifecycle writer for the named catch-up-state risk, the frozen-operation V2 acceptance path for the named outbox-routing risk, and the two reported inherited receipt mutations / hierarchy bootstrap migration for debt attribution.
- Did not rerun the reported suites. Ran one bounded, in-memory diagnostic against `IntegrationService.tick`/`start`/`stop` to verify the concrete fairness and selector-failure seams; it produced the starvation and permanently faulted-loop behavior described above.

## Assessment

**Task quality:** Needs fixes

**Reasoning:** The component boundaries and persistence primitives are clean, but catch-up loss and the service's unfair/fault-terminal loop can strand durable work. Those three scoped runtime defects must be corrected before Task 10b relies on this substrate.
