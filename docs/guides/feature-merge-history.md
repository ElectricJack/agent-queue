# Feature history and integration merges

Leaf feature work is squashed on the feature branch before its first review.
The worker preserves the final tree and attribution, pushes the final commit,
and submits that exact SHA for review. Published task branches may be rewritten
only with an explicit expected-SHA force-with-lease while owned by that worker.
Reviewed or delivered commits are not rewritten. Follow-up changes need a new
review; parent branches retain their child merge history.

The integration builder computes a three-way tree merge using the recorded
source base and writes a merge commit: first parent is the current candidate,
second parent is the exact reviewed feature tip. Clean child-to-parent delivery
uses the same parent order. This is the ancestry equivalent of `git merge --no-ff`.
Each feature tip becomes reachable from integration, and from main after the
exact CI-tested candidate is promoted. First-parent history shows one merge per
feature, while the complete graph retains reviewed source history.

Existing reviewed branches are merged as-is. They are not retroactively
squashed, because that would invalidate review identity. Existing single-parent
candidates must be superseded and rebuilt with fresh CI before promotion; old
candidate objects and evidence remain retained for audit. Already-published main
history is never rewritten.

When main advances across an accepted candidate repair and the ancestry-preserving
merge conflicts, the current bounded root-repair stage owns the resolution. AQ
freezes the exact candidate and new-main commits in the stage dossier, hands the
integration branch to that stage's current delegate, and accepts only a two-parent
merge of those commits. The accepted merge becomes a new candidate revision whose
construction base is the frozen new-main commit; prior CI never carries forward,
so that exact revision must pass the configured root checks before promotion. A
later main movement causes another bounded rebuild rather than rewriting either
reviewed history.

Database fields named `squash_sha` and `generated_squash_sha` retain their names
for compatibility; new clean deliveries store the merge commit SHA there.
Conflict-repair evidence and ownership fences continue to control publication.

For a child conflict, the resolution may retain the exact reviewed child tip
as the second parent of one merge commit. Its first-parent chain must start at
the expected parent head; any following repair commits stay on that chain.
Unrelated side branches, additional merge parents, and reversed parent order
are rejected. The complete resolution tree and commit range remain bound to
the persisted repair evidence before publication.
