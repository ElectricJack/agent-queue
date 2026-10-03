# Integration train mechanism inventory and simplification candidates

Revision 2, 2026-10-02. Task: eager-orbit-98 (revision of rev-agile-ridge; revision 1 was task brisk-cascade). Source snapshot: `5de0a28c7efd1d214cb608c22c61a95b750c9170` (`origin/main` at the time of writing). Reviewer: Claude Fable 5.1 (`deep-high-claude`), as requested in the decision note.

**What this revision adds.** Revision 1 was a complete inventory of the integration mechanism (84 modules, 1,808 callables) and a table of overlap candidates. The decision note asked for something different: a considered review of the system with a list of recommendations for simplifying the integration train so that it is robust (it never gets blocked and always finds a path forward on its own), simple (reusable building-block methods), and policy-driven (the policy lives in playbooks that a project can modify for its own integration rules). Part I below is that review. Part II points at the revision 1 inventory, which is unchanged and remains revision 1 of this review; it is the reference the recommendations point into. Part III holds the evidence appendices.

Everything in Part I was checked against this checkout, the operator database (read-only `SELECT`s, no state changed), the daemon's JSON logs for 2026-10-01 and 2026-10-02, and the playbook run and step receipts for the last three days. Where a number comes from the live install it is labelled as such; it describes one install on one day, not a general law. Nothing here authorizes a change: it is a recommendation set for the operator and supervisor to approve, reorder or reject.

---

# Part I: Review and recommendations

## Navigation (Part I)

- [1. Summary](#1-summary)
- [2. Evidence: how the train blocks today](#2-evidence-how-the-train-blocks-today)
- [3. Target shape: one reconciler, twenty primitives, policy in the playbook](#3-target-shape-one-reconciler-twenty-primitives-policy-in-the-playbook)
- [4. Blocker catalogue: a path forward for every non-human stop](#4-blocker-catalogue-a-path-forward-for-every-non-human-stop)
- [5. Simplification: what to merge, what to delete, what to keep](#5-simplification-what-to-merge-what-to-delete-what-to-keep)
- [6. Sequencing and acceptance](#6-sequencing-and-acceptance)
- [7. Risks, non-goals and open questions](#7-risks-non-goals-and-open-questions)
- [8. Changes from revision 1](#8-changes-from-revision-1)
- [Part II: the revision 1 mechanism inventory](#part-ii-mechanism-inventory-revision-1)
- Part III: evidence appendices [A](#appendix-a-stop-point-sweep-108-rows), [B](#appendix-b-operator-recovery-surface-49-actionable-controls), [C](#appendix-c-policy-knobs-python-decisions-and-what-the-playbooks-decide)

## 1. Summary

The train delivers. Thirty-six batches have been promoted to `main` on this install, twenty-eight of them in the last two days under the continuous policy. It does so only with a supervisor and an operator standing next to it: 283 branch-owner recoveries recorded, 34 messages naming an incident in two days, 54 open escalations (many of them "Supervisor delivery unavailable", that is, the recovery path itself failing), eight paused epics of which two are stuck on repair ladders that have burnt 14 and 5 stages respectively without a single counted CI attempt, and 81 policy-generation transitions in a month because changing one line of policy requires draining the train. The code that does this is 56,253 lines in `src/integration/` plus 13,042 lines of commands, contracts, CLI, doctor checks and queries; 62 command handlers, 39 `aq integration` subcommands, 20 doctor checks, 45 tables and 139 distinct top-level outcome codes. It has grown by 64,000 lines since 1 September across 438 commits and 16 design specs, and the original design named 23 primitives.

Three structural causes explain nearly every stop ([§2.3](#23-structural-causes)):

1. **The train is edge-triggered.** A playbook run fires once per event, calls a command, and ends. A typed refusal such as `busy`, `stale`, `wait` or `human_required` ends the run "visibly failed" and nothing re-examines that subject until some other event happens to arrive. In the last three days 22 of 51 `promote-green-candidate` runs and 20 of 26 parent `dispatch-debug` runs ended that way.
2. **Policy is in Python; the playbooks relay.** Forty-one distinct policy decisions (stage ladder, exhaustion, escalation, admission, batch sizing, retention, who repairs) are hard-coded or frozen in Pydantic models. The shipped `root-train` playbook contains no decision at all and `parent-integration` contains one. A project cannot run a different integration policy without a code change, which is the opposite of what the design asked for (goal 8, [design §2](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md)).
3. **Every incident became an operator control.** Of 49 actionable operator controls, 25 are mechanical: the system already held the signal and the proof, and the only reason a human ran the command is that nothing was wired to run it. Seven more are one-off migrations that will never be needed again on a fresh install. Each control brought a module, a spec, a doctor check and a few dozen outcome codes.

The recommendation is not to patch these one at a time; that is what the last five weeks did. It is to change the shape ([§3](#3-target-shape-one-reconciler-twenty-primitives-policy-in-the-playbook)):

- **One level-triggered reconciler.** Every integration subject (a root batch, a parent episode, a source) has one durable row, one owner and one `next_due_at`. On every pass the reconciler observes the subject, asks the project's policy what to do, does one idempotent thing, and sets the next due time. Events only pull a due time forward. There is no state in which a subject is waiting for nothing.
- **About twenty mechanism primitives**, each a typed, idempotent, fenced operation that returns facts (`observe_subject`, `merge_members`, `publish`, `ci_observe`, `writer_file`, `writer_release`, `wait`, `gate`, `eject`, `cleanup`...). They replace the 62 handlers and most of the 84 modules.
- **Policy as a decision table in a per-project playbook.** On conflict, on red, on exhaustion, on a stopped writer, on a moved base, on a failed child, on an unanswered gate: the playbook decides, from the facts the observer returns, which primitive to call. A project with different rules copies the playbook and edits the table. The policy pinned to a running subject is the playbook artifact already pinned today, so a policy edit never needs a drain.
- **A never-blocked guarantee** ([§3.6](#36-the-never-blocked-guarantee)): at every pass a subject is progressing, waiting with a due time, or held by an explicit human gate. The only indefinite stop is a human gate with no timeout, which is by definition a human decision. Reviewer rejections, product holds, gate answers, aborts and explicit root authorizations stay binding ([§3.7](#37-human-decisions-that-stay-binding)).

What the recommendation keeps, unchanged: exact-SHA green CI on the candidate that reaches `main`, fenced expected-old pushes with a journal row before every write, one publisher per repository, trusted CI and attestation in App mode, nothing fabricated and nothing discarded. These are the invariants of [design §5](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md) and [factory policy §B and §C](../../../docs/concepts/factory-policy.md), and they are not what makes the train block.

The sequencing ([§6](#6-sequencing-and-acceptance)) starts with eight small fixes in the current code that remove the worst of today's stalls in days, then introduces the reconciler behind the existing root-train policy, then parents, then folds development mode in as a policy variant, and only then deletes. Each phase has an acceptance statement of the form "a batch with N conflicts and a red member reaches `main` with zero operator commands".

## 2. Evidence: how the train blocks today

### 2.1 Scale

| Measure | Value | Source |
|---|---|---|
| Modules in `src/integration/` | 84 (82 created since week 36) | `wc -l`, `git log --diff-filter=A` |
| Lines in `src/integration/` | 56,253 | `wc -l` |
| Lines in integration commands, contracts, CLI, doctor and query modules | 13,042 | `wc -l` |
| Command handlers (`_cmd_integration_*`, `delivery_*`) | 62 | `src/commands/integration_commands.py` |
| `aq integration` subcommands | 39 | `src/cli/integration.py` |
| Doctor checks (`integration.*`) | 20 (5 with `--fix`) | `src/doctor/integration_checks.py:2201-2371` |
| Integration tables | 45 | `src/database/tables.py` |
| Distinct top-level outcome codes across the operator surface | 139 (plus about 80 second-tier reason codes) | contracts, `task deliver`, `task close --obsolete` |
| Commits touching integration since 2026-08-25 | 438 (59, 127, 11, 159, 82 per week) | `git log` |
| Lines added / deleted in `src/integration/` since 2026-09-01 | 64,045 / 6,577 | `git log --numstat` |
| Integration design specs since 2026-09-04 | 16 (9 in September, 7 in the first two days of October) | `docs/superpowers/specs/` |
| Integration test files / lines | 97 / 61,319 | `tests/` |
| Primitives named by the original design | 23 | [design §10.2](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md) |

The growth is incident-driven. The seven October specs are, in order: a rebind for a parent repair stage frozen on an unpublished head, a recovery for a preserved parent resolution, a constraint fix for stages past ordinal one, a migration join for two sibling revisions, a recovery for an invalid source ancestry, a recovery for legacy PRs the train cannot seat, and a per-root authorization that exists because changing one allowlist entry required stopping the train. Each one is correct on its own terms. Each one added an operator-only command, a module, a doctor check and a spec, and none of them removed a way for the train to stop.

### 2.2 The live train on 2026-10-02

The `agent-queue` project runs mode `train` at policy generation 86 with `on_exhausted: continue`, `conflict_scope: batch` and `source_ci: true` on the root boundary, `on_failed_child: block`, and a 15-check required set (`projects.hierarchical_integration_policy`, read 2026-10-02 19:40 UTC). The observations below are from the same read.

**Repair ladders that burn stages without working.** Parent operation `bcab5af6` (epic `azure-vault-92`) is `escalated` at stage 13. Its 14 stages: ten cancelled, three expired, one failed, one active; attempts counted: 0 in 13 of them, 1 in the other. Stages 2 through 7 all carry the same subject head `93da7393`, each started about an hour after the previous one and each ended with zero attempts. Operation `338ab85e` (epic `calm-grove-25`) has the same shape at stage 4. Across every repair stage ever created on this install (83 stages over 46 operations) the total number of counted CI attempts is 3. The attempt budget in `RepairPolicy` (`primary_attempts`, `debug_attempts`, `src/integration/models.py:85-88`) has in practice never been the thing that ended a stage; stages end by wall-clock expiry (16), cancellation (26), pass (36) or failure (4). The expiry path (`RepairService.expire`, `src/integration/repair.py:1898-2029`) allocates a successor stage through `_activate_debug_on` (`:3926`) without asking whether the expiring stage's writer was ever claimed, ever pushed, or ever had CI requested; a delegate that waited in the pool queue for an hour exhausts its stage exactly like one that tried three times and failed. The no-progress guard added on 2026-10-01 (`_supervisor_recovery_on`, `:4108`) compares subject SHAs, and the subject of a collecting parent moves every time another child lands, so it rarely fires for parents.

**Events nobody consumes, retried forever.** `integration_outbox` holds 618 undelivered rows, all with `last_error = "no enabled matching playbook durably accepted the event"`, for five event types no shipped playbook subscribes to: `integration.branch_materialization_pending` (439), `integration.root_delivered` (117), `integration.cleanup_pending` (51), `integration.human_blocked` (11). The oldest is from 8 September; the most-retried has 6,169 attempts. The retry has exponential backoff and no cap (`src/integration/outbox.py:297-322`). `integration.human_blocked`, the one event that means "a human is needed here", has no listener.

**Ownership rows outlive their writers.** 133 branch-owner rows are `reserved` and 4 `attached`; 116 of the reserved rows belong to tasks that are already `COMPLETED` and 10 to tasks still `DEFINED`. 283 `integration_owner_recoveries` rows say `released` and 9 `preserved_and_released`: stranded-fence recovery is a steady-state activity on this install, yet the automatic sweep that does it (`_sweep_stranded_owners`, `src/orchestrator/core.py:1253-1267`) ships disabled (`integration.owner_recovery_sweep`, `src/config.py:2270`), so it is run by hand through `aq integration release-owner` or `aq doctor --check integration.stranded_fences --fix`.

**Escalations are mostly the recovery path failing.** 54 escalations are `needs_human`. Many are `Supervisor delivery unavailable for project agent-queue`: the train's designated recovery owner, the supervisor, could not be reached, and that fact became a human escalation. 34 messages in two days name an incident; their subjects include `repair-no-progress`, `Repair stage 9 needs trigger rebind`, `Repair stage 9 expired; delegate cannot close`, `Task recovery: repair-bcab5af6-…-{1,4,6}`.

**Runs that end failed and wait for luck.** Playbook runs in the last three days (`playbook_v2_runs`, `playbook_step_receipts`):

| Rule | Completed | Failed | Typed outcome of the failed step |
|---|---|---|---|
| `root-train.promote-green-candidate` | 29 | 22 | `wait` 15 (a repair writer still holds the branch), `runtime_error` 6, `unauthorized` 1 |
| `parent-integration.dispatch-debug` | 6 | 20 | `stale` 14, `busy` 9, `human_required` 2 |
| `root-train.continue-closed-root-repair` | 20 | 12 | `wait` 8 |
| `root-train.construct-and-test` | 31 | 2 | `conflict` 19 (then dispatched), `source_moved` 1, `contract_violation` 1 |
| `parent-integration.promote-delivery` | 11 | 2 | `conflict` 7 (then repair started), `stale` 2 |
| `root-train.seal-due-frontier` | 380 | 0 | (438 of the 476 batches ever sealed were empty) |

A `wait` on promotion is documented as expected ("the first `integration.candidate_green` run ends on `wait`… that is expected", [troubleshooting](../../../docs/guides/integration-troubleshooting.md#a-green-batch-never-promotes)), and a separate service, `GreenPromotionReconciler` (`src/integration/green_continuation.py:149`), exists only to re-emit the event up to six times with backoff and then stop. That is the pattern in miniature: an event-driven rule cannot wait, so a second mechanism is bolted on to re-fire the event, and that mechanism has its own exhaustion.

**Noise that hides signal.** In the two-day log window: 6,400 warnings of the form `Completed train root <id> has PR … but no eligible review source` for eleven roots (one line per root per tick, no action attached); the `GitHub PR reviews` source exceeded the 5-second tick 3,450 times and the remote reconciliation pass runs its 18 sources serially (`IntegrationService._reconcile`, `src/integration/service.py:107-157`), so the review poll's latency is every other source's latency; 112 `Could not open the pull request for train root fleet-ember-…` warnings.

**Policy churn.** 81 `integration_rollout_transitions` rows in a month. The last twelve reasons are drains and re-enables: "Drain current work for a narrow admission-policy update", "Cancel authorization maintenance drain: nonterminal unclaimed branch reservations prevent reaching drained". `configure` requires the project disabled, desired disabled, not draining and no active work (`src/integration/controls.py:776-858`), and a reserved unclaimed branch owner counts as active work, so under continuous delivery the train in practice never drains; the explicit per-root authorization command exists for exactly that reason ([2026-10-01 spec](../../../docs/superpowers/specs/2026-10-01-explicit-root-authorization-design.md)).

**CI wall-clock.** Candidate CI on this repository: median 9.8 minutes from candidate construction to conclusive evidence, p75 16.7, maximum 148 (88 observations). Promoted batches: median 0.2 hours from seal to final evidence, p75 0.47, maximum 32.6. The shipped primary stage deadline is 30 minutes and the debug deadline 60 (`models.py:85-87`), and the design counts queue time, agent time and CI waiting against that clock ([design §9.1](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md)). A delegate that is filed, waits twenty minutes for a pool seat, pushes, and waits twenty minutes for CI has used its whole primary stage without a counted attempt.

### 2.3 Structural causes

**C1. Edge-triggered rules with no wait.** A V2 playbook rule is one activation per event: trigger, steps, terminal (`src/playbooks/runtime.py`). The shipped rules route on the command's typed outcome and, for every outcome that is not success, "end the current run without fabricating success" ([root-train.md, Failure handling](../../../src/prompts/integration_playbooks/root-train.md)). Nothing schedules a re-look. Progress therefore depends on another event arriving: a delegate closing (`integration.repair_delegate_closed`), a deadline firing, a green CI observation, a service tick that happens to re-emit. Where no such event exists, the subject sits. Each place this was noticed, a bespoke re-driver was added: `GreenPromotionReconciler`, `_schedule_construction_retry` (`src/integration/candidates.py:661`, retries `base_moved` every 60 s forever), `reconcile_delegate_reservations`, `RootPullRequestReconciler`, `CollectingParentRecovery`, `completion_recovery.py`, the orchestrator's own successor-stage dispatch (`src/orchestrator/core.py:1217-1229`). Revision 1's loop map lists nine separate loops; the policy survey found that the orchestrator path bypasses the playbook entirely (decision D8 in Appendix C) and that two parent rules (`file-children`, `checkpoint-parent`) listen for events no code emits.

**C2. Policy in Python; playbooks relay.** The survey of policy knobs and decisions ([Appendix C](#appendix-c-policy-knobs-python-decisions-and-what-the-playbooks-decide); summarized in [§3.5](#35-policy-as-a-decision-table)) found 41 decisions made in Python that a project might reasonably want to make differently: when to escalate (`repair.py:1114-1162`), the two-shape budget ladder (`:3926-4058`), what counts as progress (`:3943-3958`), who repairs and in which workspace (`:1417-1581`), how the playbook's literal stage number is overridden server-side (`:3033-3078`), batch membership and ordering (`src/integration/scheduler.py:834-963`), the construction-base choice coupled to `on_exhausted` (`candidates.py:314-321`), retention (`cleanup.py:537-555`), the push-only root delivery with no PR-merge option (`main_promotion.py:595-620`), and the development publisher's own park/repair/supersede rules (`development.py:1539-2066, 3834-4046`). The compiled `root-train` artifact has zero decision steps; `parent-integration` has one (`on_failed_child`). The project copies `agent-queue-root-train` and `agent-queue-parent-integration` compile to graphs identical to the shared ones; the "continuous policy" prose in the project copy is documentation, and the behaviour comes from `on_exhausted: continue` in the frozen JSON. One field, `on_exhausted`, carries three unrelated meanings: the repair ladder, "build from current main instead of the sealed base" (`candidates.py:314-321`), and "re-file source-CI repairs without limit" (`src/commands/integration_commands.py:133`).

**C3. Budgets measure the wrong thing.** The design's attempt budget ("an attempt is consumed only when it records a conclusive result for the exact candidate SHA", [design §10.2](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md)) is sound but, on this install, inert: 3 counted attempts in 83 stages. The clock budget is what fires, and it counts the one thing the writer cannot influence, queue and CI latency. The ladder then interprets expiry as exhaustion and allocates a successor with a fresh clock, which is how an operation reaches stage 13 with nobody having tried anything. There is no state for "the delegate has not been claimed yet" or "CI has been requested and is pending", so the expiry rule cannot distinguish capacity starvation from genuine failure.

**C4. Every incident became an operator control.** The operator-surface survey ([Appendix B](#appendix-b-operator-recovery-surface-49-actionable-controls)) classified the 49 actionable controls: 25 HAND (mechanical; the system had the signal and the proof), 11 DECISION (a human picks between materially different outcomes: `eject`, `abort`, `enable`, `authorize-root`, `adopt`, `settle-parked`, `develop`, `cancel-preserving`, `resume`, `task close --obsolete`, `ci-repair-adopt`), 7 MIGRATION (one-off: `reconcile-unmaterialized`, `waive-history`, `adopt-legacy-deliveries`, `bind-legacy-repositories`, `materialize-root`, `rebind-reused-identity`, `migrate-provenance`), 6 DIAGNOSTIC. Three of the HAND controls duplicate ticks that already run (`release-delegates`, `stale_schedule --fix`, `reserve-owner` for delegates). Two cannot be run by the supervisor the profile tells to run them (`record-noop`: refused for non-LOCAL callers, `integration_commands.py:34-41, 499-500`; `recover-unwritten-resolution`: LOCAL-only in `promotion.py:477-479`). Every HAND control is a place where the train stopped and waited for a person to type what the daemon could have done.

**C5. `human_required` is the catch-all.** `RepairService.dispatch` (`repair.py:1346-1730`) returns `human_required` from at least eight distinct situations: the delegate task row is missing and cannot be restored, the writer kind is not a delegate, the delegate's identity does not match, the task id already exists, the target has no owner row, and several ownership shapes. None of these is a decision a human needs to make; they are states the code did not expect. Routing them to a human gate makes the unexpected state look like policy and gives the policy layer nothing to act on.

**C6. Three delivery engines.** `development` mode (`development.py`, 4,172 lines, plus validation, result parser, stalls, settlement, delivery truth, delivery observer: about 7,000 lines) re-implements candidate assembly, parking, conflict repair with its own three-generation chain, publication with its own journal, and its own stall detection and supervisor messaging, next to the train's. The legacy `pr_merge` path (`integration.merge_ci_policy`, `src/git/ci_gate.py`) is a third. The [concept page](../../../docs/concepts/integration.md#modes) describes development mode as what you get when you drop the verifier, the attestation and the repair ladder from the train; that is a policy difference, not an engine difference.

**C7. Frozen policy as a JSON blob forces drains.** `HierarchicalIntegrationPolicy` is copied into every batch, operation and stage at creation (`scheduler.py:528-548`, `repair.py:194-280`, `parent_completion.py:83-147`). Freezing is right: a running subject must not change its rules under it. Freezing the *whole project policy as one blob* and then refusing any edit while any subject is live is what forces the drain. The playbook artifact pin that already exists (`integration_operation_artifact_pins`, `PlaybookRoute.artifact`) is the correct unit of freezing: a subject runs under the policy version it started with; new subjects take the new version.

**C8. Unbounded noise.** Items in §2.2: unsubscribed outbox events retried forever (D41), per-tick warnings for the same eleven roots, the serial remote pass gated by the slowest poller, 438 empty batch rows.

## 3. Target shape: one reconciler, twenty primitives, policy in the playbook

### 3.1 Principles

1. **Level-triggered, not edge-triggered.** The reconciler derives the next action from the subject's current durable state on every pass. Events are accelerators: they pull `next_due_at` forward. Losing an event costs latency, never progress.
2. **One subject, one owner, one due time.** Every integration subject has exactly one row that says what it is waiting for and until when. A subject with no due time is a bug the reconciler reports, not a state it tolerates.
3. **Mechanism returns facts and typed outcomes; policy returns decisions.** A primitive never decides what to do next. It observes, mutates one thing idempotently under a fence, and reports. The playbook maps facts to the next primitive call.
4. **Every refusal has a next action.** `busy`, `stale`, `wait`, `base_moved`, `pending` are facts with a natural wait; `unknown(reason)` is a fact that policy maps to a bounded wait plus a message; none of them ends a subject's progress.
5. **Humans decide; they do not hand-crank.** A human gate is created by policy, carries a question and choices, and may carry a default-after-timeout chosen by policy. Mechanical recovery is never a human's job.

### 3.2 The subject model

An integration *subject* is one of:

- a **root batch**: a sealed manifest of reviewed sources, a construction base, zero or more candidate revisions, headed for the default branch;
- a **parent episode**: one collection generation of a parent task's branch, with child receipts, a verification subject and a completion;
- a **source**: one reviewed task head awaiting admission (source CI, review state, ancestry).

Each subject row carries: `kind`, `project_id`, `repository_id`, `policy_artifact` (the pinned playbook artifact it runs under), `phase`, `next_due_at`, `wait_reason`, `writer` (a lease: task id, fence token, session, claimed_at, last_push_at, stop proof), `budget` (stage ordinal, started_at, deadline_at, attempts counted, class), `head` (the exact SHA the phase refers to), `gate_id` (when held), and a journal pointer.

Phases, shared by all subject kinds: `admitting`, `building`, `testing`, `repairing`, `promotable`, `publishing`, `published`, `cleaning`, `done`, `waiting(reason, until)`, `held(gate)`. A root batch and a parent episode walk the same phases; the only differences are the target ref (`main` versus the parent branch), the admission predicate, and whether a verifier is filed, all three of which are policy.

Two definitions are made precise because today's budget logic depends on them being vague:

- **Attempt**: one conclusive CI observation (green or red, trusted producer, exact head) for a head the subject's writer published. Infrastructure outcomes, cancelled runs and superseded runs are not attempts (unchanged from the design).
- **Progress**: the subject's head moved to a head the writer published, or an attempt was counted. Queue time, a new writer identity, a new generation number and a repeated check on the same head are not progress (unchanged from the 2026-10-01 no-progress spec).

And one new fact the observer must return because the expiry rule cannot work without it: **writer status**: `none`, `filed` (task exists, unclaimed), `claimed` (session alive, no push yet), `working` (pushed at least once), `stopped` (proof of stop: session gone, workspace clean or preserved), `unknown`.

### 3.3 The reconciler

One loop, one function per subject, run for every subject whose `next_due_at <= now`, paged and isolated as `IntegrationService._reconcile` already is (`service.py:107-157`, keep its exception isolation and its one-remote-pass concurrency boundary):

```
for subject in due_subjects(now):
    facts    = observe_subject(subject)              # primitive 1, read-only
    decision = policy(subject.policy_artifact, facts) # the playbook's decision table
    outcome  = act(decision)                          # one primitive call, fenced, idempotent
    schedule(subject, outcome)                        # sets next_due_at; never leaves it null
```

`schedule` enforces the never-blocked rule: a `wait` decision must carry an `until`; `held` must carry a gate; anything else gets `next_due_at = now` (progress continues) or `now + backoff` (a transient refusal). An event for a subject (`ci_completed`, `delegate_closed`, `push_observed`, `gate_answered`, `child_completed`) sets `next_due_at = now` and nothing else. The outbox becomes an accelerator with a short retention; an event for a subject that does not exist or a type with no consumer is recorded in the audit log and dropped.

This is how `GreenPromotionReconciler`, the construction retry, delegate-reservation reconciliation, root-PR reconciliation, collecting-parent recovery, completion recovery, the orchestrator's successor dispatch, the repair-deadline tick, the candidate-CI tick, the parent-CI tick, the intent tick and the cleanup tick collapse into one visit: they are all "look at the subject and do the next thing".

The reconciler is mechanism. It makes no policy choice: `policy(...)` is the compiled playbook's decision table evaluated against `facts`, exactly as the one existing decision step does today (`parent-integration` `failed-policy`, a `cases`/`when`/`default` step over a binding; see the compiled artifact), only with the whole observation as the binding.

### 3.4 Mechanism primitives

Twenty primitives. Each is a command contract, playbook-callable, with a small closed set of outcomes; each either reads or performs one fenced mutation; each is replayable. "Replaces" names the module clusters of the revision 1 inventory whose logic folds into it.

| # | Primitive | Does | Outcomes | Replaces (revision 1 inventory modules) |
|---|---|---|---|---|
| 1 | `integration_observe_subject` | Returns the complete fact set for one subject: members/children (head, base, review state, ancestry), remote heads, default-branch head, candidate head and revision, CI evidence per exact head (`none`/`pending`/`green`/`red`/`infra`, age), writer status and lease, budget (ordinal, started_at, deadline_at, attempts), unresolved journal rows, open gate, conflicts (member, files), base moved. Read-only. | `observed`, `not_found` | `status.py`, `controls.py` preflight, `admission.py`, `delivery_truth.py`, `delivery_observer.py`, the 15 report-only doctor checks, `live_operations.py`, `stale_schedule.classify`, `repair_progress.py` |
| 2 | `integration_seal` | Snapshot the admissible frontier into a new root batch subject. Admission predicate is supplied as facts (review state, kind, allowlist, source CI) and the policy says which must hold. Produces no row for an empty frontier. | `sealed`, `empty`, `busy` | `scheduler.py` (seal half), `settling.py`, `stale_schedule.py` |
| 3 | `git_materialize_ref` | Create a ref at an exact base, refusing an unexpected tip. | `created`, `exists_exact`, `exists_other` | `hierarchy.materialize_exact_branch`, `branch_materialization.py`, `root_materialization.py` |
| 4 | `git_merge_members` | Apply an ordered member list onto a base in the retained clone, with the repository's generated-file regeneration rule; record each member's result. One implementation for root candidates, parent collection and development batches. | `merged(head)`, `conflict(member, files)`, `source_moved(member)`, `base_moved` | `candidates.py` (construction), `collection.py`, `child_delivery.py` (apply), `development.py` (assembly), `regeneration.py`, `migration_heads.py` |
| 5 | `git_preserve` | Push an exact SHA to a retention ref nobody owns. | `preserved`, `exists` | `development.py` preservation, `owner_recovery` preserve, `preserved_repair.py` |
| 6 | `git_publish` | Journal row first, then a fenced expected-old push, then remote read-back. One implementation for parent branches, candidate branches, `main` and conflict resolutions. The lease and the journal are the only concurrency control. | `published`, `target_moved`, `unknown_after_push` | `promotion.py`, `main_promotion.py`, `development.py` publish, `push_conflict_resolution`, `candidate_ref_mutations`, `attestation` prewrite |
| 7 | `git_ancestry` | Exact ancestry facts between SHAs and refs (is-ancestor, merge-base, patch-equivalence). | `facts` | `source_ancestry.py`, `delivery_truth.py`, `pr_delivery.py` proof, `legacy_deliveries.py` proof |
| 8 | `ci_request` | Make CI run on an exact head (publish the snapshot ref or dispatch the workflow) and record `requested_at`. | `requested`, `already_running`, `unavailable` | `parent_ci.publish_parent_snapshot`, the `aq/integration/**` branch push |
| 9 | `ci_observe` | Trusted exact-SHA check observation for one head; appends evidence; classifies conclusive versus infrastructure. | `green`, `red`, `pending`, `infra`, `none`, `untrusted` | `ci.py`, `candidate_ci.py`, `parent_ci.py` (observe half), `source_ci.py`, `github_review_poll.py` (CI half) |
| 10 | `ci_attest` | App-mode attestation check run on an exact head. | `attested`, `already`, `refused` | `attestation.py`, `hosted_attestation.py`, `trust_manifest.py` (verification), `app_mode.py` |
| 11 | `writer_file` | File one writer task for a subject with a class hint, a brief and a budget; idempotent on (subject, ordinal). One filing path for repair delegates, verifiers, source-CI repairs and development repairs. | `filed`, `exists`, `configuration_blocked` | `repair.py` delegate creation, `parent_completion` verifier filing, `development.ensure_repair`, `integration_commands` source-CI repair |
| 12 | `writer_lease` | Reserve or transfer the subject's target ref to one writer under a new fence token with an expiry. | `leased`, `busy(holder)`, `stale` | `ownership.py`, `canonical_reservation.py`, `transfer_owner`, `repair.reserve_delegate`, `accepted_repair.py` |
| 13 | `writer_stop_proof` | Prove a writer stopped (session gone or drained, workspace unlocked, local work pushed or preserved) and release its lease; preserves unpublished work with primitive 5. | `released`, `preserved_and_released`, `live`, `unknown` | `owner_recovery.py`, `stale_owners.py`, `finished_owners.py`, `drain_owners.py`, `delegate_release.py`, `completion_recovery.py`, `obsolete_close` owner release |
| 14 | `record_receipt` | Record that an exact source head reached an exact target head (child into parent, member into `main`). | `recorded`, `exists` | `task_delivery_receipts` writers, `root_intent_members`, `episode_receipt_acceptances`, `record_noop` |
| 15 | `record_attempt` | Count one conclusive observation against the subject's current budget. | `counted`, `not_an_attempt`, `stale` | `repair.record_result`, `attestation._enqueue_candidate_result` |
| 16 | `record_decision` | Append the policy's decision for a subject to its journal (what was observed, what was decided, by which artifact). | `recorded` | new; today's decisions are implicit in log lines and dossiers |
| 17 | `wait` | Set `next_due_at` and `wait_reason` for a subject. The only way a subject idles. | `waiting` | every bespoke re-driver in C1 |
| 18 | `gate` | Create or reuse one human gate for a subject with a question, choices and an optional default-after-timeout; `held` until answered or defaulted. | `created`, `reused`, `answered(choice)` | `gate_create` use in `parent-integration`, `escalation_create` use in `ci-main-sentinel`, `_human_block_on` |
| 19 | `eject` | Remove one member or child from a subject with an audited reason; the subject re-enters `building`. | `ejected`, `not_a_member` | `controls.eject`, `removal_guard.py` (part), repair-ejection amendment |
| 20 | `cleanup` | Delete or retain refs and close PRs for a published subject under the policy's retention facts; idempotent; bounded retries recorded on the subject. | `clean`, `pending`, `irreversible_marker` | `cleanup.py`, `release.py`, `branch_discard.py`, `delivery_branches.py`, `pr_delivery.py` |

Three things are deliberately *not* primitives: `resume` (a subject in `held` is resumed by answering its gate), `abort` (a policy decision expressed as `eject` of every member plus `cleanup`), and any `rebind`/`redrive`/`recover-*` (each is an `observe` fact plus a policy decision plus one of the primitives above; [§4](#4-blocker-catalogue-a-path-forward-for-every-non-human-stop) shows the mapping).

### 3.5 Policy as a decision table

The project's integration playbook has one rule per subject kind triggered by `integration.subject_due` (the reconciler's visit) and the same rule body: observe, decide, act. The decision step is the policy. In the authored markdown form the compiler already accepts, a root-train policy reads like this (illustrative; the exact names are for the implementation to fix):

```markdown
## Rule: root-batch

On `integration.subject_due` for a root batch, call `integration_observe_subject` and bind
`s`. Then decide:

- `s.phase` is `admitting` and the frontier is non-empty every 300 s: `integration_seal`;
  empty: `wait` 300 s.
- `s.phase` is `building`: `git_merge_members` on the observed default-branch head.
  `merged`: `ci_request`, then `wait` until CI reports. `conflict` with `s.conflict_count`
  below 4: `writer_file` class `standard-high`, budget 90 min and 2 attempts, then `wait`
  for the writer. `conflict` otherwise: `eject` the conflicting member with reason
  `unresolved-after-budget`, and continue building.
- `s.phase` is `testing` and `s.ci` is `green`: `git_publish` to `main` expected-old
  `s.base`. `red`: `writer_file` for the current ordinal under the ladder
  `[standard-high 90m/2, deep-high 120m/2]`, then `wait`. `pending` older than 45 min:
  `ci_request` again. `infra` three times in a row: `gate` "CI infrastructure is failing"
  with choices `retry`, `eject`, `hold`, default `retry` after 2 h.
- `s.writer` is `filed` past its deadline: `wait` another 30 min (capacity, not failure)
  and send one message. `claimed` or `working` past its deadline with no attempt:
  `writer_stop_proof`, then `writer_file` the next ordinal. Ladder exhausted on an
  unchanged head: `eject` the members touched by the writer's commits, else `gate`
  "repair exhausted" with default `eject-newest` after 24 h.
- `s.writer` is `stopped` with unpublished work: `git_preserve`, `writer_file` next
  ordinal.
- `s.base_moved` while `testing`: `git_merge_members` on the new base (rebuild).
- `s.phase` is `published`: `cleanup` with retention 7 d for failed work, delete
  successful source refs; `pending` after 5 tries: `wait` 1 h.
- `s.gate` answered: apply the answer (`eject`, `retry`, `hold`).
```

Every line is a `cases`/`when` entry over `s` in the compiled artifact. Every frozen field of today's `HierarchicalIntegrationPolicy` and every one of the 41 Python decisions maps to one line of this kind:

| Today (frozen JSON field or Python decision) | In the policy table |
|---|---|
| `repair.primary_seconds`, `primary_attempts`, `debug_seconds`, `debug_attempts`, `debug_intelligence_class`, `primary_intelligence_class` (`models.py:85-89, 156`) | the ladder literal `[class budget/attempts, …]` per subject kind |
| `on_exhausted` (`models.py:91`; three meanings) | three separate lines: ladder exhausted; construction base; source-CI repair cap |
| `conflict_scope` (`models.py:90`) | whether `writer_file` on conflict receives one member or the batch |
| `on_main_moved` (`models.py:193`) | the `base_moved` line |
| `on_failed_child` (`models.py:192`) | the parent rule's failed-child line: `block`, `gate`, or `eject` the child |
| `branchless_parent` (`models.py:191`) | whether the parent rule files a verifier |
| `admission`, `authorized_task_ids`, `source_ci` (`models.py:152-153, 92`) | the `admitting` predicate; the allowlist is a vault list the observer reads, not a frozen field |
| `required_checks` (`models.py:74-79`) | an input to `ci_observe`, pinned per subject at seal |
| `cleanup.*` (`models.py:170-174`) | the `published` line |
| `interval_seconds` (schedule row) | the `admitting` cadence |
| Batch size (none today, `scheduler.py:844-884`) | an `admitting` cap |
| D5 no-progress rule (`repair.py:3943-3958`) | "ladder exhausted on an unchanged head" |
| D6 three-consecutive-failures message (`repair.py:1192-1200`) | a `message` action on a `when` |
| D13 construction base (`candidates.py:314-321`) | explicit `building` line |
| D19 unbuildable source aborts the batch (`candidates.py:414-573`) | `eject` the source, continue |
| D20 `base_moved` retried every 60 s forever (`candidates.py:649-701`) | `wait` with the project's backoff |
| D28 push-only root delivery (`main_promotion.py:595-620`) | `git_publish` to `main`, or `open PR and wait for merge` for projects that require PR review on `main` |
| D35 development ordering, park-whole-batch, advisory validation (`development.py:1711-2044`) | the development variant of the same table ([§5.6](#56-development-mode-as-a-policy-variant)) |

Per-project variation is then what the decision note asks for: copy the playbook, change the lines, review the bundle through the existing reviewed-bundle path (`scripts/rebuild-reviewed-playbook-artifacts.py`, `src/prompts/reviewed_playbooks/`), activate it at project scope. A subject pins the artifact it started under (today's `integration_operation_artifact_pins`), so activating a new version never requires a drain: running subjects finish on the old table, new subjects start on the new one. The admission allowlist becomes data the observer reads (a vault file or a task label), so authorizing one more root is an edit, not a policy generation.

Three guard-rails keep the table from becoming the new Python:

- The compiler refuses a policy whose table has no `wait` bound on every non-terminal branch and no default on every `gate` (or an explicit `no-default` that names the human decision). This is where the never-blocked rule is enforced at authoring time.
- A primitive's outcome set is closed and small; a policy that names an outcome the contract does not have fails compilation (today's contract fingerprint already does this).
- The shipped default table is the current continuous policy, transcribed line by line, so the first cut changes no behaviour.

### 3.6 The never-blocked guarantee

At the end of every reconciler visit a subject is in exactly one of three states:

1. **Progressing.** An action was taken and `next_due_at = now` (continue) or `now + short backoff` (a transient refusal such as `busy(holder)` or `unknown_after_push`).
2. **Waiting** with `wait_reason` and `next_due_at <= now + max_wait`, where `max_wait` is a project constant (the shipped default could be one hour). A wait past its due time is itself a fact (`s.wait_overdue`) that the table must handle; the compiler refuses a table that does not.
3. **Held** by a gate row. The gate carries a question, choices, and either a default-after-timeout or an explicit `no-default` annotation.

The reconciler itself enforces 1 and 2 (it will not store a null due time or an unbounded wait). The compiler enforces 3. Therefore the only way for a subject to stop indefinitely is a gate marked `no-default`, which is an explicit human decision made by the policy author and shown on the dashboard as such. "The train never gets blocked" becomes a property that is checked, not a hope.

Two consequences:

- **Ejection is the universal path forward for a batch.** A member that cannot be made green within the policy's budget is ejected with an audit row and goes back to the frontier with a repair task attached; the rest of the batch continues. Today this exists as a manual `eject` plus the repair-ejection amendment ([design §7.2a](../../../docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md)); it becomes the default policy outcome, and a project that would rather hold writes `gate … no-default` on that line.
- **Supervisor recovery is not a stop state.** Today `supervisor_recovery` and `escalated` are terminal for the mechanism ("replayed evidence, timeouts, dispatches and service ticks cannot allocate another delegate", [no-progress spec](../../../docs/superpowers/specs/2026-10-01-repair-no-progress-retirement-design.md)) and the supervisor is expected to run a command. In the target shape the same condition is a fact (`ladder_exhausted`, `no_progress`) with a policy line; a message to the supervisor is an *action* the line may add, never the thing the subject waits for. The 54 open "supervisor delivery unavailable" escalations are what happens when a human-shaped recovery path is the only path.

### 3.7 Human decisions that stay binding

The supervisor's note asks that explicit human decisions be preserved. They are, and they are the only things that may hold a subject:

- A **reviewer's rejection** of a source head or of a reviewed playbook bundle. A rejected source is not admissible; the frontier excludes it until a new head is approved (unchanged).
- A **product hold** on a task or project (`manual_pause`, project inactive). The observer reports it; the policy's only legal action is `wait` (unchanged).
- A **gate answer**. A policy line that creates a gate without a default stops the subject until a human answers; the answer is recorded with primitive 16 and applied on the next visit.
- An **abort** or **eject** chosen by a human through the gate's choices, or directly through the two remaining operator decision commands ([§5.1](#51-operator-controls)).
- An **explicit root authorization** for a kind the admission predicate does not admit (today's `authorize-root`): it becomes an entry in the admission list, still recorded with operator, reason and exact head.
- **Approval of a policy change**: a new playbook artifact goes through the same reviewed-bundle approval as today.

What is *not* a human decision and is removed from the human's hands: recovering a stopped writer's lease, re-dispatching a stage, re-requesting CI, rebinding a stage to the head that was actually published, reopening a collection the daemon cancelled, clearing a schedule request whose batch ended, retrying cleanup, and the rest of the HAND list in [§4](#4-blocker-catalogue-a-path-forward-for-every-non-human-stop).

## 4. Blocker catalogue: a path forward for every non-human stop

A code sweep for this revision found 108 places where the train stops making progress on its own ([Appendix A](#appendix-a-stop-point-sweep-108-rows) lists each with file:line). Two facts explain most of them: a failed playbook run is never retried (`src/playbooks/runtime.py:577-611`), and while any batch is in a live lifecycle, including `human_blocked`, every later sweep coalesces into the existing request and the batch's lease keeps being renewed (`src/integration/scheduler.py:184-221, 309-317`), so one stuck batch freezes the whole project's train.

The table groups the 108 into classes. For each class: how it is unblocked today, the automatic path in the target shape, and the policy line that governs it. "Human decision" marks the classes where a human genuinely chooses between different outcomes; everything else is a hand that the reconciler replaces. Sweep ids (A1…G8) refer to the rows of Appendix A.

| Class (sweep ids) | Today's exit | Automatic path in the target shape | Policy line |
|---|---|---|---|
| **Typed refusal ends the run** (`wait`, `busy`, `stale`, `configuration_blocked` on dispatch, promote, transfer, release, verify, complete; A18, A21, A24, B7, B9, B13, B16, G5) | A later event, a bespoke re-driver, or `aq playbook run` by a supervisor | Every refusal is a fact with a wait: `busy(holder)` waits for the holder's lease expiry or stop proof; `stale` re-observes immediately; `wait` is a bounded wait; `configuration_blocked` is a gate with the exact field named. The reconciler revisits on `next_due_at`; no run is terminal. | `max_wait`; backoff |
| **Stage expires with no work done** (A13, A17, A19, B11, C1; the stage-13 ladder in §2.2) | Successor stage, then `human_required`; `resume`/`abort`/`cancel-preserving` | The observer returns writer status. `filed` past deadline is capacity: extend and message once. `claimed`/`working` with no attempt: stop-proof, then next ordinal. Ladder exhausted on an unchanged head: `eject` the touched members (root) or the conflicting child (parent), else `gate` with default. Attempts and clock are both facts; the table says which one ends a stage. | the ladder literal; `on_exhausted` line; `eject` default |
| **No progress between stages** (A20, C10, C11; `recover-preserved-repair`, `rebind-detached-repair`, `rebind-repair`) | `escalated` plus a supervisor message; three operator rebind commands | A stage is bound to the head the observer sees published, never to a workspace-local head; a stopped writer's unpublished work is preserved by `git_preserve` and the next writer's brief names the preserved ref. There is no detached stage to rebind. Superseded intents are a fact (`s.intent` names the current one). | none needed |
| **Green but not promoting** (A18, A22) | Six continuations then a warning; `aq playbook run` replay; an unknown push outcome blocks every later promotion with no command | `promotable` is a phase; the writer holding the branch is `busy(holder)` with a bounded wait; `unknown_after_push` is resolved by `git_ancestry` on the next visit (the remote either contains the prepared SHA or it does not) and the journal row is finalized either way. | `max_wait` |
| **Build blocked before stage 0** (A11, A12, A10) | Nothing; replay `integration.sealed` by hand or `cancel-preserving` | `building` is a phase with a due time regardless of whether a stage exists; `git_merge_members` is re-run on every visit; `configuration_blocked` is a gate naming the field. | `max_wait` |
| **Attestation or cleanup prewrite with unknown outcome** (A16, A26) | Nothing; no command | `unknown_after_push` fact resolved by read-back on the next visit (`ci_attest` and `cleanup` are idempotent on the exact head); after `max_wait` without resolution, a gate naming the write. | `max_wait`; gate |
| **Stranded or stale branch owners** (D1–D5, D7, D8; 283 live recoveries) | `release-owner`, `release-stale-owners`, `reserve-owner`, three doctor checks, a sweep that ships disabled | `writer_stop_proof` runs on every visit of a subject whose writer status is `stopped` or `unknown`; a lease has an expiry; a finished task's lease on a delivered ref is released by `cleanup`. Refusals (`live`, `checkout_in_use`, `origin_unreachable`) are facts with waits. | none |
| **Collection stalls** (B2, B4, B5, B6, B8) | `record-noop`, `redrive-child`, `redrive-root`, `reopen-collection` | A no-code child (`head == base`, close `pass`) is a `record_receipt(noop)` the observer proposes and the table accepts; proof failures wait with backoff; a parent is a subject visited on its due time whether `PAUSED` or `BLOCKED`; a cancelled collection operation does not exist (cancel is `eject` of the current child, the episode continues). | parent rule lines |
| **Failed child** (B1) | Parent paused forever unless `ask`; no way to record skipped | Policy chooses: `block` (gate, no default), `ask` (gate with default), or `eject` (the child is removed from the episode with a receipt of kind `skipped`, the parent completes without it, the child stays FAILED and visible). | `on_failed_child` line |
| **Parent CI never counts** (B14, B15, A15; 4 parent evidence rows ever) | Clock expiry; `record_result` answers `stale` when no stage exists | `ci_request` records `requested_at`; `pending` older than the project's CI p95 is re-requested; red CI with no writer files one (the table does not need a stage to exist first). | `ci_pending_max`; the red line |
| **Train never sweeps** (A1–A3, G2–G4, A27) | Stale-request classifier, `clear-stale-request`, doctor `--fix` | There is no schedule request. `admitting` is a phase of a per-project subject visited every `interval`; a seal that cannot proceed is a wait; a batch in any phase does not stop the next seal (two subjects may coexist when the policy allows; the shipped default keeps one). | `interval`; `max_live_batches` |
| **Events nobody consumes** (G1, G6, B12, B18; 618 live rows) | Retried forever | The outbox accelerates; an event with no subject or no consumer is recorded and dropped; `human_blocked` and `configuration_blocked` are gates, not events. | none |
| **One hung remote call stops every source** (G7) | Daemon restart | Each primitive call runs under a timeout; a timed-out visit is a `wait` with backoff on that subject only. | `call_timeout` |
| **Source admission** (A4, A5, A8, A9, F4) | Review approval, `authorize-root`, reopen the task | Admission is the observer's facts against the table's predicate; a red or conflicting source files one repair under the ladder; an ancestry failure reopens once then files a repair; a migration collision is a conflict handled like any other. Review approval stays a human decision. | `admitting` predicate |
| **Root PR and review-source identity** (A6, A7, E16; 6,400 log lines) | `redrive-root`, `materialize-root`, `close-delivered-pr` | A root's PR is opened by the `published` line of the parent episode (or at close for a leaf root) and retried on every visit; a root without an identity the train can seat is reported once with a gate, not warned per tick; a PR whose work is on `main` by `git_ancestry` is closed by `cleanup`. | `published` line |
| **Development parked and unrepaired** (E1, E2, E6–E11, E13, E14) | `sweep --retry`, `adopt`, `settle-parked`, `cancel-preserving`, two doctor checks | Development mode is the same table with local validation ([§5.6](#56-development-mode-as-a-policy-variant)); parked content is a conflict with a writer; exhausted generations are the ladder's `eject` line; an unexplained remote move or history loss is a gate (human decision). | development table |
| **Repair budget exhausted under `human`** (A19, B11, E7) | `resume`, `abort` | `gate` with choices `retry`, `eject`, `hold`; `resume` is the `retry` answer; `abort` is `eject all`. | `on_exhausted` line |
| **Cleanup conflicts and drains** (A25, D7) | `retry-cleanup`, `release-stale-owners` | `cleanup` is idempotent and revisited; a drain is "no subject in a live phase", which the reconciler can always reach because every subject progresses or is held. | retention |
| **Ambiguous writes refused by `resume`/`abort`** (C6, C7, C12, C13) | Four recovery commands, two LOCAL-only | Ambiguity is `unknown_after_push`, resolved by read-back; `recover-unwritten-resolution` is "the reserved resolution was never pushed", which the observer sees (`resolution_push_started_at` null) and the table handles by re-leasing the writer. | none |
| **Policy or route misconfiguration** (A1, B12, C3, G2, E3, E4, F3) | Edit config, drain, re-enable | A gate naming the exact field, created once per subject; the subject waits; no drain. | gate |
| **Legacy identity** (B20, B21, D6, E15) | Seven migration commands | Keep the commands until their counts are zero ([§5.1](#51-operator-controls)); the target shape has no concept of a legacy subject. | n/a |
| **Sentinel** (F1–F5) | Escalation after two attempts | The sentinel becomes a source subject whose "red main" admission files a repair under the same ladder; its escalation is a gate with a default (`retry`). | ladder |

Human decisions in the table above, and only these, may stop a subject: review approval or rejection (A5, B3), a failed child under `block` (B1), budget exhaustion under a `no-default` gate (A19, B11, E7), an unexplained default-branch move or history loss (E1, E2), `eject`/`abort` chosen through a gate, and the migration one-offs while they exist.

## 5. Simplification: what to merge, what to delete, what to keep

### 5.1 Operator controls

| Class | Count today | Target |
|---|---|---|
| HAND (mechanical) | 25 | Deleted; each is a line in [§4](#4-blocker-catalogue-a-path-forward-for-every-non-human-stop). `flush` survives as "set `next_due_at = now`". |
| DECISION | 11 | Four remain: `aq integration gate answer <gate> <choice>` (replaces `resume`, `abort`, `eject`, `adopt`, `settle-parked`, `cancel-preserving`, `task close --obsolete`, `ci-repair-adopt`), `aq integration authorize <task> --head` (admission list entry), `aq integration policy activate <artifact>` (replaces `enable`/`develop`/`configure`), and `aq integration hold <subject> --reason` (an explicit product hold, which today is `manual_pause`). |
| MIGRATION | 7 | Kept behind a `--legacy` group until their doctor counts read zero on the operator's install, then deleted with their modules ([§5.2](#52-modules)). |
| DIAGNOSTIC | 6 (+15 report-only doctor checks) | Two: `aq integration status` (the observer's output for a project or subject, including the blocker, wait reason, due time and gate) and `aq integration explain <subject>` (the last recorded decision and why). |

Every control that remains is callable by the supervisor and the local operator alike; the two mismatches found by the survey (`record-noop` refused for the supervisor the profile tells to run it; `recover-unwritten-resolution` LOCAL-only despite the grant) disappear with the commands.

### 5.2 Modules

Target layout for `src/integration/`, with the revision 1 inventory modules each absorbs:

| Target module | Absorbs |
|---|---|
| `subjects.py` (subject rows, phases, due times, journal) | `models.py` (policy models removed), `scheduler.py` (lease and request halves), `settling.py`, `stale_schedule.py`, `outbox.py`, `green_continuation.py`, `completion_recovery.py`, `collecting_parent_recovery.py`, `cancelled_collection_recovery.py`, `live_operations.py`, `status.py` |
| `observe.py` (primitive 1) | `admission.py`, `delivery_truth.py`, `delivery_observer.py`, `repair_progress.py`, `source_ancestry.py`, `publishable_artifact.py`, `delivery_path.py`, `preflight.py`, the 15 report-only doctor checks |
| `gitops.py` (primitives 3–7) | `hierarchy.py` (materialize, resolve, verify), `branch_materialization.py`, `root_materialization.py`, `candidates.py` (construction), `collection.py`, `child_delivery.py` (apply), `promotion.py`, `main_promotion.py`, `development.py` (assembly, preserve, publish), `regeneration.py`, `migration_heads.py`, `epic_branch.py` |
| `ci.py` (primitives 8–10) | `ci.py`, `candidate_ci.py`, `parent_ci.py`, `source_ci.py`, `attestation.py`, `hosted_attestation.py`, `trust_manifest.py`, `app_mode.py`, `review_evidence.py`, `github_review_poll.py` |
| `writers.py` (primitives 11–13) | `repair.py` (delegate filing, reservation, dispatch), `ownership.py`, `canonical_reservation.py`, `owner_recovery.py`, `stale_owners.py`, `finished_owners.py`, `drain_owners.py`, `delegate_release.py`, `accepted_repair.py`, `published_repair_handoff.py`, `repair_rebind.py`, `detached_repair_rebind.py`, `preserved_repair.py` |
| `records.py` (primitives 14–16) | receipt, attempt and decision journals; `provenance.py`; `root_authorization.py` (the admission list) |
| `gates.py` (primitives 17–19) | `_human_block_on`, `_supervisor_recovery_on`, `controls.eject`, `removal_guard.py` (ownership guards move to `writers.py`) |
| `cleanup.py` (primitive 20) | `cleanup.py`, `release.py`, `branch_discard.py`, `delivery_branches.py`, `pr_delivery.py`, `root_pull_requests.py` |
| `reconciler.py` | `service.py` (keep its isolation and concurrency boundary), the orchestrator's integration hooks (`core.py:1213-1267`) |
| deleted outright | `controls.py` (except the policy activation CAS), `recovery_controls.py`, `identity_rebind.py`, `provenance_migration.py`, `legacy_deliveries.py`, `legacy_repositories.py`, `manual_delivery.py`, `obsolete_close.py` (its task-side close stays in sessions), `train_onboarding.py` (replaced by a policy template), `development_validation.py`, `development_result_parser.py`, `development_stalls.py`, `development_settlement.py`, `epic_dependencies.py`, `epic_pr.py`, `parent_completion.py` (its verification logic moves to `observe.py` and `ci.py`) |

Nine modules, each with one responsibility. A line budget of 10,000 to 12,000 for `src/integration/` is realistic: the Git, CI and ownership primitives already exist in the revision 1 inventory and are mostly being de-duplicated (its overlap table lists the pairs), and the control, recovery, legacy and development-engine code is being removed rather than rewritten. The 61,000 lines of integration tests shrink with the controls they test; the invariant tests (exact SHA, fences, journal-before-push, trust) are ported to the primitives.

### 5.3 Tables

45 integration tables today. Target: `integration_subjects`, `integration_subject_members` (members, children, receipts of kind code/noop/skipped), `integration_writers` (leases with fence, expiry, stop proof), `integration_attempts` (CI evidence per exact head, the attestation publications), `integration_journal` (publishes, decisions, ejections, cleanup; the one audit trail), `integration_gates` (or reuse the existing `gates` table with a subject column), `integration_outbox` (short retention), and the two existing task-side tables `task_branch_origins` and `task_delivery_receipts`. Nine. The 36 others (`integration_batches`, `_batch_members`, `_candidate_revisions`, `_candidate_member_results`, `_candidate_publications`, `_candidate_resolutions`, `_candidate_ref_mutations`, `_root_intent_members`, `_repair_operations`, `_repair_stages`, `_repair_stage_evidence`, `_parent_episodes`, `_parent_verifications`, `_parent_operation_completions`, `_episode_receipt_acceptances`, `_parent_verification_evidence`, `_promotion_intents`, `_delegate_releases`, `_owner_recoveries`, `_branch_owners`, `_check_evidence`, `_attestation_publications`, `_cleanup_items`, `_release_results`, `_history_waivers`, `_history_waiver_consumptions`, `_rollout_transitions`, `_legacy_gate_applicability`, `_legacy_suppression`, `_legacy_deliveries`, `_operation_artifact_pins`, `_outbox_artifact_pins`, `_root_authorizations`, `_source_ci`, `_child_dispositions`, `project_integration_schedules`, `project_integration_leases`) are migrated into them or dropped. The migration is one revision per phase of [§6](#6-sequencing-and-acceptance), idempotent as the repository's migration rules require.

### 5.4 Outcomes

139 top-level codes become the closed sets in the primitive table ([§3.4](#34-mechanism-primitives)): about 45 across the twenty primitives, with `unknown(reason)` replacing the many spellings of "I did not expect this state". A policy that names an outcome a primitive does not return fails compilation, as today.

### 5.5 Doctor checks

20 become 3: `integration.subjects_overdue` (a subject past `next_due_at` by more than one interval: the reconciler is not visiting), `integration.subjects_held` (gates open, with age), `integration.trust` (App-mode anchors). Everything else the old checks reported is either an observer fact shown by `status` or no longer a state the system can be in.

### 5.6 Development mode as a policy variant

The development publisher ([concept page](../../../docs/concepts/integration.md)) is the train with: admission "every `COMPLETED` task with a pushed branch in this repository, dependency-ordered", no review requirement, no PR, local validation jobs instead of hosted CI, `advisory`/`focused`/`none` as the red-line choice, park-one-member-and-continue on conflict, a three-generation repair ladder, and `publish` by fenced push. Every one of those is a line in the decision table of [§3.5](#35-policy-as-a-decision-table); `ci_observe` gains a `local` producer that runs the policy's validation preset in a detached snapshot (the job runner exists: `src/jobs/`). The retained clone, the preservation ref and the journal-before-push are primitives 4–6. Nothing about development mode needs its own engine, its own journal, its own stall detector or its own repair chain. Projects currently in development mode (`matter-engine-cpp`, `agent-queue-web`) get the shipped `development` policy table, which transcribes `DevelopmentPolicy` field for field.

The legacy `pr_merge` gate (`integration.merge_ci_policy`, `src/git/ci_gate.py`) is the third engine; it is already suppressed for every project with a managed mode and should be removed with the `disabled` mode's last users.

### 5.7 Noise

Four changes that need no redesign: drop the unsubscribed outbox types or add sinks (618 rows, G1); log the "no eligible review source" condition once per root per state, not per tick; poll GitHub reviews only for roots whose PR `updated_at` changed since the last observation; do not insert a batch row for an empty seal (438 of 476 rows).

### 5.8 Relation to the revision 1 overlap table

The 24 rows of the revision 1 overlap table are all subsumed: the MERGE rows (adapters, retained-store and Git helpers, evidence folds, blocker construction, generated-file handling, cleanup mechanics, rebind diagnostics) are the de-duplication inside primitives 1, 4, 6, 9, 13, 15 and 20; the KEEP rows that preserved distinct authority (abort versus preserve, canonical versus delegate reservation, trust boundaries, retention policies) remain distinct as policy lines or as separate primitives (12 versus 13, 10 versus 9, 20 with retention facts), not as separate implementations.

## 6. Sequencing and acceptance

Each phase is deliverable on its own and leaves the train running. Phase 0 is in the current code; phases 1 to 4 build the target shape next to it and move subjects over one kind at a time.

### Phase 0: stop the bleeding (days, current code)

1. **Outbox**: drop or sink the five unsubscribed event types; cap retries at the project's `max_wait` (`src/integration/outbox.py:216-228, 297-322`).
2. **Stage expiry**: in `RepairService.expire` (`repair.py:1898-2029`), before `_activate_debug_on`, read the delegate's status; if the task was never claimed, extend the deadline by one primary budget and message once; if it was claimed and never pushed, run stop-proof and refile the same ordinal rather than allocating a successor. Cap successor stages per unchanged subject head at two.
3. **Refusal retries**: have the service's `repair dispatch` source (`service.py:143`) re-dispatch any active stage whose last dispatch answered `busy`, `stale` or `wait`, with backoff; raise `GreenPromotionReconciler`'s cap from six to unbounded with a one-hour ceiling on the backoff (`green_continuation.py:47-49`); add a parent-intent pass to `_tick_intents` (B7).
4. **Owner recovery sweep on by default** (`config.py:2270`); the 283 recoveries are the evidence that it is routine.
5. **`human_required` in dispatch**: return `unknown(reason)` with the exact reason string, message the supervisor once per (operation, reason), and leave the stage retryable by item 3 (`repair.py:1401-1437, 1476-1483, 1586-1595, 1674-1698`).
6. **Review poller**: condition the per-root warning on a state change; poll only changed PRs (`github_review_poll.py:40-125`).
7. **Empty seals**: return `empty` without inserting a batch row (`scheduler.py:541-548`).
8. **Call timeouts**: wrap each `_source` callback in `asyncio.wait_for` with a per-source budget so one hung GitHub call cannot stop the remote pass (`service.py:231-249`; G7).

Acceptance: the two escalated parent operations are either completed or held by a named gate; `integration_outbox` undelivered count is zero; no stage on the install reaches ordinal 3 on an unchanged head; the supervisor receives no `Task recovery: repair-…` message for a week.

### Phase 1: the reconciler and the observer, root batches only

Add `integration_subjects` and the reconciler loop; implement `integration_observe_subject` over today's tables; transcribe the shipped continuous root policy into the decision-table playbook; run the reconciler in **shadow** for one week (it records its decision on every visit and does nothing), and compare its decisions with what the event-driven rules did. Then switch root batches over: the old `root-train` rules are disabled, the reconciler acts, the existing primitives (`integration_build_candidate`, `integration_ci_evidence`, `integration_promote_main`, `integration_cleanup`, `integration_repair_dispatch`) are called as they are.

Acceptance: the real-Git/PostgreSQL scenario from the continuous-delivery spec ("two simultaneous conflicts, three colliding migration revisions, a closed repair worker, red then green candidate CI, exact promotion, cleanup, next batch") passes with the reconciler and **zero** operator commands; the same scenario with a delegate that is never claimed reaches `main` by ejection within the policy's budget; a shadow-week diff shows no decision the old rules made that the table did not.

### Phase 2: parent episodes

Parent episodes become subjects; `on_failed_child` gains `eject`; verifier filing is a table line; the collection, promotion-intent and parent-CI ticks fold into the visit; `redrive-child`, `redrive-root`, `reopen-collection`, `record-noop`, `rebind-*`, `recover-preserved-repair` are deleted.

Acceptance: an epic with eight children, one failed and one conflicting, completes or is held by one gate with no operator command; the stage-13 shape cannot be reproduced.

### Phase 3: development mode as a policy

Development projects move to the `development` table; `ci_observe` gains the local producer; `development.py` and its six satellites are deleted; `sweep`, `adopt`, `settle-parked`, `cancel-preserving`, `develop` are replaced by gate answers and policy activation.

Acceptance: `matter-engine-cpp` and `agent-queue-web` deliver a batch with a parked member and a validation failure with no operator command.

### Phase 4: delete

Controls, tables, doctor checks and modules per [§5](#5-simplification-what-to-merge-what-to-delete-what-to-keep); the legacy group once its counts are zero; one squashed migration per table family; the selection catalogue and generated clients regenerated.

Acceptance: `src/integration/` under 12,000 lines; `aq integration --help` lists six subcommands; `aq doctor` lists three integration checks; the full suite green on CI with the invariant tests ported.

## 7. Risks, non-goals and open questions

- **Rewrite risk.** The target is reached by strangling, not replacing: the reconciler runs in shadow first, takes over one subject kind at a time, and calls the existing primitives until each is de-duplicated. At no point is there a cut-over that the operator cannot roll back by re-enabling the event rules.
- **The decision table becomes the new Python.** The guard-rails in [§3.5](#35-policy-as-a-decision-table) (bounded waits, closed outcome sets, a shipped default that transcribes current behaviour) are the answer, together with keeping decisions declarative: a table line is a `when` over observer facts and one primitive call, never a loop or a computation.
- **Trust.** App-mode attestation, the trust manifest and the producer identity are untouched; primitive 10 is today's `attestation.py` with the event plumbing removed. The exact-SHA-green-before-`main` invariant is enforced by `git_publish` refusing a `main` publish whose head has no green attempt, as `integration_promote_main` refuses today.
- **Capacity is a policy input the train cannot fix.** A delegate that cannot be claimed because the pool has no seat is reported as a wait, not a failure; whether the project prefers to wait, to lower the class, or to eject is a table line. The train stays unblocked; the work may still be slow.
- **Not in scope.** The review flow (gates, reviewed bundles), routing (`default-assignment-routing`), the worker close pipeline, the GitHub credential modes and the dashboard are unchanged except where they consume integration facts.
- **Open questions for the operator.** (1) Should the shipped default on ladder exhaustion be `eject` with an audit row, or a gate with a 24-hour default to `eject`? The never-blocked guarantee holds either way; the difference is how long a stuck member waits for a human. (2) Should a parent's failed child default to `block` (today) or `eject` with a `skipped` receipt? (3) Is one live batch per project a hard rule, or a policy default?

## 8. Changes from revision 1

- Added Part I (this review): the evidence section with live-install measurements, the structural causes, the target shape (reconciler, twenty primitives, decision-table policy), the never-blocked guarantee and the list of binding human decisions, the blocker catalogue with an automatic path for every non-human stop, the simplification plan with deletion lists, the sequencing with acceptance statements, and the risks.
- Reviewer and route as requested in the decision note (Fable 5.1, `deep-high-claude`).
- Part II is a pointer to the revision 1 inventory, which is unchanged and stays revision 1 of this review (the 256 KB document limit does not allow repeating it); its overlap table is related to the primitives in [§5.8](#58-relation-to-the-revision-1-overlap-table).
- Added Part III, the evidence appendices: the 108-row stop-point sweep (A), the operator-surface classification (B: 49 controls, 25 HAND, 11 DECISION, 7 MIGRATION, 6 DIAGNOSTIC), and the policy-knob and decision survey (C: 41 decisions, 5 unsubscribed events, the frozen-policy mechanism). The live-database and log measurements of 2026-10-02 are also recorded as a comment on task eager-orbit-98.

---

# Part II: Mechanism inventory (revision 1)

The inventory itself is revision 1 of this review, unchanged: all 84 modules of `src/integration/` and their 1,227 functions and methods, the command, contract, CLI, doctor and query surfaces, the supporting call sites, the shipped playbook rules, and the overlap and simplification candidates table (1,808 callables in all). It is not repeated here because a review document is limited to 256 KB; read it with `aq review show --review-id rev-agile-ridge --revision 1`, or in the repository as `docs/superpowers/specs/2026-10-02-integration-train-inventory.md` (commit `162da5913`, branch of task brisk-cascade). Every module named in Part I's "Replaces" and "Absorbs" columns is a row in that inventory, and [§5.8](#58-relation-to-the-revision-1-overlap-table) relates its overlap table to the primitives.

---

# Part III: Evidence appendices

## Appendix A: Stop-point sweep (108 rows)

A read-only code sweep at snapshot `5de0a28c7` of every place where the train stops making progress on its own. "Auto?" says whether a timer or sweep retries it today. "D/H" says whether a person has to make a real **decision** or is merely a **hand** typing a recovery command the system could have run itself. Paths are relative to the repository root. Rows marked "traced" were established by reading code paths and should be confirmed against the live daemon before acting on them: A11, A24, B7, B10, B13, F2 and G7.

Two facts explain many of the rows: a failed playbook run is not retried (the outbox and retained pending events retry until a run exists, `src/integration/outbox.py:299-322`, `src/playbooks/runtime.py:558-611`; once the run exists a typed failure ends it for good, and only the pollers in `src/integration/service.py:107-160` re-drive anything afterwards); and one stuck batch freezes the whole project's train (while any batch is in a live lifecycle, including `human_blocked`, every later sweep is merged into the existing request, `src/integration/scheduler.py:184-221`, and the batch's lease keeps being renewed, `:309-317`).

### A. Root train (seal, candidate, CI, promote, cleanup)

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| A1 seal-config | `src/integration/scheduler.py:501-526,541-548` | Seal raises: request not outstanding, project not in train mode, repo row missing, or root route artifact mismatch | Request outstanding, `sweep_due` delivered, no batch | After 1 hour the request is treated as stale and auto-released (`stale_schedule.py:82,276-283`), but the next sweep fails the same way; fix config, then `clear-stale-request --apply` | Loops | H |
| A2 seal-lease-orphan | `src/integration/scheduler.py:550-563,651-653` | Expired lease has no resumable batch, or the frontier was lost | Lease row plus a batch stuck in `sealing` | Nothing; `clear-stale-request` refuses because another batch holds the lease | No | H |
| A3 seal-busy | `src/integration/scheduler.py:487-488,587-590` | Live lease or live batch; only the triple-stale path re-queues itself (`:420-436`) | Request outstanding, no batch | Released automatically after the 1-hour grace | Yes (1 h) | — |
| A4 migration-defer | `src/integration/scheduler.py:620-633,800-832` | Alembic head collision | Member skipped every sweep; supervisor message "needs rechaining" | Rechain the branch, then a fresh approval | Re-checked each sweep | D (new review) |
| A5 root-review | `src/integration/scheduler.py:870-882`, `github_review_poll.py:146-175` | No GitHub approval of the exact head, or changes requested | Root COMPLETED with a PR; evidence missing or rejected | Approve on GitHub, or `authorize-root` under authorized admission | Polled every 30 s | **D** |
| A6 root-no-source | `src/integration/github_review_poll.py:94-100` | Root has a PR but no eligible review source | Only a warning is logged | `aq integration materialize-root` | No | H |
| A7 root-no-PR | `src/integration/root_pull_requests.py:66-135,162-212` | Epic not verified, or leaf checkpoint still equals its base | No PR | `redrive-root`; other cases retried with backoff | Partly | H |
| A8 source-CI finite | `src/commands/integration_commands.py:124-134`; `scheduler.py:895-952` | Source CI repair task FAILED and policy is not `continue` | Repair task FAILED; member never sealed | New head, or reopen the task | No | **D** |
| A9 source-ancestry | `src/commands/integration_commands.py:207-250` | Ancestry repair not authorized, a tree mismatch, or the task was already reopened once | One supervisor message, then nothing | `aq task reopen-with-feedback` | No | H (tree mismatch: D) |
| A10 withdraw-wait | `src/integration/candidates.py:459-471,649-701` | An invalid member exists but a write or live delegate is still pending | Batch `building`, retried every 60 s | Clears when the blocker settles | Yes | — |
| A11 build-blocked-pre-stage0 (traced) | `src/integration/candidates.py:292-311,326-330`; `:657-658` | Build answers `wait` or `configuration_blocked` before repair stage 0 starts, so no stage deadline exists | Batch `sealed`; lease renewed forever; request counts as active | Fix config, then replay `integration.sealed` (`aq playbook run`) or `cancel-preserving` | **No** | H |
| A12 start-raise | `src/integration/candidates.py:347-348` | `repair.start` fails, so the run raises | Batch `building`, revision `constructing`, no stage | Same as A11 | No | H |
| A13 construct-stall | `src/integration/repair.py:2031-2087,76` | Construction stopped after stage 0 started | Re-driven once with 600 s grace; then debug stage; then `human_blocked` | (bounded) | Yes | → A19 |
| A14 main-moved-wait | `src/commands/integration_commands.py:1810-1816`; `main_promotion.py:884-912` | Policy says `on_main_moved: wait` | Build answers `base_moved` and never retries | Stage deadline escalates, eventually to a human | Bounded | **D** (by policy) |
| A15 candidate-CI-pending/config | `src/integration/attestation.py:294-295`; `ci.py:524-527` | CI never reports, or trust/App errors | Batch `testing` | The 5 s poll repeats silently until the stage deadline | Bounded | H for config |
| A16 attestation-prewrite | `src/integration/attestation.py:851-858,156-163`; `repair.py:1970-1983`; `stale_schedule.py:330-337` | Attestation publish attempted (prewrite recorded) but its record never appears | Publication `reserved` with `prewrite_at` set | **Nothing.** Stage deadline held at "not due"; releasing the request is blocked even after cancel; no command clears it | No | **D** |
| A17 root-budget-unenforced | `src/integration/attestation.py:717-759`; `repair.py:1192-1200` | Repeated red CI on a root batch | Attempts counted in the stage dossier only; `record_result` and the stuck-batch message are reachable only from the parent playbook | Deadline only | — | (notification gap) |
| A18 green-not-promoting | `src/integration/green_continuation.py:47-49,336-390`; `repair.py:1984-1991` | Promotion keeps answering `wait` (writer attached, short lease, …) | Batch `testing`, candidate green, no intent; the deadline never fires | Six continuations, then `exhausted` (warning only, `:412`); then supervisor `aq playbook run integration.candidate_green` | Yes, then no | H |
| A19 human-blocked | `src/integration/repair.py:1152-1162,2014-2024,4159-4222` | Last stage exhausted under `on_exhausted: human` | Operation `human_required`, batch `human_blocked`; lease renewed, so the **whole train is frozen** | `resume` (resets the clock, not the attempts), `abort`, `eject`, `cancel-preserving` | No | **D** |
| A20 no-progress | `src/integration/repair.py:3943-3958,4108-4157` | Debug stage ends with the head unchanged since the previous stage | Operation `escalated`, stage marked `supervisor_recovery` | Root batch: `resume` refuses (`recovery_controls.py:126-130`), `abort` refuses (`:569-570`), so only `cancel-preserving` works. Parent: `recover-preserved-repair` | No | **D** |
| A21 promote-readiness-blocker | `src/integration/green_continuation.py:303-310` | Promotion blocked (e.g. configuration) | Nothing is emitted, so the retry budget never runs out | Fix the blocker, then redrive | No | H |
| A22 root-intent-prewrite | `src/integration/main_promotion.py:527-552,614-628,1378-1383` | The push to main was attempted and its outcome is unknown | Intent `prepared`/`pushed`, mutation has a prewrite marker; blocks every later promotion (`:452-468`) | Only resolves if main comes to contain the prepared commit (unresolved-intent tick); **no command** | Polls | **D** |
| A23 lease-renew-refused | `src/integration/scheduler.py:321-339` | Lease fence differs from the one the bound intent recorded | Warning logged; lease expires | Nothing | No | D |
| A24 release-wait (traced) | `src/integration/release.py:228-247`; single-shot event at `main_promotion.py:1323-1339` | Release answers `wait` or `invariant_error` | Batch `promoted`, lease held; classifier says `active` (`stale_schedule.py:209-217`) | Replay `integration_release` | **No** | H |
| A25 cleanup-conflict | `src/integration/cleanup.py:537-556,615-671`; `recovery_controls.py:689-698` | A ref moved, an owner is active, or retries are exhausted | `cleanup_state=conflict`; collector owner kept; drain blocked | Failed items: `retry-cleanup`. Conflict items: by hand | Retryable items: yes | H/D |
| A26 cleanup-irreversible | `src/integration/recovery_controls.py:699-720` | A cleanup write was attempted (prewrite) and the outcome is unknown | `irreversible_prewrite_at` set | Nothing | No | D |
| A27 request-blocked | `src/integration/stale_schedule.py:236-245,523-524` | An ended batch still has unresolved writes | Verdict `blocked` | Settle what the blockers list names | No | D |
| A28 reviewed-file-guard | `src/integration/candidates.py:3686-3720`; `doctor/integration_checks.py:2180-2198` | Repair touches a reviewed (e.g. migration) file | Guard recorded on the stage; batch `repairing` or `human_blocked` | `aq integration eject`, then rechain the source | No | **D** |

### B. Parent collection and hierarchy

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| B1 failed-child | `src/integration/parent_completion.py:561-566,584-586`; `parent-integration.md:31-41,65-78` | A child FAILED | Parent `PAUSED` with blocker `failed_child`; a human gate if policy is `ask` | Retry the child or reparent it (`hierarchy.py:982-1010`). **No command records skipped/ineligible** (record-noop only at `integration_commands.py:2342-2430`). Resolving the gate does not waive the child | No | **D** |
| B2 no-code-child | `src/integration/child_delivery.py:254-258` | Checkpoint still equals the origin base | Child cannot be assembled | `aq integration record-noop` | No | H |
| B3 reviewer-reject | `src/integration/child_delivery.py:697-702` | Reviewer rejected the head, or a reviewer task is open | Child never assembled | Rework the child | No | **D** |
| B4 proof-fails | `src/integration/child_delivery.py:441-497` | Remote branch moved or is unpublished | Retried with backoff up to 3600 s | `redrive-child` | Yes | H |
| B5 blocked-collector | `src/integration/collection.py:59-64`; `collecting_parent_recovery.py:88-172` | Parent BLOCKED (e.g. stale session) | Collection only scans `PAUSED` parents | `aq integration redrive-root` | No | H |
| B6 cancelled-collection | `src/integration/development.py:3539-3551`; `child_delivery.py:271-276` | `cancel-preserving` was run on the collection operation | Operation cancelled; parent stays `awaiting_children` | `reopen-collection` | No | H |
| B7 parent-intent-prepared (traced) | `src/integration/promotion.py:230-232,336-341,752-753`; `collection.py:192-208`; `core.py:1860-1862` | Target moved, or fence/branch busy, after the intent was prepared | Intent stays `prepared`; every later promotion refused as "unresolved promotion"; the service's intent tick ignores parent intents | Replay the same `delivery.ready` (`aq playbook run`); otherwise nothing | **No** | H |
| B8 source-moved-dedup | `src/commands/integration_commands.py:2546-2547`; `collection.py:164-225` | Same child identity keeps answering `source_moved` | The re-queued event has the same id, so it is a no-op | Rework the child, or `redrive-child` | No | H |
| B9 conflict-dispatch | `src/integration/repair.py:1397-1437,1476-1483`; `pending_dispatches` `:122-151` | Repair dispatch answers busy / human_required / configuration_blocked with no delegate linked | Stage active with no writer | Stage deadline, then escalation | Bounded | H |
| B10 debug-handoff-service (traced) | `src/integration/repair.py:3313-3314,1310-1319`; `core.py:1919-1922` | The service's own repair service is built without stop/handoff confirmers | Every retry answers `busy`; debug deadline burns down to `human_blocked` (human policy) | Command-path `integration_repair_dispatch` | No | H |
| B11 parent-exhausted | `src/integration/repair.py:4196-4205` | Repair budget spent | Parent `BLOCKED` | `resume`, `abort` | No | **D** |
| B12 verifier-route | `src/integration/parent_completion.py:274-298` | `parent.verifier_intelligence_class` unset | `configuration_blocked`; the event it emits has no consumer | Change the policy | No | H |
| B13 integration-ready-1shot (traced) | `src/integration/parent_completion.py:345-379`; `integration_commands.py:457-481`; manual pause `:1057-1061` | Owner transfer answers busy / stale_owner / human_required | Checkpoint `integration_ready`; nothing polls that state | Replay `task.integration_ready` (transfer is idempotent) | **No** | H |
| B14 parent-CI | `src/integration/parent_ci.py:35-37,93-98,139-141` | CI not green, a conflicting snapshot ref, or a trust error | Parent `verifying` | Fix trust or CI | Polls every 30 s | H |
| B15 parent-CI-red-no-stage | `src/integration/repair.py:1076-1079` | Red parent CI while no repair stage exists | `record_result` answers stale; nothing assigns a writer | A new head from the verifier or parent worker | No | D |
| B16 verify/complete 1-shot | `parent-integration.md:93-98,121-125`; `parent_completion.py:1188-1219` | stale or invalid evidence, manual pause | Run fails | Replay the event | No | H |
| B17 materialize-refused | `src/integration/branch_materialization.py:114-170`; `hierarchy.py:946-963` | Remote ref already exists with an unexpected tip | Task never claimable | By hand (pick the tip) | Retried forever | **D** |
| B18 bus-trigger-lost | `src/playbooks/runtime.py:270-285` | `task.completed`/`failed`/`child_added` dispatch fails | Logged only; failed-child gate never created | Replay the event | No | H |
| B19 file/checkpoint dirty | `parent-integration.md:41-51` | Dirty or stale checkpoint | Run fails | Worker closes again | No | H |
| B20 legacy children | doctor/status `missing_receipt` | Parent predates train mode | Child flagged forever | `adopt-legacy-deliveries`, `bind-legacy-repositories` | No | D |
| B21 orphaned ops | `src/doctor/integration_checks.py:359-405` | Live operation in a project no longer hierarchy/train | Report only | Operator ends it | No | **D** |

### C. Repair stages and delegates

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| C1 dispatch-human_required | `src/integration/repair.py:1401-1416,1586-1595,1674-1698` | Delegate missing or mismatched, foreign owner, failed post-transfer check | Stage keeps `repair_task_id` | `aq integration reserve-owner`; deadline; reconcile tick (`:1274-1308`) | Partly | H |
| C2 progress-git-error | `src/integration/repair.py:1521-1534` | Fetching preserved progress fails | `preserved_progress_blocker` in the dossier | Fix git; deadline | Retried each tick | H |
| C3 no-debug-class | `src/integration/repair.py:1421-1425` | `debug_intelligence_class` empty | `configuration_blocked` | Change the policy | No | H |
| C4 id-collision | `src/integration/repair.py:1434-1437` | A task already exists with the repair id | `human_required` | Remove or rename it by hand | No | H |
| C5 resolution-required | `src/integration/repair.py:1801-1828` | Parent delegate closes without a recorded push of its resolution | Close refused | Worker runs resolve-conflict + push-conflict-resolution | No | H (agent) |
| C6 resume-ambiguous | `src/integration/recovery_controls.py:111-130,194-195` | Writes in doubt, or wrong state | `ambiguous` / `invalid_state` | Settle the writes | No | D |
| C7 abort-guard | `src/integration/recovery_controls.py:569-573` | Operation not `human_required`, or writes in doubt | Refused | `cancel-preserving` | No | D |
| C8 stranded-delegates | `src/integration/repair.py:153-187`; doctor `:2306-2311` | Operation ended | Delegate ticket `PAUSED`/`READY` | Tick retires it (skips live writers); doctor `--fix`; `release-delegates` | Yes | — |
| C9 retired-live-writer | troubleshooting guide `:1378-1397` | Pool worker still holds a retired delegate | Session live | Agent `drain-ack`; reconciler then stops it | Once acked | H (agent) |
| C10 superseded-intent | doctor `stale_repair_intents` `:1371` | Delegate still names a superseded intent | Report only | `rebind-repair` | No | H |
| C11 detached-frozen | `src/integration/detached_repair_rebind.py:150-240` | Debug stage frozen on an unpublished head | Stage active, cannot admit its writer | `rebind-detached-repair` | No | H |
| C12 unwritten-resolution | `src/integration/promotion.py:469-555` | Reserved resolution never written | Intent `resolution_reserved` | `recover-unwritten-resolution` (local operator only); refused if the push may have started | No | D |
| C13 candidate-member | `src/commands/integration_commands.py:967,2653` | Member resolution stuck | Candidate resolution row | `recover-candidate-member` / `resolve-candidate-member` | No | H |
| C14 reopened-dispatch | `src/integration/repair.py:140-146` | Dispatch after `reopen-collection` fails | Stage with no task | `pending_dispatches` retries it | Yes | — |
| C15 continue-successor | `src/integration/repair.py:122-151,1256-1272` | Writer closed under the `continue` policy | — | Repair-continuation tick (`core.py:1213-1229`) | Yes | — |

### D. Ownership, fences and stopped writers

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| D1 handoff_pending | `src/integration/ownership.py:136-156,112-113` | Handoff marked pending, then stop confirmation failed | Owner `handoff_pending` forever | `release-owner`; doctor `stranded_fences --fix`; owner-recovery sweep (**off by default**, `config.py:2270`, `core.py:1253-1267`) | No by default | H |
| D2 attached-gone | `src/doctor/integration_checks.py:797-848` | Writer gone; every claim fails "not reserved" | Owner `attached` | As D1; development mode's `preserve_stopped_owners` (`development.py:3644`); 30 s ready-owner pass (`completion_recovery.py:55-71`) | Partly | H |
| D3 recovery-refused | `src/integration/owner_recovery.py:99-104` | writer_live, checkout_in_use, origin_unreachable, or stale_fence | Audit row written | Change the condition; there is no force | No | H |
| D4 finished-owner | `src/integration/stale_owners.py`; doctor `:2364-2370` | Finished task still owns its branch, with work not on main or a failed task's ref gone | Owner `reserved` | `release-stale-owners`; doctor `finished_branch_owners --fix` | No | **D** |
| D5 missing-canonical | `src/doctor/integration_checks.py:631-710`; `canonical_reservation.py:21-28` | Train producer has no reservation | Report only | `reserve-owner` | No | H |
| D6 reused-identity | `src/integration/identity_rebind.py` | Task inherited a deleted task's identity | Report only | `rebind-reused-identity --discard-tip` | No | **D** |
| D7 drain-never-done | `src/integration/controls.py:1838-1852` | Any active work remains, or the drain target is not `disabled` | `draining=true` | `release-stale-owners`, `retry-cleanup` | Tick (only toward disabled) | H |
| D8 stale-lease | `src/integration/stale_owners.py:467-482` | Lease expired but cleanup incomplete | Lease kept | Finish cleanup first | No | H |

### E. Development publication and delivery

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| E1 remote-moved | `src/integration/development.py:941-952` | Publishing row and the target moved in a way git cannot explain | The whole project's sweep raises every tick | `adopt --accept-equivalent` | No | **D** |
| E2 history-loss | `src/integration/development.py:1205-1206` | Default branch was force-pushed | Error | Human investigates | No | **D** |
| E3 url-mismatch | `src/integration/development.py:729` | Retained clone points at a different URL | Error | Fix config | No | H |
| E4 validation-dirty | `src/integration/development.py:1992` | Validation wrote to the tree | Blocked | Fix the validation command | No | H |
| E5 publish-unconfirmed | `src/integration/development.py:1238-1242` | Push not confirmed | Row `publishing` | Next sweep reconciles | Yes | — |
| E6 repair-failed | `src/integration/development.py:3878-3883,329-330` | Repair task FAILED; the same id is reused, so nothing new is filed | Parked; chain "finished" | `sweep --retry`, `adopt`, `settle-parked`, `cancel-preserving` | No | **D** |
| E7 generations-exhausted | `src/integration/development.py:99,3919-3933` | Three repair generations used | `repair_generation_exhausted` | Operator | No | **D** |
| E8 source-missing | `src/integration/development.py:3901-3913` | Repair source not found anywhere | Diagnostic | By hand | No | H |
| E9 dispatch-failed | `src/integration/development.py:2210-2218,2557-2592` | Repair dispatch raised | `publisher_diagnostic` tick count | Doctor `publisher_stalled` | Retried | H |
| E10 validation-infra | `src/integration/development.py:1064-1076` | Validation infrastructure failed 3 times in a row | Deferral row; supervisor message | Fix the environment | Re-validates each tick | H |
| E11 candidate-stalled | `src/integration/development_stalls.py:18-26,66`; `config.py:2276` | Five identical skips | Skip metadata; one supervisor message | `sweep --recover-child`, `migrate-provenance` | Re-evaluated | H/D (cycles: D) |
| E12 cancel-live-writer | `src/integration/development.py:3446-3486` | Writer not proven stopped | Refused | Stop the writer | No | H |
| E13 branch-cleanup-exhausted | `src/integration/development.py:88-90` | 8 failed attempts | `exhausted` | `git.stale_branches --fix` | No | H |
| E14 discard-parked | `src/integration/branch_discard.py:52,248-296,435-436` | Remote moved (conflict) or 8 failures | `discard_state` conflict/failed | Doctor `branch_discards --fix` (re-arms only) | No | H/D |
| E15 no-PR-path | `src/integration/manual_delivery.py` | Repository cannot host a PR | Task BLOCKED | `aq task deliver` | No | H |
| E16 legacy-PR | `src/integration/pr_delivery.py` | Work delivered under other commits | PR left open | `close-delivered-pr` or a carrier root | No | D |

### F. CI sentinel and main health

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| F1 escalate | `src/commands/ci_commands.py:38,452-453,477-487`; `ci-main-sentinel.md:64-73` | Two spent repair attempts | One escalation per failure signature, reused every tick; no new repairs | Fix by hand, or `ci_repair_adopt` a live task; or main turns green | No | **D** |
| F2 completed=spent (traced) | `src/commands/ci_commands.py:39-40,380` | Repair is COMPLETED but its PR is still in the train | Counts as a spent attempt, so escalation can fire while the fix is still in flight | Human notices | No | D |
| F3 unknown-silent | `src/commands/ci_commands.py:306-329,347-348,439-440` | No repo URL, or GitHub authorization fails | The rule ends "done" every tick; no alert | Fix config | No | H |
| F4 train-dependency | `src/commands/ci_commands.py:262-272` | Sentinel repairs must go through train admission | Waits for A5 (review approval) | Review | — | **D** |
| F5 report-only | `src/doctor/integration_checks.py:2234-2246` | Unreviewed PRs | Report only | Operator | No | D |

### G. Schedule, outbox and service loop

| ID | Where | Trigger | Durable state | What unblocks it today | Auto? | D/H |
|---|---|---|---|---|---|---|
| G1 unconsumed events | `src/integration/outbox.py:222-228,299-322` | No shipped playbook consumes `integration.human_blocked` (`repair.py:4213`), `task.integration_configuration_blocked` (`parent_completion.py:280`), `integration.cleanup_pending`, `integration.root_delivered`, `integration.collection_redriven`, `integration.branch_materialization_pending` | Outbox rows retried forever (no maximum); the human-blocked notice never reaches anyone | Add a consumer | Endless | H |
| G2 sweep in_flight | `src/integration/stale_schedule.py:271-275` | Route never accepts `sweep_due` | Never released | Fix the route or activation | No | H |
| G3 unsealed | `src/integration/stale_schedule.py:82,276-283` | Event accepted but no batch sealed | 1-hour grace | Then auto-released; `clear-stale-request` to skip the wait | Yes | — |
| G4 stale-request | `src/integration/scheduler.py:157-161` | Ended batch still owns the request | — | Auto-released; doctor `--fix`. Only runs for enabled and due schedules (`integration_reconciliation_queries.py:59-63`) | Yes* | — |
| G5 run-terminal | `src/playbooks/runtime.py:577-611`; `root-train.md:95-99` | Typed failure of an accepted run | Run `failed`; event resolved | `aq playbook run` replay | No | H |
| G6 no-run | `src/playbooks/runtime.py:584-585` | Event selects no rule | Protected pending event, never expires | Retried forever | Endless | H |
| G7 pass-hang (traced) | `src/integration/service.py:89-97,107-160` | One reconcile source hangs (git or GitHub call) | No new pass starts and there is no timeout, so CI, deadlines, intents, cleanup and drains all stop | Restart the daemon | No | H |
| G8 inactive project | `src/database/queries/integration_reconciliation_queries.py:65` | Project not `ACTIVE` | Schedule and lease renewal frozen | Reactivate the project | — | D |

Errors that are swallowed but retried (not real stops): `src/integration/service.py:159-160,241-249,268-276`; `green_continuation.py:193-197`; `development.py:2733-2739,2755-2785`; `core.py:1228-1229`; `collection.py:77-78`; `parent_ci.py:93-98`; `stale_schedule.py:570-572`.

### The ten most common stop points, by the amount of recovery tooling around each

1. Stranded branch owners (D1, D2, D4, D5): `release-owner`, `release-stale-owners`, `reserve-owner`, three doctor checks, the owner-recovery sweep, the completion recovery pass and `preserve_stopped_owners`.
2. Repair exhausted to `human_required` (A19, B11): `resume`, `abort`, `eject`, `cancel-preserving`.
3. No-progress repair incidents (A20): `recover-preserved-repair`, `rebind-detached-repair`, `rebind-repair`, `release-delegates --archive-obsolete`.
4. Train never sweeps (G2–G4, A1, A27): the stale-request classifier, `clear-stale-request`, doctor `--fix`, the scheduler's own release.
5. Parent collection stalls (B2–B6): `redrive-child`, `reopen-collection`, `redrive-root`, `record-noop`, plus two doctor checks.
6. Development parked or unrepaired batches (E6–E11): `sweep --retry/--recover-child`, `adopt`, `settle-parked`, `cancel-preserving`, plus two doctor checks.
7. Stranded delegates (C8): automatic retirement, doctor `--fix`, `release-delegates`.
8. Green batch never promotes (A18): green continuation, then supervisor `aq playbook run`.
9. Root delivery identity (A5–A7, E16): `redrive-root`, `materialize-root`, `authorize-root`, `close-delivered-pr`, the legacy adopt/bind commands.
10. Cleanup and drain residue (A25, D7): `retry-cleanup`, `release-stale-owners`.

Stop points with no automatic exit and no recovery command (the likeliest silent wedges): A11/A12, A16, A22, A24, B7, B13, B10, G7, and the FAILED-child case in B1.

## Appendix B: Operator recovery surface (49 actionable controls)

A read-only inventory of every operator command that gets the train moving again, classified as DECISION (a human picks between materially different outcomes), HAND (mechanical; the system had the information and the only reason it did not act is that nothing was wired to), MIGRATION (a one-off legacy or upgrade path) or DIAGNOSTIC (read-only). "IC" is `src/commands/integration_commands.py`, "CT" is `src/commands/contracts/integration.py`, "CLI" is `src/cli/integration.py`.

**Scope.** Three caller tiers (`src/api/scope.py`): a loopback CLI call with no token is LOCAL and skips scope checks (`:231`); an elevated supervisor session may call any command (`:284-299`); a worker session gets only `integration_status` (`:109`) and `integration_resolve_candidate_member` (`:113`). `OPERATOR_INTEGRATION_CONTROLS` (`:160-193`) lists the controls refused to non-elevated callers. Almost every handler also calls `integration_operator` (`src/commands/supervisor_authority.py:11-49`), which admits the LOCAL operator or a live, named, project-matched supervisor and refuses playbook steps, daemon services and workers. Consequently none of the operator controls can run from a playbook or a service; "op" below means this rule.

### B.1 `aq integration` subcommands

| # | CLI / command id | Scope | Handler → service | State read → transition | Outcome codes (CT line) | Symptom | Class |
|---|---|---|---|---|---|---|---|
| 1 | `status` / `integration_status` | agent (own project), supervisor, playbook if granted (IC:581-599) | IC:577 → `controls.py:275` | Reads mode, generation, preflight blockers, repairs, cleanup, live operations; writes nothing | status, not_found, unauthorized (CT:1201) | Any stall | DIAGNOSTIC |
| 2 | `record-noop` / `integration_record_noop` | LOCAL, SERVICE, or a granted playbook; **supervisor refused** (IC:34-41, 499-500; not in the supervisor capability list `profile.md:121-158`) | IC:2342 → `record_disposition` | Child checkpoint at expected head + `pass`/`no-op` completion + approved review evidence → noop delivery receipt for the parent | recorded, stale_head, invalid, delivery_target_fixed, runtime_error, unauthorized (CT:2287) | `redrive-child` or the collector says a no-code child needs `record-noop` (`child_delivery.py:254-258`) | HAND |
| 3 | `resolve-candidate-member` | the repair session itself (`scope.py:110-113`) | IC:2653 | Reserves, publishes and accepts the session's own candidate member | accepted, already_accepted, wait, stale, invariant_error (CT:1968) | Normal repair-writer step | agent protocol (not counted) |
| 4 | `flush` / `integration_flush` | op; playbook if granted (IC:810-817) | IC:806 → `controls.py:1120` | Also runs completion recovery (IC:851-864). Train: `mark_due(..., "manual")`; development: a sweep; observe/hierarchy: read-only preflight | due, not_due, coalesced, disabled, draining, eligibility, not_found (CT:1272) | "Go now" | HAND |
| 5 | `eject` / `integration_eject` | op | IC:824 → `controls.py:1132` | Removes one member from a sealed / repairing / human_blocked batch: new revision, lifecycle back to `sealed`, re-enqueues `integration.sealed` (`:1640-1673`); no members left → aborted (`:1519-1536`) | ejected, unknown_batch, not_a_member, invalid_state (CT:1279) | Doctor `reviewed_file_guard` | DECISION |
| 6 | `enable` / `integration_enable` | op | IC:867 → `controls.py:533` | CAS on mode / desired mode / draining / generation; rollout transition, legacy suppression, schedule, waiver consumption (`:662-753`) | enabled, disabled, draining, blocked, stale, not_found (CT:1286) | Rollout or drain | DECISION |
| 7 | `reconcile-unmaterialized` | op | IC:905 → `controls.py:1012` | Binds `repo_id IS NULL` tasks to the designated repository; reserves origins; generation +1 | reconciled, nothing_to_reconcile, blocked, stale, not_found, hierarchy.invalid, hierarchy.busy (CT:1293) | Pre-rollout tasks | MIGRATION |
| 8 | `waive-history` | op | IC:890 → `controls.py:1781` | Inserts a history waiver; only when every blocker is `legacy_pr_merge_gate` | waived, stale, not_waivable, not_found (CT:1308) | `enable` blocked only by legacy merge gates | MIGRATION |
| 9 | `resume` / `integration_resume` | op | IC:927 → `recovery_controls.py:55` | Operation `human_required` → active/escalated; stage failed/expired/cancelled → active with a **new deadline, same attempts** (`:262-289`); batch `human_blocked` → repairing (`:309-317`); parent BLOCKED → PAUSED (`:360-458`); completed delegate → PAUSED (`:460-558`) | resumed, ambiguous, invalid_state, stale, not_found (CT:1315) | `repair[].state=human_required`; `blocked_collectors` | DECISION |
| 10 | `abort` / `integration_abort` | op | IC:936 → `recovery_controls.py:560` | Operation and stage → cancelled; batch `human_blocked` → aborted; delegates released; schedule request freed (`:575-635`) | aborted, ambiguous, invalid_state, not_found (CT:1322) | As resume | DECISION |
| 11 | `retry-cleanup` | op | IC:946 → `controls.py:160`, `recovery_controls.py:676` | Cleanup items failed/retryable → retryable, attempts 0; `cleanup_state` conflict → pending; missing items materialized (`controls.py:171-206`) | requeued, materialized, ambiguous, nothing_to_retry, not_materializable, not_found (CT:1191, 1329) | Drain stuck on cleanup | HAND |
| 12 | `release-owner` | op | IC:1005 → `owner_recovery.py:227` | Owner attached/handoff_pending → released with a fresh fence; unpublished work pushed to `aq/preserved/<row>`; workspace unlocked; claim released; audit row | released, preserved_and_released, not_eligible (writer_live, checkout_in_use, origin_unreachable, stale_fence, not_recoverable_state), not_found (CT:1345) | Claims fail "canonical branch is not reserved by this task" | HAND |
| 13 | `reserve-owner` | supervisor or LOCAL (handler check) | IC:1068 → `canonical_reservation.py:21`, or `repair.py:1321` for delegates | No/released row → reserved for a detached READY/BLOCKED producer; delegates go through `RepairService.dispatch` | acquired, already_reserved, not_eligible (9 reasons, `canonical_reservation.py:34-160`), not_found (CT:1353) | Doctor `missing_canonical_owners` / `missing_repair_owners` | HAND |
| 14 | `release-stale-owners` | op | IC:1091 → `stale_owners.py:130` | Reserved rows with a finished owner whose tip is on main (or whose ref is gone) → released; expired lease deleted | released, nothing_to_release, invalid, not_found (CT:1361); per-row "kept" reasons `stale_owners.py:95-112` | Drain never completes | HAND |
| 15 | `adopt-legacy-deliveries` | op | IC:1434 → `legacy_deliveries.py:177` | Inserts legacy delivery rows by proof or by operator decision (`--supersede`/`--retire`/`--accept`) | adopted, nothing_to_adopt, blocked, invalid, not_found (CT:1533) | Observe-mode `missing_receipt` / `no_parent_collection` | MIGRATION (flags are DECISION) |
| 16 | `bind-legacy-repositories` | op | IC:1462 → `legacy_repositories.py:54` | Terminal hierarchy members `repo_id` NULL → designated repository | bound, nothing_to_bind, invalid, not_found (CT:1549) | `repository_not_designated` | MIGRATION |
| 17 | `close-delivered-pr` | op | IC:1482 → `pr_delivery.py:56` | Proof comment, close PR, record `integration.pr_closed_delivered` | would_close, closed, nothing_to_close, undelivered, changed, blocked, not_eligible, not_found, invalid (CT:1557) | Open PR whose work landed under other commits | HAND |
| 18 | `clear-stale-request` | op | IC:1119 → `controls.py:130` → `stale_schedule.py:476` | Clears `outstanding_request_id` (or promotes the catch-up); deletes the ended batch's lease; verdicts `stale`/`unsealed` | cleared, would_clear, nothing_to_clear, blocked, changed, invalid, not_found (CT:1371) | `flush` answers `coalesced` forever | HAND |
| 19 | `redrive-root` | op | IC:1144 → `root_pull_requests.py:223` | Opens the missing PR; for a BLOCKED collecting root hands off to `CollectingParentRecovery` (`:225`): BLOCKED → PAUSED | would_open, opened, would_collect, collecting, nothing_to_redrive, blocked, changed, not_eligible, not_found, invalid (CT:1389) | Completed root with no PR | HAND |
| 20 | `materialize-root` | op | IC:1176 → `root_materialization.py:32` | Inserts a branch origin and a `working` checkpoint for a legacy leaf root with a PR | would_materialize, materialized, changed, blocked, not_eligible, not_found, invalid (CT:1411) | `unmaterialized_train_pr` (`stall_checks.py:376`) | MIGRATION |
| 21 | `authorize-root` | op | IC:1205 → `root_authorization.py:68` | Inserts a root authorization for one exact source | would_authorize, authorized, already_authorized, changed, blocked, not_eligible, not_found, invalid (CT:1422) | User-approved root the admission predicate does not admit | DECISION |
| 22 | `redrive-child` | op | IC:1234 → `child_delivery.py:502` | Inserts approved evidence (`operator_redrive`) or reissues a receipt; queues the parent's collection | would_advance, advanced, nothing_to_redrive, blocked, changed, not_eligible, not_found, invalid (CT:1433) | Doctor `stuck_children` | HAND |
| 23 | `reopen-collection` | op | IC:1272 → `cancelled_collection_recovery.py:144` | Cancelled collection operation → active/escalated; collector fence +1; fresh stage; parent BLOCKED → PAUSED | would_reopen, reopened, nothing_to_reopen, ambiguous, blocked, changed, not_eligible, not_found, invalid (CT:1453) | `redrive-child` says "no live collection operation" | HAND |
| 24 | `rebind-reused-identity` | op | IC:1309 → `identity_rebind.py:126` | Sets `retired_at` on inherited origins; copies the checkpoint into an event, deletes it | rebound, would_rebind, nothing_to_rebind, unproven, blocked, changed, invalid, not_found (CT:1474) | Doctor `reused_task_identity` | MIGRATION (`--discard-tip` is DECISION) |
| 25 | `rebind-repair` | supervisor or LOCAL | IC:1342 → `repair_rebind.py:25` | Stage trigger/dossier → the current intent; reserves the conflict resolution | would_rebind, rebound, already_reserved, changed, blocked, not_found (CT:1493) | Doctor `stale_repair_intents` | HAND |
| 26 | `recover-preserved-repair` | supervisor or LOCAL | IC:1373 → `preserved_repair.py:45` | Reserves the preserved candidate under a new collector fence and publishes it; needs an `escalated` operation and an expired no-progress stage (`:287-302`) | would_recover, recovered, already_recovered, changed, blocked (about 30 reasons, `:165-644`) (CT:1502) | No-progress incident with preserved work | HAND |
| 27 | `rebind-detached-repair` | supervisor or LOCAL | IC:1399 → `detached_repair_rebind.py:53` | Stage starting commit and trigger → the published head and its intent; delegate → PAUSED; dispatch | would_rebind, rebound, already_rebound, changed, blocked, not_found (CT:1515) | Delegate BLOCKED `slot_reset_failed` | HAND |
| 28 | `release-delegates` | op | IC:955 → `recovery_controls.py:638` / `delegate_release.py:577` | Delegates of an ended operation → FAILED with a release row; `--archive-obsolete` archives them | released, nothing_to_release, invalid_state, not_found (CT:1337) | Doctor `stranded_delegates` | HAND |
| 29 | `recover-candidate-member` | op | IC:967 → `candidates.py:1344` | Candidate resolution `pushed` → accepted or rejected | accepted, already_accepted, rejected, stale, wait (CT:1578) | `resume` answers `ambiguous` on a pushed reservation | HAND |
| 30 | `develop` / `integration_develop` | op | IC:2994 → `development.py:3136` | Mode → development with a validation policy | configured, blocked (CT:3139) | Doctor `delivery_path` | DECISION |
| 31 | `adopt` / `integration_adopt` | op | IC:3029 → `development.py:1251` | Records an operator completion/equivalence decision | adopted, blocked (CT:3139) | Doctor `development_conflicts_unrepaired` | DECISION |
| 32 | `sweep` / `integration_development_sweep` | op | IC:3043 → `development.py:1521` / `2068` | Builds and publishes a development batch; `--retry` parked work; `--recover-child` | delivered, idle, parked, base_moved, blocked (CT:3139) | Doctor `development_publisher_stalled` | HAND |
| 33 | `settle-parked` | op | IC:3059 → `development.py:2435` | Parked operation → cancelled; members not owed (or dismissed back to the queue) | settled, dismissed, already_terminal, blocked (CT:3178) | Parked delivery the target does not owe | DECISION |
| 34 | `migrate-provenance` | supervisor or LOCAL | `git_commands.py:29` → `provenance_migration.py:57` | Inventory; `--apply` publishes Git provenance refs | inventory, migrated, blocked (CT:3157) | A held close needing legacy sources | MIGRATION |
| 35 | `cancel-preserving` | op | IC:3073 → `development.py:3380` | Repair operation → cancelled; delegates retired; owners kept | cancelled, already_terminal, blocked (CT:3139) | Doctor `orphaned_operations` | DECISION |
| 36 | `onboard-train` | client-side; reads `get_project`, `integration_status`, `list_tasks` (CLI:1162-1167) | `train_onboarding.py` (pure) | Writes local files only with `--write-*` | — | Moving a project onto the train | DIAGNOSTIC |
| 37 | `trust-manifest` | supervisor or LOCAL | IC:695 | Read-only | 15 codes (CT:1210) | App-mode setup | DIAGNOSTIC |
| 38 | `app-verify` | supervisor or LOCAL | IC:764 | Read-only | verified + 9 refusals (CT:1244) | Doctor `app_mode` | DIAGNOSTIC |
| 39 | `app-setup` | client-side | wraps `app-verify`; `--apply` runs `gh variable set` with the operator's own login (CLI:1771-1775) | — | — | App-mode setup | DIAGNOSTIC |

### B.2 Related commands outside `aq integration`

| CLI / id | Scope | Handler | Transition | Codes | Class |
|---|---|---|---|---|---|
| `aq system integration-recover-unwritten-resolution` | scope admits supervisors (`scope.py:170`, `profile.md:140`) but the **service requires LOCAL** (`promotion.py:477-479`) | IC:2901 → `promotion.py:469` | Intent `resolution_reserved` with no push started → superseded plus a successor | recovered, already_recovered, not_recoverable, runtime_error (CT:3276) | HAND |
| `aq system integration-transfer-owner` | op plus playbook (IC:388-403) | IC:372 | Owner fence transfer | transferred, busy, stale_owner, human_required (CT:1604) | HAND (playbook primitive) |
| `aq task deliver` | op (`scope.py:191`) | `git_commands.py:215` → `manual_delivery.py:69` | BLOCKED → COMPLETED (`operator_delivery`), fast-forward push; refused in development/hierarchy/train (`:102-108`) | 21 codes (`manual_delivery.py:79-301`) | HAND |
| `aq task close --obsolete` | op (`session_commands.py:757`) | `session_commands.py:725` → `obsolete_close.py:173` | Task → COMPLETED and abandoned; owners released; refused in hierarchy/train (`:267-273`) | closed, already_obsolete, plus 10 `obsolete.*` codes | DECISION |
| `aq git ci-baseline-status` | playbook `ci-main-sentinel` | `ci_commands.py:386` | Read-only | green/red/pending/unknown | DIAGNOSTIC |
| `aq git ci-repair-adopt` | playbook | `ci_commands.py:492` | Sets the task's dedup key and `ci_baseline_repair` metadata | adopted, recorded, unchanged | DECISION |

### B.3 Doctor checks (`src/doctor/integration_checks.py:2201-2371`)

Five with `--fix`:

| Check | Fix | What the fix does | Already automated? | Class |
|---|---|---|---|---|
| `integration.branch_discards` | `:575` | Discard rows conflict/failed → pending, attempts 0 | No; transport failures park after `MAX_ATTEMPTS=8` (`branch_discard.py:52`); re-arming a `conflict` row parks it again | HAND (failed rows); conflict rows need a person |
| `integration.stranded_fences` | `:829` | `OwnerRecovery.recover_many(principal="doctor")`, the same code as `release-owner` | Yes, `_sweep_stranded_owners` (`core.py:1253-1267`), but `owner_recovery_sweep` defaults to False (`config.py:2270`) | HAND |
| `integration.stranded_delegates` | `:1569` | `release_delegates` | Yes: `retire_terminal_delegates` every tick (`service.py:113-115`, `repair.py:153-187`) | HAND (redundant) |
| `integration.stale_schedule` | `:1712` | Releases `stale` requests only | Yes: the scheduler (`scheduler.py:157, 249-262`); `unsealed` turns `stale` after 1 hour | HAND (redundant) |
| `integration.finished_branch_owners` | `:1967` | `release_finished_branch_owners` | No tick | HAND |

Fifteen report-only checks, each pointing at a command: `reviewed_file_guard` → `eject`; `delivery_path` → `develop`/`task deliver`; `operational` → `status`; `orphaned_operations` → `cancel-preserving`; `reused_task_identity` → `rebind-reused-identity`; `unreviewed_prs` → none; `missing_canonical_owners` → `reserve-owner`; `stranded_dependents` → restore the branch by hand; `development_publisher_stalled` → `sweep --recover-child`; `development_conflicts_unrepaired` → `adopt`/cancel; `app_mode` → `app-verify`; `stale_repair_intents` → `rebind-repair`; `missing_repair_owners` → `reserve-owner`; `stuck_children` → `redrive-child`; `blocked_collectors` → `resume`/`redrive-root`.

### B.4 Counts

| Group | HAND | DECISION | MIGRATION | DIAGNOSTIC |
|---|---|---|---|---|
| `aq integration` subcommands (38 operator ones) | 17 | 9 | 7 | 5 |
| Related commands (6) | 3 | 2 | 0 | 1 |
| Doctor `--fix` actions (5) | 5 | 0 | 0 | 0 |
| **Total (49 actionable)** | **25** | **11** | **7** | **6** |

The fifteen report-only doctor checks add 15 DIAGNOSTIC (21 in total). `resolve-candidate-member` is excluded as the repair agent's own protocol step.

### B.5 The signal the system already has for each HAND control

1. `record-noop`: the collector sees `head_sha == base_sha` with a `pass`/`no-op` completion and only prints a hint (`child_delivery.py:254-258`).
2. `flush`: the periodic schedule tick (`service.py:162-177`).
3. `retry-cleanup`: cleanup items in failed/retryable, or a promoted batch with `cleanup_state=pending` and no items (`controls.py:171-183`).
4. `release-owner`: `OwnerRecovery.candidates(quiet_seconds=600)`; the sweep exists but is off.
5. `reserve-owner`: the `missing_canonical_owners` query (`integration_checks.py:651-691`); for delegates `reconcile_delegate_reservations` already runs every tick (`service.py:145-147`, `repair.py:1310-1319`).
6. `release-stale-owners`: `hierarchical_integration_draining=true` plus the rows from `drain_blockers_on` (`controls.py:2031-2037`).
7. `close-delivered-pr`: an open PR whose head is ancestor / patch-equivalent / content-equivalent to main; `onboard-train` already lists them (CLI:1227-1240).
8. `clear-stale-request`: the verdict from `classify_outstanding_request_on`.
9. `redrive-root`: `RootPullRequestReconciler` retries the PR with backoff (`core.py:1939`); the BLOCKED-collector branch has a complete diagnosis (`needs_attention=session_not_live`, reserved collector fence) but no tick runs it.
10. `redrive-child`: the collector's `ensure_evidence` declines `would_advance` results carrying `receipt_reissue` or an existing evidence id (`child_delivery.py:461-471`).
11. `reopen-collection`: parent checkpoint `awaiting_children` while its episode's operation is `cancelled`; `_parent_refusal` already detects it (`child_delivery.py:271-276`).
12. `rebind-repair`: the `stale_repair_intents` query (`integration_checks.py:1391-1428`).
13. `recover-preserved-repair`: the stage dossier's `supervisor_recovery` incident plus the `integration_owner_recoveries` row naming the preserved ref.
14. `rebind-detached-repair`: delegate `needs_attention=slot_reset_failed` plus a frozen head not on the parent branch (`detached_repair_rebind.py:329-339, 403-426`).
15. `release-delegates`: already automated (`service.py:113-115`).
16. `recover-candidate-member`: `integration_candidate_resolutions.state='pushed'` after the stage expired.
17. `sweep`: the publisher's stall metadata (`candidate_skipped`/`candidate_stalled`); it messages the supervisor instead of acting.
18. `recover-unwritten-resolution`: intent `resolution_reserved` with `resolution_push_started_at` and push evidence both NULL (`promotion.py:486-491`).
19. `transfer-owner`: the playbook step already does this.
20. `task deliver`: blocked context `session_close_pipeline_stop` plus `outcome=pass` and a pushed branch (`manual_delivery.py:51, 90-98`).
21. `branch_discards --fix`: `discard_state='failed'` after transport retries ran out.
22. `stranded_fences --fix`: as 4.
23. `stranded_delegates --fix`: already automated.
24. `stale_schedule --fix`: already automated.
25. `finished_branch_owners --fix`: `finished_branch_owners()` rows with no blocker (`finished_owners.py:110-122`).

### B.6 Legacy and migration code that can be deleted when its one-off job is done

| Module (lines) | Dead when |
|---|---|
| `src/integration/legacy_deliveries.py` (709) | No project's status shows `missing_receipt` with cause `no_parent_collection` and `adopt-legacy-deliveries` answers `nothing_to_adopt` everywhere. Only `LegacyDeliveryAdoption` (`:156-709`) can go; `legacy_delivered_children_on` (`:123`) is imported by `status.py:36-39` and `legacy_repositories.py:29` while `integration_legacy_deliveries` rows exist. |
| `src/integration/legacy_repositories.py` (169) | No observe/hierarchy/train project has terminal hierarchy tasks with `repo_id IS NULL`. |
| `src/integration/provenance_migration.py` (498), plus `git_commands.py:29-44`, CLI:897-921, CT:3157-3177 | Every development project returns `zero_fallback=true` (`:192-193`). |
| `src/integration/identity_rebind.py` (714), plus doctor `:1991-2067` and CLI:634-681 | `integration.reused_task_identity` finds zero rows; `_task_identity_exists` now blocks name reuse (`src/task_names.py:101`). |
| `src/integration/root_materialization.py` (272), plus CLI:495-514 | `unmaterialized_train_pr` is empty. |

Partial paths with the same condition: `controls.py` waive-history (`:1781-1818`, `:495-501`, `:559-565, 642-661, 714-737`); `controls.py` reconcile-unmaterialized (`:1012-1118`); doctor `integration.stranded_dependents` (`:850-1038`, covers an older publisher bug); doctor `integration.orphaned_operations` (`:359-405`, new rows already prevented); `development.py` older-cancellation replay (`:3417-3427`). Keep: `finished_owners.py` (any mode switch produces these rows) and `live_operations.py` (shared).

### B.7 Outcome codes

108 distinct contract-level outcomes across the operator controls plus `resolve_candidate_member`, `transfer_owner`, `recover_unwritten_resolution` and `record_noop`; `unauthorized` and `runtime_error` are returned by handlers but declared by no contract (110); `task deliver` adds 17, `task close --obsolete` 11, `ci_repair_adopt` 1: **139 distinct top-level codes**. About 80 more second-tier reason or cause strings sit inside results (`stale_owners.py:95-112` 16; `owner_recovery.py:99-104` 4; `identity_rebind.py:80-94` 12; `legacy_deliveries.py:86-109` 11; `delegate_release.py:519-561` 10; `controls.py` preflight 10; `preflight.py:97-123` 6; the App-mode item codes in `app_mode.py` not enumerated).

### B.8 Mismatches

1. The supervisor profile says to use `record-noop` (`profile.md:397`), but the handler refuses every non-LOCAL session (IC:34-41, 499-500) and the capability is not granted.
2. Scope and the profile grant `recover-unwritten-resolution` to supervisors, but the service is LOCAL-only (`promotion.py:477-479`).
3. The automated replacement for `release-owner` exists but is switched off (`owner_recovery_sweep`, `config.py:2270`).
4. Three HAND paths duplicate ticks that already run: `release-delegates` / `stranded_delegates --fix`; `stale_schedule --fix` / `clear-stale-request`; `reserve-owner` for repair delegates.

## Appendix C: Policy knobs, Python decisions, and what the playbooks decide

A read-only survey of where integration policy lives at snapshot `5de0a28c7`. "Changeable" means a project could change it today without a code change: config = `config.yaml`; row = the project row through `aq project set … --expected-integration-generation` (only while disabled and drained, `controls.py:847-858`); playbook = a reviewed playbook can change it; no = hard-coded.

### C.1 `HierarchicalIntegrationPolicy` (hierarchy and train modes; `projects.hierarchical_integration_policy`, frozen into each batch, operation and stage)

| Knob | Defined | Default | Changeable |
|---|---|---|---|
| `parent/root.repair.primary_seconds` | `src/integration/models.py:85` | 1800 | row |
| `…repair.primary_attempts` | `:86` | 3 | row (enforced for parent operations only; D6) |
| `…repair.debug_seconds` | `:87` | 3600 | row |
| `…repair.debug_attempts` | `:88` | 3 | row |
| `…repair.debug_intelligence_class` | `:89` | required | row |
| `…repair.conflict_scope` (`member`/`batch`) | `:90` | `member` | row (only the root value is read) |
| `…repair.on_exhausted` (`human`/`continue`) | `:91` | `human` | row |
| `…repair.source_ci` | `:92` | False | row (only the root value is read) |
| `…repair.debug_profile_id` | `:98` | deprecated; new writes refused (`controls.py:820-826`) | n/a |
| `…required_checks` {version, names, producer_id} | `:74-79, 151` | required | row |
| `…admission` (`reviewed`/`authorized`) | `:152` | `reviewed` | row (only the root value is read) |
| `…authorized_task_ids` | `:153` | `()` | row |
| `…route` (`PlaybookRoute`: system or own-project scope only) | `:116-143, 155` | required | row |
| `…primary_intelligence_class`, `…verifier_intelligence_class` | `:156, 160` | None | row |
| `branchless_parent` (`skip`/`declared`/`verifier`) | `:191` | required | row; only `verifier` is ever tested (`parent_completion.py:274`), `skip` and `declared` behave identically |
| `on_failed_child` (`block`/`ask`) | `:192` | required | row; acted on by the playbook's one decision |
| `on_main_moved` (`rebuild`/`wait`) | `:193` | `rebuild` | row |
| `cleanup.max_attempts`, `retry_base_seconds`, `retry_max_seconds`, `successful_source_refs`, `failed_work_retention_seconds` | `:170-174` | 5, 30.0, 3600.0, `delete`, 604800 | row (the default branch is always retained, `scheduler.py:637-641`) |

The onboarding preset `build_policy` (`train_onboarding.py:860-917`) writes `branchless_parent=verifier`, `on_failed_child=block`, `on_main_moved=rebuild`; with `continuous=True` also `on_exhausted=continue` (parent and root), `conflict_scope=batch`, `source_ci=True`, `admission=authorized`.

### C.2 `DevelopmentPolicy` (development mode; same column, read live each tick; `development.py:413-446`)

`validation` (`focused`/`advisory`/`none`, default `focused`, `:415`), `commands` (`:416`, required when focused), `timeout_seconds` (300, max 3600, `:419`), `slot_wait_seconds` (600, `:422`), `interval_seconds` (300, `:423`), `max_batch_size` (50, max 500, `:424`), `regenerate` (`:430`), `regenerate_timeout_seconds` (600, `:432`). All changeable by row through `integration development configure` (`development.py:3136`).

### C.3 Project columns and the schedule row

`projects.integration_mode` (`direct`/`pull_request`/NULL, `tables.py:64-66`; train requires `pull_request`, `controls.py:812-813`); `hierarchical_integration_mode` (`tables.py:67-72`, `integration_enable` CAS only); `…_desired_mode`, `…_draining`, `…_generation` (`tables.py:75-92`); `integration_repository_id` (`:73`); `assignment_playbook_id` (`:57-62`, default `default-assignment-routing`); `project_integration_schedules.interval_seconds` (`tables.py:4436`, default `scheduler.py:53`, set by `integration enable --mode train --interval-seconds`); `project_integration_schedules.enabled`; task-level `tasks.integration_mode` (`models.py:496`; chain `:142-178`).

### C.4 `config.yaml` (`IntegrationConfig`, `src/config.py:2215`)

`integration.default_mode` (`pull_request`, `:2233`); `merge_ci_policy` (`warn`, `:2246`), `merge_required_checks` (`:2252`), `merge_require_up_to_date` (True, `:2266`) for the legacy `pr_merge` gate; `owner_recovery_sweep` (False, `:2270`); `publisher_stall_after` (5, `:2276`); `github_app` / `scratch_probe` (`:2253-2254`); `auto_task.max_verification_retries` (2, `:590`); `test_selection.*` (`:2446-2464`). There are no `hierarchy.*`, `delivery.*` or `ci.*` sections (`:4427-5033`).

### C.5 Playbook-level knobs

Routing YAML (`src/prompts/default_playbooks/default-assignment-routing.md:110-152`): `origins.integration_repair {narrow:false}`, `origins.development_repair {narrow:false}`, `kinds`, `lanes`, `reserved`, `balance`; the only place that decides which model or harness authors a repair. `ci-main-sentinel` (`ci-main-sentinel.md:8, 44-47`): `timer.15m`, repair class `deep-high`, priority 5; its attempt limit `DEFAULT_MAX_ATTEMPTS=2` is in `src/commands/ci_commands.py:38`.

### C.6 Hard-coded constants (not changeable without code)

| Constant | Location | Value |
|---|---|---|
| `STUCK_BATCH_ATTEMPTS` | `repair.py:73` | 3 |
| `CONSTRUCTION_REDRIVE_GRACE_SECONDS` | `repair.py:76` | 600 |
| Seal retries / re-sweep delay | `scheduler.py:420, 433` | 3 tries, +5 s |
| `TrainService.DEFAULT_PAGE_SIZE` (paging only; a batch has no size cap, `:844-884`) | `scheduler.py:399` | 64 |
| `SETTLING_EXTENSION_SECONDS` / `SETTLING_CAP_SECONDS` | `settling.py:11-12` | 300 / 1800 |
| `UNSEALED_GRACE_SECONDS` | `stale_schedule.py:82` | 3600 |
| `INTEGRATION_LEASE_SECONDS`, claim horizon, tick margin | `integration_schedule_queries.py:13,17,18` | 300 / 135 / 65 |
| `CONSTRUCTION_RETRY_SECONDS` | `candidates.py:219` | 60 |
| Candidate `regenerate_command` / timeout (not wired from any policy, `integration_commands.py:1607-1616`) | `candidates.py:258-259` | `scripts/regenerate-generated.sh` / 600 |
| Green re-drive base / max generations / grace | `green_continuation.py:47,49,52` | 60 / 6 / 60 |
| `FAILED_BRANCH_KEEP_SECONDS`, `PROTECTED_BRANCHES` | `delivery_branches.py:80,82` | 14 days; `main`, `gh-pages` |
| Development branch cleanup attempts / backoff / rows per tick | `development.py:88-92` | 8; 300 s to 6 h; 10 |
| `REPAIR_GENERATIONS` / `REPAIR_CHAIN_LIMIT` | `development.py:99, 257` | 3 / 8 |
| Development repair `max_retries` | `development.py:3973` | 3 |
| `INFRA_ALERT_AFTER` | `development_validation.py:41` | 3 |
| `POLICY_KINDS` (auto-authorized task types) | `root_authorization.py:34` | {feature, bugfix} |
| `branch_discard` retry base / max / attempts | `branch_discard.py:47-52` | 30 / 3600 / 8 |
| `REFUSAL_THROTTLE_SECONDS` / `DEFAULT_QUIET_SECONDS` | `owner_recovery.py:114,118` | 300 / 600 |
| Outbox page / retry | `outbox.py:186-188` | 100; 1 s to 300 s, retried forever |
| Integration service page / tick | `service.py:42-43` | 100 / 5 s |

### C.7 Policy decisions made in Python

None can be overridden by a playbook today.

| # | File:line | Rule |
|---|---|---|
| D1 | `repair.py:1114-1162` | When counted failures reach `primary_attempts` or `debug_attempts`: stage 0 always escalates to debug; stage >0 escalates again only if `on_exhausted=continue`, else human block. The playbook sees only `escalate`/`human_required`. |
| D2 | `repair.py:1091-1101` | Infrastructure-classified or inconclusive evidence never counts as an attempt. |
| D3 | `repair.py:1898-2029`, driven by `service.py:179-194` | Deadline expiry follows the same ladder, invoked by the service tick, not a playbook. `integration.repair_deadline_due` is emitted only on membership ejection (`controls.py:1609-1630`). |
| D4 | `repair.py:3926-4058` | Two budget shapes only: stage 0 uses `primary_*`; every later stage uses `debug_*` and `debug_intelligence_class`. Under `continue` the ladder is unbounded. |
| D5 | `repair.py:3943-3958, 4108-4157` | A debug stage whose subject SHA did not advance gets no new stage; a supervisor incident is sent; the operation stays `escalated`; the writer is preserved. |
| D6 | `repair.py:1192-1200, 1209-1254` | Three consecutive counted batch failures send one supervisor message. `record_result` is reached only via `integration_record_repair` (`integration_commands.py:2155`), which only the parent playbook calls. Root red CI is counted in `attestation.py:~714-760` with no limit check, so the root ladder is time-driven only. |
| D7 | `repair.py:4159-4222` | Human block: parent task → BLOCKED, or batch lifecycle → `human_blocked`; then `integration.human_blocked` is emitted. |
| D8 | `repair.py:122-151`; `orchestrator/core.py:1217-1229` | Under `continue`, the orchestrator auto-dispatches successor stages itself, bypassing the playbook. |
| D9 | `repair.py:1256-1272, 1375` | When a writer closes under `continue`, the next stage opens automatically. |
| D10 | `repair.py:3169-3258`; `:1417-1473`; `:1562-1581, 3309+`; `:1488-1511` | Who writes: parent stage 0 reuses the live attached verifier; otherwise a new root-level PAUSED delegate `repair-<op>-<stage>` with `class_hint` = stage class; the debug writer inherits the primary's dirty workspace; for batch stage >0 the predecessor's owner is force-recovered first. |
| D11 | `repair.py:567-574` | Stage 0 uses `primary_intelligence_class`, falling back to `debug_intelligence_class`. |
| D12 | `repair.py:3033-3078` | The playbook's literal `stage 0` is remapped to the active continued stage; the playbook's stage choice is ignored. |
| D13 | `candidates.py:314-321` | `on_exhausted=continue` ⇒ construct from current main rather than the sealed `base_sha`. |
| D14 | `candidates.py:1627-1630, 2373-2378, 3593`; `repair.py:3650-3659`; `scheduler.py:928-930` | `conflict_scope=batch` ⇒ the delegate resolves all members at once; multi-parent commits recorded; sources in CI state `conflict` admitted. |
| D15 | `candidates.py:2283-2420` | Member conflict ⇒ the partial head is published and the current stage dispatched. |
| D16 | `candidates.py:2315-2320` | A member touching a reserved `.aq` path counts as a conflict. |
| D17 | `candidates.py:2322-2337` | A member already present as an ancestor is applied as a no-op. |
| D18 | `candidates.py:2422-2481` | Generated-file-only conflicts are resolved by regenerating (always tried). |
| D19 | `candidates.py:414-573` | A Git-proven unbuildable source aborts the whole batch, then catches up and reseals the valid members. |
| D20 | `candidates.py:649-701` | `base_moved` or `source_moved` ⇒ construction retry every 60 s, indefinitely. |
| D21 | `candidates.py:930-972` | On a rebuild, accepted CI repairs are preserved with a two-parent merge. |
| D22 | `candidates.py:974-1033`; `repair.py:2714-2936` | A rebuild conflict is frozen under the current stage budget (no new budget). |
| D23 | `integration_commands.py:1810-1838` | `on_main_moved`: `rebuild` ⇒ auto-rebuild; `wait` ⇒ `base_moved`. |
| D24 | `scheduler.py:834-963`; `:870-882`; `:885, 953-962`; `:889-894`; `:895-952`; `:598-633, 800-832` | Batch membership: every eligible member, no size cap; a member must be in `pull_request` mode with approved exact review evidence; sorted by `(task_id, head)` then dependency order; authorized-admission filter; source-CI chain filter; Alembic head collisions deferred with a supervisor message. |
| D25 | `scheduler.py:568-590` | One live batch per project. |
| D26 | `scheduler.py:100-247`; `settling.py:15-69` | Sweeps fire only if enabled and settled; repeated triggers coalesce into one catch-up. |
| D27 | `green_continuation.py:330-372` | A green-but-unpromoted batch is re-emitted with exponential backoff up to 6 times, then only logged. |
| D28 | `main_promotion.py:595-620` | Root delivery is always a fenced fast-forward push to main; no PR-merge option; the audit PR is only closed (`cleanup.py:960-1029`). |
| D29 | `cleanup.py:537-555`; `:960-1029`; `:1073-1093` | Cleanup backoff from the frozen policy; exhausted attempts fail the item; source PRs always commented and closed; integration branch deleted; failed-stage workspaces held for the retention period. |
| D30 | `parent_completion.py:524-586` | Any FAILED child without a disposition makes readiness `failed`; otherwise `waiting` or `ready`. |
| D31 | `parent_completion.py:274-330` | `branchless_parent=verifier` with no session attempts ⇒ a verifier task is filed; a missing verifier class sends `configuration_blocked`. |
| D32 | `parent_completion.py:913-935` | Verification requires success for exactly the required names, from the producer, excluding infrastructure evidence. |
| D33 | `admission.py:375-394` | In development mode a dependent waits until its prerequisite is delivered, not merely completed. |
| D34 | `review_evidence.py:98-118`; `root_authorization.py:209-277` | Root admission by kind allowlist, per-task allowlist or grant; `reviewed` requires an approved verdict. |
| D35 | `development.py:1539-2066`; `:1711-1743`; `:1888-1945`; `:1959`; `:2022-2044`; `:2007-2020`; `:987` | Development publisher: candidates ordered by `updated_at`; in a dependency cycle the newest repair supersedes; a conflicting member is parked alone; batch capped at `max_batch_size`; failed validation parks the whole batch; infrastructure failure defers; `advisory` passes regardless. |
| D36 | `development.py:3834-4046`; `:3984-4040` | Development repair: generation >3 refused; a single-source repair nests under its source if depth allows, else the source gets a `blocks` edge. |
| D37 | `integration_commands.py:104-161` | Source-CI red ⇒ repair filed; previous repair FAILED: `human` ⇒ `human_required`; `continue` ⇒ another attempt with no cap. |
| D38 | `integration_commands.py:204-240` | Source-ancestry failure ⇒ one automatic reopen; otherwise a supervisor message. |
| D39 | `development_validation.py:41`; `development.py:1065-1076` | Three infrastructure deferrals in a row ⇒ supervisor message. |
| D40 | `development.py:3234-3315` | Switching to development mode is refused while train/hierarchy work remains. |
| D41 | `outbox.py:216-221, 297-322` | An event with no accepting playbook is retried forever. |

### C.8 What the shipped playbooks decide versus relay

The compiled graphs (`src/prompts/reviewed_playbooks/{root-train,parent-integration}/artifact.json`) and the `agent-queue-*` variants compile to identical rules and steps apart from id, scope and prose; the "continuous policy" prose in `agent-queue-root-train.md:78-84` is descriptive, the behaviour coming from `on_exhausted=continue`.

`root-train` (frontmatter `root-train.md:1-16`; eight triggers; no guards or filters): `seal-due-frontier` (`integration_seal`; `empty` → `integration_release`), `construct-and-test` (build → ci; `conflict` → dispatch; green, red and pending all end at "done"), `promote-green-candidate` (promote; `base_moved` → build → ci or dispatch), `repair-red-candidate` (one dispatch), `continue-closed-root-repair` (close-current → build → ci or dispatch), `dispatch-debug` (one dispatch with no stage; the server uses the active stage, `integration_commands.py:2222`), `release-promoted`, `cleanup-promoted`. **No decision steps.**

`parent-integration` (frontmatter `parent-integration.md:1-21`; 13 triggers; 14 compiled rules): `completed-child-readiness` / `failed-child-readiness` (guard `exists task.parent_task_id`; `delivery_readiness`; on `failed` a decision step `cases:[{label:"ask", when: readiness.on_failed_child eq "ask", goto ask-human}]`, `default: blocked`; the value comes from `parent_completion.py:599`), `file-children` and `checkpoint-parent` (relays; **no emitter found** for `task.child_added` or `task.parent_checkpointed` in `src/`), `promote-delivery` (promote; `conflict` → `repair_start` → dispatch literal stage 0, remapped by D12), `project-delivery-readiness` (same decision), `wake-parent-verifier`, `record-repair-result` (filter `conclusion: failure`; escalation decided by D1), `verify-parent` (filter `success`, `target_kind: parent`), `dispatch-debug` (literal stage 1), `expire-repair-stage` (normally pre-empted by the service tick), `reconcile-resolution-push`, `complete-verified-parent`, `observe-repair-close` (a sink so the outbox has an acceptor). **One decision step.**

`ci-main-sentinel` has real branching (green/pending/unknown end; `red` files and adopts; `red_escalated` escalates with choices); `default-assignment-routing` holds real policy (the routing YAML).

### C.9 The frozen-policy mechanism

Written: at seal the live policy is dumped into `integration_batches.policy_snapshot` and `artifact_snapshot` (`scheduler.py:528-548, 634, 666, 691, 724`); `reserve_batch_operation_on` (`repair.py:194-280`) copies it into `integration_repair_operations.policy_snapshot`, `artifact_snapshot`, `required_check_version`, `route_*`, pins the artifact in `integration_operation_artifact_pins`, and raises if an existing operation's snapshot differs (`:216-222`); a parent operation is frozen when the episode is reserved (`parent_completion.py:83-147`); each stage stores `boundary.repair` in `integration_repair_stages.policy` (`repair.py:423, 634, 4018`).

Read: budgets from `stage.policy` (`repair.py:1115-1117, 1136, 1263, 1940, 2001`; `controls.py:1603-1607`; `recovery_controls.py:199-201`; `candidates.py:1498`; `preserved_repair.py:375`); successor stages from `operation.policy_snapshot` (`repair.py:3936-3937`); integrity checks reject drift (`repair.py:4262-4290`). Events carrying `operation_id` go to the operation's pinned artifact (`playbooks/services.py:130-166`; `runtime.py:380-420`); only `task.completed`, `task.failed`, `task.child_added`, `task.parent_checkpointed` and `integration.sweep_due` fall back to the live route (`services.py:66-74, 167-183`).

Changing mid-operation: through supported controls, impossible (`configure` requires disabled, desired disabled, not draining, no active work, generation CAS; `controls.py:776-858, 928-977`; the same gate on `edit_project`, `project_commands.py:318-360`). Several paths nevertheless read the live policy: seal-time admission, source-CI and conflict-scope filtering (`scheduler.py:886-930`); review authorization (`review_evidence.py:81-118`); root authorization (`root_authorization.py:209`); source-CI repair (`integration_commands.py:104-161`) and ancestry (`:204-216`); review polling (`github_review_poll.py:182`); protection (`protection.py:548-552`); guarded by the generation bound into review and source-CI evidence (`integration_source_ci.policy_generation`, `tables.py:680`). Development mode has no freezing: `DevelopmentPolicy` is re-read every sweep (`development.py:1541, 2725`) and `configure` overwrites it without checking parked work (`:3316-3328`).

### C.10 Events the outbox emits and who subscribes

`enqueue_integration_event` (`outbox.py:127-175`) injects `project_id` and `event_id`; schemas at `src/event_schemas.py:1604-1880`.

| Event | Emitted at | Payload (plus `project_id`, `event_id`) | Shipped subscriber |
|---|---|---|---|
| `integration.sweep_due` | `scheduler.py:238-246`; `release.py:313-321`; `stale_schedule.py:438-446`; seal retry `scheduler.py:427-435` | `operation_id` (= request id) | root |
| `integration.sealed` | `scheduler.py:776-788`, `:368-381`; `candidates.py:695-700`; `repair.py:2075-2080`; `main_promotion.py:902-911`; `controls.py:1665-1674` | `batch_id`, `operation_id` | root |
| `integration.candidate_green` / `_red` | `attestation.py:774-787`; `green_continuation.py:132-145` | `operation_id`, `batch_id`, `revision`, `head_sha` | root |
| `integration.repair_exhausted` | `repair.py:4049-4057` | `operation_id` only (schema allows `stage`; not sent) | root, parent |
| `integration.repair_delegate_closed` | `repair.py:1850-1892` | `operation_id`, `stage`, `task_id`, `session_id`, `instance_token`, `workspace_id`, `fence_token`; batches add `batch_id`, `revision`, `head_sha` | root, parent |
| `integration.batch_promoted` / `cleanup_requested` | `main_promotion.py:1323-1337` | `operation_id`, `batch_id`, `revision`, `intent_id`, `head_sha` | root |
| `integration.root_delivered` | `main_promotion.py:1250-1264` | `operation_id`, `batch_id`, `revision`, `member_ordinal`, `receipt_id` | **none** |
| `integration.human_blocked` | `repair.py:4213-4221` | `operation_id` | **none** |
| `integration.ci_completed` | `parent_ci.py:168-179` | `operation_id`, `target_kind`, `task_id`, `generation`, `head_sha`, `evidence_ids`, `evidence_id`, `conclusion` | parent |
| `integration.repair_deadline_due` | `controls.py:1618-1630` (ejection only) | `operation_id`, `stage`, `deadline_event_id` | parent |
| `integration.resolution_push_observed` | `integration_delivery_queries.py:471-482` | `operation_id`, `promotion_intent_id` | parent |
| `delivery.ready` | `collection.py:209-224` | `operation_id`, `operation_key`, `source_task_id`, `source_head`, `source_base`, `expected_target`, `fence` | parent |
| `delivery.applied` | `integration_delivery_queries.py:789-796` | `operation_id`, `promotion_intent_id`, `receipt_id`, `source_task_id`, `target_task_id`, `repository_id`, `target_branch` | parent |
| `integration.cleanup_pending` | `integration_delivery_queries.py:798-805` | as `delivery.applied` | **none** |
| `task.integration_ready` | `parent_completion.py:353-379` | `operation_id`, `task_id`, `title`, `episode_id`, `generation`, `head_sha`, `verifier_task_id`, `target`, `expected_token`, `next_owner_id`, `next_role` | parent |
| `task.integration_verified` | `parent_completion.py:992-1008` | `operation_id`, `task_id`, `title`, `generation`, `head_sha`, `verification_id` | parent |
| `task.integration_configuration_blocked` | `parent_completion.py:280-294` | `operation_id`, `task_id`, `title`, `reason` | **none** |
| `integration.branch_materialization_pending` | `hierarchy.py:1394-1409` | `operation_id`, `origin_id`, `task_id`, `repository_id`, `branch`, `parent_ref`, `base_sha` | **none** |
| `task.child_added`, `task.parent_checkpointed` | schema only (`event_schemas.py:1679-1698`) | | parent, but **no emitter found** |
| `task.completed`, `task.failed` | event bus, not outbox (`orchestrator/events.py:44-50, 115`; `monitoring.py:425`) | hydrated task | parent |

Audit-only (`events` table, not subscribable): `integration.collection_redriven` (`collecting_parent_recovery.py:270`), `integration.collection_reopened` (`cancelled_collection_recovery.py:95, 892`), `integration.migration_deferred`, `integration.batch_source_withdrawn`, `development.operation`.
