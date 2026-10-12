# Repair preparation under PostgreSQL contention

Task: `crisp-quest-25`.

PostgreSQL deadlocks (`40P01`) and serialization failures (`40001`) during
pool claim preparation are transient. After the failed transaction rolls
back, retry preparation at most three times with exponential backoff. Each
retry rechecks the same session, task and claim epoch. Preserve the existing
repair reservation and fence; do not emit a permanent prepare failure, mark
`needs_attention`, consume a repair attempt, or allocate a successor stage.
If contention persists, retain the fenced preparing claim for the next claim
call and return `no_ready_work`. Real preparation errors retain the existing
failure diagnostics and guarded cleanup.
If a notification transaction conflicts after activation has committed,
return the current claimed result without preparing the checkout again.

Managed preparation must acquire session and task activation locks before
the ref advisory/owner lock. Ordinary repair allocation takes batch intent,
then the existing task (when present), then the ref lock. The incident's
deadlock was preparation holding the ref advisory lock while waiting for
the session, against heartbeat's session-before-ref renewal. This order also
removes the ref-before-task inversion with repair recovery/filing. Task
activation uses `FOR NO KEY UPDATE`: reset's independent salvage writes must
still be able to take task foreign-key key-share locks.
Legacy repair budget transactions keep their project/operation/stage/task
order and finish before preparation enters branch exclusion.

On a later train visit, an existing detached ordinary repair whose lease was
released or expired must receive a reservation for its original target after
publication is confirmed. Preserve its immutable repair input, task identity
and allocation count. A live claimed writer, another current lease holder,
a held batch, or an unconfirmed target prevents reacquisition. Reacquisition
of a lost/expired lease advances its fence, fencing the old writer; an intact
lease and transient preparation retries preserve the existing fence.

Reservation recovery runs on each eligible train visit with a confirmed
published candidate, including pending checks and unknown observations; it
does not depend on reaching red/conflict allocation. This pass may only
restore an existing detached filing, never allocate a successor. A READY task
whose session still holds its claim is not detached and cannot receive a new
fence.

An ordinary pool repair restores its slot before accepting close and releasing
the managed lease. If a managed job pins the slot (`jobs.workspace_busy`),
close is deferred with the same task status, claim epoch, retry count and lease.
The worker waits for verified job cleanup and retries close; an accepted close
skips the second slot restore.

Acceptance covers raw and SQLAlchemy-wrapped PostgreSQL failures, bounded
retry exhaustion/resumption, non-transient diagnostics, real transaction
rollback, preparation concurrent with session/task-before-ref recovery,
independent reset salvage transactions, and train
replay of a missing reservation. Run focused and area claim/repair/train
checks, changed-path ruff, and the disposable swarm smoke check when worker
scope permits it.
