# Task11c requirements — authenticated controls and atomic project cutover

Begin after reviewed11b. Read this first, task-11-brief.md binding constraints,
task-11-slices.md, task-11c-entrypoints.md and final11a/11b reports for actual APIs.
No whole-plan read. Same workspace, defaults disabled, no real enable/probe/operator
DB/config or forge changes.11d owns handcrafted CLI/doctor/guide after this review.

## Public surfaces and authority

Create src/integration/controls.py; extend integration command contracts/handlers and
registry/generic execution. Register status,flush,enable,probe,waive_history,resume,
abort,retry_cleanup with existing envelope and strict typed args/outcomes. Resolve
operation/batch/task project relationships server-side before authorization. Status
same-project; flush existing explicit project/capability authority; all other controls
LOCAL operator only, not elevated session or trusted generic playbook. Never accept
caller-supplied principal/operator identity. Internal reconciliation has a separate
trusted service entrypoint, not an authorization bypass on public controls.

integration_enable is the sole public effective-mode writer. Guarded edit_project
configures only repository/policy under LOCAL, disabled+desired-disabled+drained/no
active-work and hierarchy-locked generation CAS; rollout fields explicitly rejected.
Ordinary project edits retain prior authority. Public deletion must refuse live
integration state. Internal project query compatibility is not a public enable API.

## Preflight and cutover

Resolve complete typed parent/root policy and artifacts, primary/debug/profile/class
routes and required branchless verifier, current required check/producer/trust manifest,
designated retained GitHub.com repository, origin/object retention, integrator/runtime/
playbook readiness, exact hosted-workflow variables, observed transport+worker+control-
plane isolation, effective protection and matching positive/negative probe receipt.
Use reviewed11b typed facts; no bool substitute, fabricated evidence, lexical class
ranking, implicit probe or provider/config writes. Missing facts visibly block.

Observe may collect read-only eligibility and security blockers, but cannot create
schedules/leases/refs/intents/receipts/evidence/checks/repair tasks/gates. Disabled flush
returns disabled; observe/hierarchy flush returns eligibility only; train flush creates
or coalesces one durable manual request. Scheduler.mark_due currently creates/locks a
schedule before mode check: guard at the authoritative mutation boundary with project
hierarchy-first lock order, not merely the CLI handler. Guard new sealing while draining.

External preflight reads precede locks. Under hierarchy lock re-read generation/config/
accepted fact identities and CAS effective/desired/draining plus schedule and per-project
legacy suppressions, append transition/waiver applicability in ONE transaction. Failures
leave no partial enablement. Historical waiver requires exact current blocker_digest,
recorded operator/reason and single immutable consumption; preserve historical gates and
real pinned-review requirements, never manufacture a review/delivery receipt.

Guard _cmd_pr_merge in src/commands/git_commands.py immediately after resolving project,
before filesystem/forge/CI for hierarchy/train, every caller. Exact-main promotion uses
its existing separate protocol, not an arbitrary command bypass flag. Preserve disabled/
observe legacy behavior. Suppress only project merge sweep and per-branch final-review
legacy route at services.ready_activations (global timer uses activation project),
engine.dispatch_event (hydrated event project), and integration destination parity.
Re-enabling activation cannot bypass predicates; do not globally disable pipeline.

Suppression must not strand required review approval: preserve the ordinary per-task
review path and prove that a managed completed root/child receives its exact pinned
review evidence without calling ordinary pr_merge. Existing review_evidence.py accepts
the established reviewer/final-reviewer profiles and uniquely graph-bound subjects;
reuse that contract, do not invent a new trusted profile or fabricate a receipt just
to replace legacy per-branch-final-review. Include the actual event-to-review completion
path in acceptance, not only assertions that the legacy merge call is rejected.

Wire reviewed transport accepted-fingerprint and live revalidation into ACTUAL daemon
candidate, repair publication, main promotion, attestation and cleanup provider/Git
factories. Frozen operations retain exact accepted authority after activation disable;
changed unsafe topology blocks mutation rather than bypassing its checks. No default
test inspector or literal successful proof reaches production construction.

## Drain, human controls and recovery

Disable with active work sets desired disabled, suppresses future sweeps/seals and drains
frozen operations before restoring recorded legacy policy. Install deterministic
background drain completion using existing bounded service/orchestrator reconciliation;
do not require repeated operator enable calls to finish a requested drain. Release or
catch-up events cannot start a new train while draining. Preserve operation audit.

Resume/abort only human-required state with explicit LOCAL authority. Reconcile ALL
unresolved Git/ref/main/resolution, attestation publication and cleanup irreversible
prewrites before ownership/terminal changes. Expiry alone never clears ambiguity.
Abort never rewinds/pushes main or deletes audit/receipts; retain failed work per frozen
policy. Reuse existing owner/stage/session fencing and readback proof, stop a writer
only through existing safe observed-stop/handoff rules. Config rollback is forward
draining/configuration, no DB downgrade. Cleanup retry requeues only safe exact existing
work; never resets an irreversible marker to permit another POST.

## Required verification

TDD full authority/cross-project matrix, observe zero-mutation and all flush modes,
complete/stale preflight, atomic CAS and waiver/suppression rollback, all three legacy
bypass paths, re-enabled activation, actual daemon wiring, disabled drain through
release/catch-up, human-required resume/abort, ambiguous reservations, cleanup retry,
and no main write from abort. Use real services/DB, fake external agents/GitHub/clock.
Include deterministic PostgreSQL cutover/control races on unique scratch DBs where
lock behavior matters. One explicit affected-area gate, aq test no upwardworkers or
whole suite; Ruff changedfiles/diff. If shared command fingerprints change, rebuild only
affected reviewed fixtures/manifests, discard only your timestamp-only churn.

No subagents/live probes/operator mutations/push/PR/mainmerge. apply_patch/exact-path
commits. Report full exact RED/GREEN outputs, checklist, downstream CLI/doctor APIs,
daemon wiring and limitations in task-11c-report.md. Raise concrete unresolved safety
questions rather than inventing a new auth/sandbox architecture.
