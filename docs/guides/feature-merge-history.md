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

Database fields named `squash_sha` and `generated_squash_sha` retain their names
for compatibility; new clean deliveries store the merge commit SHA there.
Conflict-repair evidence and ownership fences continue to control publication.

For a child conflict, the resolution may retain the exact reviewed child tip
as the second parent of one merge commit. Its first-parent chain must start at
the expected parent head; any following repair commits stay on that chain.
Unrelated side branches, additional merge parents, and reversed parent order
are rejected. The complete resolution tree and commit range remain bound to
the persisted repair evidence before publication.
