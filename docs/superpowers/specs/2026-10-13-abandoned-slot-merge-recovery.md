# Abandoned slot merge recovery

Task: `crisp-nexus-83`.

During claim preparation, recover an interrupted merge in the acquired managed
slot before checkpoint restoration or branch-origin preparation. Also recover a
stale slot holding the requested branch before detaching it. The latter must be
unlocked, without a live session, retained branch owner, job pin or foreign claim.
An acquired slot may have only the incoming task's preparing session; active or
foreign holders refuse recovery without changing the index or working tree.
Audited operator handoff retains its strict dirty-destination refusal; it does
not recover a merge in the destination before validating that destination.

Before `git merge --abort`, archive a binary diagnostic patch of the working tree
against HEAD, including conflict markers, HEAD/MERGE_HEAD and unmerged index
entries, as `task_context(type=worktree_salvage)`. Attribute it to the sentinel's
previous task, or to the incoming task when unknown. Capture without staging the
live index. Respect the configured salvage size limit, recording an explicit
diffstat-only diagnostic when exceeded. This recovery diagnostic is required even
when ordinary dirty salvage is disabled. A failed archive or abort preserves the
merge for investigation and returns a preparation error; successful recovery
continues preparation in the same attempt.

Regression coverage uses real temporary Git repositories for staged merges,
conflicted merges, checkpoint restoration and stale branch holders, and proves
live/locked/retained/foreign-claimed slots and failed archives remain unchanged.
