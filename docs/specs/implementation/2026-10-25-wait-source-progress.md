# Wait source progress and recovery

The listener must renew during its initial drain and run independently of maintenance.
It queues briefly for the graph fence before releasing its connection on contention.
Each source transaction has a five second work budget. A timed out transaction rolls
back completely; a separate leased cursor CAS may advance past that event, leaving
its undispatched row as durable retry ownership. Later events may proceed. Cancellation
and lease loss never authorize this advance. Retry passes are bounded and back off.
A drain pass yields between attempts after five seconds and schedules another tick,
so a backlog cannot indefinitely delay watermark refresh or a recovery command.

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

CI events have no lifecycle effects without a pending verified subscriber of the
same source/generation whose watermark precedes the event. Pool, config and workspace
invalidations likewise only reobserve resource subscribers. Retire these unused
observations in forward prefixes and retained batches behind the cursor, including
events still on retry backoff, under the graph and listener lease fences. Resource
matching conservatively preserves any pending verified resource subscriber with a
preceding watermark. Provider invalidations always retain lifecycle dispatch: even
with zero pending waits they can release held continuations. No source observation
or owner progress preparation is needed for a retired batch. Registration remains
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
never trusts caller-supplied evidence or a former dispatched stamp. Missing sources,
contention and absent leadership return a refusal; no wake is fabricated.
For a CI, pool, config or workspace source, the command also retires retained events
of that source/generation through the requested sequence when no pending verified
subscriber anywhere needs each event. It reports and audits the number retired. A project-scoped recovery
must preserve any event still needed by another project's subscriber. Retirement
uses bounded batches; repeat the command to drain additional history.

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
