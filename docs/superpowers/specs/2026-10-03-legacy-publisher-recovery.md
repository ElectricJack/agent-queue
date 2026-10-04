# Legacy development publisher recovery

Task `steady-stone-52` repairs recovery of already completed legacy work.
Follow-up `grand-rapids-78` found two more shapes it did not cover and is
recorded in the same terms below.

* A checkpoint whose only train binding is an episode, with no verification,
  verified checkpoint, live parent operation or parent adoption anywhere in its
  history, uses ordinary Git completion provenance. A cancelled parent operation
  that never verified or completed never bound anything, so its episode is as
  bare as a pre-train one; any verification or completion record, and any
  operator adoption even a stale one, still fails closed. A partial verified
  binding still fails closed. Containment requires retained exact provenance; an
  episode or branch tip alone never proves delivery.
* `aq integration migrate-provenance PROJECT --task-id TASK --source SHA
  --reason REASON --apply` may attest a completed generation with no database
  completion row. Its generation is the recorded current completion id, or a
  persisted legacy identity fenced to the completed incarnation. Missing-row
  attestations also retain that legacy identity so archiving, which drops
  current-generation metadata and claim epochs, preserves the decision.
  No passing worker completion or CI result is fabricated. `--no-artifact`
  replaces `--source` for explicitly artifact-free work and requires a reason.
  Existing recorded source evidence cannot be replaced by either attestation.
  Apply rechecks task identity and excludes active writers under a row lock;
  the operator identity, reason and actual result are recorded in the audit log.
  Reopening creates a different generation, so old evidence cannot satisfy it.
  `tasks.legacy_completion_id` rotates only on entering or leaving COMPLETED;
  comments, findings, description edits and same-status writes preserve it.
  Archive copies it into `archived_tasks.legacy_completion_id`. Revision
  `a00000000062` preserves the former timestamp-derived locator and recovers
  successful live operator attestations from their audit results only when
  they postdate the task's last reopen fence. It never copies a delivery answer
  or fabricates Git evidence. Archived rows retain their former exact locator;
  ambiguous historical decisions still require explicit operator resolution.
* A COMPLETED task whose latest completion is its current generation but did not
  pass is attested on a `--reason` too, fenced to that generation. Such a
  generation recorded what it read, not an artifact of its own, so
  `--no-artifact` replaces those commits and `--source` may name another commit,
  which must already be contained in the target: the control can only clear
  work the target holds. The failed close stays as recorded, and the audit names
  its outcome.
* Invalid parent completion and parent provenance mismatch remain distinct
  publisher skip reasons, including dependency propagation and recovery advice.
* Legacy delivery adoption gets a 600-second CLI response budget.
* Doctor suppresses uncollected repairs when shared Git delivery truth proves
  containment or settlement. Missing or unavailable evidence remains reported.

Tests use disposable PostgreSQL and local Git remotes. Live recovery belongs to
the operator. No target branch is moved by migration or by this worker, and no
work is merged between `main` and `vg-vt-improvements`.
