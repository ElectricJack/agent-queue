# Development integration

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

Approved by the operator on 2026-09-09 following the safeguard review. Implement the fast path as the normal development workflow, preserving strict hierarchy/train for installations that require its attestations.

Development mode uses published source commits, aggregate manifests, and a durable ref-mutation journal. It does not require intermediate CI, reviewer/verifier ownership transfers, frozen playbook episodes, or historical receipt chains. Root validation is an explicit project command list or advisory/none policy; evidence names the actual SHA and command outcome. One publisher per repository serializes remote writes with PostgreSQL advisory exclusion and exact expected-head Git pushes. Crashes reconcile the observed remote before another publication. Conflicts park the affected source revision and permit independent work to continue. New commits or explicit retries reopen parked work.

Operator adoption records observed target SHA, source/task identities, reason and actual validation evidence. It never fabricates a hosted CI success. Cancellation preserves old refs and workspaces, releases only detached or provably stopped writers, and quarantines uncertain writes on their target. Task completion and delivered-to-main are separate facts. Organizational hierarchy does not prescribe a mutable delivery target in development mode.

Implementation checklist:
- [x] durable journal, development policy and commands
- [x] automatic parent aggregation/root publication with focused validation
- [x] worker close and planning integration; no duplicate legacy review/merge path
- [x] stopped-owner preservation and cancellation/adoption reconciliation
- [x] status, docs and focused regression/e2e validation
- [x] live rollout and remaining branch delivery, including task records
