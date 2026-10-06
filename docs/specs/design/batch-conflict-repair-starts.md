# Batch conflict repair starting heads

Task: `sound-journey-95`. Extends the Git-first train and exact completion
provenance contracts.

When a frozen batch rebuild conflicts, its repair starts at the exact partial
head returned by the merge: the fetched target plus the members merged before
the conflict. Before filing, the train publishes that head on the candidate
ref through the managed lease and an expected-old comparison. This applies
when the ref is absent and when it contains an older repaired candidate. A
live repair writer, moved ref, held batch or failed publication prevents filing.
The train never substitutes the older candidate for the partial head.

The conflict brief names that published partial head, the conflicting member,
the members already merged into it and every remaining member in frozen order.
The observation retains the replaced candidate SHA when a rebuild displaces it.
The target branch stays unchanged until a complete candidate passes its gates.

Before allocating a conflict repair after a completed repair, inspect its immutable Git
completion source. If it equals the repair's starting head and does not contain
the current fetched target, report `repair_no_progress_missing_target` with the
task, starting head, completed head and target SHA. Repeated visits retain that
blocker and allocate no further attempt. Missing completion provenance reports
`repair_completion_unconfirmed`, rather than treating an unverified close as
progress. A missing prior repair input reports `repair_input_unconfirmed`.
Ordinary task completion remains an account of worker work; the train
still derives delivery from Git. A changed completed head may be rebuilt when
the target subsequently moves; a delivered batch settles before either guard.

Verification uses real Git and disposable PostgreSQL: replace an older repaired
head after an external target move, confirm the partial head is published and
named in the filed brief, preserve the existing candidate on failed or refused
transport, and repeat visits after an unchanged completion missing the target
without allocating a successor. Exercise live managed leases and changed
completion sources too.
