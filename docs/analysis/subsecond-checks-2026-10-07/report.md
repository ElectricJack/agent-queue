# Sub-second AQ checks: implementation audit and measurement evidence

Task: `calm-current-21`. Source: `259c0f56ecf9a9d9e3c0a18d31df9b67264eea3f`.
Date: 2026-10-07. Research only; no production behavior changed.
The assigned `deep-low-codex` rung maps to `gpt-6-astra` in the installed
`vault/intelligence-classes/deep-low.md`, read during this audit. Task routing
reports the strict profile preference honored; its `observed_models` field was
empty. The configured mapping is evidence of intent, not independent turn-level
model telemetry.

## Finding and scope

Prioritize delivery selection, especially root selection and cold/moved-input
proofs. Do not optimize model choice: no model call appears in this selection
chain. A universal “every check completes in one second” promise would be false.
A bounded routine decision can return an explicit pending/unknown result in that
time while a separately measured worker gathers exact evidence. Publication,
fresh remote observation, first Git proof, validation, and a whole fleet sweep
are different operations and must retain their own timing and correctness rules.

There are two distinct costs: unnecessary repeated work and necessary expensive
proof. Removing the first is useful but insufficient. Existing immutable caches
already make one warm proof very cheap; our local fixture demonstrates this.
Telemetry also demonstrates severe long-tail selection latency, not just a slow
fetch. Neither measurement establishes the production SQL-versus-Git split.

This audit inventories all 963 Python modules under `src` syntactically and
records 1,427 check-like or await-in-loop functions in [inventory.json](inventory.json).
That is repository-wide discovery, **not** a claim to have manually verified every
function or dynamically covered every backend/configuration. The call-chain
inventory below is the manually inspected set relevant to the requested checks.
Generated clients, frontend rendering, migration/startup work, and LLM execution
are not claimed as measured recurring-check costs. Dynamic plugin code outside
this repository is not available to this audit. Remaining coverage is explicitly
assigned to the benchmark plan rather than declared complete by inference.

Orientation: `profile.md`, `docs/contributing/repo-map.md`, the current
[Git-first train runbook](../../guides/git-first-train-runbook.md),
[event-loop responsiveness spec](../../specs/design/event-loop-responsiveness.md),
[completion provenance spec](../../specs/design/git-completion-provenance.md),
[train simplification record](../../superpowers/specs/2026-10-02-integration-train-simplification-review.md),
and the implementation. Historical design records are labeled as historical;
where they describe the former subject-runtime path, they are not evidence that
that path is active today. `profile.md` also contains old memory/SQLite-era
rationale; PostgreSQL and the actual query/engine code govern this audit.

## Evidence classes and measured results

**Reported by task, not independently reproduced:** selection 197.46s versus
fetch 5.18s; later 175.38s versus 7.88s; 55 targets / 47 running; scheduler await
chains in Git/provenance and a PostgreSQL transaction. Suspended await chains
identify what a task waits for, not CPU ownership or the database blocker.

**Measured read-only runtime telemetry:** [telemetry.json](telemetry.json) retains
sanitized timing records from a bounded 1,500,000-byte tail of `agent-queue.log`,
its sample hash, timestamps and summary. Window: 18:06:14.725792–18:53:16.936484 UTC.
The sample contains 1,159 completed visit records, including only eight main/root
records. Nearest-rank quantiles, seconds:

| Stage | n | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| Complete visit | 1159 | 12.215 | 346.204 | 427.674 | 676.946 |
| Selection | 1159 | 0.938 | 332.865 | 409.936 | 666.272 |
| Fetch snapshot | 1159 | 5.433 | 10.201 | 12.655 | 15.066 |
| Epic settlement | 1064 | 1.191 | 7.843 | 14.724 | 26.244 |
| Root selection only | 8 | 257.480 | 666.272 | 666.272 | 666.272 |

There were 87 root-discovery-probe warnings and 98 hierarchy-frontier warnings.
These are occurrence counts, not unique incidents. Recent individual nonroot
selections around 0.4s coexist with hundreds-of-seconds tails. Root n=8 is too
small for an SLO estimate. Completed-only sampling omits running/cancelled visits
and is biased; mixed cache states, deployments and host load are uncontrolled.
Source checkout SHA is recorded, but the daemon's deployed SHA was not established.
Do not attribute apparent improvements to this audit or any sibling task.
Stage wall time includes I/O and scheduling waits; it is not CPU time, and
concurrent visits' times cannot be summed as machine utilization. Lane-setup
substage times overlap their enclosing stage.

**Measured disposable local Git fixture:** [profile_proofs.py](profile_proofs.py),
[proofs.json](proofs.json). 200 exact completion identities, two commits, one
shared source, 55-target-equivalent repeated root reads, serial execution,
no network and no database. Metadata objects are real Git commits; the actual
`GitTruthSnapshot.is_delivered` and `GitProvenance` implementations run. Setup
is excluded. Inherited daemon Git identity is preserved; no operator repository,
configuration or database is modified.

| Case | repetitions | time per 200 proofs | Git processes |
|---|---:|---:|---:|
| Cold metadata/ancestry | 1 | 4.319s | 1402 |
| Warm exact proofs | 100 | p95 0.006614s; p99 0.007667s | 0 total |
| Two root scans × 55 targets, warm | 110 | p95 0.004947s; p99 0.006050s | 0 total |
| Changed completion generation | 1 proof | unknown, missing provenance | 0 |
| Cold diagnostic cache-only | 1 proof | unknown, proof unavailable | 0 |

The 55 targets are a repetition model, not 55 concurrent train visits. The
fixture matches the known target scale and 200-member cap, not production
history or unknown completed-origin cardinality. Shared source/short history
makes ancestry easy; it does **not** estimate patch-search, distinct-source,
cache-thrash, database or GitHub performance. A single cold run has no meaningful
p95/p99 confidence even though the JSON summary mechanically includes them.
Warm counts imply full-proof cache hits after priming; these are not instrumented
production cache-hit ratios. Counted Git processes are at the actual async
result subprocess seam; the measured proof path uses that seam.

Host: AMD Ryzen 9 5900X, 24 logical/affinity CPUs, Python 3.12.3,
WSL2 Linux `6.18.33.2-microsoft-standard-WSL2`; load averages at telemetry capture
14.63/14.54/12.24. Storage latency, memory pressure, PostgreSQL version/settings,
and daemon process affinity were not measured. This is a busy-host exploratory
profile, not quiet-host acceptance certification.

## Delivery call chains and findings

### D1. Selection repeats repository-wide scans before target filtering

[IntegrationTrain._visit_target](../../../src/integration/train.py#L704)
(704–720) fetches then calls
[DatabaseBatches.open_batch](../../../src/integration/train_sources.py#L448)
(448–526). It calls `_candidate_ids` at 459, then `pending` at 481, whose first
step calls `_candidate_ids` again at 685.
[_candidate_ids](../../../src/integration/train_sources.py#L657) (657–675) does:

1. `_pending_tasks(limit=None)` over all completed live origins in the repository;
2. `delivered` → `project_delivered` against the root;
3. `delivery_targets(reduced=True)` and only then filters this target;
4. applies the 200-member cap after these costs.

Static multiplicity: T active targets × two calls × N repository candidates gives
O(TN) request construction/root-proof lookups before target narrowing, even for
empty lanes. At T=55, N=200 this is 22,000 root candidate considerations per wave,
not 22,000 Git subprocesses. N can exceed 200: the limit is applied late.

Static SQL count for one nonempty all-live path with undelivered routed IDs:
`_pending_tasks` two statements (146–165), default branch one (826–834),
`load_delivery_requests(reduced=True)` four (331–385), legacy delivery one
(218–225), reduced target routing six (186–226). Thus 14 statements per candidate
pass, 28 per normal `open_batch`, excluding stack/supersession/current/pending
work; archive misses add a statement and an over-limit window adds another.
When everything is delivered, routing short-circuits. This is a **static count**,
not production query telemetry. SQL predicates may scan more rows than returned.

Recommendation: compute one visit-owned candidate snapshot after DB identity
capture; route first, retain prerequisites outside the member window, evaluate
root containment only for relevant inputs; pass results to pending. Do not move
the 200 limit ahead of dependency handling or drop root-delivered filtering:
`pending` 795–824 explicitly protects prerequisites outside the window. Stack
refresh/supersession can mutate inputs (467–477): if they change an origin or
completion, discard the selection snapshot and return/recompute. A request-local
memo is safer initially than a cross-tick “eligible” cache.

### D2. Warm caches exist; cold historical proof is still expensive

[GitTruth._completion](../../../src/integration/git_truth.py#L217) validates once
per pinned metadata OID; `_record_key` 211–215 includes store, repository id/URL,
completion identity and both observed metadata refs. `_proofs` in `is_delivered`
(536–639) additionally binds the full DeliveryRequest, source base and target OID.
The bounded LRU default is 2,048 per cache (112–137); `_pair` 196–202 binds source
and target OIDs. Therefore D1 is not evidence that warm visits repeat all Git.

On misses, [GitProvenance._read/_validate](../../../src/integration/provenance.py#L128)
(128–199) validate object, parent and tree identity through multiple subprocesses.
The fixture's seven metadata processes per distinct identity plus shared exact
object/ancestry explain 1,402 processes. There is no in-flight dedup in `_completion`
or full-proof population: concurrent readers can miss before the first fills.
This is a plausible cold stampede, **not measured production attribution**.

[is_delivered](../../../src/integration/git_truth.py#L536) proceeds from ancestry
through AQ-Source trailer, whole-source patch, tree history, and no-op merge.
[_whole_patch](../../../src/integration/git_truth.py#L410) (410–492) is potentially
quadratic in relevant history ranges, already bounded to 150 patch probes and
source paths/base history. Each probe has more than one subprocess. `_equal_tree`
reads tree history, and merge-noop may still be costly. The cap bounds comparisons,
not elapsed time or subprocess queueing. Target movement invalidates pair/proof
keys; request-version churn and >2,048 active keys can evict warm entries.

Recommendation: instrument miss reasons first; single-flight immutable work by
existing exact key, with bounded memory/queue and cancellation semantics. Stage
validated objects via a bounded Git batch reader if measurements justify it;
preserve the exact parent/tree/identity validation. Cache whole-source immutable
patch ingredients by repo/source/base/target-attribute-tree and algorithm version,
not by branch or task id alone. Do not extend error/unknown lifetime into success.
Do not simply raise the LRU limit or remove patch proof: measure cardinality,
working-set churn, miss ratio and retained bytes first. Reverts and rewritten
history are exactly why weaker “same current branch/tree” shortcuts are unsafe.

### D3. Discovery timeouts retain stale targets and can repeat expensive probes

[DatabaseTargets._project_targets](../../../src/integration/train_sources.py#L305)
(305–345) gathers all pending roots/routes/epics/open batches. A root probe has
five seconds for snapshot **and** `project_delivered`; timeout keeps `delivered`
empty and consequently retains owed routes. This is conservative and correct,
but the runtime fetch p50 alone is 5.43s. The two samples are not paired, so this
is a plausible explanation, not proof that every timeout is fetch-bound.
The 87 warnings confirm repeated failed probes. No automatic origin retirement
is justified by timeout. Unknown evidence cannot prove a target is stale.

[IntegrationTrain.tick](../../../src/integration/train.py#L497) (497–524) awaits
discovery before scheduling visits and starts one task per idle target without
an explicit global/repository visit semaphore. Existing per-target single-flight,
not-before cadence and repository rate-limit pause (652–691) are valuable;
55 targets can nevertheless compete for one repository and DB pool.

Recommendation: discovery reads a versioned background root-proof index and a
cheap open-batch/origin delta. Misses retain work and enqueue deduplicated probes;
report reason/age. Proven delivered identities may suppress routes, but current
open batches and promotions remain targets. Add fair bounded admission (initial
experiment: two evidence workers per repo, eight globally; one fetch per repo)
with priority for root progress and aged targets. These numbers are tunable
starting hypotheses, not demonstrated optima. Track queue age and throughput;
reducing concurrency can worsen starvation if fairness is omitted.

### D4. Pending-member guards have N+1 database reads

[DatabaseBatches.pending](../../../src/integration/train_sources.py#L677)
(677–824) already batches origins, dependencies, repairs and requests. For each
undelivered member it calls `stacks.current(task_id)` (742), then a source-bound
`current` (781), then `source_contains_stack` (782).
[StackedBranches.current/source_contains_stack](../../../src/integration/stacked_branches.py#L557)
(557–629) each read the origin; even an unstacked candidate can cause three
origin queries/checkouts. At 200 admitted non-epic unstacked members that is up
to 600 extra origin reads, before PR checks; failures short-circuit. Stack-bearing
members additionally load project, prerequisite identities and checkpoint.

Recommendation: carry a bulk origin/stack projection into a pure preflight guard;
retain authoritative DB identity rereads at publication. One first-stage check
must not replace the later source-bound check without preserving its semantics.
Batch `_admission_times` (601–641) too: it performs insert/select-for-update per
member in one transaction, with updates on invalid identities; bulk upsert and
read can shorten lock holding without changing admitted-at reset behavior.

### D5. Epic readiness can hold a connection while awaiting Git and another reader

`pending` opens a connection at 752 and invokes
[_epic_current_on](../../../src/integration/train_sources.py#L961) (961–1017).
It reads graph/checks and awaits
[EpicReadinessEvaluator.evaluate](../../../src/integration/epics.py#L246)
(246–316), which recursively proves children, reads a Git tree at 294 and invokes
`TreeReviews.verdict` at 304. Thus a DB checkout can outlive the query work and
nested readers can need another connection. `current_on` (318–337) additionally
checks remote freshness and reevaluates; `complete_on` takes a hierarchy lock.
This establishes the risk mechanism; pool saturation/deadlock was not measured.

Recommendation: collect graph/check/review inputs on one short connection,
release it for Git, then revalidate graph digest/current completion/holds under
the writer lock. Never move publication authorization out of its fence merely
to shorten timing. Instrument connection hold time and nested acquisition before
increasing pool size.

### D6. Fetch sharing has different behavior on train and observer paths

Train lane snapshot calls `GitTruth.snapshot` directly
([train_sources.py](../../../src/integration/train_sources.py#L1448), 1448–1453).
`GitTruth.snapshot` (139–180) can share only a fetch started after caller arrival,
with a second fetch for arrivals during the first. This preserves its freshness
contract; it is not a TTL cache.

[DeliveryObserver._snapshot](../../../src/integration/delivery_observer.py#L567)
(567–636) checks its read-only 30s cache before taking `_fetch_lock(path)` and then
calls `truth.snapshot` while holding that outer lock. Sequential lock arrivals
therefore do not overlap inside GitTruth's sharing machinery; waiting cache
readers also do not recheck the cache after acquiring the lock. This is a static
serialization/duplicate-fetch opportunity, conditional on actual caller mix.
Train-direct calls do not have this same outer-lock chain.

Recommendation: use one coordinator with an arrival generation captured before
waiting; fresh writers require a fetch whose start satisfies their own freshness
contract, while advisory readers may reuse a stamped snapshot. Never allow an
arbitrary TTL observation to authorize a claim/publish. Transport retry/backoff,
repository identity verification and expected-old ref checks remain intact.

## Repository-wide recurring-check inventory

References below name the code inspected and the operation's resource footprint.
They are static observations unless linked to measurements above. N means relevant
rows, P projects, S sessions, R recipients, E dependencies, T targets.

| Area and call chain | Implementation evidence | Cost / existing guard / action |
|---|---|---|
| Scheduler cascade → gate sweep → promotion → routing → schedule/pools → housekeeping | `orchestrator/core.py:2954–3185`, `run_one_cycle` body; `_schedule:3799–4032` | Serial awaits add wall time; exception isolation does not isolate latency. Branch materialization, gate GitHub checks, and housekeeping can postpone later steps. Preserve promotion order, move slow evidence to owned background collectors. |
| Scheduler state assembly | `core.py:_schedule:3832–3996` | Active tasks, hierarchy/admission, agents/sessions/profiles/workspaces; per-project token usage and workspace count, per-busy-agent task reads. O(P+S) query opportunities. Share one cycle projection where identity rules allow; do not claim pure `scheduler.py` is the I/O bottleneck. |
| Pool sizing / status | `orchestrator/pools.py:_measure_pools:223–405`; `commands/ops_commands.py:_cmd_pool_status:361–420` | Per-project admission/counts/capacity; display reruns fleet measurement and inspects sessions even with a project view filter. Cached-only Git still leaves DB/tmux work. Use a stamped background display projection; preserve fleet-wide capacity semantics. |
| Hierarchy frontier | `integration/delivery_observer.py:hierarchy_frontier_modes:417–520` | Projects evaluated sequentially with 60s normal / 10s display timeouts; cycle shares results between scheduler and pools. Boundaries are not subsecond. Cache-only display misses correctly withhold proof; don't replace unknown with “not ready” certainty. |
| Development admission | `integration/admission.py:observe_admission:341–429`, `structural_candidates:432`; `AdmissionSnapshot.matches:300–338` | Batched DB inputs, then serial per-scope remote observation and final input/freshness checks; structural paging does not bound all work. Reuse immutable evidence, retain final matches/locks and fail-closed moving-target behavior. |
| Claim handler → observation → atomic selection → preparation | `commands/claim_commands.py:_cmd_task_claim:253–409`, `_attempt_claim_once:557–786`; `database/queries/claim_queries.py:select_ready_for_profile:718–841` | Git admission before transaction; session/task CAS and SKIP LOCKED protect ownership. Two ordered queries already replace affinity CASE sort. Long-poll 60s and worktree preparation are not routine check service time. Event registration precedes attempt to prevent lost wakeups. |
| Claim activation / freshness | `claim_queries.py:activate_claim:1052–1160`; `delivery_observer.py:DeliveryView.verified_on:295–335` | Rechecks mutable claim/input identity; per-task source-base query in verified view is a batching candidate. Do not cache the winning claim or let read projections grant ownership. |
| Provenance / delivery | D1–D6; `integration/delivery_truth.py:load_delivery_requests:331–504` | Reduced request loading already batches queries and skips legacy parent machinery. Metadata/proofs have bounded immutable caches. More batching must keep archived/current-generation distinction. |
| Git processes / shared repo locks | `git/manager.py:_arun:668–739`, `arun_git_result:741–824`, `arepository_transaction:826–833`, `afetch_origin:1919` | Async child process avoids loop blocking but still incurs spawn/queue/disk cost. Serialized mutation commands and object borrowing exist. Instrument wait, run, bytes and cancellation separately; don't replace with synchronous subprocesses. |
| Branch publication | `integration/lock.py:lock_on:85–109`, `fenced_push:279–325` | PostgreSQL advisory and row exclusion deliberately span authorized remote write/readback. Slow lock acquisition can consume connections. Keep exact expected-old CAS/lease and ambiguous outcome reconciliation. Queued decisions should return pending instead of waiting for this section. |
| CI / GitHub / PR admission | `integration/checks.py:ExactChecks.read:467`, `refresh:504`, `refresh_if_due:530`; `integration/ci.py:_hosted_listing:1107–1120`; `train_sources.py:pending:785–793`, `_pr_checks:1778` | Per-member PR admission can include remote work. Exact check rows, refresh cadence and request-scope hosted-list cache already exist; provider observation occurs outside DB transaction. Key refresh by exact head, producer, required version, attempt and policy; pending rerun must supersede old green. |
| GitHub authentication / transport | `git/github.py:bind_repository:316`, `run_read:482`, `run_write:534`; `git/github_cli.py` | Remote latency/rate limits not controllable below1s. Share safe reads with identity/trust separation; do not reuse read auth to authorize writes. Train already has repository-level rate-limit pauses. |
| Message delivery / timeout synthesis | `messages/delivery.py:run_delivery_pass:83–188`, `check_reply_timeouts:190–316` | One recipient query plus one pending query per recipient, sequential activity/start/nudge; after nudge one message is CAS-marked. Busy recipients stay pending. Reply timeout checks query replies and may read transcript per candidate. Batch lookups and bound recipient work; retain one verified terminal notification and idempotent delivery semantics. |
| Session liveness and wakeups | `sessions/reconciler.py:tick:330–353`, `_step_observe:498–531`; `messages/session_lens.py`; `core.py:_reconcile_sessions:3590–3636` | Serial session steps; provider last_activity per live row and conditional activity writes. Existing harness progress caches and offloaded transcripts prevent some repeated parsing. Coalesce per-session observations and budget tmux work; unknown liveness is not death. |
| Durable waits | `agent_waits.py:AgentWaitReconciler.tick:196`; `commands/wait_commands.py:_cmd_reconcile_agent_waits:183`; `database/queries/agent_wait_queries.py` | Command-owned durable reconciliation plus producer reads; use bounded scans, indexes and event-triggered wakeups with periodic catch-up. A satisfied wait is not proof its producer succeeded. |
| Event bus / timers | `event_bus.py:emit:125–157`, `waiter:172–211`; `timer_service.py:tick:384–465` | Emit awaits handlers sequentially. Timer emissions therefore include downstream latency, and `_save_state` follows each fire. Optimize handler ownership/coalescing without losing durable replay or wait registration ordering. |
| Plugin cron | `plugins/registry.py:tick_cron:1341–1387`, `_run_cron_safe:1389–1403`; `schedule.py:matches_schedule:80` | Already creates background tasks and skips overlapping same job. Do not propose “make cron async” as missing. Due-expression evaluation, many distinct jobs, CPU-bound plugin code and shutdown/circuit behavior still need budgets. |
| API / status / health / graph | `commands/project_commands.py:_cmd_get_status:27–61`; `commands/task_commands.py:5419–5424`; `api/health_monitor.py:52–73`; `orchestrator/layout_step.py` | get_status loads tasks to count; task diagnostics request cached-only proof; health already background-cached; layout is scheduled separately. Add bounded pagination/aggregate counts and display snapshots where actual route timing warrants it, not duplicate a health cache. |
| DB shared pool | `database/engine.py:ObservedQueuePool:98–124`, `_install_query_observer:127–145`, `create_engine:150–204`; `queries/transaction_queries.py:immediate:39–52` | Pool defaults include 30s acquisition bound and overflow equal to pool_size; checkout metric includes new-connection setup. Immediate is ordinary read-committed transaction, not a global SQLite lock. Query timing alone cannot distinguish server lock from execution. |
| Provider/routing/resource checks | `providers/availability.py`, `routing/readiness.py`, `orchestrator/route_needed.py`, `resources/`, `metrics/` (function index in inventory) | Provider policy evidence and host probes influence admission but not delivery proof semantics. Need event-loop lag/resource queue attribution before changing concurrency; no measured dominance established here. |
| Sweep/watch/recovery work | `core.py:3152–3185,3457–3590`; `vault_watcher.py`; `workspace_spec_watcher.py`; `integration/service.py:tick:110,_bounded:163` | Rate limits, scheduled watcher checks and bounded background integration already exist. Synchronous log cleanup/filesystem walks can still consume loop time; watchdog evidence must identify a live offender before rewriting them. |

## Precise service objectives and measurement contract

A **routine check** is one bounded decision for a named subject and captured input
version: `{state, reason, input_identity, observed_at, evidence_age, refresh_id}`.
Examples: one target's next-action eligibility, one already-prepared claim
admission decision, one exact-head CI status read, one recipient's delivery
eligibility, one status snapshot read. Start at request/queue acceptance, end when
that decision is available; include local queue wait, DB acquisition, decoding,
and serialization. Track ASGI dispatch latency separately from socket/loop delay.

Exclude intentional long-poll idle time only in a separately labeled active-service
metric; retain total caller latency as well. Fleet tick duration, time-to-delivery,
claim preparation, cold clone/fetch, remote CI completion, review approval and
validation are never silently renamed “checks” or omitted from user-visible SLIs.
A quick pending answer is not successful evidence gathering: measure pending age,
refresh completion latency, starvation, and end-to-end delivery independently.

Proposed warm objectives (not achieved claims): p95 ≤500ms and p99 <1,000ms at the
stated 55-target scale, with 47 concurrently requested visits, 200 selected members,
5,000 live tasks and explicit completed-origin/history/metadata cardinalities.
Use the same 5900X-class host, record actual CPU/memory/storage/DB settings, and
repeat both quiet and representative busy load. 5,000 tasks is a proposed test
fixture, not a measurement of production. Include 2,049+ proof keys to cross the
current LRU boundary and 1,000 historical completed origins to test late filtering.

Budget decomposition per routine decision: queue ≤100ms, pool acquisition ≤100ms,
SQL+materialization ≤150ms, pure decision ≤50ms, serialization ≤50ms; 450ms planned,
50ms p95 headroom and 500ms p99 contingency. These are component budgets, not a
mathematical guarantee that percentiles add. Enforce a total deadline; missing
proof returns unknown/pending with durable refresh ownership. Normal warm display
checks should require zero remote requests and zero Git subprocesses. Selection
query count should be bounded by bulk projections plus member-independent joins,
not three origin queries per member; initial target ≤20 statements per visit,
verified per branch/fixture rather than imposed on publication.

Cannot honestly guarantee <1s: network freshness/credential negotiation, Git fetch
or large history traversal, fsync/DB lock under contention, process scheduling on
an overloaded host, cold cache/clone, external CI, human approval or executable
validation. Keep those outside the short decision API, not outside accounting.
Mutation authority continues to require current task/generation/holds/policy,
exact source/base/target, trusted exact-head checks, required tree review, branch
lease and remote expected-old comparison. Unavailable evidence never authorizes
an operation or retires owed work.

## Ranked recommendations and concrete coordination plan

Impact estimates below are directional/static opportunities, not measured speedups.

| Rank / owner coordination | Change | Impact / confidence | Effort / risk / dependency |
|---|---|---|---|
| 1 — `brisk-lantern-51` | Nested selection spans and one target-filtered visit projection; batch stack guards | Up to half duplicate candidate passes removed, plus O(TN) → repository projection + relevant-member work; high confidence in duplication, medium in wall-time share | Small/medium; invalidation after stack refresh and outside-window prerequisites are mandatory |
| 2 — same delivery lane | Separate short decision from cold evidence; fair bounded repo workers, single-flight exact keys | Can cap routine wait while preserving proof; high architectural confidence, unmeasured throughput optimum | Medium/high; durable queue/dedup, cancellation, stale-result discard and starvation tests |
| 3 — discovery/fetch owner | Progress-aware root discovery; unify observer/train fetch coordination | Reduce repeated timed-out scans and serial observer fetches; high mechanism confidence, medium attribution | Medium; post-arrival freshness and open-batch retention cannot weaken |
| 4 — DB/epic owner | Short graph/read projection, Git outside read checkout, transactional recheck | Reduces held-connection pressure; high static confidence, production contribution unknown | Medium/high; preserve exact graph/review/hold checks and publication exclusion |
| 5 — scheduler/status owner | Shared cycle aggregates and stamped display projections | Reduces N+1 queries and interactive remeasurement; medium confidence | Medium; no cached claim authority; explicit age/unknown and invalidation |
| 6 — `fair-falcon-22` | Batched recipients/session observations, bounded parallel different-recipient delivery | Isolates slow nudge and repeated reads; medium confidence | Medium; per-recipient order, CAS, pause and verified nudge constraints |
| 7 — `clear-vault-30` | Instrument timer/cron dispatch and handler time; bounded distinct-job concurrency | Avoid mistaken focus on existing async cron; benefit unmeasured | Small/medium; timer durable state and event ordering remain |
| Cross-cutting — `bright-pinnacle-83` | Preserve one combined batch repair and exact source manifest through optimization | Prevents duplicate/fragmented repair work, not a demonstrated selection speedup | Coordinate membership generation and repair dedup identity; do not fork another repair design |
| Cross-cutting — permissions `e7b1db92c` | Keep scoped verification/push permission repair separate from evidence timing | Removes worker friction, not Git/SQL selection latency | Commit adds playbook CI verification, git sync grants and additive worker capability sync; no timing shortcut or token bypass |

No duplicate implementation tasks were created and no other task was modified.
A factual telemetry message was sent to the selection worker; their progress
need not wait for this plan. The remaining named work is coordinated through the
interfaces and acceptance conditions here; its uninspected status is not assumed.

**Phase 0: establish attribution (selection owner, first).** Add context-local visit
id and nested spans for pending SQL, root proof, routing, stack, PR, epic readiness,
connection acquire/hold and process queue/run. Count attempted/completed/failed SQL,
returned rows and candidate counts at each narrowing boundary; use fingerprints,
not raw SQL parameters. Existing `engine.py` pool/query metrics and train stage
metrics are the starting point. Distinguish DB lock wait using safe read-only
operator telemetry (`pg_stat_activity`/`pg_locks`), not worker access bypass.
Record Git argv class, process count/bytes, GitHub endpoint class/request count,
cache hits/misses/evictions/single-flight joins and event-loop lag. One inclusive
span tree avoids double counting. Keep labels bounded and omit credentials/content.
Acceptance: explain ≥95% of sampled wall time through mutually attributable
exclusive work/wait spans, report unclassified remainder; retain n and histograms.

**Phase 1: remove duplicated local work (brisk-lantern-51 owns selection).** Refactor
visit-local projection, bulk stack identity and admission writes. Key projection
by repository/target plus task completion/version, origin generation/base,
parent/dependency/stack digest and policy/holds. Changes before use invalidate it;
writer lock still rereads. Acceptance: unchanged candidate/blocker sets versus
current implementation across archived/reopened/moved/repair/aborted/window cases;
query/process/row budgets improve at 55 targets; no early-limit starvation.

**Phase 2: bounded evidence service and discovery.** Persist a refresh request
before returning a refresh id. Dedup key includes repo URL/id/store, pinned target
OID, completion metadata OIDs, source/base, request fingerprint and proof algorithm
version. Queue one outstanding refresh per exact key; distribute fairly by repo
and age. No DB connection held while waiting for Git/network. Failures/backoff
remain explicit; cancel waiter without cancelling shared useful work, and remove
failed single-flight entries so later generations can retry. Restart resumes
pending requests; orphan result cannot grant authority. In-memory immutable facts
may be dropped safely, but persisted status must name its observation identity.
Acceptance: p99 decision <1s during 10s fetch/30s provider failure; no false-ready,
unbounded queue, root starvation, leaked child process or stuck cancellation.

**Phase 3: scheduler, status, messages and cron integration.** Independent sibling
work continues. Publish versioned cycle snapshots for displays; invalidate on
project/policy/profile/route/task/claim/workspace/provider/quota changes. Readout
can show stale/unknown immediately; atomic claim still uses current predicates.
Message worker dedups by message/recipient and attempt, caps per-recipient work,
preserves the one-message verified terminal nudge. Cron owner measures timer emit
handler costs separately from job runtime and retains per-job overlap guards.
Acceptance: inject a slow recipient, plugin and Git proof; unrelated status and
ready work still meet latency objectives, while ordering and backpressure hold.

**Phase 4: acceptance and rollout.** Use disposable PostgreSQL and local Git remote
fixtures; 10,000 warm decision samples for meaningful p99, multiple runs with
confidence intervals, plus cold/moved/evicted arms reported separately. At minimum:
55 targets/47 visit demand; N=200 and 1,000 pending origins; 5,000 tasks; deep/wide
dependency graph; 2,049+ cache keys; history with 150+ patch candidates; 50 recipients;
200 sessions; DB pool constrained to expose nested acquisition. Measure query
plans/buffers only on disposable data. Test rerun-pending replacing green,
retarget/reopen/policy/review changes, missing ref, revoked approval, publication
lease loss and ambiguous push readback. Keep one combined repair and exact frozen
members. Production canary is read-only instrumentation first, then operator-owned
rollout; rollback to old selection, never to weaker authorization. Regression
criteria include throughput/pending-age and correctness, not merely faster replies.

## Verification and remaining limits

Reproduction commands (run from repository root):

```sh
python docs/analysis/subsecond-checks-2026-10-07/collect.py
python docs/analysis/subsecond-checks-2026-10-07/profile_proofs.py
```

Focused validation passed: 10 tests in 12.26s (14 warnings) using the disposable
PostgreSQL test service. The initial invocation refused to run because the test
DSN was unset; no test failed. The successful command was:

```sh
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres aq test tests/test_integration_git_truth.py -k 'warm_completion or cached_only or shared_fetch or cached_completion' -q
ruff check docs/analysis/subsecond-checks-2026-10-07/*.py
```

The focused cases verify warm reuse, immutable cached records, invalidation and
cache-only withholding, post-arrival fetch freshness, cancellation and failed
fetch handling. Both reproduction scripts completed; the fixture's assertions
passed. Ruff passed. No production implementation or test module changed.

The collector only reads source, Git HEAD and a bounded log tail; rerunning
replaces the sample artifacts, so preserve the committed evidence before rerun.
The proof fixture only creates/removes its own temporary Git directory.
No daemon restart, production SQL, migrations, remote Git mutation, whole-suite
load or model call was used to profile selection.

Production query counts/rows, cache hit ratios, process counts, GitHub requests,
connection/lock wait attribution and actual data cardinalities remain **unmeasured**.
This is an explicit limit, not a zero-valued result. Static counts and the local
fixture narrow the hypothesis and define an actionable instrumentation plan;
they do not satisfy a production p95/p99 acceptance run. Whole-system latency
coverage remains pending Phase 0/4.

Canonical knowledge operations were attempted with this held task's identity:
capabilities advertised enabled read/write operations, but both record search and
knowledge create (`--source-task-id calm-current-21`) returned `record.forbidden`.
No record id, readback or graph link was created. The supervisor was notified;
Markdown artifacts are not a substitute for the required graph save. This gap
must be resolved or explicitly deferred; it cannot be silently marked complete.
The implementation plan is submitted separately as an uncommitted AQ review draft;
this committed file is the requested research evidence/report, not an approved plan.
