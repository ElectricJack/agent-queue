# Batch conflict repair starting heads

For new ordinary root/development conflicts, Phase A6 ejects the conflicting
member and its dependency closure and rebuilds the remainder instead of
allocating a whole-batch repair. The contract below applies to retained repair
paths, including existing repair writers and epic collection/refresh; see
[conflicting completed sources](conflicting-source-batches.md).

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

A frozen batch with an existing repair revisits its published candidate before
fresh source admission and rebuilding. Restore only the current detached
allocation's lease, using the allocator's locks and fresh published-ref proof;
preserve its input and attempt count. Holds, attached workers, moved refs and
unavailable Git evidence do not authorize a new fence. Open sealed batches keep
their target discoverable even when no completed source remains pending.
`aq integration reserve-owner` resolves repairs through their current batch input
or active stage, rather than their creator kind. It uses the same recovery proof
and never allocates a successor. The stall sweep names aged READY repairs whose
missing branch reservation excludes them from the claim frontier.

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

Task-free fixer launch reservations survive a daemon restart. On a conclusive
repair visit, a reservation still `launching` after 120 seconds enters the
existing launch retry path if no session was registered or its registration
is confirmed stopped. The grace period starts again for each launch attempt;
older reservations use `created_at`. Retry preserves the retained input and
checkout and releases the lane with bounded backoff. A registered session that
may still have a process remains retained. Registration and launch completion
must match the session id, instance token and launch start timestamp, so a late
attempt cannot register or overwrite its replacement. Exact green and withdrawn
publication authority retain their existing precedence.
