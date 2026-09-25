# CI at integration boundaries

Full GitHub CI runs automatically on pull requests into `main` and on pushes to
`aq/integration/**` and `aq/parent/**`. A push to `main` does not launch it, and
neither does a push to a feature branch that has no PR. Manual testing is
available through `workflow_dispatch`:

```bash
gh workflow run tests.yml --ref YOUR_FEATURE_BRANCH
```

All four required suites remain enabled. `main` only receives work that was
tested before it landed: a PR's own run, an integration candidate that the train
promotes by its exact tested SHA, or the development publisher's pre-publish
validation. A promoted candidate keeps its integration run's check runs on the
commit, so `main`'s head still shows them. The per-commit `main` run this
replaced only repeated that work: `main` ran the suite 66 times between
2026-09-22 and 2026-09-24, about 13 minutes each across four arms.

A newer push cancels obsolete CI on the same PR or integration branch. A draft
PR runs once it is marked ready for review. A same-repository PR from an
`aq/integration/**` branch skips its own run, because the push to that branch
already tests the same head. The unused GitHub merge queue trigger is removed;
enabling GitHub's merge queue later requires restoring that trigger.

The [CI main sentinel](../../src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md)
still judges `main`'s head from the check runs on that commit. A head that is
not an exact tested candidate has none of its own and reads as `pending`, so the
sentinel waits rather than filing a repair.

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

A `pull_request` run reads the workflow from the PR's merge with `main`, so a
PR from an old branch follows this policy as soon as it is on `main`, unless the
branch itself edits `tests.yml`.
