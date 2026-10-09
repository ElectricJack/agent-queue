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
The reopen retains branch, origin, integration mode and routing; it appends
feedback and clears the assignment and stored PR URL as ordinary reopening
does. Feedback, recovery accounting and the transition commit together.

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
