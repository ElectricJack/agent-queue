# Wait source progress and recovery

The listener must renew during its initial drain and run independently of maintenance.
It queues briefly for the graph fence before releasing its connection on contention.
Each source transaction has a five second work budget. A timed out transaction rolls
back completely; a separate leased cursor CAS may advance past that event, leaving
its undispatched row as durable retry ownership. Later events may proceed. Cancellation
and lease loss never authorize this advance. Retry passes are bounded and back off.
A drain pass yields between attempts after five seconds and schedules another tick,
so a backlog cannot indefinitely delay watermark refresh or a recovery command.

Contiguous CI observations of the same source and generation may reobserve once at
the newest sequence. Contiguous pool/config/workspace invalidations with the same
producer and project scope may also reobserve once. Current canonical predicates,
owner fences and subscription watermarks still decide outcomes. Provider transitions
and other event kinds remain individual because they carry lifecycle effects. Every
coalesced row is stamped only with the successful transaction; deferred rows remain
undispatched. A bounded prefix stops at unrelated lifecycle events.

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
