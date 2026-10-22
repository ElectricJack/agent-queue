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

An ordinary open epic batch also settles when every exact frozen source is
proven on the designated repository's current default branch, even if the epic
ref has disappeared after a hand landing. Recheck the repository binding,
frozen membership, operator holds and default head under the project and batch
locks. Paused batches, partial or unknown proofs, epic sync/refresh batches and
promotion intents retain their existing protocols. Record the observed default
ref and SHA without publishing or recreating the epic. Cleanup may use that
default branch only after independently proving all frozen inputs there; its
usual branch ownership, reachability and exact-tip guards still apply.

When an ordinary root or development candidate fails only checks proven to
fail on its frozen target, the train may replace its batch to admit a pending
baseline fix. Require trusted green checks on the exact additional source,
including every pre-existing failing name, and normal source admission. Freeze
the original exact members together with the additional admitted sources in
one atomic replacement. Retain the old batch, checks and membership as history,
carry its repair attempt count, and record the replacement and reason. Never
replace paused or held batches, outstanding repairs, or candidates with new or
unproven failures. A missing or red fix leaves the bounded suite retry intact.
The replacement must pass its own candidate checks and publication gates.

For batches previously aborted by hand, the owning project supervisor may use
`aq integration release-withheld-source BATCH_ID --task TASK_ID` to preview recovery
and apply with the exact source SHA, target SHA and a nonblank reason. This
retains the existing hold, writer, completion, repair and repair-budget guards;
it neither changes abort history nor publishes a target.

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
partial, unknown, held or promotion evidence. Record the operator
and reason in an event. Settlement remains idempotent and retains normal cleanup.

The explicit settlement control also recovers a legacy epic-sync batch when
every exact frozen source is proven on the designated repository's current
default branch. Containment in the epic alone remains insufficient. Preview
reports the default `target_ref` and observed SHA; apply requires that SHA
and rechecks repository binding, frozen membership, intent, holds and Git
freshness under the project and batch locks. Paused, aborted, partial and unknown
inputs refuse settlement. Retain all membership, baseline, check, attempt and
prior promotion history; record observed project delivery without publishing an
epic or emitting its promotion-policy events. A promoted sync can replay the
control to resume guarded cleanup, including after the epic ref disappeared.
Cleanup independently proves all frozen inputs on the default branch before
using it, retaining the usual owner, reachability and exact-tip guards.

Train status reports `repair_open` only for the batch's current allocated
ordinary repair. An unfinished older delegate remains historical evidence when
a successor exists; its task status, holds and budgets are unchanged.
