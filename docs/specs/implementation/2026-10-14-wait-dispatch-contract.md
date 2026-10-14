# Wait outcome integration contract

This dormant dispatcher slice consumes graph owner commit
`debbec299fc82a41c342315f816634cbe0be9871` and checkpoint owner commit
`7e1402dabd831d6096378ccf61ba7df233fb2350` by ancestry-preserving merges.
The approved hibernation/wait specifications remain authoritative. V2 stays off;
supervisor wake/cancel/reroute semantics await the existing human escalation.

## Transaction and graph ownership

`WakeDispatcher.commit` runs inside the listener's READ COMMITTED transaction.
It validates the durable source event, takes the graph fence, discovers old edge
endpoints, then locks the component's tasks before owner/source/wait rows.
Current process, claim, source identity/generation and fresh source observations
are required. A terminal CAS clears active edges with `graph.clear_edges` and
calls `graph.recompute_from(conn, changed_keys, now=...)` with the OLD endpoints
included. Base priority is never written. Outcome, retraction, measurement,
event, escalation resolution and delivery intent commit or roll back together.

The graph implementation is the default recomputation callback. Explicitly
missing recomputation, source locking or required escalation resolution refuses
the transaction and retains the listener cursor. Native database source locks
are provided; external source owners must provide their transactional fence.
`wait_resolve_escalation(conn, escalation, source_event, outcome=..., now=...)`
must resolve the existing escalation inside this transaction and return
`{"success": true}`; a missing callback or refusal rolls back the outcome. It is not an
authorization to invent supervisor decision semantics.

## Delivery and downstream ownership

Registration preserves `decision_inputs.registration_receipt` unchanged and
stores `decision_inputs.delivery.instance_token` and `held_cursor_seq` alongside
it. Outcome delivery merges metadata without replacing the receipt. Terminal
execution waits keep `cleared_at=NULL` until acknowledgement. The generic graph
`resolve_wait` helper clears that field and is therefore not the dispatch path.

In-place outcomes use the checkpoint owner's `wait_outcome_envelope`, with one
durable message id `wait:<id>:outcome`. Ordinary delivery holds routine mail and
does not stamp it read/delivered. A successful wake nudge records its receipt;
external terminal I/O is not crash-atomic with PostgreSQL. Outcome commit alone
does not release liveness protection. `held_messages` returns the routine-mail
snapshot after the registration cursor. The inert `acknowledge_in_place` helper
illustrates one transactional cursor/mail/liveness release and has no public
handler registration; task .15 owns the actual acknowledgement lifecycle and
fresh-continuation generation binding. Prime never acknowledges mail.

For an already parked continuation, verified stop/process/claim evidence permits
one sleeping-to-queued transition and HIBERNATED-to-RESUME_PENDING task change.
The hibernation owner (.14) owns races before parking; dispatch refuses those
until its transactional transition callback exists. The resume owner (.15) owns
launch, fresh-session binding, held mail for the old profile/session and ACK.
Shared-profile consumer scoping is not implemented by the in-place mail guard.
There is no reverse dependency on these downstream tasks and no runtime enablement.
