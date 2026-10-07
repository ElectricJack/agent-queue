# Generated artifacts in train merges

Task: `prime-vault-27`.

Parallel branches regenerate `tests/selection_catalogue.json` independently.
The catalogue is already marked `merge=aq-generated`, but child-to-parent
promotion and accepted CI repair rebuilds onto newer main use plain
`merge-tree` and escalate those overlaps as source conflicts.

Train merges must use the exact requested merge base and merged source tree.
When both sides change an artifact marked `merge=aq-generated` in that tree,
regenerate it in an isolated scratch worktree, including when Git reports a
clean text merge. For generated-only conflicts, restore the current side's
artifact in the scratch tree before regeneration. Missing current-side files
are restored as deletions. Mixed or source-only conflicts remain conflicts.

The shared mechanism belongs in `src/integration/regeneration.py`. Candidate
member construction, child promotion and accepted-CI-repair main rebuilds use
it. The development publisher retains its existing configured regeneration
policy. The regenerator defaults to `scripts/regenerate-generated.sh`, runs
with the existing timeout and database refusal environment, and may change
only paths marked generated. Failure retains the caller's conflict evidence
and existing repair route. Scratch worktrees are removed on every outcome.

Regeneration constructs a tree; it grants no publication authority. Existing
source review, branch fences, exact remote leases, ancestry, receipts and CI
checks still apply to the resulting commit. Both merge parents are retained.

Claim-time prerequisite stacks use this same mechanism. Generated-only overlaps
are rebuilt from all merged sources and recorded in the regeneration log.
A source conflict is `stack_prerequisites_conflict`, not a slot-reset failure.
Reserve one ordinary isolated stack-repair branch from the dependent's published
tip (or its proven parent base), with exact prerequisite IDs, refs, OIDs and
conflicting paths in the brief. The branch materialization scanner publishes
the reservation. A named claim-admission predicate keeps the dependent READY
but unclaimable until that repair has a passing completion; repeated
preparation cannot duplicate it. This is preparation state, so it does not
add a delivery dependency on the isolated repair branch.

After a passing repair completion, observe its exact published head and prove
that it preserves the repair starting point before using it as the new merge
base. Merge every freshly proven prerequisite and the current parent onto that
head, so moved prerequisites cannot be silently omitted. Only a successful
stack and claim activation clear the conflict diagnostic. Preserve the resolved
overlay as a merge base for later preparations of the same dependent. These admission waits
do not consume slot-reset retries or quarantine the pool worker. The existing
single-prerequisite fast path and immutable filing origin remain unchanged.

Verification includes two branches regenerating a real selection catalogue
from different test modules, canonical combined output, preserved source
files and merge parents, replay, generated-only main rebuild, source/mixed
conflict refusal, failed regeneration, and rejection of non-generated writes.
