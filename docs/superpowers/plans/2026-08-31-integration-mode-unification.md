# Integration-Mode Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the dead `auto_approve_plan` flag, replace `requires_approval` with an explicit
`integration_mode: direct | pull_request` policy (task override → project policy → config default),
and retire the legacy AWAITING_APPROVAL / AWAITING_PLAN_APPROVAL status machinery in favor of the
gate/review-pipeline architecture.

**Architecture:** `integration_mode` is a nullable TEXT on tasks (and a nullable TEXT on projects);
NULL means "inherit". Effective mode = plan-subtask parent's task-level mode (if plan subtask and
parent sets one) → task.integration_mode → project.integration_mode → config
`integration.default_mode` (shipped default: `pull_request`). The orchestrator git pipeline
(`_get_execution_rules`, `_phase_verify`, `_phase_integrate`) consumes only the effective mode.
Worker completion always transitions to COMPLETED (session and legacy paths unified); review and
merge policy live in the default pipeline playbook (reviewer/final-reviewer tasks, `task` +
`pr-merged` gates). Human approval is a `human` gate. AWAITING_* statuses, `approve_task`,
`approve_plan`, `reject_plan`, `delete_plan`, and the 60s approval poller are deleted;
`_poll_pr_merged` moves to core.py beside the gate sweep that uses it.

**Tech Stack:** Python 3.12 / SQLAlchemy Core / Alembic (SQLite + PostgreSQL), FastAPI-generated
OpenAPI → openapi-python-client + @hey-api/openapi-ts, React dashboard, pytest.

**Spec:** the task description of `fresh-cascade` (aq task show fresh-cascade); design references:
`docs/superpowers/specs/2026-08-21-dashboard-v2-and-work-pipeline-design.md`,
`src/prompts/default_playbooks/default-pipeline.md`.

## Global Constraints

- Migrations must work on SQLite AND PostgreSQL; handle `tasks` and the archived tasks table.
- Migration must not fabricate approval/merge state: any active row in AWAITING_APPROVAL or
  AWAITING_PLAN_APPROVAL fails the upgrade with exact per-row remediation commands.
- Backfill compatibility policy (explicit): `requires_approval=1` → `'pull_request'`,
  `requires_approval=0` → `'direct'` (uniform, both tables) — preserves per-row behavior exactly.
- Shipped config default for new/NULL rows: `integration.default_mode: pull_request` — direct
  integration only via explicit policy (project or task).
- Never hand-edit generated clients (`packages/aq-client`, `packages/aq-ts-client`); regenerate
  from OpenAPI.
- Do not remove reviewer/final-reviewer profiles, `pr_merge`, general gates, `pr-merged` polling,
  `is_plan_subtask`, task comments, or dependency semantics.
- Commands return `{"success": bool, ...}` dicts; async-first git.

---

### Task 1: Characterization tests for current `requires_approval` behavior

**Files:**
- Create: `tests/test_integration_mode.py`

Characterize (against current code, so they run green before the refactor, then are updated in
Task 3 to target the new API — keep pure-logic assertions in helper-level tests so they survive):
- `_get_execution_rules`: PR-mode prompt says push + `gh pr create`, no merge; direct-mode prompt
  says merge to default + push; intermediate subtask says stay on branch.
- `_phase_verify` scenario selection: direct mode auto-merges to default; PR mode requires an open
  PR when on the task branch with a remote; worktree mode leaves merging to `_phase_integrate`.
- `_phase_integrate`: PR mode skips the base merge into default (step 4); direct mode merges+pushes.
- Session close (`_complete_session_task_locked`): outcome=pass + pipeline ok → COMPLETED with
  pr_url carried in the task.closed event; pipeline stop → BLOCKED.
- Regression guard: a task in PR mode must never reach the base-merge/auto-merge code paths —
  asserted via the effective-mode helper once it exists (Task 2), i.e. deleting/renaming the field
  cannot silently turn PR work into a merge because the git phases branch on
  `effective mode == "pull_request"` from one helper.

### Task 2: Introduce the integration-policy contract

**Files:**
- Modify: `src/models.py` (Task.integration_mode, Project.integration_mode, constants,
  `resolve_integration_mode` pure helper)
- Modify: `src/config.py` (new `IntegrationConfig` with `default_mode: str = "pull_request"`,
  parsed from `integration:`; delete dead `inherit_approval`)
- Modify: `src/database/tables.py` (tasks + archived tasks: add `integration_mode` TEXT nullable,
  drop `requires_approval`, `auto_approve_plan`; projects: add `integration_mode` TEXT nullable)
- Modify: `src/database/queries/task_queries.py`, `archive_queries.py`, `project_queries.py`
  (serialization)
- Create: migration `migrations/versions/<rev>_integration_mode.py` (preflight + add + backfill +
  drop; SQLite batch_alter where needed)
- Test: `tests/test_integration_mode.py` (resolve helper), `tests/test_migration_integration_mode.py`

**Interfaces:**
- Produces: `resolve_integration_mode(task_mode, parent_task_mode, project_mode, default_mode) -> str`
  and constants `INTEGRATION_MODE_DIRECT/_PULL_REQUEST`, `INTEGRATION_MODES`.
- Produces: `Orchestrator._effective_integration_mode(task) -> str` (async; db lookups) — Task 3.

Preflight (in migration, before DDL): SELECT ids in ('AWAITING_APPROVAL','AWAITING_PLAN_APPROVAL')
from tasks; if any, raise RuntimeError listing ids with remediation:
`aq task reopen <id>` (re-run) / `aq task set <id> --status BLOCKED` equivalent SQL
`UPDATE tasks SET status='BLOCKED' WHERE id='<id>';` (park) — documented in Task 8 docs.

### Task 3: Rewire orchestrator git pipeline + unify completion

**Files:**
- Modify: `src/orchestrator/context.py` (`_get_execution_rules(integration_mode=...)`)
- Modify: `src/orchestrator/git_ops.py` (`_phase_verify`, `_phase_integrate`,
  `_reopen_for_verification` transition kwargs)
- Modify: `src/orchestrator/execution.py` (legacy COMPLETED path: drop AWAITING_APPROVAL
  transitions; PR path → COMPLETED with pr_url persisted; drop `requires_approval and not pr_url`
  manual branch)
- Delete: `src/orchestrator/approval.py` (move `_poll_pr_merged` to `core.py`); remove cascade
  call `_check_awaiting_approval`, `_last_approval_check`, `_no_pr_reminded_at`
- Modify: `src/state_machine.py` (drop AWAITING_* states/transitions and PLAN_*/PR_CREATED events)
- Modify: `src/models.py` (drop AWAITING_* from TaskStatus)
- Test: update `tests/test_integration_mode.py`; fix `tests/test_orchestrator.py` and friends

### Task 4: Remove legacy commands and surfaces

**Files:**
- Modify: `src/commands/task_commands.py` (delete `_cmd_approve_task`, `_cmd_approve_plan`,
  `_cmd_reject_plan`, `_cmd_delete_plan`, plan-cleanup helper usage; create/edit/serialize
  integration_mode; reopen_with_feedback keeps mode)
- Modify: `src/commands/claim_commands.py`, `src/task_graph/creator.py`, `src/mcp_interfaces.py`,
  `src/mcp_registration.py`, `src/cli/tasks.py`, `src/cli/menus.py`, `src/cli/formatters.py`,
  `src/cli/styles.py`, `src/cli/formatter_registry.py`, `src/cli/auto_commands.py`
- Modify: `src/tools/definitions.py`, `src/api/models/task.py`
- Modify: discord: `notification_handler.py`, `notifications.py`, `embeds.py`, `bot.py`;
  `src/notifications/builder.py`
- Modify: `src/workflow_pipeline_view.py`, `src/commands/helpers.py`, `src/git/manager.py`,
  `src/orchestrator/monitoring.py`, `src/agents/terminals.py`, `src/api/terminal_stream.py`,
  `src/database/queries/blocked_state.py`, `agent_queries.py`, `message_queries.py` (comment)
- Delete: `src/doctor/plan_checks.py` wiring once migration preflight covers it; trim
  `src/plan_parser.py` + `AutoTaskConfig` dead fields if uncalled
- Modify: `src/commands/project_commands.py` (project integration_mode edit/show)

### Task 5: Update default project policy surfaces & config docs

Covered inside Tasks 2/4 edits; verify `aq schema` output and MCP descriptions describe
integration_mode.

### Task 6: Dashboard

**Files:**
- Modify: `dashboard/src/components/CreateTaskModal.tsx` (remove auto-approve; integration mode
  select: inherit/pull_request/direct), `dashboard/src/pages/TaskDetail.tsx`,
  `dashboard/src/panes/task-detail/index.tsx` (+ test), `dashboard/src/components/TaskActions.tsx`,
  `dashboard/src/pages/command-center/*` (drop AWAITING_* filters/styles/actions),
  `dashboard/src/pages/work/WorkIndex.tsx`

### Task 7: Regenerate OpenAPI + clients

- Dump spec offline via the FastAPI app; run `scripts/regenerate-api-client.sh --from-file` and
  `scripts/regenerate-ts-client.sh --from-file`; build dashboard/ts tests.

### Task 8: Docs/specs sweep

- Update: `docs/specs/models-and-state-machine.md`, `orchestrator.md`, `command-handler.md`,
  `mcp-server.md`, `docs/specs/design/work-graph.md`, `worktree-execution.md`,
  `messaging/discord.md`, `docs/specs/runtimes/development-guide.md`; ops note for the migration
  preflight remediation.

### Task 9: Full verification

- `pytest tests/ -n auto`; dashboard `npm test`; migration up/down on SQLite + PostgreSQL (if
  available); final repo grep for `auto_approve_plan|requires_approval|AWAITING_APPROVAL|
  AWAITING_PLAN_APPROVAL|approve_plan|approve_task` — remaining hits only in migrations/history
  and docs describing the legacy migration.
