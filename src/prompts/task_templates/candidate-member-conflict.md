{{description}}

## Candidate member conflict

Batch: {{batch_id}}
Candidate revision: {{revision}}
Member ordinal: {{ordinal}}
Partial head: {{partial_head_sha}}
Member source base: {{source_base_sha}}
Member reviewed head: {{source_head_sha}}

{{member_start}}Resolve only this member's conflict, commit the repair as a linear non-merge range, and do not push the integration branch yourself. Record `git rev-parse HEAD`, `git rev-parse HEAD^{tree}`, and each commit from `git rev-list --reverse PARTIAL_HEAD..HEAD`, then run:

    aq integration resolve-candidate-member \
      --resolved-head-sha RESOLVED_HEAD_SHA \
      --resolved-tree-sha RESOLVED_TREE_SHA \
      --repair-commit-sha REPAIR_COMMIT_SHA

Repeat `--repair-commit-sha` in oldest-to-newest order for every repair commit. The command derives the batch, member, operation, partial head, claim, workspace, and branch fence from this authenticated assignment; never supply or push a replacement lineage by hand.