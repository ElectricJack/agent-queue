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

- A stopped parent repair left an obsolete claim file in its unlocked slot.
  Cross-slot preparation consequently refused the branch. A narrowly checked
  stopped-session claim retirement is under validation.
- Candidate adoption creates a new revision, but the background continuation needs
  to recover its publication before attempting CI attestation.
- As of the latest task inventory, keen-harbor has seven completed children and
  one running child; noble-ridge.1 is running. These counts are snapshots, not
  delivery receipts.
- Matter Engine (`matter-engine-cpp`) has no queued tasks in the current API
  inventory. The representative workload still needs selection.

Each acceptance item requires current persisted state, Git/PR/CI evidence, and
appropriate focused or scale-test results before it can be checked off.
