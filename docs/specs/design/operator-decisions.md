# Durable operator decisions

Operator instructions about a task, integration batch, or repair operation are
shared object state, never private supervisor mailbox state. A supervisor that
receives a decision through chat or Discord records it before acknowledging or
acting on it; messages reference the returned decision ID.

`aq decision record` records the operator's identity, verbatim decision, source
channel/reference, server timestamp and authenticated recorder. The object is
resolved server-side and must belong to the caller's project. Global and project
supervisors read the same history through `aq decision list`, task show/explain
and integration status. Workers cannot record or release operator decisions.
The API scope gate resolves `object_id` using its required `object_kind` tag for
these two commands, through the same object lookup as their handlers. Missing
objects, invalid tags and foreign projects fail closed without injecting a
`project_id` argument into the strict command contracts. Integration control
refusals for a foreign project retain the typed `unauthorized` outcome.

Effects are explicit: `note` records context; `hold` prevents integration changes
for the object and its related tasks/batches/operations; `release` references one
exact hold on the same object. A release is another audited operator instruction,
not deletion or an implicit override. Multiple holds require multiple releases.
An idempotency key replays the identical record and rejects changed payloads.
The source and operator are supervisor attestations, not proof of a human login;
the authenticated recorder is stored separately. No LLM classifies decisions in
scheduling code.

Integration commands consult active holds before dispatch. Broad project controls
are refused if they could affect a held object. Read-only diagnostics remain
available. Git-first batch authorization rechecks holds before publication;
reconciler observations and transactional human-hold checks also include them.
This does not recall an external write already in flight when a hold is recorded.
Supervisors must inspect current state after recording a hold during publication.
