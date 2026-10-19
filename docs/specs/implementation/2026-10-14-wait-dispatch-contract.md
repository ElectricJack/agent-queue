# Wait outcome integration contract

This dormant dispatcher slice consumes graph owner commit
`debbec299fc82a41c342315f816634cbe0be9871` and checkpoint owner commit
`7e1402dabd831d6096378ccf61ba7df233fb2350` by ancestry-preserving merges.
The approved hibernation/wait specifications remain authoritative. V2 stays off.

## Section 10.5 correction — 2026-10-19

Global engineering direction `msg-4c43f9741f374eaeaa0202ce8ae70e78`, supplied in the
claim-8 startup packet `msg-ac0c74e5408e4a1cad7539e299a7f8a6`, supersedes the older
section 10.5 decision table. The canonical clarification escalation
`escalation-1e5fb3c7-4f6c-49ca-80dd-2da65e4c568c` is cancelled at revision 2.
The historical reviewed specification and delivered checkpoint remain unchanged.

- Supervisor **wake** commits `outcome=cancelled`, with `decision=wake` and the
  supervisor note in the outcome envelope, using the ordinary fenced delivery.
- **cancel** cancels the wait and continuation, requests termination of a live
  holder, and leaves its task PAUSED for the supervisor. It creates no wake or
  resume intent and does not release a live writer for scheduling.
- **reroute** is a separate explicit release, requiring a proved stopped process
  and stored summary. It preserves a handoff and clears the route through the
  normal admission router; project, review, dependency and resource gates remain.
- Only **human wait_lift**, with a verified human response, may commit
  `lifted_by_supervisor`. An operational wake never fabricates a human answer.

An escalation deadline creates an operational supervisor inbox notice and leaves
the wait pending. Extending creates the next deadline generation without changing
the registration time, ceiling anchor, held cursor or immutable receipt. A third
identical registration after wakes without branch/comment/task-edit progress
forces hibernation and raises a supervisor escalation. Supervisor wakes/lifts reset
that loop window.

### Incident generation compatibility — 2026-10-19

Escalations permanently deduplicate `(project_id, source_kind, source_identity)`
and retain terminal incidents. Using just the wait id, as the historical section
10.5 example did, prevents a second incident after `extend`. This implementation
uses `source_identity=<wait_id>:<generation>` and
`incident_key=wait:<wait_id>:<generation>`. `wait_escalations.wait_id` and
`generation` remain the authoritative link, with `prior_escalation_id` preserving
the chain. No schema or terminal-incident rewrite is needed. This compatibility
choice was reported to the supervisor in `msg-7ba5090e15db48e2b9c6d272abacb37f`.

`escalate_to_human` changes the verified condition to the existing human
message-thread adapter. The escalation outbox delivers the question; an archived
owner-authored request binds the reply cursor without a second human delivery.
Only a verified transport reply is copied into the owner's thread as human
evidence. In-place conversion requests a checkpoint via `wait_suspend`; the
hibernation owner still proves process stop and releases the claim. Source
dispatch waits for that proof before queueing a continuation.

Loop detection defaults to three registrations in 3,600 seconds. It compares
normalised conditions and source generations against acknowledged outcomes,
using saved/observed Git heads, comments and explicit task edits as progress.
Lifecycle-only status events do not reset the window. Missing Git evidence cannot
establish a loop. The loop event identifies the wait and registration history;
checkpoint creates the actual continuation id and generation later.

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
`{"success": true}`; a missing callback or refusal rolls back the outcome. The
default service implementation withdraws operational incidents with source-event
evidence; it never records an unreceived human answer.

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

## Claim-8 verification — 2026-10-19

- Required acceptance: `test_wait_dispatch`, `test_escalation_commands`,
  `test_escalation_delivery`, `test_session_reconciler`, `test_handoffs` and
  `test_agent_waits`: 472 passed.
- Affected-area rerun: `test_wait_decisions`, `test_wait_holding`, `test_waits`,
  `test_message_delivery` and `test_event_schema_registry_validation`: 975 passed.
- Final focused checks after the equal-timestamp comment progress fix:
  `test_wait_decisions`, `test_wait_queries` and `test_wait_outcomes`: 135 passed,
  including 27 operational decision/loop tests.
- Disposable `e2e-env.sh --reset` and `e2e-smoke.sh`: 20/20 scenarios passed.
  Home `/tmp/aq-e2e-bold-zenith-11-12-claim8`; log
  `/tmp/bold-zenith-11.12-claim8-e2e.log`. The wrapper stopped its disposable daemon.
- Changed-file Ruff, generated-file drift and whitespace checks passed. No API
  model/router or schema changes were made. No whole-suite test run was used.

The broader area run exposed a nullable `body_kind` predicate regression and
dependency/fixture mismatches; these were corrected before the passing reruns.
The package dependency ratchet and ordinary-mail delivery checks remain intact.
The daemon refused scoped knowledge enumeration with `record.forbidden`; that
limitation was reported once and findings were recorded on the task and here.
