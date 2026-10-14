# Required CI at task completion

Ordinary pull-request tasks without a managed hierarchy, train, or development
publisher may complete successfully only when the project's required hosted
checks are green on their exact published source commit. This closes the gap
that allowed clear-crest-48.4.14 to complete with its Windows build failing.

Use the project's `hierarchical_integration_policy.root.required_checks` when
present, including its producer and version. Otherwise use
`integration.merge_required_checks`; with `merge_ci_policy: required` and no
names, judge every reported check. An unconfigured project has no new completion
CI requirement. An invalid configured policy is unavailable evidence, never an
implicit waiver. Merge `warn`/`off` does not waive explicitly declared checks.

New project policy writes must use canonical positive decimal GitHub App IDs
for both parent and root required-check producers (GitHub Actions is `15368`).
Reject slugs, signs, leading zeroes, numbers and booleans before activation;
existing frozen operation snapshots remain readable. The read-only doctor check
`integration.ci_producer` reports stored invalid identities with project and
boundary, including policies with no `ci` block and disabled projects.

Resolve the task's own repository, local branch commit, and published branch
commit. All must agree with any explicit close commit. Read authenticated check
runs for that SHA, reject foreign heads/producers, and check branch identity
again after observation. A changed head invalidates the result. Missing, pending,
cancelled, red, or unreadable checks refuse close and retain the worker's claim.
Failing closes and obsolete retirement remain available. Managed publishers keep
their existing exact-candidate validation; completion is still distinct from
delivery to the default branch.

A clean checkout on the assigned task branch whose commit equals the
authenticated repository's default-branch commit has no source changes to
trigger CI. Recheck branch, cleanliness, source commit and default commit before
accepting this exemption; unknown or changed evidence does not waive CI. Use the
task's bound repository and its default branch when present. Record the exact
source/base equality as completion CI evidence so later audits do not require
source checks for that accepted unchanged commit. Task type and no-op intent
alone never waive checks; documentation commits still require configured CI.

Review-only deliverables (including implicit research/design reviews), read-only
roles and no-code closes must not auto-commit or publish dirty checkout drafts,
even on error or skip-verification paths. Preserve the files and retain the claim
with actionable feedback: submit the review to the vault, move the local draft
outside the source checkout, and retry. The clean unchanged checkout then closes
as no work for the source repository, without requiring a source PR or CI.
Committed source changes and tasks declaring source deliverables still follow
the normal delivery rules. Spec-ingest/database-only roles use the same unchanged
source proof without needing git-push or CI-audit capabilities.

Check before close side effects and again after completion-pipeline remediation.
Record the verified source SHA in the accepted completion record, so a branch
move cannot turn a previous green observation into evidence for a different SHA.
Update the close's identity-bound recovery draft before the terminal transition;
a restart must recover that same verified SHA.

`aq ci completed-status --project-id PROJECT` is a supervisor/operator read.
For each COMPLETED task with a completion CI requirement, observe its latest
accepted completion commit (legacy work-commit metadata is a fallback). Never
substitute a branch's current head for a missing recorded commit. Return red
tasks separately from pending/unavailable observations; include commit, check
names and links. This audit mutates no task and does not assert default-branch
delivery. Ordinary workers cannot invoke it, and project supervisor scope stays
pinned. Cache duplicate repository/SHA/policy observations within a single audit.
