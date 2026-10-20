# Editing generated task instructions

AQ renders generated worker instructions from `<data_dir>/vault/templates/tasks/`.
The default `data_dir` is `~/.agent-queue`. Initialization installs missing files;
operator edits survive upgrades. Source defaults are in `src/prompts/task_templates/`.

Edit an installed file with the supervisor's granted file tools or an operator editor.
For example, change `source-ci-reopen.md`, preserving the fact placeholders you want
displayed. The next in-place repair uses the edit immediately; no deployment, restart,
or profile reload is needed. Existing task descriptions remain snapshots.
`root-pr-check-repair.md` can display the observed source SHA, base SHA, PR URL,
policy generation, and failed checks with their test IDs.
Historical `source-ci-repair.md` delegate instructions remain installed; new
source-CI recovery uses `source-ci-reopen.md` without overwriting the old file.

Templates are plain Markdown. `{{task_id}}` substitutes the corresponding fact.
Only the variables listed below are available. Facts are inserted verbatim once,
so braces in test output are preserved. There are no conditionals or executable
expressions. Unknown or malformed placeholders and unreadable edits raise errors;
a missing installed file uses the shipped default. Empty templates are allowed.
Optional sections are selected by code and rendered separately, so wording cannot
change repair budgets, branch authority, or required acceptance gates. The factual
`Development repair generation:` prefix remains outside the template because
retry accounting reads it.

The supervisor and local operator can access this directory through existing file
commands. Worker and playbook principals gain no extra file access. Escaping
symlinks and other vault paths are excluded from this access exception.

## Generated instruction inventory

The inventory covers daemon-authored task descriptions, handoffs, and repair/reopen
feedback in `src/`. Section templates preserve leading/trailing whitespace from
composed descriptions. Variables listed here are available even if the default
wording does not display all of them.

| Template (`.md`) | Producer | Variables |
| --- | --- | --- |
| `adversarial-review-clean-room` | `src/commands/review_commands.py` | None |
| `adversarial-review` | `src/commands/review_commands.py` | `current_kind`, `current_title`, `review_id`, `revision`, `show_command` |
| `aggregate-verifier` | `src/integration/records.py` | None |
| `batch-conflict-repair` | `src/integration/repair.py` | `batch_id`, `description`, `manifest`, `merge_start`, `partial_head_sha`, `revision` |
| `batch-conflict-resume` | `src/integration/repair.py` | None |
| `batch-conflict-start` | `src/integration/repair.py` | None |
| `candidate-baseline-owned` | `src/integration/candidate_baseline.py` | None |
| `candidate-baseline-pre-existing` | `src/integration/candidate_baseline.py` | None |
| `candidate-baseline-unavailable` | `src/integration/candidate_baseline.py` | None |
| `candidate-baseline-unproven` | `src/integration/candidate_baseline.py` | None |
| `candidate-ci-awaiting` | `src/integration/repair.py` | `cause`, `identity` |
| `candidate-ci-green` | `src/integration/repair.py` | `identity` |
| `candidate-ci-identity` | `src/integration/repair.py` | `batch_id`, `candidate_line`, `construction_base_sha`, `revision`, `run_line`, `state` |
| `candidate-ci-no-failure` | `src/integration/repair.py` | None |
| `candidate-ci-unobserved` | `src/integration/repair.py` | None |
| `candidate-ci-unrecorded-failure` | `src/integration/repair.py` | None |
| `candidate-member-conflict` | `src/integration/repair.py` | `batch_id`, `description`, `member_start`, `ordinal`, `partial_head_sha`, `revision`, `source_base_sha`, `source_head_sha` |
| `candidate-not-built` | `src/integration/repair.py` | None |
| `ci-repair-accept-checks` | `src/commands/ci_commands.py` | None |
| `ci-repair-accept-tests` | `src/commands/ci_commands.py` | None |
| `ci-repair-no-test-ids` | `src/commands/ci_commands.py` | None |
| `ci-repair-other-owners` | `src/commands/ci_commands.py` | `task_ids` |
| `ci-repair` | `src/commands/ci_commands.py` | `acceptance`, `attempt`, `facts`, `ref` |
| `development-conflict-recovery` | `src/integration/development.py` | `target_branch` |
| `development-regeneration` | `src/integration/development.py` | `command`, `generated_merge_driver` |
| `development-repair` | `src/integration/development.py` | `branches`, `candidate_sha`, `failure_details`, `generation`, `recovery`, `sources`, `target_branch` |
| `development-source-recovery` | `src/integration/development.py` | `target_branch` |
| `git-verification-feedback` | `src/orchestrator/git_ops.py` | `attempt`, `bullet_list`, `closing`, `max_retries` |
| `git-verification-held` | `src/orchestrator/git_ops.py` | None |
| `git-verification-restart` | `src/orchestrator/git_ops.py` | None |
| `github-issue-fix` | `src/commands/github_issue_commands.py` | `number`, `report`, `repository`, `review_id` |
| `github-issue-investigation` | `src/commands/github_issue_commands.py` | `number`, `title`, `url` |
| `integration-repair-preserved-progress` | `src/integration/repair.py` | `completed_member_ordinals`, `progress_ref`, `progress_sha` |
| `integration-repair-refiled` | `src/integration/repair.py` | `previous_claim_epoch` |
| `integration-repair-stage` | `src/integration/repair.py` | `dossier`, `operation_id`, `ordinal`, `starting_sha` |
| `member-conflict-resume` | `src/integration/repair.py` | None |
| `member-conflict-start` | `src/integration/repair.py` | None |
| `object-internal-handoff` | `src/commands/object_loop_commands.py` | None |
| `object-result-handoff` | `src/commands/object_loop_commands.py` | None |
| `object-round-handoff` | `src/commands/object_loop_commands.py` | `attempt_id` |
| `object-score-handoff` | `src/commands/object_loop_commands.py` | None |
| `object-self-reference` | `src/commands/object_loop_commands.py` | None |
| `ordinary-repair-budget` | `src/integration/repair.py` | `seconds`, `stage` |
| `ordinary-repair` | `src/integration/repair.py` | `brief`, `repair_input`, `target_ref` |
| `prerequisite-overlay-conflict` | `src/integration/stacked_branches.py` | `child_head`, `overlay_head` |
| `prerequisite-parent-conflict` | `src/integration/stacked_branches.py` | `conflicting_files`, `parent_head` |
| `prerequisite-stack-repair` | `src/integration/stacked_branches.py` | `head`, `overlay_conflict`, `parent_conflict`, `prerequisite_heads`, `task_id` |
| `promotion-draft-notes` | `src/commands/promote_commands.py` | None |
| `promotion-prepare` | `src/commands/promote_commands.py` | `default_branch`, `notes_details`, `notes_instructions`, `version`, `version_source` |
| `retired-writer-close-feedback` | `src/integration/delegate_release.py` | `reason_1`, `reason_1_upper`, `retirement_disposition` |
| `review-rejected-issue` | `src/commands/review_commands.py` | `review_id` |
| `review-revision` | `src/commands/review_commands.py` | `review_id` |
| `root-pr-check-repair` | `src/integration/root_pr_recovery.py` | `checks`, `policy_generation`, `pr_url`, `source_base_sha`, `source_sha` |
| `source-ancestry-repair` | `src/integration/source_ancestry.py` | `branch`, `evidence_id`, `found`, `observation_details`, `pr_url`, `source_base`, `source_generation`, `source_head` |
| `source-ci-reopen` | `src/integration/source_ci.py` | `checks`, `policy_generation`, `source_base`, `source_branch`, `source_generation`, `source_head`, `source_pr_url`, `source_repository_id`, `state`, `task_id` |
| `train-ci-not-triggered` | `src/integration/train.py` | `candidate_ref`, `head`, `missing_push_reason`, `source` |
| `train-conflict-batch-scope` | `src/integration/train.py` | None |
| `train-conflict-carried` | `src/integration/train.py` | None |
| `train-conflict-complete` | `src/integration/train.py` | `starting_sha` |
| `train-conflict-member-scope` | `src/integration/train.py` | None |
| `train-conflict-regenerate` | `src/integration/train.py` | `regeneration_command` |
| `train-conflict-remaining` | `src/integration/train.py` | None |
| `train-conflict-verify` | `src/integration/train.py` | None |
| `train-default-sync-merge` | `src/integration/train.py` | `decision_ref`, `decision_sha`, `starting_sha` |
| `train-default-sync-observation` | `src/integration/train.py` | `decision_ref`, `decision_sha` |
| `train-default-sync-preserve` | `src/integration/train.py` | None |
| `train-default-sync-publish` | `src/integration/train.py` | None |
| `train-default-sync-regenerate` | `src/integration/train.py` | `regeneration_command` |
| `vibecop-repair` | `src/plugins/internal/vibecop.py` | `error_count`, `project_name`, `summary`, `workspace_path` |
| `workspace-sync-merge` | `src/orchestrator/sync_workflow.py` | `default_branch`, `project_id`, `workspace_info` |
| `workspace-sync` | `src/commands/agent_commands.py` | `default_branch`, `project_id`, `workspace_paths`, `workspace_root` |

## Other task creation sites

These sites do not add generated worker policy prose:

| Site | Description source |
| --- | --- |
| `src/playbooks/executors/agent_task.py`, `src/orchestrator/core.py` | Playbook-authored prompts, already editable in the vault. |
| `src/task_graph/creator.py`, `src/commands/task_commands.py`, `src/commands/task_subtask_commands.py`, `src/commands/phase_commands.py`, `src/commands/task_changes.py`, `src/database/queries/hierarchy_queries.py`, `src/integration/hierarchy.py` | Caller-authored descriptions, graph criteria, or factual standing-parent bookkeeping. |
| `src/commands/proposal_commands.py`, `src/knowledge/task_creation.py`, `src/database/queries/triage_queries.py` | Caller/spec-authored task packets and descriptions. |
| `src/integration/writers.py` | Caller/playbook brief followed by factual subject, generation, head, deadline, attempt, and members. |
| `src/integration/repair_rebind.py`, `src/integration/detached_repair_rebind.py` | Reuses repair templates; adds factual supersession/conflict intent. |
| `src/integration/train_sources.py` | Publication source records with factual labels rather than worker instructions. |
| `src/orchestrator/pools.py`, `src/commands/claim_commands.py`, `src/commands/profile_commands.py` | Empty task-shaped launch/runtime placeholders, not generated work descriptions. |

Task titles, observed checks, URLs, SHAs, branches, captured log output, authored
review comments, and machine-read accounting markers remain facts in code.
Profile roles, session launch instructions, prime fragments, and shipped playbooks
already have their own editable Markdown sources; they are not duplicated here.
