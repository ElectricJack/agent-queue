# Wait outcome integration contract

## Dedicated listener connection arguments — 2026-10-24

The listener opens a dedicated asyncpg connection using the SQLAlchemy engine
URL. It omits unset URL fields and merges query parameters into one argument
dictionary, with query values taking precedence. Unix-socket query hosts and
ports must reach asyncpg without duplicate keyword arguments; ordinary TCP
fields and other query parameters pass through unchanged.

## Cancelled transaction recovery — 2026-10-24

Graph-lock watchdog cleanup uses local connection-lifetime state, without
accessing `Connection.info` or reconnecting. When cancellation invalidates an
asyncpg connection, SQLAlchemy must finish rollback before any subsequent use.
Scheduler sync and listener drain preserve committed prefixes, roll back the
interrupted owner's work or event together with its cursor, and retry on a clean
connection. A failed attempt must not prevent a later matching message event
from resolving its pending wait.

The `wait_scheduler` health check reports consecutive sync failures per project,
including exhausted sync budgets. A successful completed sync clears only that
project's failure streak. Ordinary graph contention defers work without adding
or clearing failures. Health inspection performs no database work for this check.

## Supervisor delivery and CI observation correction — 2026-10-23

Operational wait and continuation incidents remain internal during initial
delivery, reconciliation and retries. Only a committed `escalate_to_human` wait
decision authorizes human delivery; `needs_human` alone does not. Unread project
supervisor notices use the existing global-supervisor fallback. A previously
suppressed, unbound root may be retried after that explicit decision, while an
ambiguous transport attempt retains its reconciliation state.

CI source observation runs independently of integration-train visits and
continuation maintenance, including while new V2 admissions are disabled.
Provider quota responses preserve pending waits and defer observation until the
repository backoff expires. Each remote observation has a five-second budget,
including reads under the graph fence, so stalled CI calls release supervisor
controls. This narrows the remote-read budget without changing the source
listener's lease or transaction fences.

## Terminal owners and refused deadlines — 2026-10-22

Terminal task source events retire the owner's waits through the shared
archive/deletion retirement path: cancel pending outcomes and outstanding
deadlines, clear edges, withdraw incidents, and cancel continuations and
reservations without sending a wake. This includes scheduler waits and retracts
the terminal owner's downstream priority donations. The listener reobserves the
current task under graph/component locks, so a delayed terminal event cannot cancel a reopened
attempt. Deadline firing repeats this retirement check to recover old terminal
owners even when their source event has already been consumed.

A permanent owner/request refusal quarantines only that deadline by setting
`cancelled_at`. The service records `wait.deadline_quarantined` with the exact
refusal and sends one durable project-supervisor notice in the same leased
transaction. It then removes the heap entry and processes later deadlines.
Unavailable infrastructure, lost leases and unknown errors remain retryable and
must not be marked fired or permanently cancelled.

`aq wait cancel <wait-id>` also lets a live project supervisor or local operator
cancel an orphaned task wait before escalation. A failed owner fence is required;
the transaction retires only that wait, its deadlines, edges, incidents and
continuations. It preserves resolved outcomes, any replacement claim and its
task-level reservations, and never pauses or wakes the task. Worker cancellation
continues to require the exact live holder.

## Resume recovery correction — 2026-10-21

Continuation maintenance runs independently of the source listener and main
orchestration cycle. Each queued/reserved/launching continuation has at most one
local dispatch attempt in flight; a slow or refused attempt must not prevent
other continuations from requesting admission. Failed commands are logged and
retried through the existing admission, generation and task-control fences.
Service shutdown cancels and joins outstanding attempts, retaining durable
reservation and launch state for restart recovery.

The listener renewal cadence is not a query timeout. Source batches and deadline
commands may use the full lease budget; cursor locking, expiration checks and
transaction rollback still fence expired leaders. Queries exceeding that budget
remain failures rather than authorizing stale writes.

`wait_policy.resume_stall_seconds` defaults to 300 seconds and supports project
overrides. A resolved, uncleared wait whose task remains RESUME_PENDING past that
age creates one durable project-supervisor notice per continuation generation.
The notice survives daemon restarts and does not reset the wait's age, cancel its
outcome, force admission or acknowledge delivery. Ordinary retries continue;
paused and acknowledged tasks do not generate notices.

An IN_PROGRESS owner or producer is not itself unavailable. Task sources retain
the published claim-generation fence; a changed generation or missing source can
still resolve an active owner's wait as source_unavailable. Dispatcher outcomes
record the unavailable observation's reason and revision for diagnosis, without
turning that outcome into producer success.

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
the chain. No schema or terminal-incident rewrite is needed. The project
supervisor approved this correction in `msg-e01f1f13c3c94afd88b7812f5800b5c6`,
replying to `msg-7ba5090e15db48e2b9c6d272abacb37f`. Allocation is serialized
under the current wait/deadline locks; decisions and resolution use the bound
`wait_escalations.escalation_id` and current expected generation. Concurrent
same-generation firing creates one incident; an old generation cannot decide
the new incident after extension.

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

## Scheduler maintenance lock scope — 2026-10-22

Scheduler sync discovers candidate owner identities without the graph lock, then
reconciles each owner in a separate transaction. Fresh admission observations,
wait receipts, deadline preservation, stale-row cleanup, initial age audit and
priority retraction commit together for that owner. The graph fence is released
between owners so slow per-item authorization cannot serialize an entire bridge
or scheduler pass against unrelated wait mutations. New candidates are picked up
on a later scan; cleanup always re-reads the current owner's rows under the fence.
An interrupted pass resumes from committed receipts without renewing deadlines.
The startup bridge retains its project and identity receipts and its local
single-flight lock; it does not acquire a project-wide database fence.

## Transaction and graph ownership

### Resume graph-lock fairness — 2026-10-24

The source listener commits one event, its dispatch effects and its cursor in
each graph transaction. It releases the fence between events even when the
backlog fits in one batch and no renewal interval has elapsed. A slow individual
event still uses the lease budget and rolls back atomically on failure.

Supervisor requeue joins the advisory-lock queue before reading or writing its
held continuation. Refusal and cancellation roll back all changes, including
the version and audit record; suspension generation is preserved on success as
well. Restart starts continuation maintenance alongside legacy bridge adoption,
so a blocked startup bridge cannot suppress RESUME_PENDING retries.

After external admission preparation, resume reservation joins PostgreSQL's
advisory-lock queue before reading its wait/task/continuation transaction state.
The listener's nonblocking acquisition cannot overtake a queued resume between
batches. Resume uses a 35-second acquisition budget, exceeding the listener's
30-second batch lease budget; unrelated background reconciliation retains its
short polling budget. An acquisition timeout remains `wait.graph_busy`, rolls
back its savepoint and restores the connection's prior `lock_timeout`. A successful
acquisition retains the same transaction-scoped graph fence through all reservation
revalidation and mutations. Generation, stopped-writer, task-control and admission
checks remain required; queue position supplies no authority to resume.

The lifecycle emits `wait.resume_graph_busy` with continuation id, suspension
generation and consecutive refusal count. Any other dispatch result resets that
generation's streak; continuations leaving the ready set discard their local
counter. The diagnostic is process-local and does not rewrite durable outcomes
or reset the wait's age.

### Short graph transactions — 2026-10-22

Resource subscription verification reads only the named admission constraint.
The initial scheduler/resume admission observation still evaluates all constraints;
verifying a subscription is not an admission grant. Each verification and
post-subscribe observation reads current owner, claim, project, profile and config
bindings, plus the SQL inputs for that constraint, on the caller's connection.
Frontier predicates, workspace capacity and fleet token aggregation are not
repeated for unrelated blockers. Token aggregation is omitted when neither a
global nor a project budget is configured. No constraint verdict is cached across
observations, transactions or passes; committed source changes remain visible.
Before acquiring the graph fence, the listener prepares Git progress only for
task waits matching its next source event (resource waits for an invalidation).
It rechecks the cursor under the lease lock and retries preparation if another
drain advanced it. The candidate set is not truncated to the first 200 unrelated
pending waits; dispatch still revalidates current source and owner rows.

The graph fence protects database revalidation and mutations. Git, filesystem,
provider/terminal operations, router artifact loading and observations using
other database connections run before acquiring it. An admission pass captures
external delivery identities first; `AdmissionReader` then checks those identities
and current task, project, profile, occupancy and workspace inputs on the owned
connection. Stale delivery evidence blocks admission. An unprepared admission
read under the fence refuses instead of doing external work.

Resume refreshes its Git prerequisites before capturing the admission pass.
The locked reservation revalidates that pass, including expiry and policy, so
newly loaded evidence can admit work without accepting expired observations.

Scheduler reconciliation discovers candidates first and commits one owner at a
time, including legacy age audit. It owns only scheduler-owned subscriptions;
task-owned resource waits retain their delivery owner. Cycle checks read the
reachable semantic component with the existing node budget rather than scanning
every dependency and wait in the fleet.

The caller audit covers these paths:

| Callers | Work outside the graph transaction | Revalidation inside |
| --- | --- | --- |
| scheduler, resume, listener, deadlines, supervisor decisions | router/delivery observations | current admission inputs and delivery snapshot identities |
| registration, atomic job wait, source dispatch | Git progress heads | task/session path and branch; comments and edits read fresh |
| hibernation advance | fallback checkpoint facts | continuation generation, claim, branch, session instance and summary |
| cold restart | provider stop proof, Git ancestry and checkout detach | all task/wait/continuation/workspace/session rows compared before retirement |
| raw wait queries, graph helpers, recovery, acknowledgement, human waits, cancellation, eviction, measurements, legacy bridge/projections | no external effects in fenced bodies | transaction-local SQL and transactional source/lifecycle callbacks |
| task deletion/archive | existing separate external-effect phase | existing fenced retirement transactions |

Cold restart keeps its per-task control and row locks during external cleanup,
then releases those row locks before acquiring the graph fence in the normal lock
order. Failed revalidation retains database ownership for an idempotent retry.
Escalation and routing helpers use the caller's connection when composing with a
wait transaction. Transactional adapter callbacks must not open another
connection or perform external I/O.

Every graph acquisition, including the listener's nonblocking acquisition, arms
one watchdog per transaction. After five seconds it logs `wait.graph_lock_slow`
with the acquisition caller and elapsed hold time, even while Python is awaiting
outside PostgreSQL. Commit and rollback cancel it; transaction-end timing also
catches event-loop stalls. The existing twenty-second acquisition refusal remains
retryable and does not authorize a stale write.


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
handler registration; combined task .14 owns the actual acknowledgement lifecycle and
fresh-continuation generation binding. Prime never acknowledges mail.

For an already parked continuation, verified stop/process/claim evidence permits
one sleeping-to-queued transition and HIBERNATED-to-RESUME_PENDING task change.
Global direction `msg-0aa6536c439d43e2a99d951a1f6b79b7`, relayed in
`msg-e01f1f13c3c94afd88b7812f5800b5c6`, folds the former .15/.16/.17 work into
combined task .14; those former leaves are archived obsolete. Combined .14 owns
hibernation races before parking, launch, fresh-session binding, held mail for
the old profile/session, ACK, sleep decisions and eviction. Dispatch refuses
pre-parking outcomes until that owner's transactional transition callback exists.
Shared-profile consumer scoping is not implemented by the in-place mail guard.
There is no reverse dependency on an archived leaf and no runtime enablement.
The current chain is 12 → 14 → {18, 19, 20} → 21 → 22. Global owns .22's operator
migration/config/default activation; .12 introduces no human engineering gate.

## Claim-8 verification — 2026-10-19

- Required acceptance: `test_wait_dispatch`, `test_escalation_commands`,
  `test_escalation_delivery`, `test_session_reconciler`, `test_handoffs` and
  `test_agent_waits`: 472 passed.
- Affected-area rerun: `test_wait_decisions`, `test_wait_holding`, `test_waits`,
  `test_message_delivery` and `test_event_schema_registry_validation`: 975 passed.
- Final focused checks after the equal-timestamp comment progress fix:
  `test_wait_decisions`, `test_wait_queries` and `test_wait_outcomes`: 135 passed,
  including 27 operational decision/loop tests.
- After supervisor feedback, the expanded `test_wait_decisions` passed all 28
  tests, including concurrent same-generation expiry and deciding generation 2
  after rejecting a stale generation-1 decision.
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
The supervisor retained the generation/ownership disposition in canonical
knowledge record `record:aa118354-9dff-451e-927e-de32526c1b43`; task comments are
implementation evidence, not worker knowledge-save receipts. Supervisor feedback
was consumed before close, and the requested same-generation concurrency and
generation-2 decision regressions were added without a production-code change.


## Candidate integration repair — 2026-10-20

Infrastructure holds use `source_kind=continuation`,
`source_identity=continuation:<id>:<generation>:<reason>` and the
same incident key, so a superseded generation cannot bind. Only the internal
wait transaction may create them, and they are delivered to the supervisor like
wait deadline incidents. The continuation must still be live, hibernated and
bound to the same task, wait, project, generation and branch;
allowed reasons cover stop, push, workspace and detach failures during suspension
or resume, exhausted resume launches, and a pause observed during launch. A
resolved wait may still have an active continuation whose infrastructure failure needs this incident. Ordinary wait deadline
incidents retain their generation binding described above.

A queued continuation records its first admission request before capacity is
available, then grants that reservation only after fresh admission succeeds.
Metrics distinguish condition wait from the interval between request and grant;
a pending request does not occupy a slot. Task deletion and archive share one
retirement path: pending waits are cancelled and cleared, deadlines and edges
retired, wait and hold incidents withdrawn, live continuations and open
reservations cancelled, and priority projections removed. Neither
`continuations.task_id` nor `admission_reservations.task_id` references `tasks`,
so suspension history and admission audit keep their task identity, and the
independent wait outcome audit is retained. Bulk hierarchy changes publish every changed dependency source
in the mutation transaction using a bounded number of database statements.
All wait feature flags remain disabled by default.

CI cache refreshes publish a source event only when the check's evidence changes;
refreshing observation/due timestamps alone updates the cache without another wake.
The comparison and cache write serialize per check, including concurrent first
observations. A transition back to an earlier result still publishes a new revision.

The listener retires contiguous batches of CI events without pending verified
subscriptions and pool invalidations without pending verified resource waits.
It checks subscriptions under the registration graph fence and commits dispatched
stamps with the leased cursor. It never skips past an event requiring dispatch.
Task retirement and provider recovery events retain their per-event side effects.
This same automatic drain repairs an existing irrelevant backlog on deployment;
no operator deletion or cursor jump is required. Relevant events retain fresh
observation, progress preparation, ownership fences and per-event rollback.


## Retry and repair of resume workspace preparation

Workspace preparation retries contention (`wait.graph_busy`), transport failures,
and OS busy/timeout errors up to three attempts, with durable 5/10 second backoff.
The queued continuation retains the same reservation, agent, staged checkout,
saved head and resolved source outcome. Reserved capacity remains counted during
backoff. Identity, dirty-checkout and other non-retryable failures hold immediately;
exhausted retries create the existing `resume_workspace_failed` incident.

A live owning supervisor or local operator can issue `wait_decide` with
`decision=requeue`, `wait_id`, `continuation_id`, `expected_generation` and a
nonblank note. The transaction repeats project, generation, stopped-writer,
branch and resolved-wait fences. Only workspace/launch resume holds are eligible;
suspension, pause and uncertain-stop holds require their own recovery. Requeue
preserves the source and checkout, records actor/reason/time in facts and an audit
event, and resets the retry budget. It does not acknowledge the outcome. The hold
incident is withdrawn when the fresh process receives prime, atomically with
outcome delivery; failed retries retain the incident.
