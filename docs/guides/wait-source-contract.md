# Wait source writer and consumer contract

Source rows are committed unconditionally, including while wait activation is off.
A notification is a prompt to drain the outbox, never satisfaction evidence. Writers
use their owning transaction; a rollback removes both the event and its NOTIFY.
Replaying `(source_key, source_generation, source_revision)` inserts nothing.

| Condition | Canonical source identity | Generation / owning writer |
| --- | --- | --- |
| task | `task:<task>` | task claim epoch / status writers |
| dependency | `dependency:<dependent>:<upstream>:<type>` | upstream claim epoch / edge and upstream status/delivery writers |
| job | `job:<job>` | attempt 1; retry creates a new job / terminal result writer |
| message | `message:<thread>` | 0 / all message insert paths |
| human task comment | `human:task_comment:<task>` | task claim epoch / `add_task_comment`, user author only |
| review | `review:<review>` | document revision / guarded review transition |
| batch | `batch:<batch>` | baseline generation / lifecycle writers and integration outbox |
| playbook condition | `playbook_condition:<run>:<wait>` | registered wait snapshot version / all five wait resolution paths |
| CI check | `ci_check:<repository>:<exact-sha>:<check-name>` | 0 / successful new `ExactChecks` observation upsert |
| timer | the wait's persisted source identity | the wait's persisted generation / due-deadline CAS |
| resource | `resource:<blocker>:<opaque-admission-identity>` | 0 / the explicit invalidation bridge below |

`ci_run` and pull-request events belong to the watcher leaf. They are not covered by
the existing exact-check writer. The listener/timer leaf owns non-due deadlines.
These are consumer/producer seams, not claims that those runtimes are enabled.

## Resource matching decision

Supervisor decision `msg-ea555d920d0d41eea8a556871fdf2090` explicitly corrects the
implementation spec section 9.1 scheduler-cycle-generation wording for this bounded
implementation. The current scheduler and `AdmissionSnapshot` have no cycle counter;
external resource mutations also occur outside scheduler cycles. Resource streams
and subscriptions therefore use **generation 0**. Cycle/observation/mutation identity
is the opaque revision, not an incarnation. A revision change permits reobservation;
it is not a permanent equality requirement against the registration snapshot.
Approved review/spec bytes remain unchanged. This correction does not imply full-plan
runtime acceptance. A future real incarnation requires coordinated producer/subscriber
fencing, not a timestamp, truncated hash or per-event generation.

Canonical blockers are only `slot`, `workspace`, `provider`, `backoff`, `budget`,
`policy_hold`, `branch_owner`, and `batch_hold`. An identity comes from the admission
reader's normalization and contains whatever scope/instance/config/owner binding that
reader needs. A pool profile or workspace/provider label is a **producer** identity;
it is never guessed to be that opaque admission identity.

Producer events use `resource_invalidation:<producer>:<mutation-identity>`, generation
0. They are durable invalidations, not resource condition kinds. They carry no config
values, secrets, message bodies or satisfaction verdicts.

| Actual transition | Producer identity and revision | Candidate typed blockers |
| --- | --- | --- |
| pool measurement | fleet profile / measurement timestamp revision | all eight; the new admission snapshot also invalidates time-dependent predicates |
| workspace release | actual workspace, project scoped / mutation UUID | slot, workspace, branch_owner |
| provider state transition | provider, fleet scoped / committed transition generation revision | slot, provider, backoff, budget |
| completed config reload | global / reload UUID retained on replay | all eight |

The broad pool/config mappings deliberately allow extra **reobservations**, never
extra satisfactions. In particular, a pool cycle B can reobserve a wait registered in
cycle A with the same generation 0. No producer infers a typed condition key from a
profile alone. A project-scoped workspace invalidation cannot select foreign-project
subscriptions; global invalidations require each reader to check its own scope.

## Bridge and remaining consumer seams

`resource_invalidation_candidates(conn, committed_event, after_id='', limit=200)`
authenticates the event's sequence, key, generation, revision and payload against the
outbox. It pages pending verified resource subscriptions, checking their persisted
canonical key, generation, watermark and verified project/owner/session binding.
Continue paging with the last returned wait id until the page is empty.

`reobserve_resource_candidates(conn, candidates, verify=...)` invokes the consumer's
fresh typed verifier and returns only satisfied evidence with matching canonical
source/generation/project/owner/session/claim binding and a fresh result pointer.
It writes no outcome and sends no wake. The callback must check the **current** owner
fences and call `registry.verify(conn, Condition.from_record(row['condition']),
service_scope)` with the actual admission reader. Its snapshot must reobserve owner,
project, config and predicate freshness, not copy the event payload's claim.

The registry leaf owns the authoritative admission reader/normalizer and generation-0
adapter snapshot; this branch does not invent its identity fields. The listener leaf
must route `resource_invalidation:` rows through this bridge instead of only doing
exact-key matching, page all candidates, and preserve its normal cursor/lease protocol.
The dispatcher leaf must perform its guarded owner/source lock, typed reobservation
and outcome CAS/delivery transaction; a candidate or producer outcome_ref cannot
satisfy a wait. These consumer seams must be tested in their owning leaves before
runtime activation. Writer/bridge fixtures here exercise actual pool/workspace/provider/
config transitions, durable subscription bindings and authoritative reobservation;
they do not claim that the absent listener/dispatcher is running.

## Watermark, locks and retention

Call `await db.wait_source_watermark(source_key=None)` on its dedicated connection
**before** acquiring graph/owner/source locks or writing source events. There is no
caller-connection parameter. An already-mutating caller must pass a bound prefetched
before its transaction. Writers hold shared transaction advisory lock `0x415157414954`.
The short reader exclusively try-locks it without queueing, then reads the committed
maximum and durable source cursor floor under explicit connection-level READ COMMITTED.
Release the fence before dispatch/registration locks; registration post-observes its
source in its own transaction. A bare MAX or an old REPEATABLE READ snapshot can skip
an earlier allocated sequence that commits late.

Retention removes at most 1000 dispatched rows strictly older than seven days and
at or below the durable source cursor. Pending/unread rows remain. The cursor supplies
a floor after all old rows are purged and a reader restarts with a fresh pool.

## Claim frontier compatibility

The wait-schema prerequisite replaced the frontier indexes' base `priority` keys
with `effective_priority`. The inactive claim selector still orders by base priority;
that mismatch sorted every probe, including the urgent-repair and affinity probes,
and scanned the full ordinary frontier. Revision `a00000000098` adds the original
base-priority index shapes under `idx_tasks_base_claim_frontier*`, retaining the
effective-priority indexes. This preserves current claim ordering, repair priority
and all admission predicates without activating priority propagation or reservations.
The priority/admission owner can retire these compatibility indexes only after its
runtime adopts and maintains effective priority and verifies the same strict guards.

## Listener and deadline consumer composition

`WaitService` in `src/waits/service.py` is installed next to the review notifier in
`main.py`, cancelled on shutdown, and recovery-ticked by the orchestrator. It never
connects, leases, dispatches or fires while `wait_policy.v2_enabled` is false. No
activation defaults change here. v1 reconciliation remains intact until cutover.

The dedicated asyncpg listener subscribes to both channels before taking the fenced
watermark. The `source` cursor's 30-second lease elects **both** source and timer
work, renewed every ten seconds. Each batch locks that cursor, commits dispatch and
progress together, and checks the unexpired lease before commit. A competing daemon
cannot take over a locked batch; a failed or expired batch rolls back. A sixty-second
safety drain and recovery tick recover missing notifications. Disconnects rebuild the
connection with exponential backoff capped at thirty seconds. Releasing a lease is
conditional on its unique owner, so a retiring instance cannot release its replacement.
Source retention keeps the persisted cursor floor even after every old row is purged.

The dispatcher owner installs `orch.wait_dispatcher`, whose async
`commit(conn, wait_row, committed_event, *, evidence=None)` returns a success dictionary
for both a committed outcome and an unsatisfied no-op. It must use the supplied batch
transaction, repeat owner/source fences and fresh predicate observation, and own outcome
CAS and delivery. A refusal or exception rolls back the batch; missing dispatchers leave
matching rows unread. Resource invalidations use the canonical paged bridge and fresh
`registry.verify` with the configured admission reader. The resource generation remains
zero and revisions remain opaque; producer events never establish satisfaction.

`WaitTimerDriver` rebuilds its heap from unfired, uncancelled rows on startup, notification
and recovery. It invokes the contracted, service-only `wait_deadline_fire` command.
The command locks the leader cursor, graph, task, wait, continuation, reservation and
deadline, and rejects fired,
cancelled, resolved, not-yet-due or superseded generations. Firing a `due` deadline writes
its timer source event in the same transaction. Only the dispatcher can resolve/wake it.
Registration emits `aq_wait_deadline` transactionally after inserting each deadline;
future deadline writers must do the same (a missed hint is recovered by heap rebuild).

The downstream lifecycle owners supply `orch.wait_deadline_handlers[deadline_type]`
for `escalation`, `ceiling`, `transition` and `reservation`. Each async callback receives
`(conn, deadline_row, wait_row, *, now, handler)` and returns a success dictionary;
all effects must use that transaction. The callback implements the approved effect:
escalate without a wake; invoke the sleep decision with ceiling reason; hold an overdue
stop/launch; or mark a reservation overdue. It must fence its continuation/reservation
and owner before mutation. Escalation callbacks must reobserve the authoritative source
first: completion at or before the deadline, even when processed late, takes precedence
over escalation. The driver marks `fired_at` only after the effect succeeds.
Missing handlers leave deadlines unfired; failed effects roll back their writes and the
fire together. These seams enable the downstream leaves without activating unfinished
hibernation, escalation, admission or delivery behavior. The absence of a `CANCELLED`
task status is unchanged; downstream handlers must use actual settlement states.
