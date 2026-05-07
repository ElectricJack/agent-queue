# Agent Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore automatic agent supply so READY tasks dispatch without manual `aq agent create` — the half-finished workspace-as-agent rewrite is causing the agents table to stay empty and the scheduler to permanently block.

**Architecture:** Add a pre-tick `AgentReconciler` that lazily creates agent rows when work needs them, sized by `project.max_concurrent_agents` rather than by workspace count. Rename `agents.agent_type` → `agents.profile_id` to match the field's actual meaning. Drop three legacy columns: `tasks.agent_type`, `archived_tasks.agent_type`, and `projects.default_agent_type`. Delete the dead `_task_agent_type_matches()` filter. Rewrite `Orchestrator._resolve_profile` to use `profile_id` end-to-end (eliminates the dual-purpose `agent_type` indirection). `Scheduler.schedule()` stays a pure function; the reconciler runs first each tick.

**Tech Stack:** Python 3.12, SQLAlchemy Core, Alembic, pytest (asyncio auto mode), aiosqlite, asyncpg. Repo at `/Users/jack.kern/Shared/AI/agent-queue`.

**Spec:** `docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md` (latest: commit `d59186bf` — drops `project.default_agent_type` and rewrites `_resolve_profile` per the refined design)

**Branch:** Create `agent-reconciler` off `main` before starting Phase 1: `git checkout main && git pull && git checkout -b agent-reconciler`. No worktree needed.

---

## Phase 1 — Schema changes (rename + drop, no reconciler yet)

After this phase, the daemon still won't auto-dispatch — the reconciler isn't wired. But the schema is clean. Each task ends in a commit so a regression can be bisected.

### Task 1.1: Confirm alembic state and write the migration

**Files:**
- Create: `migrations/versions/2026_05_07_rename_agent_type_and_drop_task_agent_type.py`

- [ ] **Step 1: Confirm single alembic head**

Run: `cd ~/Shared/AI/agent-queue && source .venv/bin/activate && alembic heads`
Expected: a single revision id (currently `d8e4b2c5f1a7 (head)`). If multiple heads are returned, **stop and surface to the operator** — a merge revision is needed before this work can proceed.

Capture the head id; you'll use it as `down_revision`.

- [ ] **Step 2: Read the model migration**

Read `migrations/versions/2026_04_28_rename_platform_column_to_runtime.py` end-to-end. The new migration uses the same idempotent shape (inspect schema → batch_alter_table on SQLite → native ALTER on PostgreSQL).

- [ ] **Step 3: Write the migration**

Create the file with this content (replace `<HEAD_REVISION>` with what step 1 returned — currently `d8e4b2c5f1a7`):

```python
"""rename agents.agent_type to profile_id; drop legacy agent_type columns

Revision ID: e4f2a8b1d6c9
Revises: <HEAD_REVISION>
Create Date: 2026-05-07 00:00:00.000000

OPERATIONAL NOTE
================

Four operations, each idempotent (inspects the schema first):

1. Rename ``agents.agent_type`` → ``agents.profile_id``.  Always *was*
   the profile id by string match in actual usage.

2. Drop ``tasks.agent_type``.  Coordination-category-filter never
   implemented; live DB has 1 task with NULL.

3. Drop ``archived_tasks.agent_type`` for symmetry.

4. Drop ``projects.default_agent_type``.  Was used as the
   project-scoped-profile lookup key; ``_resolve_profile`` is rewritten
   to use ``profile_id`` directly, making this column dead.

See docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4f2a8b1d6c9'
down_revision: Union[str, Sequence[str], None] = '<HEAD_REVISION>'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, col: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return col in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("agents", "agent_type") and not _has_column("agents", "profile_id"):
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.alter_column("agent_type", new_column_name="profile_id")
    if _has_column("tasks", "agent_type"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.drop_column("agent_type")
    if _has_column("archived_tasks", "agent_type"):
        with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
            batch_op.drop_column("agent_type")
    if _has_column("projects", "default_agent_type"):
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.drop_column("default_agent_type")


def downgrade() -> None:
    if not _has_column("projects", "default_agent_type"):
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.add_column(sa.Column("default_agent_type", sa.Text(), nullable=True))
    if not _has_column("archived_tasks", "agent_type"):
        with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("agent_type", sa.Text(), nullable=True))
    if not _has_column("tasks", "agent_type"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("agent_type", sa.Text(), nullable=True))
    if _has_column("agents", "profile_id") and not _has_column("agents", "agent_type"):
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.alter_column("profile_id", new_column_name="agent_type")
```

- [ ] **Step 4: Apply against the dev database**

Run: `alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade <HEAD> -> e4f2a8b1d6c9, rename agents.agent_type to profile_id; drop legacy agent_type columns` (alembic prints the docstring's first line — match the exact text you wrote in step 3).

- [ ] **Step 5: Verify schema**

Run: `sqlite3 ~/.agent-queue/agent-queue.db ".schema agents" | grep -E 'agent_type|profile_id'`
Expected: `profile_id TEXT`, no `agent_type`.

Run: `sqlite3 ~/.agent-queue/agent-queue.db ".schema tasks" | grep -E 'agent_type'`
Expected: no output (column dropped).

Run: `sqlite3 ~/.agent-queue/agent-queue.db ".schema archived_tasks" | grep -E 'agent_type'`
Expected: no output.

Run: `sqlite3 ~/.agent-queue/agent-queue.db ".schema projects" | grep -E 'default_agent_type'`
Expected: no output (column dropped).

- [ ] **Step 6: Verify downgrade reverses cleanly**

Run: `alembic downgrade -1`
Expected: success.

Run: `sqlite3 ~/.agent-queue/agent-queue.db ".schema agents" | grep agent_type`
Expected: `agent_type TEXT` is back.

Run: `alembic upgrade head`
Expected: success again.

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/2026_05_07_rename_agent_type_and_drop_task_agent_type.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Add Alembic migration: rename agents.agent_type, drop legacy columns

Four idempotent operations:
1. agents.agent_type → agents.profile_id
2. tasks.agent_type dropped (coordination-category feature never wired)
3. archived_tasks.agent_type dropped (symmetry)
4. projects.default_agent_type dropped (replaced by profile_id-keyed
   project-scoped lookup; see _resolve_profile rewrite in subsequent task)

Code references NOT yet updated — daemon will fail to start until
Task 1.2-1.9 land.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 1.2: Update tables.py

**Files:**
- Modify: `src/database/tables.py`

- [ ] **Step 1: Identify exact lines to change**

Run: `grep -nE '"agent_type"|"default_agent_type"' src/database/tables.py`
Expected: line numbers for five total columns:
- Line 47: `projects.default_agent_type` — **delete this Column entirely**
- Line 93: `tasks.agent_type` — **delete this Column entirely**
- Line 153: `agents.agent_type` — **rename to `profile_id`**
- Line 192: `rate_limits.agent_type` — **leave alone** (separate concern)
- Line 315: `archived_tasks.agent_type` — **delete this Column entirely**

- [ ] **Step 2: Apply the four changes via targeted Edits**

Don't use `replace_all`. Read each line in context first; apply Edits with enough surrounding context to disambiguate.

For line 153 (agents): change `Column("agent_type", Text, nullable=False)` to `Column("profile_id", Text, nullable=False)  # soft reference to agent_profiles.id`.

For lines 47 (projects.default_agent_type), 93 (tasks.agent_type), and 315 (archived_tasks.agent_type): delete the entire Column line including the trailing comma.

- [ ] **Step 3: Verify**

Run: `grep -nE '"agent_type"|"default_agent_type"' src/database/tables.py`
Expected: only the `rate_limits.agent_type` line remains.

Run: `grep -nE '"profile_id"' src/database/tables.py`
Expected: 3 hits (existing `projects.default_profile_id`, existing `tasks.profile_id`, new `agents.profile_id`).

- [ ] **Step 4: Smoke (no test run yet — code below still references old fields)**

Run: `python -c "from src.database import tables; print(len(tables.agents.columns))"`
Expected: a number — confirms the module imports.

- [ ] **Step 5: Commit**

```bash
git add src/database/tables.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "tables.py: rename agents.agent_type → profile_id; drop legacy columns

Drops projects.default_agent_type, tasks.agent_type, archived_tasks.agent_type."
```

### Task 1.3: Update Agent, Task, and Project dataclasses

**Files:**
- Modify: `src/models.py`

- [ ] **Step 1: Update Agent**

Find `agent_type: str  # "claude", "codex", "cursor", "aider"` (around line 338). Replace with:

```python
profile_id: str  # soft reference to agent_profiles.id
```

Also remove the `.. deprecated::` comment block on the `Agent` dataclass (around line 330) — under this design `Agent` is the persisted slot record, not deprecated.

- [ ] **Step 2: Update Task — drop the field**

Find `agent_type: str | None = None  # required agent type (e.g. "coding", "code-review", "qa")` (around line 318). **Delete the entire field** (column is dropped).

- [ ] **Step 3: Update Project — drop the field**

Find `default_agent_type: str | None = ...` (around line 242, three lines including the comment). **Delete the entire field block.**

- [ ] **Step 4: Update WorkspaceAgent docstring**

Around line 348, update to: *"API view of an agent that currently holds a workspace lock — derived from the agents + workspaces tables, not a persisted entity."*

- [ ] **Step 5: Verify**

Run: `grep -nE '\bagent_type\b|\bdefault_agent_type\b' src/models.py`
Expected: no output (or only docstring text — verify each match).

- [ ] **Step 6: Commit**

```bash
git add src/models.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "models.py: rename Agent.agent_type → profile_id; drop Task.agent_type and Project.default_agent_type"
```

### Task 1.4: Update query layer

**Files:**
- Modify: `src/database/queries/agent_queries.py`
- Modify: `src/database/queries/task_queries.py`
- Modify: `src/database/queries/archive_queries.py`
- Modify: `src/database/queries/project_queries.py`

- [ ] **Step 1: List exact occurrences in all four files**

Run: `grep -nE '\bagent_type\b|\bdefault_agent_type\b' src/database/queries/`
Expected: a list. For each match, classify:
- `agent_type=agent.agent_type` (constructing an Agent for INSERT) → rename to `profile_id=agent.profile_id`
- `agent_type=row["agent_type"]` (reconstructing Agent from row) → rename to `profile_id=row["profile_id"]`
- `agent_type=task.agent_type` (constructing a Task for INSERT) → **delete the kwarg**
- `agent_type=row.get("agent_type")` or `row["agent_type"]` (reconstructing Task) → **delete the kwarg**
- `default_agent_type=project.default_agent_type` (in `project_queries.py:48`) → **delete the kwarg**
- `default_agent_type=row.get("default_agent_type")` (in `project_queries.py:209`) → **delete the kwarg**

- [ ] **Step 2: Apply targeted Edits per file**

Read each file, then Edit each match. Do **not** use `replace_all=true`.

- [ ] **Step 3: Verify**

Run: `grep -nE '\bagent_type\b|\bdefault_agent_type\b' src/database/queries/`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add src/database/queries/
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "queries: rename agent.agent_type → profile_id; drop task and project default_agent_type passthrough"
```

### Task 1.5: Update scheduler — rename + delete `_task_agent_type_matches`

**Files:**
- Modify: `src/scheduler.py`

- [ ] **Step 1: Find the function and its caller**

Run: `grep -nE '_task_agent_type_matches|\.agent_type\b' src/scheduler.py`
Expected: function definition around line 175, call site around line 446, plus several `agent.agent_type` field accesses.

- [ ] **Step 2: Delete `_task_agent_type_matches` and remove its call site**

Read lines 170-210 (the function). Delete the entire function body and the docstring block. Then read lines 440-450 (the call site at line 446) — remove the `and _task_agent_type_matches(t, agent)` clause from the conditional.

- [ ] **Step 3: Rename remaining `agent.agent_type` → `agent.profile_id`**

Run: `grep -nE '\bagent\.agent_type|agent_type=' src/scheduler.py`
For each remaining match, Edit to use `profile_id`. Watch for:
- `provider_cooldowns.get(a.agent_type, 0)` → `provider_cooldowns.get(a.profile_id, 0)`
- Any `agent_type=...` constructor kwarg

- [ ] **Step 4: Verify no `task.agent_type` references remain**

Run: `grep -nE '\btask\.agent_type|t\.agent_type' src/scheduler.py`
Expected: no output (the field is gone from Task; any references would be dead).

- [ ] **Step 5: Verify file imports cleanly**

Run: `python -c "from src import scheduler"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add src/scheduler.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "scheduler: drop _task_agent_type_matches; rename agent.agent_type to profile_id

The category-filter feature was specced in agent-coordination.md but
never implemented (live DB has 1 task with agent_type=NULL).
Agent.profile_id replaces the misleading agent.agent_type name."
```

### Task 1.6: Rewrite `_resolve_profile` (core.py)

**Files:**
- Modify: `src/orchestrator/core.py`

- [ ] **Step 1: Read the existing implementation**

Read `src/orchestrator/core.py` lines 495-535. The current cascade uses `task.agent_type or project.default_agent_type` to look up `project:{pid}:{agent_type}` and then `{agent_type}`. With both fields gone, this needs replacement.

- [ ] **Step 2: Replace the entire function body**

Replace the function (keep the signature) with:

```python
async def _resolve_profile(self, task: Task) -> AgentProfile | None:
    """Resolve the agent profile for a task.

    Resolution order (first non-None wins):
    1. **Task-level** — ``task.profile_id`` (explicit override per task)
    2. **Project default** — ``project.default_profile_id``
    3. **None** (adapter uses built-in defaults)

    Project-scoped overrides are checked first at each level: if a
    profile with id ``project:{project_id}:{profile_id}`` exists, it
    takes precedence over the global ``{profile_id}`` profile. This is
    the row synced from
    ``vault/projects/{project}/agent-types/{profile_id}/profile.md``.

    Profiles control: model selection, permission mode, allowed tools
    allowlist, MCP server configuration, and a system prompt suffix
    that sets the agent's "role" for the task.
    """
    project = await self.db.get_project(task.project_id)
    profile_id = task.profile_id or (project.default_profile_id if project else None)
    if not profile_id:
        return None

    if project:
        scoped = await self.db.get_profile(f"project:{project.id}:{profile_id}")
        if scoped:
            return scoped
    return await self.db.get_profile(profile_id)
```

- [ ] **Step 3: Verify imports are still needed (or remove unused ones)**

Run: `grep -nE 'agent_type|default_agent_type' src/orchestrator/core.py`
Expected: no remaining matches in the resolve_profile function or its docstrings.

- [ ] **Step 4: Smoke**

Run: `python -c "from src.orchestrator import core"`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Rewrite Orchestrator._resolve_profile to use profile_id end-to-end

Eliminates the dual-purpose agent_type indirection. Project-scoped
profile feature keeps working via project:{pid}:{profile_id} lookup.
Cascade is now: task.profile_id → project.default_profile_id → None,
with project-scoped variant beating global at each level."
```

### Task 1.7: Update `commands/project_commands.py`

**Files:**
- Modify: `src/commands/project_commands.py`

- [ ] **Step 1: List the sites**

Run: `grep -nE 'default_agent_type' src/commands/project_commands.py`
Expected: lines 305-315 (edit handler), 323 (error message), 490-491 (info display).

- [ ] **Step 2: Delete the edit handler block**

Lines 305-315 read like:
```python
if "default_agent_type" in args:
    dat = args["default_agent_type"]
    # ... validation ...
    updates["default_agent_type"] = dat
```
Delete the entire block.

- [ ] **Step 3: Update the error message at line 323**

Find the error string mentioning `"default_profile_id, default_agent_type, or repo_default_branch."` — remove `default_agent_type, ` from the message.

- [ ] **Step 4: Delete the info-display lines 490-491**

```python
if project.default_agent_type:
    info["default_agent_type"] = project.default_agent_type
```
Delete both lines.

- [ ] **Step 5: Verify**

Run: `grep -nE 'default_agent_type' src/commands/project_commands.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/commands/project_commands.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "project_commands: drop default_agent_type set/info handlers"
```

### Task 1.8: Update `commands/task_commands.py` (full enumeration)

**Files:**
- Modify: `src/commands/task_commands.py`

- [ ] **Step 1: List ALL agent_type sites in this file**

Run: `grep -nE 'agent_type' src/commands/task_commands.py`
Expected: roughly the following hits (line numbers approximate):
- 855-865: validation block reading `args.get("agent_type")`
- 866-871: comment block lampshading the agent_type/profile distinction
- 927: `agent_type=agent_type` kwarg in `Task(...)` constructor
- 986-987: `if agent_type:` + `result["agent_type"] = agent_type` in response
- 1044: `"agent_type": task.agent_type,` in response dict
- 1315-1324: edit-path validation + `updates["agent_type"] = val`
- 1365: docstring mentioning `agent_type` as an editable field

- [ ] **Step 2: Delete each block**

For each line range, Edit the file to delete the agent_type-related logic. After this task, no reference to `agent_type` should remain in `task_commands.py`.

- [ ] **Step 3: Verify**

Run: `grep -nE 'agent_type' src/commands/task_commands.py`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add src/commands/task_commands.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "task_commands: drop all agent_type handling (validation, kwarg, response, edit, docs)"
```

### Task 1.9: Update remaining src/ call sites

**Files:**
- Modify: `src/scheduler.py` (any remaining after Task 1.5)
- Modify: `src/orchestrator/execution.py`
- Modify: `src/runtimes/supervisor.py`
- Modify: `src/mcp_interfaces.py`
- Modify: `src/workflow_pipeline_view.py`
- Modify: `src/cli/formatters.py`
- Modify: `src/discord/commands.py`
- Modify: `src/tools/definitions.py`
- Plus any others surfaced by the grep below

- [ ] **Step 1: Surface remaining `agent.agent_type` and `task.agent_type` accesses (separate pass to avoid masking)**

Run: `grep -rEn '\bagent\.agent_type\b|\btask\.agent_type\b' src/ --include='*.py' | grep -v '\.pyc'`
Expected: a list of remaining files. Each match is a direct field access.

- [ ] **Step 2: Surface remaining `agent_type=` constructor kwargs (separate pass)**

Run: `grep -rEn '\bagent_type=' src/ --include='*.py' | grep -v '\.pyc' | grep -v 'AGENT_TYPE_COLORS\|max_agents_by_type'`
Expected: a list. Note: do NOT also `-v 'default_agent_type'` here — that filter would mask `core.py:519`-style lines that contain BOTH substrings. After Task 1.6 there shouldn't be any such lines, but the pass is still defensive.

- [ ] **Step 3: For each surfaced file, apply targeted Edits**

For `agent.agent_type` accesses → rename to `agent.profile_id`.
For `agent_type="supervisor"` in `runtimes/supervisor.py:516,527` → rename to `profile_id="supervisor"`.
For `task.agent_type` accesses (e.g. `workflow_pipeline_view.py:489`) → delete the line/kwarg.
For Discord `/edit` slash-command `agent_type` parameter at `discord/commands.py:3308,3311` → remove the parameter from the signature and choices.
For `tools/definitions.py` JSON-schema `"agent_type"` keys at lines 640, 923, 2736, 2757, 2777, 2808 → remove each key from its task tool's input schema.
For `tools/definitions.py:2727` docstring → change `project:<pid>:<agent_type>` to `project:<pid>:<profile_id>`.

Do **not** touch (these use the substring for unrelated identifiers):
- `vault_manager.py:260` — `if scope == "agent_type"` is a vault scope name string
- `playbooks/store.py:104` — same
- `override_handler.py` — uses `agent_type` as a directory-name placeholder for profile id; the path `vault/agent-types/{agent_type}/` stays as-is per the spec
- `vault.py:1485` — `ensure_default_agent_type_playbooks` is about the `default_agent_type_playbooks/` directory in `src/prompts/`, unrelated to the column

- [ ] **Step 4: Verify**

Run: `grep -rEn '\bagent\.agent_type\b|\btask\.agent_type\b|\bproject\.default_agent_type\b' src/ --include='*.py' | grep -v '\.pyc'`
Expected: no output.

Run: `grep -rEn '\bagent_type=' src/ --include='*.py' | grep -v '\.pyc' | grep -v 'AGENT_TYPE_COLORS\|max_agents_by_type'`
Expected: no output.

- [ ] **Step 5: Smoke imports**

Run: `python -c "from src.orchestrator import core, execution; from src.runtimes import supervisor; from src import mcp_interfaces, workflow_pipeline_view; from src.discord import commands; from src.tools import definitions; from src.cli import formatters"`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add -A
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Update remaining call sites for agent.agent_type → profile_id

Targets: orchestrator/execution, runtimes/supervisor, mcp_interfaces,
workflow_pipeline_view, cli/formatters, discord/commands,
tools/definitions. Removes Discord/MCP agent_type task parameters.
Updates project-scoped profile id docstring to use profile_id key.

vault_manager.py / override_handler.py / playbooks/store.py untouched —
their 'agent_type' references are vault scope names or playbook
directory paths, not the renamed field.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 1.10: Update tests for the rename and field drops

**Files:**
- Modify: `tests/test_database.py`
- Modify: `tests/test_database_postgresql.py`
- Modify: any other test surfaced below

- [ ] **Step 1: Surface tests touching renamed/dropped fields (two-pass to avoid masking)**

Run: `grep -rEn '\bagent\.agent_type\b|\btask\.agent_type\b|\.default_agent_type\b' tests/ --include='*.py'`
Then: `grep -rEn '\bagent_type=|\bdefault_agent_type=' tests/ --include='*.py' | grep -v 'AGENT_TYPE_COLORS\|max_agents_by_type'`
Expected: a list, mostly constructor calls and assertions.

- [ ] **Step 2: Apply targeted updates per file**

For `Agent(agent_type="claude-1")` → `Agent(profile_id="claude-1")`.
For `Task(agent_type=...)` → drop the kwarg entirely.
For `Project(default_agent_type=...)` → drop the kwarg entirely.
For tests asserting `_task_agent_type_matches` behavior → delete the test (function is gone).
For tests accessing `task.agent_type` or `project.default_agent_type` → drop the assertion (fields are gone).

- [ ] **Step 3: Run only the database tests as a focused checkpoint**

Run: `pytest tests/test_database.py -v 2>&1 | tail -25`
Expected: all pass.

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/ -n auto -x 2>&1 | tail -40`
Expected: all pass. If a failure references missed call sites, fix and re-run.

- [ ] **Step 5: Commit**

```bash
git add -A
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "tests: update for agents.agent_type → profile_id; drop Task.agent_type and Project.default_agent_type assertions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 1.11: Phase 1 checkpoint

- [ ] Run: `pytest tests/ -n auto 2>&1 | tail -10`
Expected: full suite passes.

- [ ] Run: `ruff check src/ tests/ 2>&1 | tail -5`
Expected: clean (or only stylistic warnings).

- [ ] Run the daemon as smoke:

```bash
aq restart --no-dashboard
sleep 10
sqlite3 ~/.agent-queue/agent-queue.db ".schema agents" | grep profile_id
```

Expected: daemon starts without errors; agents table has `profile_id` column. (No agents will be created yet — reconciler isn't wired.)

---

## Phase 2 — `AgentReconciler` skeleton + unit tests (TDD)

Build the reconciler one decision rule at a time, test-first. Per-project agent attribution is set up in Task 2.2 so subsequent rules don't need to refactor it.

### Task 2.1: Define `ReconcileReport` and the empty `AgentReconciler`

**Files:**
- Create: `src/orchestrator/agent_reconciler.py`
- Create: `tests/test_agent_reconciler.py`

- [ ] **Step 1: Write the first failing test (no-op case)**

Create `tests/test_agent_reconciler.py`:

```python
"""Unit tests for AgentReconciler — see
docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §7.

Uses the same `db` fixture pattern as tests/test_database.py:
file-backed SQLite under tmp_path, with `Database.initialize()`.
"""
from __future__ import annotations

import pytest

from src.database import Database
from src.orchestrator.agent_reconciler import AgentReconciler


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_no_op_when_no_projects(db):
    reconciler = AgentReconciler(db)
    report = await reconciler.reconcile()
    assert report.created == []
    assert report.reassigned == []
    assert report.skipped == []
```

- [ ] **Step 2: Run, confirm fails**

Run: `pytest tests/test_agent_reconciler.py::test_no_op_when_no_projects -v 2>&1 | tail -10`
Expected: FAIL with `ImportError: cannot import name 'AgentReconciler'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/orchestrator/agent_reconciler.py`:

```python
"""Lazy agent supply — see
docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §4.1.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Outcome of one AgentReconciler.reconcile() pass."""
    created: list[tuple[str, str]] = field(default_factory=list)  # [(project_id, profile_id)]
    reassigned: list[tuple[str, str, str]] = field(default_factory=list)  # [(agent_id, old, new)]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # [(project_id, reason)]


class AgentReconciler:
    """Lazy-creates agent rows so the scheduler always has idle slots
    when there's dispatchable work. Called once per orchestrator tick,
    before Scheduler.schedule(). Does not assign tasks — only ensures
    supply matches demand subject to project.max_concurrent_agents.
    """

    def __init__(self, db: Database):
        self._db = db
        self._warned_projects: dict[str, str] = {}  # dedup for "no resolvable profile" warnings

    async def reconcile(self) -> ReconcileReport:
        return ReconcileReport()
```

- [ ] **Step 4: Run, confirm passes**

Run: `pytest tests/test_agent_reconciler.py::test_no_op_when_no_projects -v 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/agent_reconciler.py tests/test_agent_reconciler.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Add AgentReconciler skeleton with no-op pass

ReconcileReport dataclass + empty AgentReconciler.reconcile().
Following TDD; subsequent commits add one decision rule at a time."
```

### Task 2.2: Happy path with per-project agent attribution

Establishes the per-project counting infrastructure now so Tasks 2.3+ build on it instead of refactoring.

**Files:**
- Modify: `tests/test_agent_reconciler.py`
- Modify: `src/orchestrator/agent_reconciler.py`

- [ ] **Step 1: Add a fixture helper**

Append to `tests/test_agent_reconciler.py`:

```python
import time as _time
from src.models import (
    Project, Task, Agent, AgentProfile, Workspace,
    TaskStatus, AgentState, ProjectStatus, RepoSourceType,
)


async def _seed_project_with_profile(db, *, project_id, profile_id, max_agents=2,
                                      runtime="claude_sdk", workspace_count=1):
    """Create a project with a default profile and N enabled workspaces."""
    # AgentProfile requires only `id` and `name`; other fields default
    # (description="", model="", permission_mode="", allowed_tools=[],
    # mcp_servers=[], system_prompt_suffix="", install={}). The `runtime`
    # field was added in a recent migration; verify it's still keyword-able.
    await db.create_agent_profile(AgentProfile(
        id=profile_id, name=profile_id, runtime=runtime,
    ))
    await db.create_project(Project(
        id=project_id, name=project_id,
        default_profile_id=profile_id,
        max_concurrent_agents=max_agents,
        status=ProjectStatus.ACTIVE,
        credit_weight=1.0, total_tokens_used=0,
        created_at=_time.time(),
    ))
    for i in range(workspace_count):
        await db.create_workspace(Workspace(
            id=f"ws-{project_id}-{i}", project_id=project_id,
            workspace_path=f"/tmp/{project_id}-{i}",
            source_type=RepoSourceType.LINK, enabled=True,
            created_at=_time.time(),
        ))


async def _seed_ready_task(db, *, task_id, project_id, profile_id=None, priority=100):
    """Create a READY task with optional explicit profile_id."""
    await db.create_task(Task(
        id=task_id, project_id=project_id,
        title=task_id, description=task_id,
        status=TaskStatus.READY, priority=priority,
        profile_id=profile_id,
        created_at=_time.time(), updated_at=_time.time(),
    ))
```

(If model constructor signatures differ, run `python -c "from src.models import Project, Task, AgentProfile, Workspace; help(Project)"` to see required vs default fields, then adjust.)

- [ ] **Step 2: Write the failing happy-path test**

```python
async def test_creates_one_agent_for_one_ready_task(db):
    await _seed_project_with_profile(db, project_id="p", profile_id="claude-opus")
    await _seed_ready_task(db, task_id="t-1", project_id="p")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].profile_id == "claude-opus"
    assert agents[0].state == AgentState.IDLE
    assert report.created == [("p", "claude-opus")]
```

- [ ] **Step 3: Run, confirm fails**

Run: `pytest tests/test_agent_reconciler.py::test_creates_one_agent_for_one_ready_task -v 2>&1 | tail -10`
Expected: FAIL — `len(agents) == 1` fails (still 0).

- [ ] **Step 4: Implement the happy path with per-project attribution**

Replace `AgentReconciler.reconcile()` body:

```python
async def reconcile(self) -> ReconcileReport:
    import uuid
    import time as _t
    from src.models import Agent, AgentState, TaskStatus, ProjectStatus

    report = ReconcileReport()

    projects = await self._db.list_projects()
    tasks = await self._db.list_tasks()
    agents = await self._db.list_agents()
    profiles = {p.id: p for p in await self._db.list_agent_profiles()}
    workspaces = await self._db.list_workspaces()

    # Build per-agent project attribution: BUSY agents via current_task_id;
    # IDLE agents via the workspace they currently lock (if any).
    ws_owner: dict[str, str] = {}  # agent_id -> project_id
    for w in workspaces:
        if w.locked_by_agent_id:
            ws_owner[w.locked_by_agent_id] = w.project_id

    agents_by_project: dict[str, list] = {}
    unassigned_idle: list = []
    for a in agents:
        pid = None
        if a.current_task_id:
            t = await self._db.get_task(a.current_task_id)
            if t:
                pid = t.project_id
        if pid is None:
            pid = ws_owner.get(a.id)
        if pid is None:
            if a.state == AgentState.IDLE:
                unassigned_idle.append(a)
            continue
        agents_by_project.setdefault(pid, []).append(a)

    # Group READY tasks by project
    ready_by_project: dict[str, list] = {}
    for t in tasks:
        if t.status == TaskStatus.READY:
            ready_by_project.setdefault(t.project_id, []).append(t)

    for project in projects:
        if project.status != ProjectStatus.ACTIVE:
            continue
        ready = ready_by_project.get(project.id, [])
        if not ready:
            continue

        # Resolve unique profile_ids needed
        needed_profiles: set[str] = set()
        for t in ready:
            pid = t.profile_id or project.default_profile_id
            if pid:
                needed_profiles.add(pid)
        if not needed_profiles:
            self._warn_once(project.id, "no resolvable profile_id")
            report.skipped.append((project.id, "no resolvable profile_id"))
            continue

        project_agents = agents_by_project.get(project.id, [])
        existing_profiles = {a.profile_id for a in project_agents if a.state == AgentState.IDLE}

        for needed in needed_profiles:
            if needed in existing_profiles:
                continue
            # Try create
            if len(project_agents) < project.max_concurrent_agents:
                # Adopt one unassigned-idle if available; else create
                if unassigned_idle:
                    adopted = unassigned_idle.pop(0)
                    await self._db.update_agent(adopted.id, profile_id=needed)
                    report.reassigned.append((adopted.id, adopted.profile_id, needed))
                    project_agents.append(adopted)
                    existing_profiles.add(needed)
                    continue
                agent = Agent(
                    id=f"agent-{uuid.uuid4().hex[:12]}",
                    name=f"{needed}-{len(agents) + 1}",
                    profile_id=needed,
                    state=AgentState.IDLE,
                    created_at=_t.time(),
                )
                await self._db.create_agent(agent)
                agents.append(agent)
                project_agents.append(agent)
                existing_profiles.add(needed)
                report.created.append((project.id, needed))

    return report

def _warn_once(self, project_id: str, reason: str) -> None:
    if self._warned_projects.get(project_id) == reason:
        return
    self._warned_projects[project_id] = reason
    logger.warning(
        "reconciler: project=%s has READY tasks but %s", project_id, reason
    )
```

- [ ] **Step 5: Run, confirm passes**

Run: `pytest tests/test_agent_reconciler.py -v 2>&1 | tail -10`
Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/agent_reconciler.py tests/test_agent_reconciler.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "AgentReconciler: happy path + per-project agent attribution

Introduces the workspace-lock-based agent-to-project attribution
upfront so subsequent decision rules build on it without refactoring."
```

### Task 2.3: Profile-resolution failure → skip + dedup-warn

**Files:**
- Modify: `tests/test_agent_reconciler.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_skips_when_no_resolvable_profile_id(db, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    # Project WITHOUT default_profile_id, task WITHOUT profile_id
    await db.create_project(Project(
        id="p", name="p", max_concurrent_agents=1,
        status=ProjectStatus.ACTIVE, credit_weight=1.0,
        total_tokens_used=0, created_at=_time.time(),
    ))
    await _seed_ready_task(db, task_id="t", project_id="p")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 0
    assert report.skipped == [("p", "no resolvable profile_id")]
    assert any("no resolvable profile_id" in rec.message for rec in caplog.records)

    # Dedup: second pass should not log again
    caplog.clear()
    await AgentReconciler(db).reconcile()
    # Note: each test makes a fresh AgentReconciler so the dedup dict resets;
    # to test dedup, reuse the same instance:
    rec = AgentReconciler(db)
    await rec.reconcile()
    caplog.clear()
    await rec.reconcile()
    assert not any("no resolvable profile_id" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run, confirm passes** (the implementation from Task 2.2 already handles this; the test validates the contract).

If it fails: investigate; the warning may not be at the right level or the skipped tuple may be wrong.

- [ ] **Step 3: Commit**

```bash
git add tests/test_agent_reconciler.py
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "AgentReconciler test: profile-resolution failure skips + warns once"
```

### Task 2.4: Multiple profiles, under cap

**Files:**
- Modify: `tests/test_agent_reconciler.py`

- [ ] **Step 1: Write the test**

```python
async def test_creates_one_agent_per_profile_under_cap(db):
    await _seed_project_with_profile(db, project_id="p",
                                     profile_id="claude-opus", max_agents=2)
    await db.create_agent_profile(AgentProfile(
        id="claude-sonnet", name="claude-sonnet", runtime="claude_sdk",
    ))
    await _seed_ready_task(db, task_id="t-opus", project_id="p", profile_id="claude-opus")
    await _seed_ready_task(db, task_id="t-son", project_id="p", profile_id="claude-sonnet")

    await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 2
    assert {a.profile_id for a in agents} == {"claude-opus", "claude-sonnet"}
```

- [ ] **Step 2: Run, confirm passes (or fix).**

- [ ] **Step 3: Commit.**

### Task 2.5: At-cap reassignment

**Files:**
- Modify: `tests/test_agent_reconciler.py`
- Modify: `src/orchestrator/agent_reconciler.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_reassigns_at_cap(db):
    await _seed_project_with_profile(db, project_id="p",
                                     profile_id="claude-opus", max_agents=1)
    await db.create_agent_profile(AgentProfile(
        id="claude-sonnet", name="claude-sonnet", runtime="claude_sdk",
    ))
    # Pre-existing idle opus agent locked to the project's only workspace
    await db.create_agent(Agent(
        id="agent-1", name="opus-1", profile_id="claude-opus",
        state=AgentState.IDLE, created_at=_time.time(),
    ))
    # Lock the workspace to that agent so attribution works.
    # `db.update_workspace(ws_id, **fields)` accepts arbitrary kwargs
    # (see src/database/queries/workspace_queries.py:43). We're setting
    # locked_by_agent_id directly rather than using acquire_workspace
    # because acquire_workspace requires a task_id we don't want to set.
    workspaces = await db.list_workspaces()
    await db.update_workspace(workspaces[0].id, locked_by_agent_id="agent-1")
    # Sonnet task arrives
    await _seed_ready_task(db, task_id="t-son", project_id="p", profile_id="claude-sonnet")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].profile_id == "claude-sonnet"
    assert report.reassigned == [("agent-1", "claude-opus", "claude-sonnet")]
```

(If `db.update_workspace` doesn't take `locked_by_agent_id` kwarg, use the actual workspace lock API — read `src/database/queries/workspace_queries.py` to find it. Likely `db.acquire_workspace(...)` with specific args.)

- [ ] **Step 2: Run, confirm fails.**

- [ ] **Step 3: Add at-cap reassignment to the reconciler**

In `agent_reconciler.py`, inside the `for needed in needed_profiles:` loop, after the "under-cap create" branch, add:

```python
            # At cap — try to reassign an idle agent of a different profile.
            # Prefer agents whose profile_id is no longer in agent_profiles
            # (orphan profiles); else any idle.
            idle_in_project = [
                a for a in project_agents
                if a.state == AgentState.IDLE and a.profile_id != needed
            ]
            if not idle_in_project:
                continue  # truly stuck; next tick retries
            # Pick orphan-profile target first, else first idle
            orphan = [a for a in idle_in_project if a.profile_id not in profiles]
            target = orphan[0] if orphan else idle_in_project[0]
            old = target.profile_id
            await self._db.update_agent(target.id, profile_id=needed)
            target.profile_id = needed  # mutate in-memory copy too
            report.reassigned.append((target.id, old, needed))
            existing_profiles.add(needed)
```

- [ ] **Step 4: Run, confirm passes. Commit.**

### Task 2.6: Reassignment cap (1 per agent per tick)

**Files:**
- Modify: `tests/test_agent_reconciler.py`
- Modify: `src/orchestrator/agent_reconciler.py`

- [ ] **Step 1: Write the failing test** — 1 idle agent, capacity=1, three ready tasks needing three different profiles → exactly 1 reassignment, 2 tasks remain blocked.

- [ ] **Step 2: Confirm fails** (current logic could reassign the same agent up to 3 times).

- [ ] **Step 3: Add a `reassigned_this_tick: set[str]` guard** at the top of `reconcile()`, and skip an agent that's already in it before considering it as a reassignment target.

- [ ] **Step 4: Run, confirm passes. Commit.**

### Task 2.7: Workspace-required runtime, no available workspace → no creation

**Files:**
- Modify: `tests/test_agent_reconciler.py`
- Modify: `src/orchestrator/agent_reconciler.py`

- [ ] **Step 1: Write the failing test** — project with profile (`runtime='claude_sdk'`), 0 enabled-and-unlocked workspaces (e.g. all locked, OR `workspace_count=0`), 1 ready task → 0 agents created, `report.skipped` includes a "no available workspace" entry.

- [ ] **Step 2: Confirm fails.**

- [ ] **Step 3: Add the workspace gate.** In the reconciler, before deciding to create, check the runtime's `requires_workspace`:

```python
from src.runtimes import RuntimeRegistry
# ...
profile_obj = profiles.get(needed)
if profile_obj is None:
    # Profile referenced but not loaded — skip create
    report.skipped.append((project.id, f"profile {needed} missing"))
    continue
runtime_cls = RuntimeRegistry.get_class(profile_obj.runtime)
if runtime_cls and runtime_cls.requires_workspace:
    avail = await self._db.count_available_workspaces(project.id)
    if avail == 0:
        report.skipped.append((project.id, f"no available workspace for {needed}"))
        continue
```

(Adjust `RuntimeRegistry.get_class` to whatever the actual API is — read `src/runtimes/__init__.py` to confirm.)

- [ ] **Step 4: Run, confirm passes. Commit.**

### Task 2.8: No-workspace runtime (supervisor-style) → creates regardless

**Files:**
- Modify: `tests/test_agent_reconciler.py`

- [ ] **Step 1: Write the test** — project with a profile whose `runtime='supervisor'` (`requires_workspace=False`), 0 workspaces → still creates 1 agent.

- [ ] **Step 2: Run, confirm passes** (no implementation change needed if Task 2.7 is correct). Commit.

### Task 2.9: Orphan BUSY (state=BUSY, current_task_id=None or task missing)

**Files:**
- Modify: `tests/test_agent_reconciler.py`
- Modify: `src/orchestrator/agent_reconciler.py`

- [ ] **Step 1: Write the failing test** — start with 2 BUSY agents (capacity=2): one with `current_task_id` pointing at a deleted task, one healthy. Add a new READY task → reconciler resets the orphan to IDLE and reassigns it to the needed profile.

- [ ] **Step 2: Confirm fails.**

- [ ] **Step 3: At the top of `reconcile()`, add an orphan-BUSY sweep:**

```python
for a in agents:
    if a.state == AgentState.BUSY:
        ok = False
        if a.current_task_id:
            t = await self._db.get_task(a.current_task_id)
            ok = t is not None
        if not ok:
            await self._db.update_agent(a.id, state=AgentState.IDLE, current_task_id=None)
            a.state = AgentState.IDLE
            a.current_task_id = None
            logger.warning("reconciler: reset orphan BUSY agent %s", a.id)
```

- [ ] **Step 4: Run, confirm passes. Commit.**

### Task 2.10: Orphan profile_id → preferred reassignment target

**Files:**
- Modify: `tests/test_agent_reconciler.py`

- [ ] **Step 1: Write the failing test** — at cap with 2 idle agents, one valid (`profile_id='claude-opus'`), one orphan (`profile_id='deleted-profile'` not in `agent_profiles`). New READY task needs `claude-sonnet` → reconciler reassigns the orphan, leaves the valid one untouched.

- [ ] **Step 2: Confirm passes** (Task 2.5's reassignment-target selection already prefers orphans). If it fails, fix the selection logic.

- [ ] **Step 3: Commit.**

### Task 2.11: Phase 2 checkpoint

- [ ] Run: `pytest tests/test_agent_reconciler.py -v 2>&1 | tail -20`
Expected: all 10 reconciler tests pass.

- [ ] Run: `pytest tests/ -n auto 2>&1 | tail -10`
Expected: full suite passes (the reconciler isn't wired yet, so existing behavior is unchanged).

---

## Phase 3 — Wire the reconciler into the orchestrator

### Task 3.1: Write the regression integration test (will fail until wired)

**Files:**
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Find the existing orchestrator test fixture pattern**

Run: `grep -nE '@pytest\.fixture|async def orch|Orchestrator\(' tests/test_orchestrator.py 2>&1 | head -20`
Expected: a fixture pattern. Use the same pattern for the new test.

- [ ] **Step 2: Add the regression test**

```python
async def test_ready_task_dispatches_with_only_workspace_and_default_profile(orch_with_db):
    """Regression for the original quick-ember bug: a project with workspaces
    and a default profile + a READY task should dispatch within one cycle,
    no manual `aq agent create`. Tests the full reconciler → scheduler →
    executor chain. See spec 2026-05-07-agent-reconciliation-design.md §7.
    """
    orch, db = orch_with_db
    # ... (use existing helpers in the test file to seed a project +
    # workspace + default profile + 1 READY task. Adjust to match the
    # file's existing test patterns.)

    await orch.run_one_cycle()

    task = await db.get_task("regression-task")
    # Real dispatch may go to ASSIGNED first then IN_PROGRESS depending on
    # how execution is mocked in the test. Accept either non-READY state.
    assert task.status != TaskStatus.READY
    agents = await db.list_agents()
    assert len(agents) == 1
```

- [ ] **Step 3: Run, confirm fails**

Run: `pytest tests/test_orchestrator.py::test_ready_task_dispatches_with_only_workspace_and_default_profile -v 2>&1 | tail -15`
Expected: FAIL — task stays READY.

### Task 3.2: Wire `AgentReconciler` into `Orchestrator.__init__` and `_schedule()`

**Files:**
- Modify: `src/orchestrator/core.py`

- [ ] **Step 1: Add the import**

Near the top of `src/orchestrator/core.py`:

```python
from src.orchestrator.agent_reconciler import AgentReconciler
```

- [ ] **Step 2: Add the instance attribute in `Orchestrator.__init__`**

Find `Orchestrator.__init__` (around line 196). After `self._db` is set, add:

```python
self._agent_reconciler = AgentReconciler(self._db)
```

- [ ] **Step 3: Call the reconciler at the top of `_schedule()`**

Find `async def _schedule(self)` (around line 1804). At its start, before any DB reads, add:

```python
report = await self._agent_reconciler.reconcile()
if report.created or report.reassigned:
    logger.info(
        "reconciler: created=%d reassigned=%d skipped=%d",
        len(report.created), len(report.reassigned), len(report.skipped),
    )
```

- [ ] **Step 4: Run the regression test, confirm passes**

Run: `pytest tests/test_orchestrator.py::test_ready_task_dispatches_with_only_workspace_and_default_profile -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 5: Run the full orchestrator test file**

Run: `pytest tests/test_orchestrator.py -v 2>&1 | tail -30`
Expected: all pass. If any existing test now fails because agents auto-create, update those tests to either disable reconciliation in setup OR adapt assertions.

- [ ] **Step 6: Commit**

```bash
git add -A
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Wire AgentReconciler into orchestrator tick

Reconciler runs at the top of Orchestrator._schedule(), before
SchedulerState is built. Adds the regression integration test that
mirrors the original quick-ember bug.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3.3: Add multi-tick profile-reassignment integration test

**Files:**
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Add the test** — project at capacity with one idle opus agent, submit a sonnet task → next cycle reassigns the agent → following cycle dispatches.

- [ ] **Step 2: Run, confirm passes** (logic is in place from Phase 2).

- [ ] **Step 3: Commit.**

### Task 3.4: Extend the startup cleanup pass at `core.py:1349`

**Files:**
- Modify: `src/orchestrator/core.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Read the existing pass**

Read `src/orchestrator/core.py` lines 1340-1360. The current loop resets BUSY agents whose tasks are gone.

- [ ] **Step 2: Add three new behaviors:**

(a) Idle agents whose `profile_id` doesn't exist in `agent_profiles` → delete via `db.delete_agent(agent.id)`.
(b) Idle agents that exceed `project.max_concurrent_agents` for their attributed project → delete (oldest first by `created_at`).
(c) `state=BUSY AND (current_task_id IS NULL OR task missing)` → reset to IDLE (already handled by reconciler mid-run; keep at startup).

- [ ] **Step 3: Add tests** — for each new behavior, seed the DB with the relevant scenario and assert the cleanup pass produces the expected state.

- [ ] **Step 4: Run, confirm passes. Commit.**

### Task 3.5: Phase 3 checkpoint

- [ ] Run: `pytest tests/ -n auto 2>&1 | tail -10`
Expected: full suite passes.

- [ ] Run the daemon as smoke:

```bash
aq restart --no-dashboard
sleep 8
sqlite3 ~/.agent-queue/agent-queue.db "SELECT id, profile_id, state FROM agents;"
```

Expected: at least one agent row appears for atom-claude with `profile_id='claude-opus'`. Discord shows quick-ember moving to ASSIGNED or IN_PROGRESS.

---

## Phase 4 — Remove deprecated CLI commands

### Task 4.1: Delete the deprecation stubs

**Files:**
- Modify: `src/commands/agent_commands.py`

- [ ] **Step 1: List the stubs**

Run: `grep -nE 'no longer supported|Deprecated' src/commands/agent_commands.py`
Expected: stubs for `_cmd_create_agent`, `_cmd_edit_agent`, `_cmd_delete_agent`, `_cmd_pause_agent`, `_cmd_resume_agent`.

- [ ] **Step 2: Delete each stub method and its CommandHandler registration.**

Look at how commands register (likely a dict in `__init__` or via a decorator). Remove the entries for `create_agent`, `edit_agent`, `delete_agent`, `pause_agent`, `resume_agent`.

- [ ] **Step 3: Run the test suite**

Run: `pytest tests/ -n auto 2>&1 | tail -15`
Expected: most pass. Tests that exercised the deprecated commands directly will fail with "command not found."

### Task 4.2: Remove the corresponding CLI subcommands

**Files:**
- Modify: `src/cli/agent.py` (or wherever the click commands live)

- [ ] **Step 1: Find them**

Run: `grep -rnE '@.*command\("create"\)|@.*command\("pause"\)' src/cli/`
(Adjust if registration uses a different decorator pattern.)

- [ ] **Step 2: Delete each click command function for the deprecated agent operations.**

- [ ] **Step 3: Verify CLI help no longer lists them**

Run: `aq agent --help 2>&1 | grep -E 'create|edit|delete|pause|resume'`
Expected: no output.

### Task 4.3: Delete tests of the deprecated commands

**Files:**
- Modify: any test file surfaced in Task 4.1 step 3

- [ ] **Step 1:** For each failing test, decide:
  - Test exercises the deprecated CLI command → delete.
  - Test uses something else but trips → fix.

- [ ] **Step 2: Run the suite, confirm green.**

- [ ] **Step 3: Commit Phase 4**

```bash
git add -A
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Remove deprecated aq agent create/edit/delete/pause/resume

These returned errors after the workspace-as-agent rewrite. Their
replacement is the AgentReconciler — agent rows materialize
automatically. CLI help no longer advertises them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5 — Doc updates

### Task 5.1: Update spec documents

**Files:**
- Modify: `docs/specs/scheduler-and-budget.md`
- Modify: `docs/specs/models-and-state-machine.md`
- Modify: `docs/specs/design/agent-coordination.md`

- [ ] **Step 1: `scheduler-and-budget.md`** — add a paragraph (under the existing scheduling-cycle description) describing the reconciler step that runs first.

- [ ] **Step 2: `models-and-state-machine.md`** — update Agent / Task field references from `agent_type` to `profile_id`. Note that `task.agent_type` is gone (the coordination-category-filter feature was unimplemented and dropped). Reflect the reconciler-as-supplier mental model.

- [ ] **Step 3: `design/agent-coordination.md`** — add a brief note that the agent/workspace relationship is *workspace-as-resource, agent-as-project-slot*; reconciler ensures supply. Mark the category-filter design (`task.agent_type` examples like "coding"/"qa") as **deferred / not yet implemented** so the doc accurately reflects state.

### Task 5.2: Final commit

- [ ] Run: `pytest tests/ -n auto 2>&1 | tail -10`
Expected: all green.

- [ ] Run: `ruff check src/ tests/ && ruff format --check src/ tests/`
Expected: clean.

- [ ] Commit:

```bash
git add -A
git -c user.name="Jack Kern" -c user.email="jack.w.kern@gmail.com" commit -m "Update specs for reconciler + agent.profile_id rename

scheduler-and-budget gains the reconciler step description.
models-and-state-machine reflects the rename and the dropped
Task.agent_type field. agent-coordination.md marks the
category-filter examples as deferred.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final checks before opening the PR

- [ ] **Full suite green**: `pytest tests/ -n auto`
- [ ] **Lint clean**: `ruff check src/ tests/ && ruff format --check src/ tests/`
- [ ] **Migration round-trip**: from a clean checkout, `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head` all succeed
- [ ] **Smoke**: `aq restart --no-dashboard` → after ~10s, `sqlite3 ~/.agent-queue/agent-queue.db "SELECT id, profile_id, state FROM agents;"` shows auto-created rows for atom-claude
- [ ] **Discord sanity**: in `#aq-control`, run `/status` for atom-claude and confirm `quick-ember` shows IN_PROGRESS or COMPLETED
- [ ] **PR body** mentions:
  - Breaking: external clients querying `agents.agent_type` directly will break (typed SDK regenerates)
  - Breaking: external clients passing `agent_type=` to create_task / edit_task will get rejected
  - Dropped: `tasks.agent_type`, `archived_tasks.agent_type`, `_task_agent_type_matches()`, `aq agent create / edit / delete / pause / resume`
  - Links: spec at `docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md`
