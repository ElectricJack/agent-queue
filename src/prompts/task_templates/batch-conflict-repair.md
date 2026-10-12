{{description}}

## Complete batch conflict repair

Batch: {{batch_id}}
Revision: {{revision}}
Partial head: {{partial_head_sha}}

{{manifest}}

{{merge_start}}manifest order in this workspace. Resolve all conflicts together, preserving the intended features. Retain every source head as an ancestor of the final head. You may edit any necessary repair file, including earlier features and migrations; re-chain colliding migration revisions and check alembic heads. Regenerate generated artifacts from resolved sources; never hand-merge them. Run focused checks, commit repairs, and record the complete first-parent range with git rev-list --first-parent --reverse PARTIAL_HEAD..HEAD.

Submit through aq integration resolve-candidate-member with --resolved-head-sha HEAD --resolved-tree-sha TREE and one --repair-commit-sha per first-parent commit. The frozen policy makes this a complete batch resolution; the daemon derives all authority and publishes under its fence. Do not push the integration branch or main yourself. Candidate CI must pass on the exact resulting SHA before promotion.