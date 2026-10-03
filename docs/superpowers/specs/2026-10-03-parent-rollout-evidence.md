# Parent rollout evidence

Task `amber-meadow-15.5`, implementing approved `rev-agile-ridge` revision 2,
SHA256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`.
The parent wiring contract is [parent reconciler wiring](2026-10-02-parent-reconciler-wiring.md).

Acceptance uses disposable PostgreSQL and a local bare Git origin. An existing
episode with eight children, a failed child, a real merge conflict and a committed
child receipt is observed in shadow, explicitly adopted and visited without
redrive/rebind commands. It reaches one named gate; restart retains that gate
and the receipt's original episode, source and target heads. The conservative
reviewed parent policy selects a human gate for failed children and conflicts.

Restart constructs fresh observers, policy caches, primitive adapters, command
dispatch and reconciler objects over persisted state. Prepared publication is
tested before and after the remote push with a fresh promotion service: read-back
settles the original intent and produces exactly one receipt. Interrupted writer
filing and failed-verifier recovery retain the existing identity and ordinal.
The existing `keen-stone-14` recovery remains authoritative.

Rollback is an exact-version, audited transfer to legacy. A stale version or
unresolved remote write refuses the transfer without changing ownership or
receipts. Stopping the active runtime alone retains reconciler ownership and
blocks legacy mutation. After an explicit transfer, fresh legacy services can
resume the same episode. A human hold survives restart and rollback.
Legacy entry and transactional readiness consult the preserved immutable gate
answer: an unanswered gate, expired projection or hold/reject/abort answer cannot
authorize mutation. A verified retry answer permits legacy compatibility.

The eight-child test records exact disposable subject, artifact, receipt, intent,
gate and transfer identities in its JUnit `rollout_evidence` property. The report
stores this specimen separately from production cutover receipts.

Focused checks cover parent reconciler scenarios. One affected area check covers
parent subjects, completion, cancelled collection recovery and parent CI. No
whole-suite run, operator database migration, daemon restart, production flag
change, policy activation or task adoption is performed by this worker.

The rollout report must distinguish disposable test evidence from production
evidence. This worker ships its code, tests and runbook without waiting for
production transfer; production gates remain explicitly pending, with both
flags OFF and no shadow week observed or waived. Production adoption requires
recorded root cutover, shadow comparison,
scenario results and human approval. The supervisor applies migration and engine
transfers and records their exact subject versions, artifact digests and receipts.
Legacy commands/data remain available until the P4 deletion counts prove safe;
adopted subjects must not depend on manual redrive/rebind to progress.
