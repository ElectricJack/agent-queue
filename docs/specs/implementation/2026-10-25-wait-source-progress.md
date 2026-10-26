# Wait source progress and recovery

The listener must renew during its initial drain and run independently of maintenance.
It queues briefly for the graph fence before releasing its connection on contention.
Each source transaction has a five second work budget. A timed out transaction rolls
back completely; a separate leased cursor CAS may advance past that event, leaving
its undispatched row as durable retry ownership. Later events may proceed. Cancellation
and lease loss never authorize this advance. Retry passes are bounded and back off.
Budget failures and retry deadlines persist with the source row. After three
consecutive budget failures the row is quarantined, retaining its payload and
subscription ownership, and retried behind the forward cursor at five minute
intervals. A successful dispatch clears the failure streak and quarantine. A
restart respects the persisted retry deadline. Quarantine never asserts a wait
outcome or prevents later events from dispatching.
A drain pass yields between attempts after five seconds and schedules another tick,
so a backlog cannot indefinitely delay watermark refresh or a recovery command.
PostgreSQL deadlocks (`40P01`) and serialization failures (`40001`) during a
source attempt follow the same rollback, leased cursor CAS and retry backoff as
an expired work budget. Reobserve the source on retry and keep unrelated events
moving without restarting the listener. Other database errors still propagate.
An event-local dispatch exception follows that same durable retry/quarantine path;
it must not restart the listener and replay the forward head indefinitely. Lease
loss, cancellation and graph contention retain their existing fences.
For a hibernate wait whose continuation is held (including `push_failed`), return
a per-wait `owner_held` dispatch disposition with the continuation's existing
hold/escalation identity and a retry deadline. Preserve the pending wait, hold,
claim and checkout; neither source satisfaction nor a resume is authorized by
that disposition. Other subscribers to the event still dispatch. A not-yet-persisted
hibernation intent similarly requests a bounded retry. Both ordinary sources and
resource invalidations retain an undispatched source until every receipt permits
retirement, and persist the retry deadline so a new leader respects it.
Retained undispatched events older than the listener safety cadence share alternating
turns with forward work once their observation backoff expires. Preserve the next
turn across drain passes, including timeouts, so a continuous forward backlog cannot
starve retries and an unavailable source cannot starve forward work even when a pass
fits only one attempt. Existing retry batch limits still apply. Retry selection and
coalescing never advance the cursor past unrelated forward events.

CI observations of the same source and generation reobserve once at the newest
sequence through the captured committed watermark. This includes hints interleaved
with other sources and retained retries behind the cursor. Superseded hints are
retired together; if observation must retry, only the newest hint remains pending.
The forward cursor advances only through the consumed contiguous prefix: coalescing
must never skip unrelated sources between the selected hints. Already dispatched CI
hints have no remaining lifecycle effects and may retire without another observation.
Contiguous pool/config/workspace invalidations with the same producer and project
scope may also reobserve once. Provider transitions and other event kinds remain
individual because they carry lifecycle effects.

After an authenticated CI observation reports an unfinished run, further explicit
progress hints for that source share the listener's safety cadence. Keep only the
latest pending hint and continue draining unrelated sources. Terminal hints bypass
this progress cadence immediately and always require fresh observation. The cadence
is process-local; restart may reobserve immediately. Event payloads choose scheduling
only and never authorize a wait outcome.

The listener prepares authenticated CI/run/PR observations before acquiring the
graph fence. Under the fence the dispatcher locks and revalidates current owner,
claim, subscription, exact condition/source generation, repository binding and CI
cache rows before applying the prepared verdict. Changed bindings or a subscription
registered during preparation require a new unlocked observation; the locked apply
must never fall back to remote I/O. Database-backed predicates and resource admission
remain fresh transaction-local reads. Lease loss, cancellation and failed apply roll
back all outcomes and cursor changes. A deferred newest CI hint retains durable
retry ownership; superseding a hint does not prove satisfaction.

The CI source fence and every mutable exact-head cache writer lock the repository
row first, then a transaction advisory lock keyed by repository and SHA before any
cache row access for mutation or locked revalidation. This includes refresh inserts
and updates and check-reconciliation reservations/results. The source fence uses
this advisory lock instead of locking cache rows in a competing order; it also
covers first observations where no cache row exists yet. Refreshes touching several
heads acquire their fences in repository/SHA order before any check-specific locks.

CI events have no lifecycle effects without a pending verified subscriber of the
same source/generation whose watermark precedes the event. Pool, config and workspace
invalidations likewise only reobserve resource subscribers. Retire these unused
observations in forward prefixes and retained batches behind the cursor, including
events still on retry backoff, under the graph and listener lease fences. Resource
matching conservatively preserves any pending verified resource subscriber with a
preceding watermark. Provider invalidations always retain lifecycle dispatch: even
with zero pending waits they can release held continuations. No source observation
or owner progress preparation is needed for a retired batch of observation hints.
Aborted integration batch events always retain dispatch so their callback cancels
obsolete CI subscriptions even without a subscriber to the batch source. Registration remains
serialized by the graph fence. An empty retirement scan must still allow one
forward/retry attempt before the pass yields.

Forward prefix discovery uses the already loaded, bounded event window before
acquiring the graph fence. Under the fence, recheck only that planned prefix
against grouped minimum watermarks of current pending verified subscriptions;
do not repeat an event-history scan or a correlated subscriber scan per event.
Any resource subscriber conservatively preserves a resource invalidation.
If a new subscription invalidates the entire plan, release the transaction and
repeat unlocked preparation. A shortened prefix may commit on its own. Keep the
leased cursor race check, and stamp at most one event window per acquisition.
Source dispatch and outcome mutations remain fenced and atomic with the cursor.

For graph holds of at least one second, log time spent in lease validation,
prefix retirement, admission recheck, dispatch, outcome/cursor writes and
transaction completion. Include failed and cancelled attempts so a timeout does
not hide the dominant step. Acquisition wait is excluded from these hold times.

An idle drain or unsubscribed backlog must not prepare fleet-wide admission.
Only a source with a pending verified resource subscriber needs that preparation.
Check under the graph fence so concurrent registrations cannot be missed; release
the source transaction, prepare admission outside the fence, then retry dispatch
against current subscriptions. Preparation never authorizes an outcome itself.

`aq wait source-redispatch --source-seq N --project P --note REASON` lets a live named
supervisor or local operator reobserve one persisted source through normal dispatch.
The command requires a project, fences the leader lease, preserves the forward cursor,
filters wait and lifecycle effects to that project, and records an audit event. It
never trusts caller-supplied evidence. After validating project and source scope,
an already dispatched source returns success with outcome `already_dispatched` and
its `dispatched_at` timestamp, without acquiring the listener lock or lease,
preparing observations, dispatching, retiring history or writing an audit event.
Recheck dispatch state after acquiring the listener lock. For pending sources, a
work-budget refusal includes the source sequence, phase, budget and elapsed seconds,
and dispatch attempt count; an observation's own timeout is reported separately.
Missing sources,
contention and absent leadership return a refusal; no wake is fabricated.
For a CI, pool, config or workspace source, the command also retires retained events
of that source/generation through the requested sequence when no pending verified
subscriber anywhere needs each event. It reports and audits the number retired. A project-scoped recovery
must preserve any event still needed by another project's subscriber. Retirement
uses bounded batches; repeat the command to drain additional history.

The same supervisor authority supplies `aq wait source-blockers --project P` and
`aq wait source-retire --source-seq N --project P --note REASON`. These dispatch
the source recovery command with action `blockers` or `retire`. Inspection lists
old or failed undispatched sources, their cursor position, durable failure count,
quarantine/deadline and matching project subscriptions. Retirement cancels only
service-owned integration CI subscriptions for the selected source and project;
it refuses agent subscriptions and lifecycle sources. It stamps the source only
when no project still needs it and never jumps the forward cursor. Cancellation
and the supervisor audit commit together, without remote CI observation.

Canonical aborted/superseded batch events cancel that target's integration CI
subscriptions whose exact head no longer belongs to a non-aborted batch. They
preserve a head reused by an active batch and successful settlement of promoted
candidates. Speculative subscriptions retain their existing train reconciliation.
Reconciliation during train synchronization repeats this cleanup to recover old
rows and crash windows. CI observation reuses the immutable candidate check policy
captured during train synchronization, while still observing fresh remote evidence. Its remote read budget leaves time for listener transaction
bookkeeping; trust reconstruction must not recur on every observation.

An independent listener monitor checks undispatched row count and oldest commit age,
including deferred rows behind the cursor. Alert when count exceeds configurable
`wait_policy.listener_lag_events` (default 500) or age exceeds 120 seconds. Persist one
supervisor notice per project and time bucket of
`wait_policy.listener_lag_alert_minutes` (positive integer, default 15). Advancing
the oldest sequence, restart, and repeated checks within a bucket must not duplicate
it; continued lag may produce another notice in the next bucket. Lag notices are
diagnostic messages and must not create wait source events themselves. Monitoring
needs no graph fence and must continue while drain or maintenance is blocked.
Messages contain sequence/age/count and recovery guidance.
