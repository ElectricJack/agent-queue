# Required jobs and train settlement

Hosted candidate validation judges the frozen required check names, trusted
producer, repository, exact SHA and latest workflow attempt. A required job
that succeeds is green even while an unrequired job in the same workflow is
queued, cancelled or failed. Removed or waived names do not join that set.
Commit-status aggregates without contexts do not override check-run evidence.

Workflow identities and their actual aggregate conclusions remain evidence
metadata; their conclusions do not gate the required jobs. Canonical receipts
and attestations may therefore carry a null or unsuccessful workflow conclusion
while every attested check must still succeed. The hosted verifier rechecks each
required check run. A nonconclusive workflow requires job-to-attempt identity
verification, so an earlier successful attempt cannot satisfy a rerun. Preserve
job URLs in the exact-check cache without adding diagnostics to canonical bytes.

Before fresh source admission or policy supersession, a train revisits an open
ordinary batch whose complete frozen inputs are already proven on its target.
Use the existing whole-source Git proof, never candidate inclusion alone or a
mutable task status. The ordinary batch service and settlement path retain their
holds and identity checks. Partial conflict candidates and unknown proofs remain
unsettled. Epic synchronization and promotion intents keep their own
settlement protocols.

An epic refresh's single member is the frozen default head, and a refresh repair
publishes its resolved merge into the epic itself. Containment of that head is
therefore not the refresh's promotion. The train delivers a refresh only when the
epic is the default head by ancestry and an identical tree. Abort and eject of a
refresh refuse only that delivery or a promoted lifecycle. A refresh whose epic
already has the frozen default head as an ancestor, a resolved merge included,
can be settled by the supervisor control below, which never tests or publishes
the merge.

`aq integration settle-batch BATCH_ID` previews recovery of an ordinary batch
already delivered to its target. Apply requires the previewed target SHA and a
nonblank reason. It is an operator/project-supervisor control and performs no
target publication. Normal guarded post-delivery branch cleanup still runs.
Recheck the target, frozen membership and whole-source proofs under
the project hierarchy lock before normal batch settlement; refuse changed,
partial, unknown, held, sync or promotion evidence. Record the operator
and reason in an event. Settlement remains idempotent and retains normal cleanup.
