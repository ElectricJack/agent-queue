# Human escalation creation and delivery

Immediate delivery contract for `clear-orbit-68`, extending the durable model
and delivery guarantees in the Discord simplification design. Human-facing
wording and broader communication design remain separate policy work.

An open human escalation must carry a project, subject title, plain-language
reason, explicit decision, and canonical incident deep link. Existing snapshot
fields are used: `task_title` is the subject title (including project-level
incidents), `investigation` explains why a person is needed and what was checked,
and `decision_requested` states the choice. Required text must contain nonblank
content. Commands hydrate a title from a bound task, or require the caller's
`task_title`. Core producers supply their own subject snapshot.

`deep_link` is derived from the durable incident ID using `escalation_path`:
`/focus/escalations/<escaped-id>`. Creation, get and list return it. External
delivery uses the existing dashboard origin resolver, so changing the dashboard
origin does not invalidate a stored incident or admit caller-supplied URLs.

Creation and the initial root outbox row commit in one transaction. The root
uses the existing `<incident>:root:0` dedup key and root priority. A caller-owned
transaction, such as integration repair allocation, includes both writes. An
outbox failure rolls back creation. Delivery remains queued when Discord is
disabled or a project is paused. Internal `supervisor_delivery` operational
notices retain their existing supervisor-inbox-only route.

Replayed creation normally returns the original incident, without changing its
content, and queues a missing root for an open legacy incident. The old gate
migrator's known snapshot (null title and migration-only investigation) is
repaired with gate decision context under a revision fence before queuing.
Any existing root,
including an ambiguous one or a replacement generation, prevents an additional
initial root. Terminal incidents stay terminal. Legacy card adoption binds the
queued row rather than duplicating it. When a legacy question's answer was
already accepted, migration retires only its unattempted root and preserves
answer evidence and external receipts. Creation, accepted-answer preservation,
and retirement share a transaction, so a dispatcher cannot claim the transient
root between those steps.

An incident closed before its first attempted delivery stays silent. Previously
attempted sends still reconcile their external receipts before any further write.

Gate conversion snapshots the blocked tasks' titles and explains what the
gate's decision blocks in the named project. It must not use migration provenance
as the only explanation of why a human is needed.

Doctor registers `escalations.undelivered`. It reports every open human incident
older than five minutes without a confirmed root receipt, including missing,
pending, sending, retry and unknown delivery states. It reads the overdue set
directly rather than limiting observation to the newest 100 incidents. Findings
include project, incident ID, title, deep link, age, delivery state and last error.
The existing `stall.sweep` includes the same findings for active projects.

Verification covers creation rollback, caller transaction rollback, concurrent
deduplication, legacy missing-root repair, ambiguous-send preservation, gate
context, disabled delivery persistence, scan starvation, and the five-minute
diagnostic boundary.
