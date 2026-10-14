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

The admission leaf (`bold-zenith-11.7`) owns the authoritative admission reader/normalizer
and guarded service integration. The registry leaf supplies the injected generation-0
adapter seam. This ownership correction is authorized by supervisor message
`msg-5128861479914dc3b3a1f41fa700d5ad`; approved review bytes remain unchanged. The listener leaf
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

## Admission service and consumer dispatch

`SchedulerWaitService` installs `scheduler_wait_sync` at the existing wait-reconciler
cycle site. Both `v2_enabled` and `scheduler_waits_enabled` must be true; defaults
remain false. Its candidate list carries no trusted verdict: every task and typed
constraint is read again. `AdmissionReader` is the resource reader supplied to
`build_registry(resource_reader=...)` for listener and dispatcher verification.
It binds the actual task/project, repository, route/class/profile, configuration,
claim epoch and scheduler owner. The identity hashes those bindings; occupancy,
provider availability, backoff, budget and hold predicates produce opaque observation
revisions. A stale binding is unavailable, never satisfied. Pending launches and
reserved continuations occupy capacity; an in-place waiter remains busy and is also
reported as `evictable`.

The deadline-dispatch owner must call `SchedulerWaitService.deadline` for scheduler
`reservation` and `escalation` deadlines through the exact `wait_deadline_fire`
command hook. The temporary default hook refuses other lifecycle owners rather than
handling their deadlines. The listener owns discovery/leases; it must compose this
handler with its other deadline handlers before activation. Reconciliation preserves
the first admission deadline across configuration rebinding. Firing it creates one
reservation; its persisted second deadline creates one operational escalation.
Configuration reload and restart never extend either clock. Deadline replay is inert.

Reservations match the current repository, profile, class, route and claim epoch and
reuse every ordinary frontier/routing/hold/backoff/Git-admission predicate. They order
by effective priority then FIFO. The ordinary task transition cancels unconsumed
reservations in the same SQL statement; a successful claim consumes only its selected
reservation in that transaction. Disabled selection retains base-priority ordering and
adds no claim round trips. Resume owns the continuation reservation scope (including
`profile_id`) and `new_session_id`; occupancy counts its reservation before launch,
then the live new session once, never the stopped origin session.

The service does not discover deadlines, dispatch source outcomes, launch continuations
or evict sessions. Those listener/dispatcher/resume/eviction integration checks remain
required in their owning leaves before activation. No installed policy is changed by
this implementation.
