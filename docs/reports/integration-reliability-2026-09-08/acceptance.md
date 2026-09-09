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
