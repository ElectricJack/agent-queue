# Graph Visibility, Agent Markers and In-Task Subtasks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the task graph readable on landing (phases, no root clutter, an active default view, progress bars), make agent markers mean "a live agent is working this now", and let one agent tick off durable subtasks inside a single task.

**Architecture:** Three independent lanes. **B** re-derives graph worker markers from live session attempts (the predicate the digest already trusts) and makes the client treat a tiles response as authoritative. **C** adds a `task_subtasks` child table (modelled on `task_comments`) with four worker-facing commands, a prime section and a close refusal. **A** builds phases out of existing parts — a phase is a container task plus one `blocks` edge to the previous phase, so gating falls out of the persisted `is_blocked` projection with no change to the claim frontier — then adds a keyed standing parent for non-worker task creation, a server-computed "active" expanded set, and progress bars.

**Tech Stack:** Python 3.12 async, SQLAlchemy Core, PostgreSQL, Alembic; React + TypeScript + React Flow + vitest in `dashboard/`.

**Spec:** `docs/superpowers/specs/2026-09-19-visibility-subtasks-and-evaluation-planning.md` (workstreams A, B, C; D and E are out of scope here).

## What the research changed

Findings that reshape the planning doc's assumptions (all verified in code, 2026-09-19):

- **Markers are not drawn from `tasks.assigned_agent_id`.** They come from `agents.current_task_id`, scanned unfiltered in `src/api/graph_layout.py:461-482` → `dock_workers` (`src/task_graph/layout/view.py:206`). No status or liveness check exists server- or client-side, and `pruneAnnotations` (`dashboard/.../layout-v2/layoutStore.ts:111`) merges workers additively and never evicts one the server stopped reporting.
- **Several transitions never clear `agents.current_task_id`** (`skip_task`, sync-workflow completion, merge-conflict BLOCKED, conditional auto-close, reopen-cascade FAILED, retained-claim closes), and `agent_reconciler.py:78` skips any BUSY agent whose task still *exists*, at any status.
- **Worker filings already default to a child of the held task** (`task_commands.py:1816-1846`), siblings are allowed, and provenance is already recorded (`created_by_kind/id`, `discovered-from` edges). Root clutter comes from *non-session* creators (playbooks, supervisor).
- **Archival is already per finished root subtree** (`archive_queries.py:261`, 24 h), and the `active` layout variant already stubs finished containers and drops finished leaves. A4 needs no new mechanism once work stops landing in the root.
- **Nothing is expanded by default** (`expanded: []` server- and client-side) — this, not archival, is why landing on the graph shows little.
- **`MAX_STRUCTURAL_DEPTH = 3`.** phase → epic → task already uses it, so subtasks cannot be `tasks` rows. This decides C: a separate table.
- **Blockedness is a persisted projection** (`blocked_state.py`): a `blocks` edge keeps the dependent `is_blocked` until the prerequisite is COMPLETED; a blocked container stays DEFINED; a DEFINED parent withholds its children via the `parent-child` rule. So one edge between two phase containers gates the whole later phase, transitively.

## Global Constraints

- Work on branch `feat/graph-visibility` in a git worktree — **never** in `/home/jkern/dev/agent-queue2` itself (the daemon runs from that checkout). Use plain `/usr/bin/git` single commands. Commit only the paths your task owns; never `git add -A`.
- **Never** run `alembic upgrade`, `alembic stamp`, `aq start`, or `aq db upgrade`. Tests build their own databases. Export `POSTGRES_TEST_DSN` if the postgres half skips.
- Tests: `aq test <files>` for the files you touched; never bare `pytest tests/`, never raise `-n`. Exit code 75 = no slot, retry. Dashboard: `npx vitest run <path>` from `dashboard/`. If the worktree has no `node_modules`, symlink the main checkout's (`ln -s /home/jkern/dev/agent-queue2/node_modules node_modules`, same for `dashboard/`), and do not commit the link.
- `ruff check <changed paths>`; line length 100.
- PostgreSQL only: no `batch_alter_table`, no dialect branches. Name every `CheckConstraint`. `server_default` takes the bare value.
- New migration revisions must be no-ops on a fresh database (the squashed baseline builds from live metadata): guard every DDL with an inspector check, as `a0000000000a_add_task_comment_kind.py` does. Only Task C1 adds a revision: `a00000000010`, `down_revision = "a0000000000f"`. Confirm a single head with `tests/alembic_revisions.py` after writing it.
- After any change to `src/api/models/` or tool definitions: `./scripts/regenerate-api-client.sh --offline` then `./scripts/regenerate-ts-client.sh --from-file`; never hand-edit `packages/aq-client/`. **Only one task at a time may regenerate** — C2, C4, B1 and A3 touch the API surface and must do this step serially (the orchestrating session sequences them).
- Commands return `{"success": bool, ...}`; refusals use `{"success": False, "code": "<namespace>.<reason>", "error": "<prose naming the command to run>"}`.
- Policy lives in playbooks; code provides mechanism only.
- TDD: write the failing test, watch it fail, implement, watch it pass, commit.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

# Lane B — accurate agent markers

### Task B1: Dock markers from live attempts

**Files:**
- Modify: `src/database/queries/task_session_queries.py` (add query), `src/database/queries/digest_queries.py:244-282` (reuse the shared predicate)
- Modify: `src/api/graph_layout.py:461-482`
- Test: `tests/test_task_session_queries.py` (create if absent — check `pytest --co -q -k live_task_workers`), `tests/test_api_graph_layout.py`

**Interfaces:**
- Produces: `live_attempt_predicate(now: float, stale_after: float)` — module-level in `task_session_queries.py`, returning the SQLAlchemy `and_(...)` over `task_session_attempts` ⨝ `sessions` that `collect_digest_activity` uses today (attempt `ended_at IS NULL`, `state IN LIVE_ATTEMPT_STATES`, `sessions.state == "running"`, `sessions.ended_at IS NULL`, activity or start within `stale_after`).
- Produces: `async def list_live_task_workers(self, project_id: str, *, now: float | None = None, stale_after: float = <the digest's default>) -> list[dict]` returning `[{"id": agent_id, "name": agent_name, "current_task_id": task_id}]` — the exact dict shape `dock_workers` already consumes — joined to `tasks` and restricted to `tasks.status IN ('ASSIGNED','IN_PROGRESS')`, `agent_id IS NOT NULL`, one row per `(agent_id, task_id)` (latest `started_at`).

- [ ] **Step 1:** Failing query tests, using the local `db` fixture pattern (`Database(lease_dsn("test.db"))`): (a) a running session + open attempt on an IN_PROGRESS task is returned; (b) same attempt but task COMPLETED → not returned; (c) attempt `ended_at` set → not returned; (d) session `state="ended"` → not returned; (e) session `last_activity` older than `stale_after` and `started_at` older too → not returned; (f) another project's attempt → not returned.
- [ ] **Step 2:** Run `aq test tests/test_task_session_queries.py -x`; expect failures on the missing function.
- [ ] **Step 3:** Extract `live_attempt_predicate` from `digest_queries.py` (keep the digest's `started_at <= until` clause at its call site), implement `list_live_task_workers`, expose it on the Database facade the way `list_task_comments` is (`src/database/base.py`).
- [ ] **Step 4:** Failing API test in `tests/test_api_graph_layout.py`: an agent row with `current_task_id` pointing at a COMPLETED task and **no** live attempt yields `workers == []` in the tiles response; an agent with a live attempt on a visible IN_PROGRESS task yields one worker docked at it; a live attempt on a task hidden inside a collapsed container yields `in_collapsed=True` docked at the container.
- [ ] **Step 5:** In `graph_layout.py`, replace the `db.list_agents()` comprehension with `await db.list_live_task_workers(project_id)`. Leave `dock_workers` untouched.
- [ ] **Step 6:** `aq test tests/test_task_session_queries.py tests/test_api_graph_layout.py tests/test_digest_queries.py`; all pass (the digest suite proves the extraction was behaviour-preserving). Ruff. Commit `fix(graph): dock agent markers from live attempts, not agents.current_task_id`.

### Task B2: The tiles response is authoritative for the workers it covers

**Files:**
- Modify: `dashboard/src/pages/command-center/layout-v2/layoutStore.ts:70-121`
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/layoutStore.test.ts`

**Interfaces:**
- Consumes: `mergeTiles(store, response)`; the response's `nodes` and `workers`.
- Produces: unchanged signatures. New rule: **before** merging `response.workers`, drop every stored worker whose `docked_at` is the id of a node present in `response.nodes`. A response describes every worker docked in the cells it returned, so absence there means the worker left. Workers docked at nodes outside the response are kept (the existing additive rationale at `layoutStore.ts:76-78` still holds for them).

- [ ] **Step 1:** Failing tests: (a) store has worker W docked at node N; merge a response containing N and `workers: []` → W is gone; (b) store has W docked at N; merge a response for other cells not containing N → W remains; (c) response moves W from N1 to N2 (both in response) → exactly one W, docked at N2; (d) gates are unaffected by the change.
- [ ] **Step 2:** `npx vitest run src/pages/command-center/layout-v2/__tests__/layoutStore.test.ts` → the new cases fail.
- [ ] **Step 3:** Implement in `pruneAnnotations` (pass it the set of response node ids) and update the comment at `:76-78` to state the rule.
- [ ] **Step 4:** Run the whole `layout-v2/__tests__` directory; pass. Commit `fix(dashboard): evict graph workers a tiles response no longer reports`.

### Task B3: Reconcile agents left pointing at non-running tasks, and announce it

**Files:**
- Modify: `src/orchestrator/agent_reconciler.py:60-90`
- Test: the reconciler's existing test file (`pytest --co -q -k agent_reconciler | tail`)

**Interfaces:**
- Produces: the rescue condition becomes — a BUSY agent with **no live session** is reset to `IDLE, current_task_id=None` when its task is missing **or its status is not in `('ASSIGNED','IN_PROGRESS')`**. After each reset emit `agent.updated` on the bus (find the emit helper the reconciler's owner already holds; `src/commands/flock_commands.py:128` shows the event shape). It must not touch `tasks.assigned_agent_id` or any integration-owner row — retained claims keep their evidence on the task side.

- [ ] **Step 1:** Failing tests: BUSY agent, no live session, task COMPLETED → reset + one `agent.updated`; same with task BLOCKED → reset; task IN_PROGRESS → untouched; agent with a live session and a COMPLETED task → untouched; assert `tasks.assigned_agent_id` is unchanged in every case.
- [ ] **Step 2–4:** Fail, implement, pass. Commit `fix(orchestrator): reclaim BUSY agents whose task is no longer running`.

### Task B4: Doctor check `agents.dangling_current_task`

**Files:**
- Modify: `src/doctor/pool_checks.py` (add `_check_agents_dangling_current_task` + optional `_fix_…`, register in `pool_checks()`)
- Test: `tests/test_pool_doctor.py`

**Interfaces:**
- Produces: check id `agents.dangling_current_task`. WARN listing `{agent_id, task_id, task_status}` for every agent whose `current_task_id` names a task that is missing or not ASSIGNED/IN_PROGRESS **and** which has no live attempt (`live_attempt_predicate` from B1). OK otherwise. `--fix` performs B3's reset for the listed agents. Template: `_check_pools_disabled` (`pool_checks.py:459`).

- [ ] **Steps:** failing tests (OK when clean; WARN with the right `data` when dangling; fix clears it and a re-run is OK) → implement → pass → commit `feat(doctor): agents.dangling_current_task`. Depends on B1.

---

# Lane C — subtasks inside a task

**Decisions:** a subtask is a row in a new `task_subtasks` table, never in `tasks` — it is never on the claim frontier, owns no branch, is delivered with its parent's branch, and costs no hierarchy depth. Statuses: `pending | in_progress | done | skipped`. Authors: anyone who may comment on the task (planner at decomposition, or the worker on first read). Closing a task with `pending`/`in_progress` subtasks is refused with `subtasks.open` unless the caller passes `skip_open_subtasks`, which marks them `skipped`. Completion is only ever reported, never inferred. Promotion of a subtask to a real task is out of scope.

### Task C1: `task_subtasks` table, migration, query mixin

**Files:**
- Modify: `src/database/tables.py` (after `task_comments`, ~:468)
- Create: `migrations/versions/a00000000010_task_subtasks.py`
- Create: `src/database/queries/task_subtask_queries.py`
- Modify: `src/database/base.py` (mix in), `src/database/queries/task_queries.py:_delete_one` (~:1469, delete subtasks unless preserving for archive — follow `preserve_comments`), `src/database/queries/archive_queries.py` (permanent archive delete cleans them), `src/database/queries/project_queries.py` (project delete)
- Test: `tests/test_task_subtasks.py`

**Interfaces — Produces:**

```python
task_subtasks = Table(
    "task_subtasks", metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, nullable=False),            # no FK: survives archive, like task_comments
    Column("project_id", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),          # 1-based display/work order within the task
    Column("title", Text, nullable=False),
    Column("context", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("note", Text, nullable=True),                 # completion/skip note from the worker
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("status IN ('pending','in_progress','done','skipped')", name="ck_task_subtasks_status"),
    CheckConstraint("length(title) BETWEEN 1 AND 300", name="ck_task_subtasks_title_length"),
    CheckConstraint("length(context) <= 16000", name="ck_task_subtasks_context_length"),
    UniqueConstraint("task_id", "ordinal", name="uq_task_subtasks_task_ordinal"),
    Index("idx_task_subtasks_task", "task_id", "ordinal"),
)
```

```python
SUBTASK_STATUSES = ("pending", "in_progress", "done", "skipped")
OPEN_SUBTASK_STATUSES = ("pending", "in_progress")
MAX_SUBTASKS_PER_TASK = 200

class TaskSubtaskQueriesMixin:
    async def add_task_subtasks(self, task_id: str, project_id: str, items: list[dict]) -> list[dict]
        # items: [{"title": str, "context": str = ""}]; appends after the current max ordinal in one
        # transaction; raises ValueError("subtask_limit") past MAX_SUBTASKS_PER_TASK. Returns rows.
    async def list_task_subtasks(self, task_id: str) -> list[dict]          # ordered by ordinal; no `context`
    async def get_task_subtask(self, task_id: str, ordinal: int) -> dict | None   # includes `context`
    async def update_task_subtask(self, task_id: str, ordinal: int, *, status: str | None = None,
                                  note: str | None = None) -> dict | None
    async def skip_open_task_subtasks(self, task_id: str, note: str) -> int
    async def count_task_subtasks(self, task_ids: list[str]) -> dict[str, tuple[int, int]]
        # {task_id: (total, settled)} where settled = done + skipped; one GROUP BY; ids with none absent
    async def delete_task_subtasks(self, task_id: str, *, conn=None) -> None
```
Row dict keys: `id, task_id, project_id, ordinal, title, status, note, created_at, updated_at` (+ `context` on `get`). Ids: `f"{task_id}#s{ordinal}"`.

- [ ] **Step 1:** `tests/test_task_subtasks.py` with the local `db` fixture — failing tests: add two batches → ordinals 1..n contiguous; list is ordinal-ordered and omits `context`; get returns `context`; update sets status/note and bumps `updated_at`; invalid status raises; `count_task_subtasks` returns `(total, settled)` and omits tasks without subtasks; limit raises `subtask_limit`; `skip_open` flips only open rows and returns the count; hard-deleting the task removes its subtasks; archiving the task keeps them; permanently deleting the archived task and deleting the project remove them.
- [ ] **Step 2:** Run; fail on import.
- [ ] **Step 3:** Add the table; write the migration with `_has_table(bind)` guard (`"task_subtasks" in sa.inspect(bind).get_table_names()`) → `op.create_table(...)` with the same columns/constraints/index; `downgrade` drops it if present. Implement the mixin; wire the three cleanup sites.
- [ ] **Step 4:** `aq test tests/test_task_subtasks.py tests/test_archive.py tests/test_hierarchy_archive_delete.py tests/test_task_comments.py` plus the migration guards `aq test tests/test_migration_string_defaults.py tests/test_migration_boolean_defaults.py tests/test_sqlite_removal.py`; confirm one alembic head via `tests/alembic_revisions.py`. Commit `feat(db): task_subtasks — durable, non-schedulable checklist rows`.

### Task C2: The four subtask commands, fully wired

**Files:**
- Create: `src/commands/task_subtask_commands.py` (`TaskSubtaskCommandsMixin`)
- Modify: `src/commands/handler.py` (import + class bases, as `TaskCommentCommandsMixin` at `:50`/`:337`), `src/tools/definitions.py` (`_TOOL_CATEGORIES` next to `:230` + four definitions next to `:4385`), `src/api/models/task.py` (models + `RESPONSE_MODELS` next to `:832`), `src/api/scope.py` (`AGENT_COMMAND_SET` `:13`, and the reviewer sets that list `task_comments` get the two *read* commands), `src/profiles/defaults/*/profile.md` (`aq_commands`: every profile listing `task_comment` gets all four; profiles listing only `task_comments` get the two reads), `src/cli/tasks.py` + `src/cli/auto_commands.py` `HANDCRAFTED_COVERAGE`
- Regenerate: `openapi.json`, `packages/aq-client/`, TS client
- Test: `tests/test_task_subtask_commands.py`, `tests/test_cli_task_subtasks.py`

**Interfaces:**
- Consumes: C1's mixin methods.
- Produces commands (category `task`):
  - `task_subtask_add` — args `task_id?`, `subtasks: [{"title", "context"?}]` (1–50 per call), `claim_epoch?` → `{"success": True, "task_id", "subtasks": [row…]}`
  - `task_subtasks` — args `task_id?` → `{"success": True, "task_id", "subtasks": [row…], "total": int, "settled": int}`
  - `task_subtask_get` — args `task_id?`, `ordinal` → `{"success": True, "subtask": row_with_context}`
  - `task_subtask_update` — args `task_id?`, `ordinal`, `status?`, `note?`, `claim_epoch?` → `{"success": True, "subtask": row, "total", "settled"}`
- `task_id` resolution when omitted: `args.get("task_id") or await self._scoped_held_task_id()` (as `session_commands.py:686`). Writes use the same fences as comments: `_task_findings_scope_error` and `_task_findings_write_fence` from `task_comment_commands.py:23,68` (import and reuse; do not copy). Reads use `_assert_task_in_scope`.
- Every write emits a bus event `task.subtasks_updated` with `{task_id, project_id, total, settled}` — it matches the dashboard's `/^(task|agent|gate)\./` refresh predicate. Mirror `_emit_task_findings_updated` (`task_comment_commands.py:114`); titles/context stay out of the payload.
- Refusal codes: `subtasks.not_found`, `subtasks.limit`, `subtasks.invalid_status`.
- CLI (hand-written, modelled on `aq task comment` at `src/cli/tasks.py:847`; `TASK_ID` optional → falls back to `AQ_TASK_ID`, else send none and let scope resolve):
  - `aq task subtasks [TASK_ID]` — table `# | status | title`, footer `settled/total`
  - `aq task subtask-add [TASK_ID] --title T [--context C]` (repeatable `--title`; `--from-file` JSON list)
  - `aq task subtask-show ORDINAL [--task TASK_ID]` — prints title, status, note, context
  - `aq task subtask-done ORDINAL [--task TASK_ID] [--note N]`, `aq task subtask-start ORDINAL`, `aq task subtask-skip ORDINAL --note N` — all call `task_subtask_update`

- [ ] **Step 1:** Failing command tests (fixtures/helpers as `tests/test_task_comments.py` and `tests/test_claim_commands.py`: `handler`, `mktask`, `pool_session`, `scoped`): add+list round trip; omitted `task_id` resolves to the held task for a scoped session; a session scoped to another task is refused on write and allowed on read within its project; stale `claim_epoch` refused; update emits exactly one `task.subtasks_updated` whose payload has no `title`; unknown ordinal → `subtasks.not_found`; bad status → `subtasks.invalid_status`.
- [ ] **Step 2:** Failing CLI tests (pattern: `tests/test_cli_task_comments.py:42`): each verb sends the right command and args; `TASK_ID` falls back to `AQ_TASK_ID`; untrusted markup in a title renders literally.
- [ ] **Step 3:** Implement the mixin, tool definitions, response models, scope entries, profile grants, CLI.
- [ ] **Step 4:** Regenerate clients (serialised — see Global Constraints).
- [ ] **Step 5:** `aq test tests/test_task_subtask_commands.py tests/test_cli_task_subtasks.py tests/test_command_surface.py tests/test_response_model_registry.py tests/test_command_scope_matrix.py tests/test_api_client_contract.py tests/test_tool_registry.py tests/test_mcp_catalog.py`. `test_tool_registry.py` pins tool counts — update the pinned numbers by exactly four. Commit `feat(commands): task subtasks — add, list, get, update over CLI/MCP/API`.

### Task C3: Workers see their subtasks and must settle them

**Files:**
- Modify: `src/prime/sections.py:128-160` (`build_task_section`), `src/prime/templates/tool_guidance.md`, `src/skills/aq-tasks/SKILL.md`
- Modify: `src/commands/session_commands.py` (`_cmd_task_close`, between the deliverables refusal `:818-860` and the open-children refusal `:862`), `src/tools/definitions.py` (`task_close` schema gains `skip_open_subtasks: boolean`), `src/cli/tasks.py` (`aq task close --skip-open-subtasks`)
- Test: `tests/test_prime_renderer.py`, `tests/test_task_subtask_commands.py`, `tests/test_swarm_surface.py`

**Interfaces:**
- Consumes: `list_task_subtasks`, `skip_open_task_subtasks`.
- Produces: prime's task section appends, when the task has subtasks, a `## Subtasks` block — one line per subtask `- [x] 3. Title` (`[x]` done, `[-]` skipped, `[~]` in progress, `[ ]` pending), then: ``Report progress as you go: `aq task subtask-done N`. Full context for one: `aq task subtask-show N`. Work them in order unless the task says otherwise.`` Titles only — never `context`.
- Produces: close refusal `{"success": False, "code": "subtasks.open", "error": "<n> subtask(s) are still open: … Mark each with `aq task subtask-done N` / `aq task subtask-skip N --note …`, or close with --skip-open-subtasks.", "open_subtasks": [ordinal…]}`. With `skip_open_subtasks: true` the open rows become `skipped` with note `"skipped at close"` and close proceeds. The check runs only for `outcome` values that complete the task (not for a failing close).

- [ ] **Step 1:** Failing tests: prime renders the block with the right glyphs and omits it when there are none; close with open subtasks → `subtasks.open` and **no side effects** (task still IN_PROGRESS, claim still held); close with the flag → succeeds and rows are `skipped`; failing close is never refused for subtasks.
- [ ] **Step 2–3:** Fail; implement.
- [ ] **Step 4:** Add a "Subtasks" section to `aq-tasks/SKILL.md` (list, show, done/start/skip, the close rule, "add your own with `subtask-add` when a task has several distinct steps") and a line to `tool_guidance.md`. Regenerate clients (schema change to `task_close`).
- [ ] **Step 5:** `aq test tests/test_prime_renderer.py tests/test_task_subtask_commands.py tests/test_swarm_surface.py tests/test_doctor_skill_checks.py tests/test_guidance_docs.py tests/test_api_client_contract.py`. Commit `feat(worker): subtasks in prime, and a close refusal for open ones`.

### Task C4: Subtask progress on graph nodes and in the task view

**Files:**
- Modify: `src/api/models/graph_layout.py:101` (`LayoutNode` gains `subtasks_total: int = 0`, `subtasks_settled: int = 0`), `src/api/graph_layout.py` (one `count_task_subtasks(visible_ids)` per tiles/list/node response, fed into `_node`)
- Modify: `dashboard/src/pages/command-center/layout-v2/flowNodes.ts` (payload **and** `nodeSignature` `:76-81`), `dashboard/src/pages/command-center/types.ts`, the task detail panel (find via `grep -rn "task/comments\|taskComments" dashboard/src`) to list subtasks read-only next to comments
- Test: `tests/test_api_graph_layout.py`, `tests/perf/test_layout_api_statements.py` (statement budget +1), `dashboard/.../__tests__/flowNodes.test.ts`

**Interfaces:**
- Consumes: `count_task_subtasks` (C1), command `task_subtasks` (C2), event `task.subtasks_updated` (already matches the live-refresh predicate).
- Produces: `LayoutNode.subtasks_total/subtasks_settled`; `TaskNodeData.subtasks?: {total: number; settled: number}` — consumed by A4's bar.

- [ ] **Steps:** failing API test (node carries counts; tasks without subtasks carry zeros; exactly one extra statement per response) → implement → regenerate clients → failing `flowNodes` test (signature changes when `subtasks_settled` changes) → implement → detail-panel list with a vitest render test → pass → commit `feat(graph): subtask counts on layout nodes and in the task panel`.

---

# Lane A — a graph you can read on landing

### Task A1: Phases — ordered containers that gate implicitly

**Decisions:** a phase is a container task carrying `task_metadata` key `phase` = `{"order": int, "label": str}`. It may sit at the project root or under an epic (depth budget: phase → epic → task, or epic → phase → task). Creating phase *N+1* adds one `blocks` edge onto phase *N* among the same parent's phases. Nothing else gates: the blocked projection keeps the later phase DEFINED, and a DEFINED parent withholds every descendant. A phase completes by ordinary container settlement (all children COMPLETED) — a FAILED child holds the gate, which is the intent. Adding work to a completed phase is refused by the existing `container_closed`.

**Files:**
- Create: `src/commands/phase_commands.py` (`PhaseCommandsMixin`), wired like C2 (handler bases, `_TOOL_CATEGORIES` → `"task"`, tool definitions, response models, CLI verbs, regenerate)
- Modify: `src/database/queries/hierarchy_queries.py` (settlement guard — see Step 3), `src/task_graph/formulas.py` + the `aq-graph` creator only if Step 6 applies
- Test: `tests/test_phases.py`

**Interfaces — Produces:**
- `phase_create` — args `project_id`, `title`, `label?`, `parent_id?` → creates a task (`task_type="plan"`, description = label or title), calls `mark_container` **at creation** (an unflagged childless phase would otherwise be a claimable READY task), writes the `phase` metadata with `order = max(sibling phase order) + 1`, and adds `depends_on: [{task_id: <previous phase>, dep_type: "blocks"}]` when a previous sibling phase exists. Returns `{"success": True, "phase": {"id", "order", "label", "parent_id", "blocked_by": id | None}}`.
- `phase_list` — args `project_id`, `parent_id?` → `{"success": True, "phases": [{"id","title","label","order","status","is_blocked","total","done"}]}` ordered by `order` (`total/done` from `get_children_summary`).
- CLI: `aq phase create PROJECT --title T [--label L] [--parent ID]`, `aq phase list PROJECT [--parent ID]` (new click group; check how groups are registered in `src/cli/`). Tasks join a phase with the existing `aq task create --parent <phase-id>`.
- Both commands are operator/planner/supervisor surfaces: add to those profiles' `aq_commands`, not to worker profiles.

- [ ] **Step 1:** Failing behaviour tests against a real DB + handler + the orchestrator promotion step (`orchestrator_factory`, drive `_check_defined_tasks`): (a) two phases, a task in each → after promotion the phase-1 task is READY and unblocked, the phase-2 task is **not** on the frontier (`select_ready_for_profile` returns only phase 1's); (b) complete phase 1's task → phase 1 settles COMPLETED → after the next promotion pass phase 2 is released and its task is claimable; (c) a nested epic inside phase 2 with its own child is withheld too, then released; (d) a phase-1 child FAILED keeps phase 2 gated; (e) `phase_list` reports order, labels and counts; (f) adding a task to a completed phase is refused with `hierarchy.container_closed`.
- [ ] **Step 2:** Failing test: a freshly created **childless** phase is never claimable and is **not** settled to COMPLETED by `settle_containers([phase_id])` nor listed by `settle_candidates()`.
- [ ] **Step 3:** Implement. If Step 2's settlement assertion fails (the predicate's `NOT EXISTS child != COMPLETED` is vacuous for zero children), add `EXISTS(child)` to the settlement predicate in both `settle_containers` and `settle_candidates` — a container with no children has nothing to settle — and run `aq test tests/test_hierarchy_settlement.py` to prove no existing behaviour depended on it.
- [ ] **Step 4:** Run (a)–(f) in a project with `integration_mode` hierarchy as well (parametrise): the phase `blocks` edge is between root siblings with no shared parent branch, so `delivered_same_parent_prerequisites_when_hierarchical` must not strand phase 2. If it does, record the exact predicate behaviour in the test and stop — report to the orchestrating session rather than loosening an integration predicate.
- [ ] **Step 5:** Wire CLI/tool definitions/models/profiles; regenerate clients; `aq test tests/test_phases.py tests/test_hierarchy_queries.py tests/test_hierarchy_settlement.py tests/test_claim_commands.py tests/test_command_surface.py tests/test_response_model_registry.py tests/test_api_client_contract.py tests/test_tool_registry.py`.
- [ ] **Step 6:** Formulas: read `src/task_graph/formulas.py` and the `aq-graph` grammar. If a graph node can already carry metadata and nested children, document in `docs/specs/design/formulas.md` how to declare phases (container node + `metadata.phase` + a `blocks` edge) and add one formula test. If it cannot, leave formulas alone and note it in the commit body — do not extend the grammar in this task.
- [ ] **Step 7:** Commit `feat(phases): ordered phase containers gated by the blocked projection`.

### Task A2: A keyed standing parent, so automated work stops landing in the root

**Decisions:** mechanism in code, policy in playbooks. `create_task` and `ensure_task` gain `parent_key`: resolve-or-create a container under that key and file the task inside it. A settled (COMPLETED) keyed container is never reused — a fresh one is created, and the old one leaves through normal archival, so the standing parent is self-cleaning. Which key automated creators use is decided in the playbooks.

**Files:**
- Modify: `src/commands/task_commands.py` (`_cmd_create_task` `:1695`; `ensure_task` — locate with `grep -n "_cmd_ensure_task" src/commands/`), `src/tools/definitions.py` (both schemas)
- Modify: `src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md` (pass `parent_key: "maintenance"`, title "Maintenance"), its reviewed bundle under `tests/fixtures/playbooks/v2/ci-main-sentinel/` (rebuild with `scripts/rebuild-reviewed-playbook-artifacts.py`, update `manifest.md` digests)
- Test: `tests/test_work_graph_commands.py`, `tests/test_ensure_task.py`, the ci-main-sentinel playbook tests

**Interfaces — Produces:**
- `create_task` / `ensure_task` arg `parent_key: str` (+ optional `parent_title: str`, default = the key, title-cased). Mutually exclusive with `parent_id` and `root` → `{"code": "hierarchy.parent_conflict"}`. Resolution: find the project's task with `dedup_key == f"parent:{parent_key}"` whose status is not COMPLETED/FAILED; else create one (root, `task_type="chore"`, `mark_container` at creation, that `dedup_key`). Resolution + child filing happen under `lock_hierarchy_project` so two concurrent creators cannot make two containers. Response adds `"parent_id"`.
- Refused for session (worker) principals — workers already default to their held task: `{"code": "hierarchy.parent_key_not_for_sessions"}`.

- [ ] **Steps:** failing tests (first call creates the container and files under it; second call reuses it; after the container settles, the next call creates a new one and the old one is untouched; concurrent calls → exactly one container; conflict and session refusals; `ensure_task` dedup still holds for the child) → implement → update the sentinel playbook + bundle → regenerate clients → `aq test tests/test_work_graph_commands.py tests/test_ensure_task.py tests/test_hierarchy_commands.py tests/test_api_client_contract.py` plus the sentinel playbook test file → commit `feat(tasks): parent_key — a self-cleaning standing parent for automated work`.
- [ ] **Follow-up (report only):** list every other non-session `create_task`/`ensure_task` caller found (`grep -rn "ensure_task\|create_task" src/prompts src/commands`) in the commit body with a recommendation; do not change them here. The recovery-incident path is explicitly out of scope.

### Task A3: Land on the active subgraph

**Decisions:** the server computes an "active" expanded set; the client applies it when the viewer has no saved expansion for the project, and on demand from a toolbar button. Active = every container with `agg_running > 0`, plus — when nothing is running — root containers with `agg_active > 0`; ancestors always included; capped at `EXPANDED_CAP`, shallowest first.

**Files:**
- Modify: `src/task_graph/layout/view.py` (pure function), `src/api/models/graph_layout.py` (`TilesRequest.auto_expand: bool = False`; `TilesResponse.expanded_applied: list[str] | None = None`), `src/api/graph_layout.py` (when `auto_expand and not req.expanded`, compute, use it as the expanded set for this response, return it)
- Modify: `dashboard/src/pages/command-center/useGraphHierarchy.ts` (expose whether the project's document has ever stored an expansion), `dashboard/src/pages/command-center/layout-v2/LayoutCanvas.tsx` (`:305` request; store `expanded_applied` via `setExpandedTaskIds`), `useLayoutTiles.ts`, the graph toolbar (a "Focus active" button that clears the expansion and re-requests with `auto_expand`)
- Test: `tests/task_graph/layout/` (new `test_active_expansion.py`), `tests/test_api_graph_layout.py`, `dashboard/.../__tests__/LayoutCanvas.test.tsx`, `useLayoutTiles.test.tsx`

**Interfaces — Produces:**
- `def active_expansion(rows: Iterable[LayoutRow], *, cap: int) -> list[str]` in `view.py` — pure, deterministic order (depth, then `order_key`).

- [ ] **Steps:** failing pure tests (running leaf three levels down → all its container ancestors; nothing running → root containers with open work; finished-only containers never included; cap respected, shallowest kept) → implement → failing API test (`auto_expand` with empty `expanded` returns `expanded_applied` and nodes resolved against it; with a non-empty `expanded` it is ignored and `expanded_applied` is null) → implement → regenerate clients → failing dashboard tests (first load with no stored expansion sends `auto_expand: true` and persists the returned set exactly once; a project with a stored expansion — even an empty one the user chose — never sends it; the button re-applies) → implement → pass → commit `feat(graph): open on the active subgraph`.

### Task A4: Progress bars on containers, phases and tasks with subtasks

**Files:**
- Create: `dashboard/src/pages/command-center/ProgressBar.tsx`
- Modify: `dashboard/src/pages/command-center/layout-v2/ContainerNode.tsx:19-31`, `dashboard/src/pages/command-center/TaskNode.tsx:131-141`, `MobileLayoutList.tsx`, `flowNodes.ts` + `types.ts` (phase fields), `src/api/models/graph_layout.py` + `src/api/graph_layout.py` (`LayoutNode.phase_order: int | None`, `phase_label: str | None`, read from `task_metadata` for visible ids in one query)
- Test: `dashboard/.../__tests__/ContainerNode.test.tsx`, `flowNodes.test.ts`, `MobileLayoutList.test.tsx`, a new `ProgressBar.test.tsx`, `tests/test_api_graph_layout.py`

**Interfaces:**
- Consumes: `agg_completed/agg_descendants/agg_running/agg_blocked` (exist), `subtasks_total/subtasks_settled` (C4), phase metadata (A1).
- Produces: `<ProgressBar done={n} total={n} running={n} blocked={n} />` — a thin segmented bar (done / running / blocked / remaining) with an accessible `aria-label` `"3 of 7 done"`, renders nothing when `total === 0`. Uses existing Tailwind tokens from the neighbouring nodes (indigo = running, amber = blocked); keep the existing text count beside it.
- Containers: bar from the aggregates. Collapsed containers and stubs: same bar. Task cards: bar from subtasks when `subtasks.total > 0`. Phase containers: header shows `Phase {order}` + label, and a lock glyph while `is_blocked`.

- [ ] **Steps:** failing component tests (segment widths for a known input; hidden at zero; aria-label) → implement `ProgressBar` → failing node tests (container renders a bar; card with subtasks renders one and a card without does not; phase header text; `nodeSignature` changes with `phase_order`/`phase_label`) → implement, including the two `LayoutNode` fields + regenerate → `npx vitest run src/pages/command-center` and `aq test tests/test_api_graph_layout.py tests/test_api_client_contract.py` → commit `feat(graph): progress bars on containers, phases and subtask-bearing tasks`. Depends on A1 and C4.

### Task A5: Draw where derivative work came from

**Files:** `src/api/graph_layout.py` / `src/database/queries/layout_queries.py` (edge read), `dashboard/.../layout-v2/flowNodes.ts`
**Test:** `tests/test_api_graph_layout.py`, `flowNodes.test.ts`

- [ ] **Step 1:** Establish the facts first: does the tiles response include `discovered-from` edges today, and does `LayoutEdge` carry `dep_type`? Record the answer in the commit body.
- [ ] **Step 2:** If absent: failing API test (a `discovered-from` edge between two visible nodes is returned with `dep_type="discovered-from"`), implement, regenerate. Layout geometry must not change — these edges are annotations, never layering inputs (assert the layout driver's edge set excludes them, as it must already for non-blocking types).
- [ ] **Step 3:** Failing `flowNodes` test → render such edges dashed and dimmed, no arrowhead emphasis. Commit `feat(graph): show discovered-from provenance edges`.

---

## Order and parallelism

| Wave | Tasks (parallel within a wave) | Notes |
|---|---|---|
| 1 | B1 · B2 · C1 · A1 | Disjoint files. A1 and B1 regenerate clients — serialise that step. |
| 2 | B3 · C2 · A2 | C2 needs C1. |
| 3 | B4 · C3 · A3 · A5 | B4 needs B1; C3 needs C2. |
| 4 | C4 → A4 | A4 needs A1 + C4. |
| 5 | End-of-branch verification | below |

**End-of-branch verification:** `aq test tests/test_hierarchy*.py tests/test_claim*.py tests/test_task_*.py tests/test_api_graph_layout.py tests/test_api_client_contract.py tests/test_command_surface.py tests/task_graph`, `npx vitest run` in `dashboard/`, `npm run typecheck`, then `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` (claims and hierarchy were touched; strip `AQ_*`/`AGENT_QUEUE_*` from the environment first). Update `CLAUDE.md` Quick Reference with three entries (phases, subtasks, live-attempt markers) and `docs/specs/design/work-graph.md` with the phase and subtask models.

## Out of scope

Evaluation harness and repo-cleanliness metrics (planning doc D, E); promoting a subtask to a real task; restoring archived tasks; changing `archive.*` defaults; clearing `assigned_agent_id` inside `_apply_transition` (B1–B4 make the marker correct without touching retained-claim evidence; revisit only if `agents.dangling_current_task` keeps firing).
