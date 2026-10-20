# Completed epic aggregate reverification

In the active Git-first train, a completed container still owes an aggregate
completion until its required child sources are contained and its exact head
passes the current checks and tree review. Legacy checkpoint verification fields
and a released branch owner do not schedule or authorize that completion.

Every idle epic visit, and every successful child publication, must observe
aggregate readiness. A bounded selection-window deferral with no active batch
must not suppress this observation. Other admission refusals retain authority.
The full recursive readiness evaluator continues to enforce child
completion and containment, project/task holds, source identity, checks and reviews.

Discovery retains completed containers as epic targets even when their child
origins have retired or the container's own live origin is missing. A root
container owes the same aggregate visit as a nested one; it never waits for a
nonexistent parent integration run. Missing provenance remains a visible
readiness blocker, never permission to manufacture a completion. Current root
delivery proof and observed retirement of the private ref suppress obsolete
visits under the existing rules.
For a legacy container without a live origin or its own completion, delivery of
every current child plus ancestry of its observed private tip in the default
branch also suppresses an obsolete visit. A contained private tip alone cannot
excuse undelivered children or an unproved aggregate completion.

A managed root with a branch/checkpoint and completed children must display
pending aggregate verification or unavailable delivery evidence until current
delivery is proved. Its stored COMPLETED status describes implementation only.
Doctor's completed-undelivered inventory and stall sweep also report every live
completed root whose observed branch tip is not contained in the default branch,
including roots accounted for by a pending PR, batch or historical disposition.
Those records explain the pending work; they do not hide it. Unknown ancestry
remains unknown, and diagnostics never authorize delivery or branch deletion.

`integration_reverify_epic` / `aq integration reverify-epic TASK` is an operator
or live project supervisor control using the existing integration authority.
Preview requires an active designated train repository, a completed container,
a unique live materialized branch origin and an observable private branch.
It returns the current source head and existing completion identity, without
changing task, checkpoint, ownership, child, review or verification state.

Apply requires the preview head and a reason. It repeats eligibility and source
observation and refuses live claims/writers/ownership. Under the project and
branch locks, it may retire the completed epic's own `worker` reservation when
the row is `reserved`, names no session or workspace, and has no unexpired lease
or different holder. Retirement compares the row identity, state and fences,
advances its fences, clears its holder, and records an attributable event in
the same transaction. Attached rows, other owners/roles, live leases, open
batches, active integration operations and reserved ref mutations remain
refusals; preview never retires ownership. Apply then wakes the target's ordinary
backoff and requests one bounded, serialized train visit. Repository concurrency
and rate-limit admission still apply. The train may collect pending children or
continue existing checks/repair under its ordinary gates. A deferred visit returns
pending; a completed visit returns its actual state and blockers. Neither result
asserts PASS. Only the train's normal, revalidated completion path writes passing
provenance and completion evidence. Apply records an attributable event containing
the requested head, before/after completion identities and actual visit result.

Existing completed children and accepted source history are preserved. Repeated
visits reuse the immutable aggregate completion identity. Reopening and automatic
container settlement do not constitute aggregate verification; the next visit
must evaluate current inputs again.

A completion older than the task's `integration_rework_at` fence is history,
even if container settlement has returned the task to `COMPLETED`. Delivery
requests and recursive aggregate graphs must withhold that record as current
evidence. They use the reopened generation until a fresh close or aggregate
verification records its own completion. Historical records remain immutable.

After installation, the operator previews and applies the control to
`prime-forge-80` (and affected bright-grove containers), then inspects the returned
checks/review blockers and cross-epic admission. A source worker cannot certify
live P3/P5/P6 admission by publishing this change.
