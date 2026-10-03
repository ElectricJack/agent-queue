# Legacy development publisher recovery

Task `steady-stone-52` repairs recovery of already completed legacy work.

* A checkpoint whose only train binding is an episode, with no verification,
  verified checkpoint or parent operation anywhere in its history, uses ordinary
  Git completion provenance. A partial verified binding still fails closed.
  Containment requires retained exact provenance; an episode or branch tip alone
  never proves delivery.
* `aq integration migrate-provenance PROJECT --task-id TASK --source SHA
  --reason REASON --apply` may attest a completed generation with no database
  completion row. Its generation is the recorded current completion id, or a
  deterministic legacy identity fenced to the task version. Missing-row
  attestations also retain that version identity so archiving, which drops
  current-generation metadata and claim epochs, preserves the decision.
  No passing worker completion or CI result is fabricated. `--no-artifact`
  replaces `--source` for explicitly artifact-free work and requires a reason.
  Existing recorded source evidence cannot be replaced by either attestation.
  Apply rechecks task identity and excludes active writers under a row lock;
  the operator identity, reason and actual result are recorded in the audit log.
  Reopening creates a different generation, so old evidence cannot satisfy it.
* Invalid parent completion and parent provenance mismatch remain distinct
  publisher skip reasons, including dependency propagation and recovery advice.
* Legacy delivery adoption gets a 600-second CLI response budget.
* Doctor suppresses uncollected repairs when shared Git delivery truth proves
  containment or settlement. Missing or unavailable evidence remains reported.

Tests use disposable PostgreSQL and local Git remotes. Live recovery belongs to
the operator. No target branch is moved by migration or by this worker, and no
work is merged between `main` and `vg-vt-improvements`.
