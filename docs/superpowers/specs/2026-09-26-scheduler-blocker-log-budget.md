# Scheduler blocker log budget

Task: `wise-ember.19`

## Problem

`Orchestrator._log_scheduler_blockers` emits one synchronous INFO call for
each changed or cleared blocker. Starting a daemon with 10,000 READY tasks,
or changing capacity for that many tasks, can stall its event loop while the
logging handler writes thousands of records.

## Behavior

- Each scheduler tick emits at most 20 detailed blocker transition records,
  sharing the budget between blocked/reason-changed and unblocked tasks.
- Small changes retain the existing per-task messages.
- When changes exceed the budget, one additional INFO record reports omitted
  blocked and unblocked counts, and up to three `(task_id, reason)` samples
  for each omitted transition type. Unblocked samples use the previous reason.
- The bound applies even when every task has a distinct reason.
- Every observed reason is cached, including transitions omitted from the
  detailed log. An identical following tick emits no records. Cleared tasks
  are removed from the cache, including tasks assigned this tick or leaving READY.
- The cache belongs to each orchestrator instance.
- Scheduling, reason construction, and the cached snapshot used by explain
  retain their existing behavior.

## Verification

Tests in `tests/test_orchestrator.py` cover small transitions, startup with
10,000 blocked tasks, mass reason changes, mass clears, mixed blocked/cleared
changes, distinct reasons, and quiet subsequent ticks. Assert record counts
and summaries instead of machine-dependent elapsed-time budgets.

Run focused blocker tests, the orchestrator test file, related explain tests,
and Ruff on changed Python files through the existing resource controls.
