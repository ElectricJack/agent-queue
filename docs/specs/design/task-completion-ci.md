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

Resolve the task's own repository, local branch commit, and published branch
commit. All must agree with any explicit close commit. Read authenticated check
runs for that SHA, reject foreign heads/producers, and check branch identity
again after observation. A changed head invalidates the result. Missing, pending,
cancelled, red, or unreadable checks refuse close and retain the worker's claim.
Failing closes and obsolete retirement remain available. Managed publishers keep
their existing exact-candidate validation; completion is still distinct from
delivery to the default branch.

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
