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
This includes scoped operator workers: `decision_record` is explicitly forbidden
by their execution wrapper. The local operator and live named supervisors use
the shared supervisor authority gate; a project supervisor stays in its project.
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

## Record, inspect and release

Record the actual human instruction and its source reference. For example, the
local operator can hold a batch and later release that exact hold:

```bash
aq decision record --object-kind batch --object-id BATCH_ID --effect hold \
  --operator Jack --decision 'Hold this batch for inspection.' \
  --source cli --source-ref operator-session-1 --idempotency-key batch-hold-1
aq decision list --object-kind batch --object-id BATCH_ID
aq decision record --object-kind batch --object-id BATCH_ID --effect release \
  --releases HOLD_ID --operator Jack --decision 'Release the inspected batch.' \
  --source cli --source-ref operator-session-2 --idempotency-key batch-release-1
```

Use the returned hold ID as `HOLD_ID`. `--releases` is required only for a
release and forbidden for a note or hold. The object kinds are `task`, `batch`
and `operation`; the project is resolved from that object, so these commands
take no `--project` argument. Each distinct decision needs its own idempotency
key. Repeating the same key with identical content replays the record.

An operator-decision hold and a task recovery hold are separate barriers.
`aq task recover --operator-release` addresses a recovery incident; it does not
release an operator decision. Likewise, `integration release-withheld-source`
releases one frozen source from an aborted batch's withholding rule, recording
a daemon-authored audit note; it does not release a human hold. That note's
reserved source is not accepted by `aq decision record`, whose source choices
are `chat`, `discord` and `cli`.
