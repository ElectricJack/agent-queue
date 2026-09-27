---
type: implementation-plan
status: approved
date: 2026-09-27
project: agent-queue
author_task: noble-crest
approval: Jack approved the spec and implementation in chat on 2026-09-27, as recorded in the assigned task; no additional human review gate.
spec: projects/agent-queue/specs/2026-09-23-development-publisher-delivery-truth-design.md
review: rev-vivid-forge
---

# Development publisher delivery truth

Ship the ancestry-first publisher slice immediately. In parallel, bound silent
stalls and put exact task completion provenance in git. Then move admission,
settlement, recovery and diagnostics to the shared git evaluator and remove the
delivery-receipt database model. Each task below is one worker session and may
ship independently when its actual prerequisites land. Keep development mode;
switching this repository to pull requests is outside this approved work.

Canonical vault artifact:
`projects/agent-queue/plans/2026-09-27-development-publisher-delivery-truth.md`.
The repository copy and adjacent `.graph.json` preserve the reviewed work for
workers and delivery. The graph creates a new epic, not children of the planning
task. No review gate or phase-wide serialization is attached.

## Approval and review reconciliation

Before planning, `aq --json review show --review-id rev-vivid-forge --comments`
returned one inline comment, `cmt-3c1ed200fa0f`, and the withdrawal decision.
Jack's inline comment asks to remove records wherever git can answer because
agents and humans edit the repository while publication is in flight. The
withdrawal decision asks to remove duplicate state that can diverge. Both are
incorporated here. Revision 3, content SHA
`55cead4553af53ff7a437031c8e5e9cb8e111f3ce8772296389d6926c49fad29`, makes that
direction explicit, including git provenance and concurrent target movement.

The approved 09-23 vault spec still has the older F4 cache proposal. This plan
supersedes that implementation detail with Jack's explicit feedback: delivery
rows are removed, not retained as an authoritative cache or renamed receipts.
Completion events, repair work contracts, validation executions and notification
deduplication remain real workflow facts. They must not contain a second answer
to whether commits are on the target. Git is queried for that answer.

The assignment records Jack's chat approval as "I approve everything ...
accomplish all four tonight". That is the approval source for this plan; the
withdrawn review is feedback evidence, not a new approval request.

## Existing ownership and incident coverage

Inspected baseline commits, rather than assuming the task description still
describes unimplemented work:

| Existing work | Evidence in this checkout | Treatment in this plan |
| --- | --- | --- |
| bright-flare, completed sources conflict with main without repairs | `23c6fe844`; `docs/specs/design/development-conflict-repair.md`; depth-two and depth-three repair tests | Preserve the dispatch implementation and regression cases shaped like agile-torrent.9 and wise-ember.12. Do not file another conflict-dispatch task. |
| azure-lantern, adopted repairs repeatedly skipped | `e697af50b`, merged by `86338d77b`; `test_adopted_repair_cycle_drops_out_of_candidate_evaluation` | Preserve candidate pruning and stale-skip clearing, but replace their SQL receipt authority with fresh git truth. |
| azure-lantern, BLOCKED/PAUSED epics after children delivered | `a947e26e2`, included in `86338d77b`; `stale_container_clauses`, `settle_containers`, `tests/test_lifecycle_cleanup.py` | Keep settlement policy, startup/backstop triggers, hold semantics and audit events; migrate only the delivery proof and test it without receipts. |

Coordination messages were sent to both named tasks and
`session:supervisor-agent-queue`. Git proves these changes are already in the
planning baseline; no unresolved external blocks edge is needed merely because
the original assignment called them in flight. Workers must re-check current
main and retain any subsequent fixes. Do not mutate the incident tasks to test
the implementation: reproduce their shapes in disposable repositories/databases.

## Shared contract and resolved choices

1. Add `src/integration/delivery_truth.py` as the one async evaluator. Inputs
   identify project, repository, configured target, task and latest completion
   generation. Results distinguish contained, no artifact, pending and unknown,
   with the inspected target OID, source OID/generation and a reason. Unknown
   fails closed for delivery-sensitive operations and is visible diagnostically.
   No new persistent delivered boolean, task-to-target receipt or DB git index.
2. A fetch starts each publication/admission snapshot. An in-process cache is
   keyed by repository, exact target OID and completion generation, and is
   discarded on mismatch/restart. Network/git failures never mean no artifact.
   Missing worker refs never mean delivered or empty. Branchless organizational
   containers have no own artifact; their children and existing settlement rules
   determine when their workflow is done. A branchless code completion with a
   recorded artifact is not an empty container.
3. Recognize a candidate's own containment before dependency sorting, cycles,
   repair parking or parent assembly. Dependencies order only work still to
   publish. An already-contained task is removed from candidate evaluation and
   its stale skip diagnostic clears even if its ancestor is missing or cyclic.
4. `AQ-Task` trailers carry identity, but an arbitrary matching commit is not
   proof of a complete task. Bind completion provenance to the current immutable
   attempt/generation and exact final source. Reopened tasks, multiple commits
   and partial cherry-picks must not be released by an older trailer. Use the
   existing completion/claim identity as workflow identity; do not add a second
   DB mapping of git delivery. Ordinary worker commits carry task trailers;
   close verifies complete provenance without rewriting already-pushed history.
   Git-retained provenance must outlive source-branch cleanup.
5. The exact source ancestry test remains the safe proof. Git-carried explicit
   replacement/equivalence evidence can cover a complete rebased or squashed
   repair, naming the replaced task, generation and original source. Merely
   copying a trailer or cherry-picking an empty completion marker proves nothing.
   Arbitrary external squash/cherry-pick operations that omit that evidence are
   unknown, not silently delivered; ancestry-preserving external merges are
   recognized on the next snapshot without adoption. This tightens revision 3's
   overly broad assumption that every rewrite automatically preserves proof.
6. Legacy completion heads are temporary source locators, always verified with
   git. During the bridge, legacy rows may supply a missing source identity but
   their state never proves delivery. Migrate exact legacy completion and repair
   mappings into git before removing that reader. Ambiguous cases are reported
   with task ids, not guessed or discarded. The final cleanup child removes all
   legacy delivery-table access. A compatibility head fallback has an explicit
   inventory of unlabelled generations and retires when that inventory is empty;
   zero is checked by acceptance, not left as an indefinite future promise.
7. SQL cannot invoke async git inside `blocked_predicate`. Keep persisted
   `is_blocked` a graph/gate projection. Gather delivery-sensitive prerequisites
   in a batch, evaluate git outside row-lock transactions, then pass a
   request-scoped verified eligibility set into selection/claim. Recheck task
   generation, graph revision/inputs and target freshness before claiming or
   settling. A changed snapshot retries; it does not write a delivery cache.
   Paging continues past withheld candidates so one task cannot starve the rest.
   Readiness, pool demand, claims and explanation must use the same contract.
8. Use bounded observational stall metadata, not a delivery state. Default to
   five consecutive identical unsuccessful evaluations; make the threshold
   configurable and validate it as positive. A fingerprint includes task,
   completion generation, reason and relevant source/target evidence. Threshold
   crossing ends the attempt as stalled, emits one supervisor message and makes
   doctor ERROR. Continued ticks do not spam or silently retry that attempt;
   fresh git truth is still checked, and new source/target/evidence or explicit
   recovery can start a fresh attempt. Idle ticks and daemon downtime do not
   increment failures. Restart preserves the notification dedupe and count.
9. Publish independent members directly into the current aggregate. Parent
   grouping is optional and cannot block a clean sibling. Preserve exact-source
   repair contracts, depth limits and bounded repair generations from bright-flare.
   Conflict evidence names source(s), target and paths. Do not invent a
   conflicting sibling pair when the conflict is actually against main.
10. Preserve the existing exact expected-old-OID push and ancestry guard in
    `DevelopmentIntegration.publish`; do not substitute an unconditional force
    push. Re-check containment before applying, and refetch/retry on movement.
    Adoption that only confirms ancestry uses an isolated read context and does
    not wait for the long publisher lock; any state mutation still follows
    CommandHandler and its normal task/generation fencing. Explicit equivalence
    remains a separate, reasoned action with live-writer protections.
11. Keep only genuine operation intent and test/repair events needed for crash
    recovery. Resume uncertain pushes by inspecting current git, never a stored
    delivered state. Remove `development_deliveries` after its consumers migrate.
    Do not copy its old rows wholesale into a differently named delivery table.
    Preserve actual validation evidence in the existing job/operation evidence
    infrastructure; add schema only if those event types cannot represent it.
12. Apply this delivery mechanism to development integration mode. Preserve
    hierarchy/train/PR requirements and validation policies. No model calls in
    scheduling, no automatic human gate, and no worker daemon/database operations.

## Implementation tasks

The graph uses these keys. Each description embeds its scope, acceptance and
commands so the worker does not need this planning session's context.

| Key | Profile | Depends on | Deliverable and bounded scope |
| --- | --- | --- | --- |
| truth | standard-high-codex | none | Shared evaluator and immediate publisher preflight: ancestry before sorting/ancestor gates, eleven-way regression and fresh snapshots. Keep the legacy source locator only as the documented bridge. |
| stalls | standard-high-claude | none | Extract bounded stall observation/notification logic, five-tick configurable bound, doctor ERROR and restart/idempotency tests. Own diagnostics, not candidate assembly. |
| provenance | standard-high-codex | none | Git task/completion provenance and exact repair replacement format; worker commit/close integration, archive/reopen/multi-commit tests and legacy mapping migration tool. No delivery eligibility refactor. |
| isolate | standard-high-claude | truth | Remove sibling poisoning/parent gating; ancestry-only adoption during sweeps, scoped recover-child and moving-target tests. Reuse bright-flare repair dispatch. |
| admission | standard-high-codex | truth | Async git eligibility at scheduler, pools and claim boundary; retain graph-only SQL projection, generation fences, pagination and race tests. No settlement/view migration. |
| consumers | standard-high-claude | truth | Migrate hierarchy settlement, status, doctor proof, archive/removal/cleanup and legacy diagnostic readers to evaluator snapshots; preserve azure-lantern policy. No candidate assembly or claim-selection edits. |
| operations | standard-high-codex | provenance, isolate, stalls | Wire provenance into publisher truth; replace receipt-writing publisher/recovery paths with git plus genuine operation/test/repair events; migrate legacy mappings and restart recovery. |
| retire | standard-high-claude | admission, consumers, operations | Remove remaining receipt queries/table/model with guarded Alembic revision; migrate retained event evidence and compatibility CLI/API fields; codegen and docs. Exit with no delivery-row eligibility or renamed replacement model. |
| acceptance | standard-high-opencode | retire | Final deterministic incident/contract regression coverage and operator acceptance guide, current focused/area checks and disposable swarm smoke. Test-verified closure only; no new review gate or live repairs. |

There are three independent starts. `isolate`, `admission` and `consumers` can
start together after `truth`. `operations` needs the provenance, isolated
publisher and diagnostic APIs it consumes. `retire` waits for the remaining
readers/writers; `acceptance` tests their combined result. No edges encode mere
preferred sequencing, shared parentage, provider choice or paperwork. Shared
files are coordinated by symbols: isolate owns assembly/adopt/recover-child,
stalls owns diagnostics, admission owns claim/pool eligibility, consumers owns
readers/settlement. Rebase against landed changes rather than replacing them.

## Verification contracts

Focused tests use the existing module for the changed area. Broader checks run
once at each child's end, limited to its area. Use `aq test`, default marker
exclusions and the session worker cap. Never raise `-n` or run the full suite.
No pre-existing failure authorizes weakening a test; compare with the latest
vault `projects/agent-queue/notes/full-suite-baseline-<date>.md` and record it.
New test modules require selection ownership and regenerated
`tests/selection_catalogue.json`. New source paths need selection rules.

Required scenario matrix:

- A contained candidate survives an unresolvable ancestor and the eleven-way
  historical shape; source-ref deletion and archived tasks change no truth.
- Wrong repository, wrong target, stale completion generation, abbreviated or
  ambiguous source and git/fetch failure never produce false delivery.
- Multi-commit/reopened tasks and a partial cherry-pick cannot be satisfied by
  one old task trailer; complete external merges and exact repair replacements
  are recognized without DB receipts or manual adoption.
- A completed source at depths two and three conflicts with advanced main;
  exactly one bounded repair is filed/reused across restart. Close alone does
  not release the source. Delivery of the exact replacement does.
- Adopted repair cycles disappear from evaluation and stale diagnostics; an
  unrelated clean sibling still publishes when another conflicts.
- On the fifth identical evaluation doctor is ERROR and one supervisor message
  exists; repeated ticks/restart do not duplicate it. New evidence recovers.
- Adoption observes ancestry while another sweep holds the long lock; target
  movement immediately before publication retries without an unsafe overwrite.
- Claims and pool demand agree with fresh git even when legacy rows are absent
  or deliberately misleading; task reopen and target movement invalidate a
  prepared admission. Parallel claims cannot obtain the same task.
- BLOCKED/PAUSED containers settle after every required child's current work is
  delivered, once, through existing transitions and audit events; missing work,
  live sessions, explicit non-stale blockers and train ownership retain guards.
- A subprocess restarted after prepare/push uncertainty reconciles from git.
  Validation failures and infrastructure deferrals retain their actual evidence.
- Runtime imports and SQL contain no `development_deliveries` eligibility
  authority; after retirement the table is absent and the legacy fallback
  inventory is empty in migrated fixtures. Migration is idempotent.

Tasks touching claims, pools or hierarchy run
`scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` against its disposable
environment. The final acceptance worker also runs it once on the combined
branch. Workers never run `aq start`, operator migrations or live repairs.
For model/router changes run both offline client generators; never hand-edit
generated clients. All production git I/O uses async GitManager.

## Rollout and completion evidence

The supervisor can deliver `truth` and `stalls` as soon as their focused checks
pass, gaining value before the migration chain finishes. This is an incremental
rollout, not a claim that the DB-removal goal is done after the first patch.

The final operator guide names read-only checks for current agile-torrent.9,
wise-ember.12, adopted repair diagnostics and completed epics. The supervisor
owns any authorized operational repair and daemon restart. Record source heads,
target heads, test command evidence and resulting task ids; a worker checkpoint
is not proof that main contains its changes.

Close the implementation epic only after all required children are delivered,
the combined checks pass (with baseline failures named), and git delivery truth
works without the removed receipt table. Every worker publishes its own branch
before closing. The planning task ends after the plan and graph are persisted,
validated, filed, and the supervisor receives the epic id.
