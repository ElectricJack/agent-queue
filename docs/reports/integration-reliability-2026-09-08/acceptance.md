# Core reliability and speed: live integration acceptance

This records the active operator goal, including the 2026-09-08 expansion to
Matter Engine and hundreds of tasks per day. It is a completion checklist,
not a claim that the system has passed acceptance.

## Required outcomes

- [ ] Ready work starts on compatible enabled workers without repeated manual intervention.
- [ ] Failed preparation, stopped sessions, stale claim files, branch handoffs,
      daemon restarts, and repair escalation recover without losing work or bypassing fences.
- [ ] The keen-harbor CLI epic completes all 15 children, collection, aggregate
      verification, review, and delivery.
- [ ] The noble-ridge Discord epic completes all 12 children and its dependency
      graph, collection, aggregate verification, review, and delivery.
- [ ] Repaired candidate publication and subsequent CI observation resume automatically.
- [ ] Recursive feature branches isolate child and project work; leaf commits are
      squashed before first review, reviewed feature ancestry is retained on merge,
      and main advances only to the exact tested candidate. See
      `docs/guides/feature-merge-history.md` for the current history policy.
- [ ] Scale tests demonstrate capacity for hundreds of tasks per day. Record
      workload, concurrency, durations, throughput, database/query cost, and failure
      recovery. Distinguish orchestration overhead from model execution and CI time;
      do not extrapolate a small passing test into a production throughput claim.
- [ ] The pipeline is tested against Matter Engine work on isolated feature branches,
      with explicit evidence for assignment, dependencies, review, collection, CI,
      and the intended final delivery boundary.
- [ ] Reliability fixes are committed, reviewed, tested, and delivered upstream.

## Verified milestone

Root batch `integration-batch-338cbc1c90b6854a8c0811c567629d0c`, revision 4,
reached `promoted`. Its `tested_candidate_sha` and `final_main_sha` both equal
`6d2b07db0ea59aae1c2230091c6863b5d178da78`; `git ls-remote origin refs/heads/main`
confirmed that exact remote tip. GitHub CI run `34315938800` passed all three suites.
The batch repair operation is completed. This run needed operator reconciliation
of the repaired candidate publication and branch handoff, so it does not establish
automatic recovery acceptance.

## Current gaps to close

- Stopped-slot claim retirement and interrupted preparation recovery are verified
  live. The pool-principal resolution fix (`91902d7c`, 59 promotion tests passed)
  allowed the stage-1 repair to finish through its authenticated worker session.
- Candidate adoption creates a new revision, but the background continuation needs
  to recover its publication before attempting CI attestation.
- As of the latest task inventory, keen-harbor has seven completed children and
  one running child; noble-ridge.1 is running. These counts are snapshots, not
  delivery receipts.
- Matter Engine now has two real feature epics (`nimble-dune` and `smart-dune`)
  covering agent-facing selection and procedural command automation; see `matter-engine-plan.md`.
  Its scheduling is paused pending new-pipeline configuration, not implementation completion.

Each acceptance item requires current persisted state, Git/PR/CI evidence, and
appropriate focused or scale-test results before it can be checked off.

## Parent recovery evidence (2026-09-09 06:38 UTC)

The stage-1 repair completed with receipt
`receipt-af362164-cd00-55d3-932b-5b4e0990da44` for `keen-harbor.14` at
`197ce19ab598e56e4c37e6fb6470380233f64036`. The daemon automatically returned
`aq/keen-harbor` to its collector (reserved, fence 7). Collection then committed
three additional children without operator intervention: `.1`, `.2`, and `.4`.
The latest collected head is `68931458dd01a1f27712ff958521888cd4d4ba83`.

The next child, `.6`, has a new conflict:
`intent-0f17b393-8387-5318-95b5-2ca3523ff69b`. The operation still has its original
stage-0 trigger and a completed stage-1 delegate. `RepairService.start` currently
requires the original starting SHA/trigger on replay; repeated conflicts within
one parent episode need investigation without resetting the frozen repair budget.

The read-only health sweep found 37 OK, 6 informational, 10 warning, zero error
checks. Claim consistency, preparation, slot checkouts and stuck sessions passed.
A stale attention flag on the completed root repair was cleared with the public
`aq doctor --check tasks.stale_attention --fix` command and verified clear.
Configuration/profile warnings remain; this does not establish full health.

## Next conflict and throughput work

`nimble-forge` was created through AQ for repeated-conflict continuation with
frozen budgets and pinned-artifact compatibility; AQ assigned it automatically
to `agent-e9dccce8a2e5`. This is implementation in progress, not a resolved conflict.

Candidate recovery now reuses exact commit objects in the daemon retained store
and pins each required recovery ref (`b1e60ea3`). Remote head and authority checks
remain independent. The 63-test candidate suite passed; the added fresh-service
restart assertion passed separately. A one-member build fetches two unique OIDs,
not the shared base twice; replay fetches no retained inputs. This is a measured
operation-count improvement, not proof of hundreds-of-tasks/day throughput.

Matter Engine configuration inspection additionally found no registered `repos`
row for `matter-engine-cpp`. Repository registration must precede designation and
policy enablement. The project remains paused; its two feature epics are queued.

## Matter Engine rollout prerequisites (2026-09-09)

Registered repository `matter-engine-cpp` through `aq project set ...
integration-repository` and configured `integration-review-mode pull_request`.
Both guarded configuration commands succeeded; generation is now 2. Status
confirms repository designation and review policy blockers are gone. Integration
remains disabled with `policy_invalid`, and scheduling remains paused.

The clean local engine checkout is at
`f912eaf75a805dc898af0a17051baf0378105123`, 228 commits ahead of the GitHub main
head `84408b3c01b41d834c86d8b1330d920e43c968f9` (confirmed through GitHub API).
The native MSVC build and procedural CLI assumptions come from that local
baseline; feature-task origins must not silently use the older remote baseline.
No tracked GitHub workflow and no registered repository Actions runner exist.
The canonical wrapper requires VS 2022 Community, MSVC 14.44.35207, Windows SDK
10.0.26100.0, native Python 3.13, and Vulkan SDK 1.4.357.0. A runner-location
preference is pending with the user. Required native CI and baseline delivery
must be established before the two queued epics can run under the new policy.

## Automatic candidate publication recovery

`a9037973` adds a background publication-before-CI continuation using the normal
fenced candidate builder, and returns a completed detached repair delegate's
branch only when its adopted candidate exactly matches the current stage.
Validation: 84 candidate/service tests passed; two additional crash cases passed
for absent publication and interrupted publication. Changed files introduce no
new Ruff diagnostics relative to HEAD before the change. Loaded after restart.

Live batch `integration-batch-db9e3d6681c4b82624d569d2bdbf2a6e` is at revision 1,
`371391a11fc7035545d58ec9bf16af3cea123710`, with publication only for revision 0.
Stage 0 completed, but its deadline elapsed before this fix; stage 1 now owns
the reserved branch and is READY. Its disabled-Claude route was changed through
`aq task route` to enabled `deep-high-codex`. Recovery waits for that authorized
repair; it does not seize its reservation. Automatic live publication acceptance
is therefore still pending.

## Live publication recovery verified; moved-main continuation fixed

The second batch's stage-1 repair completed without code changes. AQ then
automatically published revision 1 at `371391a11fc7035545d58ec9bf16af3cea123710`,
returned its detached branch to the collector, and entered promotion. GitHub run
34319555915 independently confirmed success at that exact SHA. No manual build
command was used for that publication recovery.

Main had moved from the batch's expected base
`999990a7520dd2caa78cfbfcf388d2819a6a38c8` to
`6d2b07db0ea59aae1c2230091c6863b5d178da78`. After the unattempted mutation lease
expired, AQ superseded the stale intent and entered `building`, but emitted no
rebuild continuation. `7cc03e02` now queues the existing pinned sealed-batch
construction route atomically with that transition. Root-promotion validation:
52 other tests passed in the area run; the corrected moved-main test passed
separately, including one durable event across replay and preserved repair budget.
The fix is loaded. The already-stranded live batch was resumed once through
`aq system integration-build-candidate`; its result is still being monitored.

A stale-attention cleanup timed out and was not counted as successful. Subsequent
read-only PostgreSQL inspection found no active blocking transaction.

## Queue progression and isolated swarm acceptance

Both `nimble-forge` (parent continuation) and `eager-falcon` (root moved-main
conflict continuation) have live AQ worker sessions. Newly ready `noble-ridge.2`
and `keen-harbor.10` were routed from the disabled Claude pool to enabled Codex.
No pool bounds or agent roster entries were changed for this step.

The canonical Windows preflight passed on the engine checkout, verifying the
pinned native toolchain. `sharp-nexus` now records CI/bootstrap and baseline
delivery in matter-engine-cpp, and `nimble-dune.1` has a verified blocks dependency
on it. Workstation CI is the working assumption; no runner is installed yet.

The required Tier-1 swarm kit is running in an isolated environment:
`AQ_E2E_HOME=/tmp/aq-goal-e2e-20260909d`, port8199, database
`aq_goal_e2e_20260909d`, fake session provider (no model processes). Setup passed;
its daemon and smoke process were confirmed live. Output is in
`/tmp/aq-goal-e2e-smoke.log`; no pass/fail claim yet. This is functional protocol
acceptance, not the still-required hundreds-of-tasks/day capacity measurement.

## Loaded parent continuation and recovery follow-up

Worker nimble-forge completed. Implementation 37fcaa4a was applied as
02cec0bc; current-checkout repair/promotion tests passed 122 tests. AQ
restarted successfully with the fix loaded (PID 4191061).

The initial swarm run passed 7/8 scenarios. S5 left a foreign-project task
that consumed one of two global pool slots, starving S7. Commit 96948f1e
removes that fixture before freeing capacity. The independent S5/S6/S7
rerun passed 3/3 and stopped its daemon normally. Logs:
/tmp/aq-goal-e2e-rerun.log and /tmp/aq-parent-continuation-loaded-tests.log.
This is functional acceptance, not throughput proof.

Public integration resume returned resumed for keen-harbor operation
81d0aaee-0c3a-482c-b04c-d3afe6631cbe. Human hold cleared, but task explain
still reports parent BLOCKED by integration_repair_exhausted. Recovery
controls restore batch lifecycle without equivalent parent-task restoration.
Collection has not been verified resumed. Parent routing now selects the
enabled Codex pool. Eager-falcon remains live implementing root moved-main
conflict recovery and fresh-CI validation. Full delivery remains incomplete.

## Root conflict repair and native baseline validation

Root repair implementation e8fac053 was applied locally as ed8e8acc. Review
follow-up 4bbbd7a2 validates the delegate project, repository, branch, creator,
assignment, sessions and workspace before reuse. Nine focused real Git/DB
tests passed; combined candidate/parent repair validation is running in
/tmp/aq-combined-root-parent-tests.log. This code is not yet loaded.

Parent-resume repair brisk-apex is now assigned to agent-b75999c4bb18 in
session b0b1fe0f-3a12-482c-95d8-685bcf9e89fd, slot-4. It was routed to the
available standard-high-codex pool with matching standard-high class; no pool
bounds changed and no original-epic worker was interrupted.

Matter Engine isolated branch aq/ci-bootstrap-20260909 contains CI preparation
a0cb6cb0 and clean-build fix 3697dc4a. The fresh native MSVC build initially
failed because embedded_shaders.h had no CMake generation rule. The fix
generates shader text in the build tree with explicit input dependencies and
consumer ordering. The native build now passes, including the editor and
test executables. The 98-test CTest run is active; shader_source_tests and
vulkan_smoke_tests passed. Log: /tmp/aq-matter-native-ci.log, handle23927.
Package validation, remote Actions execution, runner registration, baseline
delivery, feature execution and throughput acceptance remain pending.

## Live recovery and native package results

Parent recovery 067a5112 required review fixes140c1a3e (encoded terminal
metadata and prevalidation) and4db76c8d (checkpoint progress within an episode).
103 area tests passed; targeted atomic rejection and generation tests passed.
Loaded through4db76c8d. Public resume restored keen-harbor to PAUSED and kept
deadline1788942168.182611. Explicit start/dispatch of the current .6 conflict
then refreshed the dossier and granted repair fence8; delegate is IN_PROGRESS.
This required manual replay: nimble-cascade now implements the missing durable
automatic resume-to-current-conflict dispatch.

Root repair pushed exact two-parent merge e13900405da3eacc523510a3513afa31df22da87.
The stage expired before close. Guarded handoff stopped/detached the writer and
granted collector fence7, but left stopped session ff7c0266-bda8-49fa-8bc9-25172b03ff98
with active claim state; its task became BLOCKED/session_not_live. Grand-apex
is implementing atomic cleanup and safe replay recovery. No fabricated close,
receipt, CI result, or operator-DB mutation was used.

Native Matter build passed. CTest passed97/98, then the sole failed wrapper
contract passed after executable-bit fix72867a10. Viewer logic passed238.67s.
Packaging passed111 hashed files plus manifest,9 imports,0 runtime DLLs and
a clean-PATH launch. Windows Git needed the isolated worktree .git pointer
converted from an absolute WSL path to its equivalent relative path. Logs:
/tmp/aq-native-package-rerun.log and /tmp/aq-native-package-validation.log.
Remote native CI, runner setup and baseline publication remain incomplete.

Noble-ridge.2 completed and .3 claimed. Task explain falsely described its
released PAUSED parent as blocking while is_blocked=0; amber-zenith tracks
typed dependency diagnostic parity. Grand-apex and nimble-cascade are live.
Workers have the isolated /tmp/aq_goal_test.py invocation for actual DB tests.
Full parent collection, final reviewed promotion, and capacity proof remain
incomplete; this report does not equate implementation close with delivery.


## Guarded recovery rollout and live native CI

Loaded local commit 08786ee2 with stopped handoff recovery and current-conflict
resume continuation. Combined isolated PostgreSQL checks passed 139 tests in
48.83s (workspace handoff, repair, operational controls), log
/tmp/aq-combined-recovery-tests-v2.log. The corrected assertion snapshots the
prior-resume deadline after fixture setup; recovery now locks project before
operation. Recovery module Ruff passed; a pre-existing ASYNC221 in an unrelated
Git fixture remains in test_integration_repair.py. Restart completed as PID
150014, with degraded health still reported.

Keen-harbor.6 now has durable receipt receipt-b16da8dd-f85a-5694-898e-b49b17503576
at exact parent head 3eada329ea1707a77d151b7d728352a2041971a2. Worker recorded
431 CLI test passes and successfully corrected historical fence5 to live fence8.
Parent collection has moved on to another conflict; full epic delivery remains
incomplete. Noble-ridge has three completed children and two running children.

Public root resume still returns invalid_state: former slot ws-hidden-hall is
now held by amber-zenith. Current cleanup correctly refuses to clear successor
ownership. New AQ task amber-crest covers exact historical-claim release while
preserving reused agents/slots. Sound-apex covers first human_required resume
with a newer conflict; current coverage proves only the active-stage replay.

Matter baseline is pushed as 72867a102a263d07d0a3de2b9c0ec52fe6841040, draft
PR https://github.com/ElectricJack/matter-engine/pull/6. Dedicated native runner
aq-matter-windows is registered and executing Actions run34326903928. Checkout
passed and build/test/package is in progress; no remote success claimed.
Feature project remains gated. Its two live epics and twelve children retain
explicit AQ dependency, workspace, review, recursive integration and exact-CI
acceptance. Hundreds/day throughput remains unmeasured.


## Native CI success and real rollout blocker

Matter Actions run34326903928 completed successfully on exact baseline
72867a102a263d07d0a3de2b9c0ec52fe6841040: 98/98 CTest cases and native package
validation passed. Complete remote log: /tmp/aq-matter-remote-ci.log.
Policy is recorded in matter-integration-policy.json and configured at generation3;
observe then train enable succeeded at generations4 and5. Project is ACTIVE.
Both feature epics have explicit manual pauses withholding children while
sharp-nexus delivers the baseline through AQ review and the exact-CI train.

Live task explain now exposes repository_not_designated for sharp-nexus: it was
created while disabled and has no repository binding. Clear-quest tracks guarded
binding and origin materialization for this rollout shape, preserving the existing
graph. Bootstrap has not started and native success alone is not delivery.

Applied grand-apex public resume regression as8be1903a (1 passed) and amber-zenith
typed dependency diagnostics as43798453 (118 passed in44.06s). Loaded through
04def40b; restart PID177684 completed with degraded health warning. Live
keen-harbor.12 explain no longer falsely reports its released PAUSED parent as
an unmet dependency. Amber-crest is now assigned and working on reused-slot
historical-claim recovery. Sound-apex remains queued for first-resume continuation.
Neither original epic nor all local AQ fixes have reached final reviewed main
promotion; hundreds/day throughput remains unverified.


## Live capacity recovery, root resume, and Matter assignment

Pool rebalancing27653d02 addresses idle workers stranded in another project
when fleet-wide supply equals demand. It honors warm floors, destination
capacity/quarantine/caps, grace and drain budget, and leaves replacement starts
to normal sizing.92 focused pool tests passed; the additional reconciler
relocation regression passed. Required isolated real-daemon swarm run passed
8/8 scenarios, log /tmp/aq-pool-rebalance-e2e.log; its daemon and private database
were cleaned up. Ruff comparison found no new diagnostics in the changed code;
existing lint debt is not claimed clean.

Applied amber-crest e7ec6005 as02ca0e24 and operation-identity review guard7391eeac.
104 repair/handoff tests and2 negative ownership tests passed. Public root resume
succeeded at deadline1788946538.1471775, retaining the bounded stage and task.
Old ff7c0266 claim cleared; ws-hidden-hall remained assigned to successor
sharp-beacon. Existing root repair task is READY pending capacity, not delivered.
Operator fixes through7391eeac are published at aq/operator-reliability-20260909;
AQ swift-journey owns reviewed train delivery. No direct main push occurred.

Applied clear-quest32933171 asb79c9e10;107 rollout/contracts/hierarchy tests passed.
Loaded via restart PID230035. Public reconcile-unmaterialized succeeded at
Matter generation6 and bound all15 task records. Origins were verified against
remote main84408b3c01b41d834c86d8b1330d920e43c968f9, not the unpublished local main.
The formerly idle outrider worker drained and the same global agent87428fad3069
was assigned to sharp-nexus in Matter session1927290f-4adf-4ce7-bb88-43ffae12fa88.
Keen-harbor.12 is now IN_PROGRESS; noble-ridge has6/12 children completed.

Correction to prior gating descriptions: a manual pause of an already-released
container preserves descendant eligibility. Both entry tasks nimble-dune.1 and
smart-dune.1 are now explicitly PAUSED, in addition to their containers. Resume
them only after baseline review, exact candidate CI and promotion are verified.

Sharp-beacon is implementing recovery for keen-harbor expiry between resolution
reservation and push (head5f41738e remains unpublished). Calm-dune tracks another
restart gap: completed keen-harbor.8 still has an attached pool claim, consuming
capacity. Sound-apex covers first-human-resume conflict continuation. Full final
reviewed delivery and hundreds/day capacity proof remain incomplete.


## First-resume verification and observed delivery bottleneck

Sound-apex82712d1a is applied locally as51f01a84. Combined repair and operational
control verification passed109 tests in42.45s, log
/tmp/aq-first-resume-combined-tests.log. This commit is not yet loaded; the
currently running daemon remains throughb79c9e10. Sharp-beacon c7b963ac/9a82e953
is not loaded: review required exact ambiguity exclusions and explicit legacy
pre-push-marker migration semantics. No production migration for that patch
has been run. Calm-dune is assigned and working on completed-attached-claim
recovery.

Activity sample07:54:13–08:54:13UTC:24 distinct currently pass-completed tasks,
including repair/support work, versus2 code delivery receipts (one per original
epic). The safe aggregate is activity-window-20260909-0854.json. Remote AQ main
still equals6d2b07db0ea59aae1c2230091c6863b5d178da78. This sample identifies an
integration bottleneck; it is not a claim of sustained hundreds/day capacity.

Sharp-nexus confirmed exact incorporation of baseline72867a10, but its clean
native rebuild exposed missing ordering for shaders_gen/embedded_spirv.h:
matter_engine_core depended on shader text generation without the SPIR-V header
generator. The worker is correcting both generation dependencies and rerunning
native validation. Prior green CI remains evidence only for that one run.
Feature entry tasks remain explicitly paused pending corrected baseline delivery.


## CLI response-loss handling and review follow-through

Operator fix6310cb23 is published at aq/operator-cli-response-20260909. Generic
command transport now reports command_error with outcome=unknown and
automatic_retry=false for interrupted writes/reads and malformed replies,
without replaying a possibly committed command. Failed health clients close
cleanly; unhealthy HTTP responses have actionable errors. The implementation
is active in the operator CLI and does not require a daemon restart.
Client/generated-client/envelope checks passed66 tests; the final response-loss
file passed12 tests, including real CLI JSON/human rendering and one-request
assertions. Logs: /tmp/aq-cli-response-failure-tests.log and
/tmp/aq-cli-response-failure-final.log. Existing client lint debt remains.

Created keen-harbor.16 to integrate and review that individual commit on the
actual epic branch. Added a blocks edge from the final audit keen-harbor.13 to
.16, so the report cannot finish before this observed restart failure is
covered. This does not claim all special streaming transports are repaired.

Sharp-beacon9a82e953 fixes duplicate-owner ambiguity but still lacked the
explicit legacy-marker upgrade requirement at close. Reopened through the
public reopen-with-feedback command; its implementation/migration remain
unloaded until the legacy uncertainty and live recovery sequence are covered.
Sound-apex51f01a84 remains committed and tested, awaiting the next combined
rollout. Neither local progress nor task completion is counted as reviewed
main delivery.

## Terminal close recovery review and root scheduling (09:19 UTC)

Calm-dune published 90e2b812 on PR546. Applied as 19cbeaad to the operator
checkout; combined completion recovery and workspace handoff suites passed
156 tests in 96.62s. Ruff passed on all five changed Python files. Evidence:
/tmp/aq-terminal-recovery-combined-tests.log. Required isolated swarm smoke
is still running; this patch has not yet been loaded.

The exact keen-harbor.8 session still has a live Claude process at its completed
turn prompt. Terminal recovery correctly requires authoritative process
termination before detaching its clean published branch. A subsequent fenced
operator stop and public integration flush will be required after rollout;
no active work has been killed to free capacity.

Root repair priority changed from 100 to 1 through aq task edit, so the next
available deep-high worker can service its rearmed deadline at
1788946538.1471775. Pool caps and active workers were not changed.

Swift-journey completed its submitted task branch at
475143b0feb4f0d8a38443ec8c17d770a59f51d9, PR544: a two-parent merge preserving
remote main 6d2b07db and operator source 7391eeac. Worker reports 514 focused
checks passed. Remote main is still 6d2b07db; this is a submitted delivery,
not proof of candidate review, CI, or main promotion. Keen-harbor.16 is now
actively integrating the separate CLI response-loss commit.

Sharp-beacon 2efa6998 conservatively backfills legacy reserved pushes with an
unknown-start sentinel. Its documented reconciliation only covers a remote
already at the reserved head, so it does not recover the actual unpublished
keen-harbor resolution (remote 3eada329, reserved local 5f41738e). Reopened with
explicit feedback requiring that guarded public recovery and tests. The worker
also squashed previously reviewed commits; feedback requires preserving those
reviewed heads by merge and additive fixes. This migration remains unapplied.
