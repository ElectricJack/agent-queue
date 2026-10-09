# Resume recovered work on its task branch

Task `fleet-ridge-45.1` supersedes the preservation destination in the owner
recovery and preserved repair designs. A task's existing branch remains its
identity across retry, reopen and reroute.

After proving a writer stopped, owner recovery snapshots dirty work using an
isolated index. If the resulting commit fast-forwards the existing origin task
branch (or creates an absent task branch), publish that exact commit there,
without force. Keep the checkout and index unchanged until the normal detach.
The release audit's `preserved_ref` may now name the task branch itself. Journal
the publication intent in the recovery audit before pushing, bound to the exact
owner fence, writer and workspace. Finalize it with the release transaction. On
restart or an ambiguous push response, re-prove the published objects before
releasing; already-published work must remain visible to the repair handoff.

Only a tip that diverges from origin uses a temporary
`aq/recovery/<owner-row-id>/<sha>` snapshot. Each incomparable tip has its own
ref; never manufacture a merge with one parent's tree to claim their contents
were combined. Workspace preparation consumes audited snapshots by a real
merge, publishes the result to the task branch without force, then deletes each
snapshot with its exact old-tip lease. A conflicting merge or changed snapshot
ref refuses preparation and retains all work. A crash after publication or
deletion is replayable by proving the audited commit is already on the task
branch. Existing `aq/preserved/` audits use the same consumption path.

Preparation retains the task branch's local and published progress, including
when its filing base or a prerequisite advances. Operator reroute, batch repair
rollover and completed parent-resolution reconciliation accept the audited
task ref or a temporary/legacy snapshot, preserving their existing stop, fence,
budget, lineage and receipt checks. Publishing recovered work alone supplies no
verification receipt or task completion.

Regression checks cover committed and dirty fast-forward recovery, same-branch
retry, real divergent merge and snapshot deletion, conflicting merges, changed
refs, and replay after publication/deletion. Run focused recovery/workspace
tests, their related area checks, and the swarm smoke test for workspace changes.
