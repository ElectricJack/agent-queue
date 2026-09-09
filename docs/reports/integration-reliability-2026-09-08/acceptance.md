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
