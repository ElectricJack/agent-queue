# Bounded train selection

The aggregate `select_batch` duration includes database reads, root delivery
proof, stack refresh, candidate proof, epic readiness and PR admission, including
waiting for shared resources. It cannot attribute an observed 197-second visit
to any of those operations without further measurement.

Each bounded visit will expose selection substage calls, item counts and elapsed
seconds, plus fetch-sharing and immutable Git cache hit/miss counts. Nested
substage times overlap; they must not be added to the visit's exclusive stages.
In-flight and interrupted stages remain visible. No repository contents or
command output are included in these metrics.

Selection routes completed task IDs before proving root delivery. Root delivery
still precedes the member limit, so delivered recent tasks cannot hide older owed
work. Historical delivery and dependency proofs remain enabled. `open_batch`
reuses its candidate IDs for `pending` only within that visit, after unchanged
stack refreshes; a changed refresh ends the visit and requires a new Git view.
Mutable eligibility and publication fences still revalidate independently.

Parent-only preparation may record a stack with no sibling prerequisites. Refresh
still requires a qualified repository snapshot for delivery, source and currentness
checks; the empty prerequisite set alone supplies no Git evidence. An unavailable
repository or failed or stale observation holds the source. An unchanged empty
stack preserves the original completion, checkpoint, branch and filing origin.

At most four visits per repository run concurrently, including requested visits.
No tasks are created just to wait for a repository slot. Admission chooses the
least recently started idle target, with unvisited targets first; other repositories
remain independent. A target blocked on the same batch, target OID and refusal
backs off from 10 seconds to at most 60 seconds. Wake clears the delay; every
retry fetches fresh Git and repeats admission checks. This is a scheduling hint,
never reusable permission or delivery evidence. Rate-limit and promotion pauses
retain their existing semantics.

Acceptance: deterministic counts show one candidate scan per open call and root
proof only for routed IDs; delivered work is removed before applying the limit;
new visits use current identities and Git refs. Concurrency, least-recently-started
fairness, bounded retry, cancellation and wake have deterministic tests. A bounded
benchmark exercises 55 targets and concurrent control-plane work, reporting
operation counts separately from machine-dependent wall time. Focused train,
source and Git-truth tests and the related integration area checks must pass.

## Production selection continuation (2026-10-14)

Retained dev visits on October 14 timed out after 900 seconds. The 13:37:23
sample spent 893.166 seconds selecting: root delivery 120.151 seconds for 234
IDs, target delivery 509.675 seconds for 17 sources, and PR admission 260.102
seconds for 14 sources. At 13:54:26, root delivery took 2.598 seconds, target
delivery 577.470 seconds for 24 sources, and PR admission 295.290 seconds for
20 sources. Neither sample recorded stack refresh. The separate hibernation
stack-refresh observation does not explain these dev timeouts.

Later supervisor readbacks reported native progress on the unchanged
`87d395` checkout: 14 roots reached settling, then batch
`train-ba18745869fd7cb04850f1bd2be342ae` sealed those 14 inputs with existing
repair `8027ec35`, attempt 1. The observation at `1791987380.201177` selected
in 2.36 seconds and took 31.70 seconds overall, using warm completion/delivery
caches and one candidate window. This successful sample supersedes any claim
that every visit times out; it does not establish a live latency guarantee,
remove the reproduced slow-prefix starvation risk, or show this fix deployed.
That native batch, its membership, repair and explicit holds remain owned by
the existing integration flow.

Selection must be able to finish a safely admitted prefix without rescanning
the same expensive prefix on every visit. New-batch selection rotates the full
undelivered routed frontier before applying the existing member cap. A
target-local task-ID cursor advances before awaiting candidate work, including
stack refresh, so cancellation or a changed stack cannot pin the next visit to
the same candidate. The cursor is an in-memory scheduling hint; restart may
discard it, and every visit reloads identities and fetches current Git evidence.

Stack-refresh work and candidate proof/admission each have a cooperative
60-second quantum. Check the quantum between candidates, allowing at least one
attempt. The existing 900-second visit deadline still bounds a stalled read;
the quantum does not cancel a fenced writer or extend that deadline. A started
stack refresh retains its existing join-on-cancellation behavior through pushed
completion recording. Unrefreshed stacks still require the ordinary current
identity and exact source containment checks. A changed refresh ends selection
and requires a fresh snapshot. Deferred candidates are never presumed eligible,
delivered or absent. Status names selection deferral and metrics count remaining
window, stack-refresh and candidate work.

Only sources fully admitted in this visit may freeze. Existing dependency,
undeclared-stack, provenance, policy, source-CI repair, exact-head review, CI,
attestation and publication checks remain mandatory. Unexamined prerequisites
still require delivery proof or withhold their dependents. The existing cadence
hints retain unexamined exact sources across rotating windows; evaluated refusals
clear their hints, and changed source identities reset admission time. No timing
hint authorizes membership. A frozen batch examines its own inputs instead of
scanning new unrelated roots, preserving its repairs and explicit pauses.
Previously observed frontier refusals remain visible during frozen visits with
their observation time and target SHA, explicitly marked `last_selection`.
These in-memory diagnostics never enter a frozen member guard or authorize
membership, release or publication; new selection replaces them with fresh
observations. Restart may discard them, just as it may discard the cursor.

Acceptance includes controlled delays and cancellation over real disposable
Git/PostgreSQL fixtures: a capped frontier of refused roots eventually reaches
an independent eligible source; a cancelled Git proof cannot produce a late
freeze or reset the continuation prefix; subsequent visits observe changed
completions; partial windows preserve cadence and prerequisite fences; stacked
refresh cancellation still records a pushed completion; existing frozen repairs
are not delayed by the new frontier. Production activation requires normal
native delivery and deployment; a worker checkpoint does not clear the incident.
