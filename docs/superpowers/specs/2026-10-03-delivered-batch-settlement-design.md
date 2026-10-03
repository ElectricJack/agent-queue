# Externally delivered batch settlement

Recover a legacy train stuck in testing after its completed repair delegate made
no changes and its candidate reached the default branch outside AQ promotion.
Normal release correctly requires exact CI and promotion receipts; abort only
accepts human-required operations. Neither supplies this recovery.

`aq integration settle-delivered-batch BATCH_ID` is an operator/supervisor
preview. `--apply --candidate SHA --target-head SHA --snapshot DIGEST --reason TEXT`
repeats all proof under repository publisher exclusion and the project lock.
The digest binds the batch, current revision, complete members/results, stages,
completion records, repository identity, request, lease and branch owners.
Changed durable identity or a moved remote default head refuses the request.

Require a built current candidate, complete applied member results, and Git
ancestry for the candidate, construction base, every frozen source base/head,
and every generated member commit. Validate source trees and result inputs.
The current repair must have a passing terminal completion; earlier delegates
must be terminal and detached. Every source and delegate must have no live
session, assigned agent or locked checkout. An empty commit list is legitimate
no-op evidence; it is never CI evidence. Any recorded repair commits must also
be contained in the observed target. Refuse human-required operations, manual
holds, open applicable human gates, reconciler ownership, attached writers,
unfinished publications, ref mutations, resolutions, attestations, promotions,
and irreversible cleanup markers. Missing evidence refuses rather than infers.

Apply atomically cancels the operation and unfinished stages, retires the batch
as `aborted`, records the authenticated target and proof in the current stage
dossier and a durable audit event, releases only detached owned reservations,
and uses the existing stale-request release to consume its exact request/lease
and preserve coalesced catch-up work. Preserve every source, receipt, validation
failure, policy pin, attempt and deadline. Do not synthesize success evidence,
mark a revision green/promoted, rewrite refs, or clean up retained sources.

Replay of the same applied candidate/target/snapshot returns `already_settled`
from the durable audit without touching a newer request. A different identity
refuses. Operator owns deployment and live application.

Regression checks cover completed no-op repair, candidate delivered under a
later target commit, complete membership, missing ancestry, changed candidate,
moved target, state changes between proof and apply, human gates, live writers,
unresolved writes, atomic release/catch-up preservation, and repeated requests.

## Incident proof and operator workflow

The operator's saved snapshot at
`~/.agent-queue/operator-checks/batch16d-recovery-snapshot-20261003.json`
has a matching canonical membership digest. Read-only checks against retained
Git objects prove all these ancestry edges (each `git merge-base --is-ancestor`
returned 0):

- Candidate `a63ee7feda627bc1bc0df72f2a36e59e5d0c4e92` to recorded main
  `2e54d186600a88d44ad734997ee6ab9f732bf793`.
- Frozen reviewed source `21183f62f4b5099e5155458a1ecee1626b2374f8` to candidate.
- Current generated member `e7b745d1e5a5fd218b84a7bf4033088ddfd17737` to candidate.
- Sealed base `00c4a38272a2e3d78a73508e6415b0abcdc254dd` to candidate.

The reviewed source tree is exactly
`550375a4c9b6bac9c08797bc3196a0032b46dcc3`. The snapshot's current stage is
expired, which settlement preserves as historical evidence. These are historical
Git checks, not a passing live preview: task completions, writers, holds, journals
and remote head must still pass the command's fresh checks.

After integrating and deploying this change, the operator runs:

```bash
aq integration settle-delivered-batch integration-batch-16d384210eebdfd6b20901e06440ca41
```

Inspect `would_settle`, candidate/target SHAs, member count, validation label and
snapshot digest. Run its exact returned `apply_command`, with the operator's
reason. A changed head or snapshot requires a new preview. A blocker requires
supported recovery of its specific evidence; do not manufacture a digest, CI
receipt or database update. Inspect integration status afterwards to verify the
old request is released and any coalesced follow-up remains scheduled. Repeating
the same applied fences must return `already_settled`.
