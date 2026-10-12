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
