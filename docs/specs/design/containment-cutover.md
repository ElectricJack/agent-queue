# Containment cutover preparation

Implements fast-smooth-integration revision 2 §§11.1–11.2 for
`stark-willow-95.2`. The approved spec in the project vault remains governing.

`aq integration cutover-containment --project ID` previews a repository-scoped
manifest. `--choices FILE` supplies per-completion choices; a new preview binds
them into the digest. Apply requires `--plan FILE --digest SHA256 --apply`.
Only the local operator can preview or apply. The command never activates the
fast flow, resumes writers, changes delivery targets, or changes deployment.
Stage 1 prepares history; consumer/publisher activation is a later rollout gate.

The manifest binds configuration generation, project/repository settings,
runtime integration/deploy settings, claims, preparations, branch owners,
operations, frozen batches, origins and stacks, retained private refs, workspace
state, branch protection, CI trust, and strict/legacy observations. Git reads use
a disposable bare store. Preview performs no database writes, ref publication,
worktree changes or repairs. Every observation carries its collector timestamp.
The digest omits only collection timestamps/tokens, so an identical fresh
observation can revalidate the plan.

Apply requires a paused project with integration disabled, no live writers or
uncertain remote publications, and an unchanged manifest. Root publisher
exclusion plus bounded database locks fence the final inventory and atomic
ledger write. Original objects are retained under immutable private refs before
the ledger commits. A failed database recheck may leave additional retention
refs, never changed target heads or a partially applied disposition. Retry is
idempotent for the same committed digest. A different plan after application is
refused: later equivalent-only history is ordinary pending work.

An equivalent-only completion requires either `delivered_by_other_means` with
the exact original source, a reachable content-carrying commit, the verified
legacy reason and operator evidence, or `reconstruct`. Reconstruction is a
visible unresolved blocker until a separately reviewed normal merge establishes
ancestry and a new preview observes it. Missing/ambiguous original sources block
apply. Nothing fabricates a receipt or uses an automatic ours merge.

The insert-once audit uses the existing `system_config` table, with one key per
project/repository: `integration.containment-cutover.v1:` plus the SHA-256 of the
exact identity pair. It introduces no table or migration. Values are bounded to
16 MiB and 10,000 completion generations, strictly validated against the manifest
digest, identities, configuration generation, retained source and disposition
evidence. Duplicate JSON fields, malformed values and namespace confusion fail
closed. There is no update/delete or progress/replay-journal port; conflicting
content or later generations are refused. The ordinary YAML config editor never
reads or writes this table; metrics and provider probes have disjoint keyspaces.
The audit preserves the full manifest, prior configuration, operator, plan digest
and dispositions across archive/reopen. Lifecycle consumers call
`DeliveryAnswer.lifecycle_exclusion` for an exact project/repository/generation/target/source
disposition. Later consumer activation uses it for telemetry exclusions and to
withhold watchdog/admission redrive; this preparation does not activate consumers.
`delivered()` continues to return the actual ancestry result. These are separate
contracts; retirement cannot manufacture containment.

Rollback keeps this ledger and every source/candidate ref. Pause writes, retain
new heads, and restore only a compatible configuration/publisher that understands
strict containment. Never reset targets or restore receipt authority. If a binary
cannot read the new operations, remain paused and fix forward. Destructive receipt
table removal, live activation, database migration and restart are outside this
implementation task.
