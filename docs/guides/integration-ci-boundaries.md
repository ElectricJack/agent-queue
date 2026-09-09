# CI at integration boundaries

Full GitHub CI runs automatically on pushes to `main`, `aq/integration/**`, and
`aq/parent/**`. Feature pushes and pull-request events do not launch the full
suite. The existing frozen parent `aq/sound-current` remains an explicit rollout
exception. Manual testing is available through `workflow_dispatch`:

```bash
gh workflow run tests.yml --ref YOUR_FEATURE_BRANCH
```

All three required suites remain enabled. Main reuses authenticated evidence
only for the exact tested candidate; otherwise it runs the full suite. A newer
push cancels obsolete CI on the same integration branch. The unused GitHub merge
queue trigger is removed; enabling GitHub's merge queue later requires restoring
that trigger.

AQ's parent verification poller publishes the assembled parent SHA to an
immutable `aq/parent/<task>/<operation-hash>/<generation>/<sha>` snapshot ref.
Only parents awaiting verification are selected. It does not rename the parent,
change its contents, or alter frozen child delivery targets. This also covers a
leaf that later becomes a parent, regardless of its original branch name.
Retries reuse an identical remote ref; a conflicting ref is never overwritten.
The normal authenticated observer records CI evidence against the exact parent
operation, generation and SHA, then emits the existing verification event.
Stale results cannot verify a newer parent generation. Snapshot refs are retained
for audit; this change does not delete them. If a parent CI run is canceled,
rerun it explicitly with `gh run rerun RUN_ID`; replaying an existing snapshot
does not push another ref or silently start replacement CI.

Existing task branch names remain valid; feature branches do not need renaming
to opt out of automatic CI. `aq/feature/<task>` is the naming convention for new
manually created feature branches. The CI boundary is the assembled snapshot,
not the spelling of a worker's existing branch.

## Rollout to existing branches

GitHub reads push workflow definitions from the pushed revision. Changing main
alone cannot retroactively change old feature branches. Merge the workflow
update from main into long-lived branches when their owners can safely do so.
Already-running feature workflows may be canceled without canceling the root
integration candidate or parent verification run. Until old branches receive
the update, their old workflow may still start a run.
