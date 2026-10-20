# Root PR check recovery

A completed leaf root whose exact PR head has a genuinely failed required
check returns to its worker through the normal READY transition. Admission
supplies the failure observed by its trusted exact-check provider, including
check names, job links and failing test IDs when the provider reports them.
Pending, unavailable, cancellation-only, missing-check and workflow-only
failures remain observation blockers; they do not prove repairable code.
An authorized repair admitted with its source does not reopen that source.

Recovery runs through CommandHandler. Before changing a task it locks the
project hierarchy and rechecks the completed task, completion identity,
repository, branch, PR, source base and project policy generation against the
observation. A stale observation cannot reopen newer or running work. Normal
integration mutation guards still refuse a frozen source or a live writer.
The reopen retains branch, origin, integration mode and explicit routing;
derived routes are recomputed normally. It appends
feedback and clears the assignment while retaining the existing PR and retry
counters. Feedback, recovery accounting and the transition commit together.

Each leaf can reopen automatically once for a given source SHA and up to
`root.repair.primary_attempts` times (three when absent). Accounting persists
across daemon restarts and completion generations. `on_exhausted: continue`
permits further changed heads, but never an unchanged head. Exhaustion or an
unchanged head produces one supervisor notification per exact completion and
a named admission blocker. A container cannot be claimed by a worker, so its
failure instead produces a supervisor repair notification. Existing bound
source-CI repairs keep their delivery path and are not superseded by reopening.

The corrected completion must pass its own exact PR checks and ordinary
candidate validation before delivery. Independent healthy roots can continue
through admission while failed roots recover.

Recovery reads delivery truth for mutation, so it can wait behind the delivery
observer's per-repository fetch. The admission gate waits for it a bounded time
(`ROOT_PR_RECOVERY_WAIT_SECONDS`) and then defers that root as `pr_checks_red`
with a `pending` recovery. The recovery keeps running past the visit that stopped
waiting. A later visit takes its outcome before the root's backoff expires and
never starts a second recovery for the same observation. A changed head, PR,
branch, policy or completion supersedes the observation and cancels its
recovery, so at most one recovery runs per target and root; the recovery's own
identity recheck still refuses any outcome that went stale while it waited. A
slow recovery therefore never spends a visit's deadline or keeps other roots
from batching.

When source-CI recovery is authorized, the one-repair source budget also applies
to root PR recovery, including under `on_exhausted: continue`. Feedback from the
source observer and the root admission gate counts across both paths. Historical
source-CI delegates cannot be reopened by this alternate path. Exhaustion sends
one notification per task across changed heads and restarts.
