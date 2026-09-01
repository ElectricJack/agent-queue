---
tags: [guide, operations, migrations, integration]
---

# Upgrading to `integration_mode` (Alembic `c4d5e6f7a8b9`)

This release replaces the per-task `requires_approval` boolean with an explicit
integration policy and deletes the legacy approval statuses. Revision
`c4d5e6f7a8b9`:

- adds `integration_mode` (`'direct'` | `'pull_request'` | NULL = inherit) to
  `tasks`, `archived_tasks`, and `projects`;
- backfills existing rows on both task tables: `requires_approval = 1` →
  `'pull_request'`, `0` → `'direct'` (each row keeps its exact old behavior —
  only rows created *after* the upgrade inherit the new policy chain:
  plan-subtask parent's task-level override → task override → project policy →
  config `integration.default_mode`, shipped default `pull_request`);
- drops `requires_approval` and `auto_approve_plan`.

## The preflight

The `AWAITING_APPROVAL` and `AWAITING_PLAN_APPROVAL` task statuses are retired
with this revision — post-upgrade code cannot load rows in those states. The
migration therefore runs a PREFLIGHT: if any **active** task is still in one of
them, `alembic upgrade head` **fails** and prints one line per offending task
with a remediation hint. The migration never fabricates approval or merge
state; an operator must disposition each row. Archived rows are unaffected
(the archive reads statuses as plain text).

## Remediation options

For each task listed by the preflight, pick ONE, then re-run
`alembic upgrade head`:

- **Re-run the task** (do the work again under the new policy):

  ```sql
  UPDATE tasks SET status='READY', assigned_agent_id=NULL WHERE id='<id>';
  ```

- **Complete it** (its PR was merged, or the work is accepted as-is):

  ```sql
  UPDATE tasks SET status='COMPLETED' WHERE id='<id>';
  ```

- **Park it for later review**:

  ```sql
  UPDATE tasks SET status='BLOCKED' WHERE id='<id>';
  ```

Alternatively, disposition the rows **before upgrading** using the pre-upgrade
daemon's commands, which still exist on the old version:
`aq task approve <id>` (complete) or `aq task restart <id>` (re-run) — then
stop the daemon and upgrade.

For an `AWAITING_APPROVAL` task with a `pr_url`, check the PR first: merged →
complete it; open/closed → re-run or park. `AWAITING_PLAN_APPROVAL` tasks
belong to the removed plan-discovery flow; there is no approval path anymore —
re-run the task or park it.

## After the upgrade

- Human approval is a `human` gate on a playbook run; waiting on a PR is a
  `pr-merged` gate resolved automatically by the orchestrator's gate sweep.
- In `pull_request` mode the worker pushes its branch and opens a PR; the task
  completes **unmerged** (with `pr_url` recorded) and the default-pipeline
  review policy owns the merge. In `direct` mode the completion pipeline
  merges the task branch into the default branch on completion.
- Set project policy via `projects.integration_mode`, the system default via
  the `integration.default_mode` config key, and per-task overrides via
  `create_task` / `edit_task` `integration_mode` (edit accepts `null` to
  clear).
