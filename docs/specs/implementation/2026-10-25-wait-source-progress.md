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

`aq wait source-redispatch --source-seq N --project P --note REASON` lets a live named
supervisor or local operator reobserve one persisted source through normal dispatch.
The command requires a project, fences the leader lease, preserves the forward cursor,
filters wait and lifecycle effects to that project, and records an audit event. It
never trusts caller-supplied evidence or a former dispatched stamp. Missing sources,
contention and absent leadership return a refusal; no wake is fabricated.

An independent listener monitor checks undispatched row count and oldest commit age,
including deferred rows behind the cursor. Alert when count exceeds configurable
`wait_policy.listener_lag_events` (default 500) or age exceeds 120 seconds. Persist one
supervisor notice per project and oldest-event episode; restart and repeated checks
must not duplicate it. Monitoring needs no graph fence and must continue while drain
or maintenance is blocked. Messages contain sequence/age/count and recovery guidance.
