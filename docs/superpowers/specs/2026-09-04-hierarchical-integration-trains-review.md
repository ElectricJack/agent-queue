# Review: Hierarchical Delivery and Integration Trains

**Reviewed:** `2026-09-04-hierarchical-integration-trains-design.md`
**Date:** 2026-09-04
**Stance:** Adversarial. The review lens is the stated goal that orchestration
policy lives in playbooks and core commands own only fenced, idempotent
mechanisms (design §10, acceptance criterion 12).

**Verdict:** The Git fencing and durable-state model are sound. The design is
not implementable as written because it assumes playbook-engine and hierarchy
behaviour the codebase does not have (§A), and it hard-codes several policies
that should be playbook inputs (§B). Sections A and B should be resolved before
the spec moves from "pending written review" to approved.

---

## A. Blocking: the design assumes things the codebase does not do

### A1. Code-less parents have no branch to checkpoint

Design §6.2 requires the parent's work to be committed and pushed and records
the parent branch HEAD as the child base checkpoint. Most real task trees are
topped by an epic or plan container that never runs an agent, has no branch and
no workspace. The design never says what a branchless parent checkpoints
against, or what "wake the parent to verify the aggregate" (§6.5) means for it.

**Required:** define that a branchless parent inherits its own parent's
checkpoint recursively down to the default branch, and that its verification
step is a playbook policy (`skip`, run declared checks, or spawn a verifier
task).

### A2. Container settlement fights guarded completion

`settle_containers` (`src/database/queries/hierarchy_queries.py:547`)
auto-completes a parent when its children reach terminal state. Design §6.5–6.6
instead require the parent to wake, verify, and close through a guarded
transaction. Left as-is, the parent completes before a single child is
promoted.

**Required:** state that settlement is suppressed for any task carrying an
integration checkpoint, or that settlement *is* the wake trigger. Pick one.

### A3. Subtasks today share the parent's branch

Worktree spec §4.4 sets `resume_branch = parent.branch_name` so a plan lands as
one PR and siblings serialize on the branch. This design gives every child its
own branch from a checkpoint. The design must say the shared-branch model is
retired for tasks under the hierarchy flag, and how `task_batch_commit` filing
many children at once advances the generation (one bump for the batch or one
per child changes when the parent wakes).

### A4. There is no `ci-run` wait and nothing emits `integration.ci_completed`

Design §12 lists "ci-run waits" as substrate. The v2 engine's wait kinds are
`event`, `human`, `task`, `timer` (`src/playbooks/definition.py:299`). `ci-run`
and `pr-merged` are *gate types* polled by `src/orchestrator/pr_polling.py`.
The design names `integration.ci_completed` but no component emits it.

**Required:** add to §10.2 a core poller that reads check runs for an exact SHA,
attaches the evidence to the batch, and emits `integration.ci_completed`. This
same poller is what makes attempt accounting trustworthy (see C3).

### A5. Timer triggers are global, not per project

Design §7.1 promises a per-project interval with project override.
`src/timer_service.py` fires `timer.{N}m` once globally with
`project_id: null`, does not persist elapsed time across restart, and fires on
every daemon boot. Per-project cadence therefore works only if each project owns
its own playbook copy with its own trigger, and every restart triggers a sweep.
Either document that, or add project-scoped timers to the core work.

### A6. Playbook-callable commands are a short allowlist

`src/commands/contracts/builtin.py` exposes roughly fifteen contracts, none of
them Git-mutating. Every §10.2 primitive must be registered as a typed contract
with named outcomes before a playbook can call it (the v2 design requires this
for every command step). The design should enumerate those contracts and their
outcomes so the playbook rules can be written against them.

### A7. Repair loops need event re-entry, not in-run loops

The foreach executor is sequential and rejects nested loops
(`src/playbooks/executors/foreach.py:88`). "Iterate focused tests, launch CI,
repair again, up to N attempts" is a loop. Commit to the event-driven shape the
§12 wording already implies: one playbook run per `integration.ci_completed`
event, budget counters in the batch row, and no in-run waiting on CI. That is
also what makes budgets survive a daemon restart (§13).

---

## B. Policy the core hard-codes that playbooks should own

### B1. Collapse the one-candidate direct path into a batch of one

Design §7.4 force-rewrites the reviewed root branch into a synthetic squash
commit. Consequences:

- GitHub dismisses stale approvals on a force-push, so the human-visible review
  evidence disappears.
- A reopened task resumes `origin/aq/<id>` (worktree spec §3.2) and finds the
  rewritten branch; the agent's history is gone.
- The path needs its own unique fencing proof (§7.4's "must contain exactly the
  reviewed root diff") that the batch path does not.

**Recommendation:** build every candidate on `integration/<batch-id>` and treat
one candidate as a batch of one. The zero/one/many choice becomes a single
decision step in the playbook instead of two core code paths. Cost: one extra
branch name per sweep.

### B2. Squash is a project policy, not an invariant

A root-subtree squash can be thousands of lines. Combined with "never bisect"
(§9), post-hoc bisection on `main` becomes useless. Squash also drops the
co-author and session trailers this repository requires on commits. Receipts do
not depend on the compression strategy.

**Recommendation:** `compression: squash | merge` as a playbook input, per
boundary (child→parent and root→main may differ).

### B3. The escalation ladder is baked into the schema

`integration_batches` (§11.3) carries "primary and debug repair tasks and
attempts" columns, and §9.2 says "exactly one" escalation.

**Recommendation:** store repair stages in `integration_repair_stages` keyed by
`(batch_id, ordinal)` with class, task, wall-clock and CI-attempt budgets. The
playbook declares the ladder as an ordered list of stages; two stages becomes
the shipped default, not a law. Acceptance criterion 8 should then reference the
configured ladder.

### B4. A single bad member stalls the whole project

No batch cap (§7.1), immutable membership (§9), no later sweep while
human-blocked (§9.3), and abort-all as the only human exit (§12). Forty green
PRs wait behind one red one for as long as repair takes. This recreates the
serialization point §1 complains about, and makes it worse: the stall now
blocks work that was never at fault.

**Recommendations, all as playbook inputs:**

- batch size cap and/or maximum candidate age;
- `on_main_moved: rebase | wait` (§7.6 today always rebuilds and reruns full
  CI, which can livelock against an active human pushing to `main`);
- a human-only "eject member and reseal" control beside abort;
- allow batch N+1 to be built on candidate N's head and promoted after N, which
  is what a merge train means elsewhere. The current design is a single-car
  train with throughput of one batch per repair duration.

### B5. `sweep_pending` is a flag where an event would do

§7.1 and §11.3 store a coalesced pending-sweep flag on the batch row. Lease
release should emit `integration.sweep_due` with a reason, and coalescing is a
dedup key on the activation. That keeps scheduling in the playbook and removes
a state column that describes the *next* sweep from the *current* batch.

### B6. Child disposition and branch cleanup are policy

"Failed children block" (§6.5) and "delete the child branch after the receipt"
(§6.4) are fixed. Worktree spec §3.4 retains branches for `retain_failed_days`
for forensics. Expose `on_failed_child: block | accept | ask` and branch
retention as inputs.

---

## C. Fencing and correctness gaps

### C1. Activation-derived idempotency keys do not prevent duplicate promotion

§10.2 derives the key from the playbook run and node activation.
`src/playbooks/receipts.py` documents that a run-plus-step key collides across
loop iterations, and a rerun after a lost run gets a fresh key, so the unique
constraint on `task_delivery_receipts.idempotency_key` would not catch a second
promotion of the same source.

**Required:** derive the key from the domain (source task, reviewed head,
target branch); record the activation as provenance. State that the
expected-old-SHA push lease is the real guard and the receipt key is the audit.

### C2. "Exactly the reviewed diff" is undefined after conflict resolution

Invariant 2 and §7.4 claim the candidate "represents the same reviewed tree
change". Applying a squash onto a moved base is a three-way apply against the
child's checkpoint; the pinned tree SHA cannot be compared after rebasing.

**Required:** define what is compared. Either the apply had zero conflicts, or
a repair receipt names the resolution. Only the first case supports the
"exactly" claim, and invariant 2 should say so.

### C3. CI attempt accounting must count observed conclusive runs

Any push to the repair surface triggers Actions under the current `tests.yml`,
so an agent can consume or bypass "launches" freely. `cancel-in-progress` on
non-`main` refs cancels the previous candidate's run when a repair commit
lands; `src/git/ci_gate.py` already treats `CANCELLED` as inconclusive.

**Required:** an attempt is consumed only when the poller (A4) records a
conclusive result for the exact SHA. Launches are not attempts.

### C4. Worker promotion is detected, not prevented

§14 says worker profiles "cannot" promote to parent branches or `main`. Nothing
on the forge enforces that for dynamic `aq/*` names. What actually happens is
that the exact-target push lease and the HEAD-plus-generation close guard
*detect* a foreign push and refuse. Say that. Prevention is real only for
`main` via branch protection.

### C5. Excluding `push` on `main` from CI leaves hotfixes untested

§8 removes `push` events for `main` from the full-CI workflow. Any bypass-actor
emergency push then never runs CI. Keep the `main` run and skip it when the tip
SHA already carries a green attestation; that is all "no redundant run" needs.

### C6. Lease and receipt scope are inconsistent

Invariant 9 says per project *repository*; `project_integration_leases` is
keyed by `project_id`; receipts model `main` as a null target task. Projects
with multiple workspace kinds have multiple repositories. Key the lease and the
receipt target by repository (kind or remote), not project.

### C7. Lease ownership by a cancellable run

An operator can cancel a playbook run. State that cancellation never releases
the lease, that batch state lives in the row, and that a new run resumes from
`integration.*` events after reconciliation (§11.4). Also: the debug task
(§9.2) inherits a branch still checked out in the stopped primary's slot, so it
needs slot affinity (worktree spec §3.4) or an explicit detach.

---

## D. Smaller inconsistencies

- Acceptance criterion 4 promises one root-subtree commit on `main`, but §7.5
  step 6 adds repair commits per batch. Fix the wording.
- §13 has a "receipt committed before observable push" row. If the primitive
  always pushes then writes the receipt, that row is unreachable. State the
  order instead of handling both.
- Migration step 4 (§15) backfills receipts for legacy `pr-merged` gates, but
  old merged PRs never pinned a reviewed head. Waiver is the only real path;
  say so.
- Goal 3 says wake the parent "exactly once", but §13 legitimately wakes it
  again after a late child. Say "once per generation".
- §7.5 step 10 closes member root PRs; their heads are not ancestors of `main`
  after squashing, so GitHub will show them closed, never merged. The design
  already treats the receipt as authoritative, but operators should be warned
  the forge will disagree.

---

## E. Recommended core/playbook split

Core contracts (fenced, idempotent by domain key, lease-token gated):

| Contract | Owns |
|---|---|
| `integration_checkpoint_parent` | record HEAD + generation, create delivery wait |
| `delivery_promote` | exact source, expected target, lease token, receipt |
| `delivery_receipts` | query by source/target |
| `integration_seal` | snapshot eligible roots + acquire repo lease, one transaction |
| `integration_build_candidate` | apply manifest on base in order, report conflicts |
| `integration_ci_evidence` | poll + attach conclusive runs for an exact SHA |
| `integration_promote_main` | expected-base fast-forward with green attestation |
| `integration_release` | release lease, emit `integration.sweep_due` if pending |

Playbook inputs (policy):

interval, batch cap / max age, compression per boundary, repair ladder (list
of class + budgets), infrastructure-retry classifier policy, `on_main_moved`,
`on_failed_child`, branch retention, eject-and-reseal permission, cleanup
retry policy.

Items B1, B3 and B4 together move the bulk of the orchestration decisions into
the playbook, which is the design's stated goal.

