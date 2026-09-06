# Task11c public mutation and legacy suppression map

Read-only Sol follow-up; controller corrected merge-handler path with rg. No edits,
tests or external state changes in the inspection.

## Effective-mode and configuration surface

project_commands.py:_cmd_edit_project currently excludes effective mode, designated
repository and hierarchy policy from its allowlist; extras silently ignored. Create
project and onboarding also cannot set these fields. Profile/playbook import/activation
can change inputs or re-enable legacy activations, but do not update rollout fields.
system_commands.py config/reload changes global integration configuration, not projects.
project_queries.update_project is trusted internal and retains effective-mode support;
desired/draining/generation are CAS-only after11a. No existing public mode writer.

11c will make integration_enable the sole public effective-mode writer. Extend guarded
edit_project only for integration_repository_id/hierarchical_integration_policy, with
tool/CLI schema integration as needed. Require LOCAL only for these sensitive fields,
not ordinary unrelated project edits. Reject rollout/CAS args explicitly. Dedicated
hierarchy-locked generation-CAS configuration helper, fully disabled/drained with no
active work. Do not call bare update_project for sensitive configuration. Snapshot
defaults never rewrite frozen operations. Keep public deletion from discarding live
integration state; report disabled/drained prerequisite at command boundary.

## Legacy routes

- src/commands/git_commands.py:_cmd_pr_merge: guard immediately after get_project,
  before filesystem/forge/CI. The mapper's pr_commands.py spelling was incorrect.
- src/playbooks/services.py:DatabaseActivationSource.ready_activations: project
  pr-merge-sweep suppression must use activation.scope_identifier because timer.30m
  may have no project_id.
- src/playbooks/engine.py:PlaybookEngine.dispatch_event: after event project hydration,
  suppress only default-pipeline/per-branch-final-review before run_rule. Other default
  pipeline rules remain active. Ordinary events don't necessarily traverse integration
  destination selection.
- src/playbooks/runtime.py:PlaybookManager._select_integration_destinations: same
  project predicate for durable outbox parity, while honoring frozen operation routing.
- Re-enabled legacy activation must not bypass filters. Never disable global pipeline.
- scheduler.mark_due must recheck observe/read-only eligibility at mutation boundary.

## Focused regressions

Local-only sensitive edit/configure/enable versus elevated supervisor; rollout fields
cannot pass edit_project; stale generation rolls back entire cutover; delete requires
disabled/drained; observe tick does not persist schedule; pre-forge pr_merge guards
with disabled/observe compatibility; project sweep filtering on global timer; only
the final-review rule suppressed; re-enabled activation cannot bypass suppression.
Reuse controls/scope/pr_merge_ci_gate/activation-source and engine test areas.
