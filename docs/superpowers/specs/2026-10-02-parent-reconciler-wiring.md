# Parent reconciler wiring

Task `amber-meadow-15.4`, approved rev-agile-ridge revision 2 phase 2.
The completed root loop/policy prerequisite is `f0dd683dd`; parent adapters,
writers and record/gate primitives retain their existing contracts.

Parent visits share the root reconciler and its single service remote pass.
They load the exact artifact pinned to the episode, never a new activation.
Subject creation occurs inside the command-owned checkpoint transaction and
defaults to legacy. Existing episodes are seeded in bounded pages only when a
reviewed artifact contains a parent table. Both feature flags remain off.

Parent authority is per parent task, durable in its subject rows. Shared
PostgreSQL session locks cover actions and remote read-back; an exclusive
transaction lock covers an audited, exact-version engine transfer. Legacy
collection, readiness projection, CI, repair deadline/dispatch and intent
reconciliation must consult that authority. Feature-off does not silently
reassign an active subject; explicit transfer to legacy supplies rollback.

The parent table chooses collection, verification, writer filing/lease/stop,
CI, and completion. Mechanism adapters retain existing promotion intents,
expected-old writes and receipt proof. A receipt can advance the aggregate
before the checkpoint: a bounded refresh visit adopts the separately observed
aggregate head and generation with the subject CAS before further action.
Failed aggregate verification invokes the existing keen-stone-14 recovery,
preserving episode, historical receipts, completion and failed head marker.
The unchanged failed head cannot file another verifier.

Unified verifier filing links the existing operation to the filed task before
leasing and waking it. Replay can finish a link after an interrupted filing.
Collector reservations are recognized by their bound operation identity.
The writer ordinal ledger survives subsequent failed verifier recoveries.
Legacy check recording retains the stage observed at guarded entry; a deadline
that wins before its commit cannot redirect old evidence into a successor stage.
Existing legacy repair stages are exposed as a dossier and reach one explicit
human gate rather than advancing an unchanged-head ladder. Failed children and
conflicts also reach explicit gates in the shipped conservative parent table.
Repair/ejection and record ports without a server-owned proof adapter refuse
with an unknown outcome; the conservative table selects a gate. Holds and
rejected reviews remain binding; every unknown/refusal retains a bounded visit.

No recovery command is deleted in this phase. Production shadow comparison,
eight-child scenario evidence, approval, operator migration and cutover belong
to `amber-meadow-15.5`. Worker tests do not substitute for those gates.

Operator handoff uses the existing engine-transfer command with
`--parent-task-id <id>`. Preview first, then supply the preview's exact subject
versions and the reviewed shadow/scenario/approval evidence for active cutover.
Rollback uses the same parent selector and `--engine legacy`; unresolved remote
writes refuse either direction until their existing read-back proves the result.
Disabling the feature flag alone retains durable ownership. No operator database,
flag, activation or engine transfer is changed by this implementation task.

Subject gates are resolved through the existing `aq gate resolve` surface with
`--resolution retry|hold`. The server verifies the local human principal and
records its identity in the immutable gate-answer journal. Agent/service callers
cannot supply human identity. A hold remains binding when the underlying failure
disappears. Capacity waits do not count as failed repair attempts; an expired
leased writer reaches an explicit gate rather than another stage.
