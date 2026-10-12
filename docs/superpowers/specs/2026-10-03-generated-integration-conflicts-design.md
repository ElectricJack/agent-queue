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

The scratch subprocess environment is an allowlist with worker database
refusal sentinels. Its tool lookup order is the daemon interpreter's directory,
the daemon user's standard `~/.local/bin` installation directory, then
`/usr/local/bin`, `/usr/bin` and `/bin` (duplicates removed). This supports a
system-Python daemon with user-installed `openapi-python-client` and `ruff`
without inheriting ambient PATH, database URLs or credentials. The canonical
scripts retain their exact generator version and required-tool checks: a
missing tool or wrong version fails regeneration, and no installation or
editable-install mutation is performed.

Regeneration constructs a tree; it grants no publication authority. Existing
source review, branch fences, exact remote leases, ancestry, receipts and CI
checks still apply to the resulting commit. Both merge parents are retained.

Claim-time prerequisite stacks use this same mechanism. Generated-only overlaps
are rebuilt from all merged sources. The stack snapshot records prerequisite
proofs and each regeneration's files, input trees and resulting merge commit.
A source conflict is `stack_prerequisites_conflict`, not a slot-reset failure.
Reserve one ordinary isolated stack-repair branch from the dependent's published
tip (or its proven parent base), with exact prerequisite IDs, refs, OIDs and
conflicting paths in the brief. The branch materialization scanner publishes
the reservation. A named claim-admission predicate keeps the dependent READY
but unclaimable until that repair has a passing completion; repeated
preparation cannot duplicate it. This is preparation state, so it does not
add a delivery dependency on the isolated repair branch.
Repairs inherit the dependent's intelligence-class hint and routing preference;
the ordinary router still assigns them. Generator failures and timeouts, including
failures after a clean text merge, use the same named blocker and repair route.

After a passing repair completion, observe its exact published head and prove
that it preserves the repair starting point before using it as the new merge
base. Merge every freshly proven prerequisite and the current parent onto that
head, so moved prerequisites cannot be silently omitted. Only a successful
stack and claim activation clear the conflict diagnostic. Preserve the resolved
overlay as a merge base for later preparations of the same dependent. These admission waits
do not consume slot-reset retries or quarantine the pool worker. The existing
single-prerequisite fast path and immutable filing origin remain unchanged.
An unusable passing repair (empty/no-new commits, missing or moved published
ref, or unrelated ancestry) reserves a fresh repair from the dependent's current
published head. That reservation closes admission again. Every conflict release
also applies the normal preparation backoff, including races with a new repair
epoch/completion, so other ready work remains claimable without consuming the
slot-reset failure budget.

Task `keen-orbit-59` extends preparation to the second boundary: the existing
child versus its exact parent or prerequisite overlay. Construct the merge in
the observer store before attaching a worker. Retain clean local child commits
as well as the published child, and pin every frozen input. Generated-only
overlaps use the canonical regenerator there; source conflicts reserve one
ordinary repair with the original child, parent and overlay OIDs and paths.
The repair checkout fetches those retained objects and starts at its reserved
origin without automatically merging its conflicting parent. Admission proves
the repair's exact passing published tip descends from every frozen input,
then merges any newly proven parent/prerequisite before activating the child.

The lower Git preparation boundary aborts an unsuccessful merge and emits a
named conflict only after verifying the original HEAD and a clean index. Both
pool and push preparation route that diagnostic through the same reservation.
A final proof check prevents activation when the parent, child, prerequisite
identity or repair ref moved during preparation. An unactivated pool checkout
may detach with a clean exact HEAD pinned in the retained observer store;
unsaved work, missing retention, unrelated branches or stale attachment identity
withhold release. Normal writer handoff still requires published work. These
waits preserve review feedback, filing origin, fences and recovery budgets.

Verification includes two branches regenerating a real selection catalogue
from different test modules, canonical combined output, preserved source
files and merge parents, replay, generated-only main rebuild, source/mixed
conflict refusal, failed regeneration, and rejection of non-generated writes.
