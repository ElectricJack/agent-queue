---
tags: [design, workspaces, multi-repo, vault, locking]
---

# Workspaces v2 — Multi-Kind, Multi-Instance Workspaces

**Status:** Draft
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable, #9 simple interfaces)
**Related:** [[agent-coordination]], [[profiles]], [[vault]], [[specs/orchestrator]], [[specs/database]]

---

## 1. Problem Statement

The current model assumes one project ⇄ one repo and one task ⇄ one workspace. This breaks down for several real cases:

- **Multi-repo work.** A task may need to lock and modify a project repo *and* a sibling package repo at the same time (e.g. land a breaking change in a shared library and update a consumer).
- **Read-only references.** An agent often needs to *read* documentation, schemas, or peer projects without acquiring a lock or risking writes.
- **Movable project vault.** The project vault is hard-coded to `~/.agent-queue/vault/projects/<pid>/`. Some projects want to keep their vault inside the project repo, on a network mount, or alongside team-shared notes.
- **Wrong-workspace risk.** Today a task can only be handed *the* workspace; with multiple writable workspaces of similar shape, "first available" can easily hand the agent the wrong tree.

The solution is a richer workspace model that supports multiple typed attachments per task, with explicit semantics for writability, locking, and provenance.

---

## 2. Vision

Workspaces become a typed, normalized concept:

- A **workspace kind** is a definition (e.g. `project-repo`, `package-mylib`, `vault`, `reference-docs`). Kinds carry capability flags: `writable`, `lockable`, `is_git_repo`, `auto_attach`, etc.
- A **workspace** is an instance of a kind, with a path, optional git provenance, and lock state.
- A **task** declares the kinds it needs at creation time. The orchestrator atomically acquires one instance per declared requirement before dispatch.

Kinds are authored as markdown in the vault — system-wide defaults plus per-project overrides — mirroring how profiles and MCP servers are configured today.

This is an **additive** evolution of the existing model. Existing single-repo projects continue to work via auto-migration: each gets a synthesized `project-repo` kind and existing tasks default to requiring it.

---

## 3. Data Model

### 3.1 New table: `workspace_kinds`

| Column | Type | Notes |
|---|---|---|
| `id` | `Text` | Stable handle, e.g. `project-repo`, `vault`. Part of composite PK. |
| `project_id` | `Text` | Either a real `projects.id` or the sentinel `__system__` for system-wide rows. **NOT NULL.** Soft reference — no FK to `projects.id` (matches the existing `agents.profile_id` pattern). Part of composite PK. |
| `description` | `Text` | Human-readable. Sourced from the markdown body. |
| `writable` | `Boolean` | Whether the agent may write to instances. |
| `lockable` | `Boolean` | Whether instances participate in lock acquisition. |
| `is_git_repo` | `Boolean` | Triggers git provisioning (clone, branch prep). |
| `repo_url` | `Text` (nullable) | Required when `is_git_repo` is true. |
| `default_lock_mode` | `Text` (nullable) | Lowercase string: `exclusive` / `branch_isolated` / `directory_isolated` (matches `WorkspaceMode.value`). Required when `lockable`. |
| `auto_attach` | `Boolean` | If true, every task in the project gets an instance of this kind attached without declaring it (vault uses this). |
| `created_at`, `updated_at` | `DateTime` | |

**Composite primary key:** `(project_id, id)`, both NOT NULL. The `__system__` sentinel value for `project_id` lets system-wide rows live in the same table without nullable PK columns (which fails on Postgres). Resolution at lookup time: project-scoped row wins over the system row with the same `id` (see §3.5).

### 3.2 Changes to `workspaces`

Add one column:

| Column | Type | Notes |
|---|---|---|
| `kind_id` | `Text` (nullable during the migration window; tightened to NOT NULL in a follow-up migration after one minor version) | The kind this instance implements. **Soft reference** — no FK constraint, since the resolution target depends on `(project_id, kind_id)` and may be either the project-scoped row or the system row. The orchestrator validates resolution at use time. |

All existing columns (`workspace_path`, `source_type`, `locked_by_*`, `lock_mode`, `enabled`) remain and keep their current semantics. For non-lockable kinds, lock columns stay NULL and the acquisition path skips them.

### 3.3 New table: `task_workspace_requirements`

| Column | Type | Notes |
|---|---|---|
| `task_id` | `Text` (FK → `tasks.id`) | Part of PK. |
| `kind_id` | `Text` | The required kind. Part of PK. |
| `position` | `Integer` | Distinguishes multiple requirements of the same kind. Server-assigned: when a row is inserted without a `position`, the orchestrator computes `MAX(position) + 1` per `(task_id, kind_id)` (or 0 if none exist). Part of PK. |
| `alias` | `Text` (nullable) | Logical name the agent uses (e.g. `primary`, `mirror`). |

**Primary key:** `(task_id, kind_id, position)`. Multiple rows with the same `kind_id` are allowed — a task can require two `package-repo` instances (e.g. for a cross-repo merge). Authors do not set `position` directly; the helper that writes requirement rows assigns it automatically.

### 3.4 Changes to `tasks`

`preferred_workspace_id` stays as an **advisory hint**. It is consumed at acquisition time: when `effective_requirements()` synthesizes a `project-repo` requirement (see §6.1), the orchestrator tries `preferred_workspace_id` first as the candidate instance, falling back to first-unlocked of the kind if it's busy or no longer matches.

### 3.5 Resolution rules

For a workspace or requirement in project P that names kind K:

1. Look up `workspace_kinds(project_id=P, id=K)`. If found, that's the effective kind.
2. Else look up `workspace_kinds(project_id='__system__', id=K)`. That's the effective kind.
3. Else: the task is unschedulable — surface as an error at task-create time (see §5.4).

Project-scoped rows shadow system rows with the same `id`; this is the override mechanism. To temporarily disable an override, delete the file (the watcher reconciles the row out, and resolution falls back to the system row). A first-class `enabled` flag on kinds is a non-goal (see §11).

Resolution always reads live DB state. The `WorkspaceKindStore` watcher reconciles markdown changes asynchronously — there is a brief window between a file delete and the row deletion during which an in-flight task creation may still see the override. The worst case is "uses the not-yet-deleted override one more time," not a stale-row inconsistency.

---

## 4. Vault Layout

Mirrors the profile and MCP server patterns.

```
~/.agent-queue/vault/                    # SYSTEM VAULT ROOT (location is fixed)
├─ workspace-kinds/                       # system defaults
│  ├─ project-repo.md
│  ├─ vault.md
│  └─ readonly-dir.md
└─ projects/<pid>/
   └─ workspace-kinds/                    # per-project overrides + new kinds
      ├─ project-repo.md
      ├─ game-repo.md
      └─ package-foo.md
```

The **system vault root** at `~/.agent-queue/vault/` is where kind definitions live and stays at a fixed location (no movability). It is distinct from a project's **vault workspace content** (the runtime-attached vault that the agent reads memory and notes from), which *is* movable per §7. Moving a project's vault workspace does not relocate `vault/projects/<pid>/workspace-kinds/`; overrides stay where the watcher can see them.

A `WorkspaceKindStore` (in `src/profiles/workspace_kind_registry.py`, alongside `mcp_registry.py`) watches both directories and reconciles to the `workspace_kinds` table. On daemon start it also ensures `vault/projects/<pid>/workspace-kinds/` exists for every project (created empty if missing) — same convention as `mcp-servers/`.

### 4.1 Markdown frontmatter

````markdown
---
id: game-repo
description: Atom games monorepo (clone of git@github.com:atom/games.git)
writable: true
lockable: true
is_git_repo: true
repo_url: git@github.com:atom/games.git
default_lock_mode: branch_isolated
auto_attach: false
---

# game-repo

Long-form notes about this kind. Body text overrides the `description`
frontmatter when both are present, matching the profiles convention.
````

### 4.2 Built-in system kinds

Three kinds ship by default in `vault/workspace-kinds/`:

- **`project-repo`** — `writable=true, lockable=true, is_git_repo=true, default_lock_mode=exclusive`. Synthesized for every existing project during migration; created on project-create going forward.
- **`vault`** — `writable=true, lockable=false, is_git_repo=false, auto_attach=true`. Implements the project vault. Auto-attached to every task.
- **`readonly-dir`** — `writable=false, lockable=false, is_git_repo=false`. Catch-all for read-only attachments.

Users can override any of these by creating a project-scoped file with the same `id`. The `project-repo` system row is treated as undeletable by the migration (recreated on daemon start if missing) so that the §10 default for `add_workspace` always resolves.

---

## 5. Task Requirement Declaration

### 5.1 At task creation

`CommandHandler.create_task` (and the MCP `create_task` tool) gain an optional parameter:

```python
requires_kinds: list[str | dict] = []
# Examples:
#   ["game-repo"]
#   ["game-repo", "package-foo"]
#   [{"kind": "package-foo", "alias": "primary"},
#    {"kind": "package-foo", "alias": "mirror"}]
```

Strings are sugar for `{"kind": "<id>", "alias": null}`. Each entry becomes one row in `task_workspace_requirements` with a server-assigned `position` (§3.3). Auto-attached kinds (e.g. `vault`) are *not* listed here — the orchestrator computes them implicitly at acquisition time.

### 5.2 Defaults when omitted

If `requires_kinds` is omitted, **no rows** are written to `task_workspace_requirements`. Defaults are computed at acquisition time by `effective_requirements(task)` (§6.1), which guarantees:

- A `project-repo` requirement is added when the task has no explicit requirements *and* the project has a resolved `project-repo` kind.
- All `auto_attach` kinds for the project are added.

This sparse-row strategy means migration does not need to back-fill any rows for existing tasks (§9 idempotency), and re-running a migration cannot create duplicates.

### 5.3 From playbook stages

Playbook stage definitions get a `requires_kinds` field that flows to spawned tasks. Stages without it inherit the §5.2 default behavior.

### 5.4 Validation at create-time

When a task is created, the orchestrator validates that each declared `kind_id` *resolves* (§3.5) against the project's effective kind set. It does **not** validate that an unlocked instance exists — that's an acquisition-time concern (§6.2), and instance availability fluctuates.

If a kind doesn't resolve, task creation fails with a clear error: `"Kind '<kind_id>' is not defined for project '<pid>' and no system default exists."` Auto-provisioning of git-backed instances when none exist for a referenced kind is tracked as future work (§12).

---

## 6. Acquisition Algorithm

### 6.1 `effective_requirements(task)`

The single load-bearing function that turns a task into the set of workspace attachments to acquire:

```python
def effective_requirements(task: Task) -> list[ResolvedRequirement]:
    project_id = task.project_id
    explicit_rows = db.fetch_task_workspace_requirements(task.id)  # may be empty

    base: list[ResolvedRequirement] = []
    if explicit_rows:
        # Materialize each row; position is taken from the row.
        for row in explicit_rows:
            base.append(ResolvedRequirement(
                kind_id=row.kind_id,
                alias=row.alias,
                position=row.position,
                preferred_workspace_id=None,
            ))
    elif resolve_kind(project_id, "project-repo") is not None:
        # Synthesize the default; assign position=0 so the canonical
        # sort is well-defined.
        base.append(ResolvedRequirement(
            kind_id="project-repo",
            alias=None,
            position=0,
            preferred_workspace_id=task.preferred_workspace_id,
        ))

    # Add auto-attach kinds the task did not already request.
    # Auto-attach kinds always sort after explicit/synthesized requirements
    # because they get a high position (10_000+ idx); they are bulk
    # additions and not meant to influence lock ordering across tasks.
    explicit_kind_ids = {r.kind_id for r in base}
    for idx, kind in enumerate(db.fetch_auto_attach_kinds(project_id)):
        if kind.id not in explicit_kind_ids:
            base.append(ResolvedRequirement(
                kind_id=kind.id,
                alias=None,
                position=10_000 + idx,
                preferred_workspace_id=None,
            ))

    # Canonical lock order: sort by (kind_id, position) — see §6.3
    base.sort(key=lambda r: (r.kind_id, r.position))
    return base
```

`ResolvedRequirement.position` is always set (no None / no missing field). `resolve_kind(project_id, kind_id)` is the §3.5 lookup that returns the effective `WorkspaceKind` (project-scoped row preferred over system row), or `None` if neither exists.

`effective_requirements()` is pure — same input always produces the same output. It is the *only* place defaults and auto-attached kinds are materialized; everything downstream reads its result.

### 6.2 Acquisition

Properties:

- **All-or-nothing.** Either every required lock is acquired in a single SQLAlchemy `begin()` transaction, or none are.
- **Deadlock-free.** Requirements are sorted by `(kind_id, position)` before locking (§6.3). All transactions touching workspaces in this codebase use the same canonical order.
- **First-unlocked instance.** Within a kind, the orchestrator picks the first unlocked instance. The `preferred_workspace_id` hint (§3.4) is tried first when the synthesized `project-repo` requirement is involved.
- **Read-only and auto-attached kinds skip locking.** They are recorded as part of the task's `WorkspaceAttachmentSet` so the runtime can expose paths.
- **Path-level conflict checks preserved.** The existing `acquire_workspace` (`workspace_queries.py:166-206`) checks for *path-level* conflicts: an `EXCLUSIVE` lock conflicts with any existing lock on the same `workspace_path`; a `BRANCH_ISOLATED` lock only conflicts with a non-`BRANCH_ISOLATED` lock on the same path. `acquire_one_unlocked` preserves this behavior unchanged — it is part of the per-instance candidate filter.
- **Per-dialect concurrency strategy** (§6.4): SQLite uses `BEGIN IMMEDIATE` plus `UPDATE ... WHERE locked_by_task_id IS NULL` row-count checks; Postgres uses `SELECT ... FOR UPDATE SKIP LOCKED` against the candidate set followed by an `UPDATE`.

Pseudocode:

```python
async def acquire_for_task(task: Task) -> WorkspaceAttachmentSet | None:
    requirements = effective_requirements(task)  # already canonically sorted
    async with db.transaction(isolation="IMMEDIATE"):  # SQLite: BEGIN IMMEDIATE
        attachments: list[WorkspaceAttachment] = []
        for req in requirements:
            kind = resolve_kind(task.project_id, req.kind_id)
            if kind.lockable:
                ws = await db.acquire_one_unlocked(
                    project_id=task.project_id,
                    kind_id=kind.id,
                    mode=kind.default_lock_mode,
                    locked_by_task_id=task.id,
                    prefer_workspace_id=req.preferred_workspace_id,  # may be None
                )
                if ws is None:
                    raise AcquisitionFailed(req.kind_id)
                attachments.append(WorkspaceAttachment(req, ws))
            else:
                ws = await db.first_workspace_of_kind(
                    project_id=task.project_id, kind_id=kind.id
                )
                if ws is None:
                    raise AcquisitionFailed(req.kind_id)
                attachments.append(WorkspaceAttachment(req, ws))
        return WorkspaceAttachmentSet(attachments)
```

If `AcquisitionFailed` is raised the transaction rolls back — no partial locks. The task stays queued and retries on the next scheduling tick.

**Naming note:** `WorkspaceAttachment` and `WorkspaceAttachmentSet` are deliberately distinct from the existing `tasks.attachments` column (which stores absolute paths to *file* attachments like images for chat input — see `src/models.py:309`). The two concepts must not be conflated.

### 6.3 Canonical lock order

Within a single task's acquisition, requirements are sorted by `(kind_id, position)` — a deterministic key — before any locking begins. **All callers** that take more than one workspace lock in this codebase use this same order. This is what guarantees deadlock-freedom: two tasks needing overlapping kinds will always attempt them in the same order, so one waits and the other proceeds.

### 6.4 Per-dialect concurrency

| Dialect | Strategy |
|---|---|
| SQLite | `BEGIN IMMEDIATE` (acquire a write lock at transaction start). Inside the transaction, candidate selection uses `UPDATE workspaces SET locked_by_task_id = ? WHERE id = ? AND locked_by_task_id IS NULL` and checks rowcount; if 0 rows updated, candidate was lost and the next is tried. |
| Postgres | `SELECT id FROM workspaces WHERE project_id = ? AND kind_id = ? AND locked_by_task_id IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1`, then `UPDATE`. `SKIP LOCKED` lets concurrent transactions on different kinds proceed without contention. |

Both strategies are encapsulated in `db.acquire_one_unlocked()` and selected by inspecting `db.dialect.name`.

### 6.5 What `acquire_for_task` does *not* do

The current `_prepare_workspace` (`src/orchestrator/workspace.py:32`) does several things beyond locking: branch-isolated worktree fallback, sentinel file management, git clone/fetch/reset orchestration, and plan-file pre-cleanup. **All of these stay in the orchestrator**, called *after* `acquire_for_task` returns — `acquire_for_task` only produces an `AttachmentSet`. The new wrapper for the single-`project-repo` case (§6.6) is responsible for invoking the existing git provisioning sequence on the primary attachment.

### 6.6 Backwards compatibility for `_prepare_workspace`

The existing single-workspace acquisition path becomes a thin wrapper over `acquire_for_task` that:

1. Calls `acquire_for_task(task)` → `AttachmentSet`.
2. Identifies the `project-repo` attachment (§8.1's `primary_path` rule).
3. Runs the existing git provisioning sequence (clone/fetch/reset/branch prep) against that attachment.
4. Performs branch-isolated worktree fallback if the primary lock was unavailable (per current `workspace.py:87` logic).
5. Manages sentinel files and plan-file cleanup as today.

No behavioral change for tasks that don't declare custom `requires_kinds`.

### 6.7 Release

When a task completes, fails, or is cancelled, the orchestrator releases all locks held by `locked_by_task_id == task.id` in a single transaction. Idempotent — safe to call multiple times.

---

## 7. Vault as a Workspace Kind

The project vault is modeled as a kind with `auto_attach=true, lockable=false`. The orchestrator:

1. On project create: provisions a `vault` workspace pointing at `~/.agent-queue/vault/projects/<pid>/` by default.
2. On every task acquisition: implicitly attaches the project's `vault` workspace (via `effective_requirements()` auto-attach pass).
3. Exposes the vault path to the runtime via the same attachment surface used for any other kind (§8).

To **move** a project's vault workspace, edit the project-scoped `vault` workspace row (or via a CLI/MCP command) — set `workspace_path` to the new location. Memory subsystem and prompt builder read the vault location from the attachment, not from a hard-coded path.

The current hard-coded `~/.agent-queue/vault/projects/<pid>/` becomes the *default* for the synthesized vault workspace, not a fixed assumption baked into the code. Note the distinction made in §4: this only moves the vault *content* (notes, memory, knowledge bases), not the kind-definition directory `vault/projects/<pid>/workspace-kinds/`, which stays under the system vault root.

### 7.1 `add_dirs` deprecation path

`Task.add_dirs` (currently the mechanism for exposing the vault to the agent) is **kept as an escape hatch** for ad-hoc paths but is no longer how the vault is injected. The runtime layer derives its allowed-paths set as:

```
allowed_paths = {a.workspace_path for a in attachments}
              ∪ {p for p in task.add_dirs if p not in attachment_paths}
```

Deduplication is explicit: any `add_dirs` entry that matches an attachment path is dropped. Migration **does not** scrub vault paths from existing `Task.add_dirs` rows — the dedup rule handles them at runtime.

---

## 8. Runtime Integration

### 8.1 `TaskContext` change

`TaskContext` currently exposes `checkout_path: str` (single path). It gains:

```python
@dataclass
class TaskContext:
    primary_path: str | None                      # back-compat alias for checkout_path
    workspace_attachments: list[WorkspaceAttachment]  # all locked + auto-attached workspaces
    add_dirs: list[str]                           # ad-hoc escape-hatch paths (post-dedup)
    # ... existing fields unchanged
```

The field is named `workspace_attachments` (not `attachments`) to avoid clashing with the existing `Task.attachments: list[str]` field (file attachments — images, etc.).

**`primary_path` selection rule:** the path of the `project-repo` workspace_attachment if present; otherwise `None`. This is deterministic — `project-repo` is a well-known kind id, not a heuristic over capability flags. Tasks that legitimately have multiple writable lockable kinds (e.g. cross-repo merge) address them by alias via `workspace_attachments`.

Each `WorkspaceAttachment` carries `(kind_id, alias, workspace_path, writable, lockable)` so runtimes and prompt builders can reason about provenance.

### 8.2 `Runtime.requires_workspace`

Becomes advisory metadata. The hard gate is the orchestrator: if `effective_requirements(task)` is empty (no explicit, no synthesized `project-repo`, no auto-attach) *and* the runtime has `requires_workspace=True`, dispatch fails with a clear error. Supervisor (`requires_workspace=False`) continues to be exempt.

### 8.3 Prompt builder

`PromptBuilder` is updated to render attachments in a structured block:

```
## Workspaces
- project-repo (writable, locked) → /var/atom/checkouts/atom-claude/main
- package-foo  (writable, locked) → /var/atom/checkouts/package-foo/feature-x
- reference-docs (read-only)      → /var/atom/refs/docs
- vault                           → /Users/jack/.agent-queue/vault/projects/atom-claude
```

Aliases, when present, are rendered alongside the kind id.

---

## 9. Migration

A single Alembic revision performs the schema changes and seeds data. The migration is idempotent — every step has an explicit matching key.

### 9.1 Schema migration

1. Create `workspace_kinds` table with composite PK `(project_id, id)`, both NOT NULL.
2. Create `task_workspace_requirements` table with composite PK `(task_id, kind_id, position)`.
3. Add nullable `kind_id` column to `workspaces`.

### 9.2 Data migration (in same revision)

Each step uses an explicit `INSERT ... ON CONFLICT DO NOTHING` (Postgres) / `INSERT OR IGNORE` (SQLite) idiom against the keys noted:

1. **Seed system kinds.** Insert system rows into `workspace_kinds` for `project-repo`, `vault`, `readonly-dir`. Match key: `(project_id='__system__', id)`.
2. **Bind existing workspaces to `project-repo`.** `UPDATE workspaces SET kind_id = 'project-repo' WHERE kind_id IS NULL`. Idempotent on re-run because the `WHERE` clause excludes already-bound rows.
3. **Provision per-project vault workspaces.** For each project missing a `kind_id='vault'` workspace, insert one with `workspace_path = ~/.agent-queue/vault/projects/<pid>/`. Match key: `(project_id, kind_id='vault')`. Skip if any vault-kind workspace exists for the project (do not overwrite a custom `workspace_path`). On first migration run, no `kind_id='vault'` rows exist by construction — every project gets a vault workspace inserted. On re-runs the predicate prevents duplicates and preserves any operator-customized vault path.

The data migration directly INSERTs the system kind rows — it does **not** depend on the `WorkspaceKindStore` watcher running first (which would be a chicken-and-egg). The watcher's job becomes "ensure markdown exists for what's in the DB and reconcile any markdown changes."

### 9.3 Vault markdown bootstrap

On first daemon start after the migration:

- The `WorkspaceKindStore` watcher ensures `vault/workspace-kinds/` exists; for every system kind row already in the DB, it writes a markdown file with that kind's frontmatter if the file is missing.
- For every project, it ensures `vault/projects/<pid>/workspace-kinds/` exists (created empty if not).

After bootstrap the markdown becomes the source of truth: subsequent edits to those files reconcile back to the DB. The seeded DB rows give the watcher a starting point and ensure the daemon comes up usable even before any markdown is written.

### 9.4 Recovery from partial migration

The migration is one Alembic revision; if it crashes mid-way, Alembic rolls back the transaction and the next `alembic upgrade head` retries from the beginning. Because every step is keyed (§9.2), retry produces the same result.

### 9.5 Back-compat window

`workspaces.kind_id` stays nullable for one minor version. After every install has been migrated, a follow-up migration tightens it to NOT NULL. The `Task.preferred_workspace_id` advisory hint stays indefinitely.

---

## 10. CLI / MCP / Discord Surface

New / updated commands (in `CommandHandler`, exposed via all three surfaces):

- **`list_workspace_kinds(project_id=None)`** — list system + project-scoped kinds. New.
- **`onboard_project(...)`** — the dashboard and CLI path for creating a project with its primary `project-repo` workspace. It validates a root-relative repository destination and orchestrates project, workspace, vault, and Git operations as specified in the project-onboarding design; it does not accept arbitrary filesystem paths.
- **`add_workspace(project_id, kind_id=None, workspace_path, source_type, ...)`** — gains `kind_id`. It remains the lower-level workspace operation. When `kind_id` is None, defaults to `project-repo` *only if* that kind resolves for the project; otherwise raises a clear error. The system `project-repo` kind is treated as undeletable (recreated on daemon start) so the default reliably resolves.
- **`create_task(...)`** — gains `requires_kinds` parameter (see §5.1).

Editing kind definitions is done by editing the markdown files in the vault — no dedicated edit command, same as profiles and MCP servers.

---

## 11. Non-Goals

- **Tagged / affinity-based instance selection.** First-unlocked is sufficient. Tags can be added later as a separate column on `workspaces`.
- **Cross-task lock coordination.** No "give me workspace X if task Y also gets workspace Z." Each task's acquisition is independent.
- **DIRECTORY_ISOLATED implementation.** Still deferred (see [[agent-coordination]] §7).
- **Profile-level kind capability gates.** Tasks declare needs directly. Profiles do not constrain which kinds a task can request.
- **`enabled` flag on kinds.** Override management is by file presence (delete a project-scoped file to fall back to the system row). Adding an `enabled` flag is unnecessary surface for a problem the file system already solves.
- **Auto-clone of missing kind instances.** If a kind is declared but no workspace instance exists, the task fails acquisition — the operator provisions an instance manually (§5.4 rejects this at create time only when the *kind* doesn't resolve, not the instance). Auto-cloning from `kind.repo_url` is tracked as future work.

---

## 12. Open Questions

- **Naming the requirements table.** `task_workspace_requirements` is descriptive but long. Acceptable alternative: `task_kinds`. Defer to implementation.
- **Auto-clone follow-on.** Worth a dedicated mini-spec — when `acquire_for_task` finds a kind has no instances and `is_git_repo=true` with a `repo_url`, should it clone on demand? Probably yes for QoL; specced separately.

---

## 13. Affected Code

Implementation will touch:

- `src/database/tables.py` — three schema changes (new tables + column).
- `migrations/versions/<new>_workspaces_v2.py` — Alembic migration with schema + data steps.
- `src/models.py` — `WorkspaceKind`, `ResolvedRequirement`, `WorkspaceAttachment`, `WorkspaceAttachmentSet` dataclasses; updates to `Workspace`, `Task`, `TaskContext`. Note: `WorkspaceAttachment` is intentionally distinct from the existing `Task.attachments` field (file attachments).
- `src/database/queries/workspace_queries.py` — multi-kind, multi-instance acquisition + release, including `acquire_one_unlocked` per-dialect strategies.
- `src/database/queries/workspace_kinds_queries.py` — new file, CRUD for kinds.
- `src/orchestrator/workspace.py` — `acquire_for_task`, `effective_requirements`, refactored `_prepare_workspace` per §6.6.
- `src/orchestrator/execution.py` — pass `AttachmentSet` to runtime layer.
- `src/runtimes/base.py` — `TaskContext` field additions; `requires_workspace` becomes advisory.
- `src/runtimes/claude_sdk.py` and `src/runtimes/acpx.py` — derive cwd + extra dirs from attachments using §7.1 dedup rule.
- `src/profiles/workspace_kind_registry.py` — new file alongside `mcp_registry.py`: in-memory registry + vault watcher.
- `src/profiles/workspace_kind_parser.py` — new file, markdown-with-frontmatter parser.
- `src/commands/` — `list_workspace_kinds`, `add_workspace` updates, `create_task` updates.
- `src/tools/registry.py` — register `list_workspace_kinds`.
- `src/prompt_builder.py` — render `Workspaces` block.
- `tests/` — see §14 for required coverage.
- `vault/workspace-kinds/{project-repo,vault,readonly-dir}.md` — built-in defaults bootstrapped on first daemon start.

---

## 14. Required Test Coverage

The implementation must include explicit tests for each of these scenarios:

1. **Concurrent same-kind acquisition.** Two tasks acquiring the same kind with one available instance — exactly one wins, the other returns NULL/raises and the task stays queued.
2. **Deadlock-order safety under contention.** Set up two writable+lockable kinds `A` and `B`, each with one provisioned instance. Task X requires `[A, B]`, task Y requires `[A, B]` — they ask for the *same* set, so the canonical sort produces the same lock order for both. Submit them concurrently; one acquires both, the other rolls back cleanly with no partial holds. Then invert the test: task X requires `[A, B]`, task Y requires `[B, A]` — verify `effective_requirements()` produces identical canonical orders for both (proving the input order doesn't escape the sort). This validates both the canonical-order claim of §6.3 and the all-or-nothing rollback of §6.2.
3. **Partial-failure rollback.** Task wants `[A, B]`. A is acquired successfully, B fails. After rollback, A is **not** still locked.
4. **Migration idempotency.** Run the migration twice end-to-end; no duplicate kinds, no duplicate vault workspaces, no orphan rows.
5. **Resolution precedence.** Project-scoped kind with id `X` shadows the system kind with id `X`; deleting the project file falls back to the system row.
6. **Auto-attach.** Task with no `requires_kinds` attaches the project's `vault` (and any other `auto_attach=true` kinds) without explicit declaration; the attachment appears in `TaskContext.attachments`.
7. **Single-workspace back-compat.** Existing tasks created before this migration with no requirement rows continue to receive a `project-repo` attachment via `effective_requirements()` synthesis.
8. **`add_dirs` dedup.** A task whose `add_dirs` includes its vault path does not see the vault listed twice in runtime allowed-paths.
9. **Per-dialect concurrency.** Same acquisition test suite passes against both SQLite and Postgres.

---

## 15. Rollout

1. Land schema + data migration with `kind_id` nullable. Existing flows untouched.
2. Land `WorkspaceKindStore` + vault layout + built-in kinds (markdown bootstrap on daemon start).
3. Land `effective_requirements`, `acquire_for_task`, and refactor `_prepare_workspace` to delegate. Single-workspace behavior preserved.
4. Land `requires_kinds` on `create_task` + playbook stages.
5. Land `TaskContext.attachments` and update runtimes.
6. Update prompt builder.
7. Tighten `workspaces.kind_id` to NOT NULL in a follow-up migration after one minor version.
