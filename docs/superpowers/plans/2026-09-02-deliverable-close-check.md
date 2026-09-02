# Deliverable Close Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an implementation task from silently closing as passed when its declared plan deliverables were not shipped.

**Architecture:** Store each task's declared deliverables as structured JSON and store the close-time evaluation, including explicit waivers, in the append-only completion record. Creation surfaces preserve the list; the close command evaluates it against the task worktree and recorded test commands before running completion. Read surfaces expose the declaration and latest evaluation to the CLI, prime renderer, reviewer context, and dashboard.

**Tech Stack:** Python 3.12, SQLAlchemy Core/Alembic, Click, Pydantic, React/TypeScript.

**Spec:** Task `crisp-ridge-12`.

## Global Constraints

- A passing close is refused only for declared deliverables lacking both a successful evaluation and an explicit `--deliverable-unmet ID: reason` waiver.
- Preserve existing task creation, completion history, and no-deliverables close behavior.
- Test modules must exist and be named by a recorded `--test` command.
- Use `aq test` for focused and area tests; regenerate the committed OpenAPI artifact for changed API models.

---

### Task 1: Persist and expose structured deliverables

**Files:** `src/models.py`, `src/database/tables.py`, `src/database/queries/task_queries.py`, `src/database/queries/result_queries.py`, `src/api/models/task.py`, a new Alembic revision, and task API tests.

- [ ] Define one normalized deliverable object (`id`, `kind`, `target`) and include declaration/evaluation lists on `Task` and `TaskCompletion`.
- [ ] Write failing persistence/read-surface tests for task declarations and completion evaluations.
- [ ] Add JSON columns with empty-list server defaults, map them through query objects, and expose them through Pydantic response models.
- [ ] Run focused persistence/API tests and commit the logical change.

### Task 2: Carry deliverables through all child-creation paths

**Files:** `src/commands/task_commands.py`, `src/commands/proposal_commands.py`, `src/task_graph/models.py`, `src/task_graph/parser.py`, `src/task_graph/creator.py`, `src/tools/definitions.py`, and creation/graph/proposal tests.

- [ ] Write failing tests covering direct create, batch proposal commit, and `aq-graph` node parsing/creation with a deliverables list.
- [ ] Validate normalized item shape and preserve the list through each respective creation flow.
- [ ] Add direct-create command schema fields so generated/API clients accept the declaration.
- [ ] Run focused creation tests and commit the logical change.

### Task 3: Enforce a passing close self-check

**Files:** a focused evaluator module, `src/commands/session_commands.py`, `src/cli/agent_surface.py`, `src/tools/definitions.py`, and session/CLI tests.

- [ ] Write failing tests for fully met deliverables, an unmet deliverable waived with a reason, and an unmet deliverable rejected without a reason.
- [ ] Implement worktree-aware file/test/grep checks; test checks must find the target in at least one recorded `--test` command.
- [ ] Parse repeatable `--deliverable-unmet ID: reason` CLI options, reject unknown/malformed waivers, return actionable gaps while retaining the claim, and save the evaluation in the completion record after an accepted close.
- [ ] Run focused close and CLI tests and commit the logical change.

### Task 4: Surface the contract to workers, reviewers, and operators

**Files:** `src/prime/templates/completion_protocol*.md`, `src/prime/sections.py`, reviewer-prime code/tests, `AGENTS.md`, `dashboard/src/pages/TaskDetail.tsx`, command-center task aggregation UI/tests.

- [ ] Write failing render tests for prime deliverable guidance and aggregate package-child results.
- [ ] Render declarations/evaluations in task detail and provide reviewer primes with child unmet counts and reasons before review instructions.
- [ ] Render an accessible checklist on task detail and package-level unmet-count badge/summary in the epic/task-tree view.
- [ ] Run targeted Python and dashboard tests; regenerate API clients/spec if their model contract changed.

### Task 5: Verify and hand off

**Files:** all changed files.

- [ ] Run Ruff on changed Python files.
- [ ] Run the focused backend area suite and dashboard test/build checks.
- [ ] Inspect diff for declaration-to-completion coverage, commit, push, and close with exact evidence.
