# Workspaces v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the multi-kind, multi-instance workspace model from `docs/specs/design/workspaces-v2.md` additively — existing single-workspace flows must keep working at every commit.

**Architecture:** Normalized schema (`workspace_kinds` defines types, `workspaces.kind_id` binds instances, `task_workspace_requirements` declares per-task needs). Markdown source of truth in `vault/[projects/<pid>/]workspace-kinds/`. Single load-bearing function `effective_requirements(task)` computes what to acquire; `acquire_for_task(task)` does it atomically with canonical lock order for deadlock-freedom. Vault becomes just another auto-attached kind.

**Tech Stack:** Python 3.12, SQLAlchemy Core (SQLite + Postgres), Alembic, pytest (asyncio auto mode, xdist), aiosqlite, asyncpg. Repo at `/Users/jack.kern/Shared/AI/agent-queue`.

**Spec:** `docs/specs/design/workspaces-v2.md` (commit `77f03410`).

**Branch:** Already on `workspaces-v2` branch off `main`.

**Current alembic head:** `e4f2a8b1d6c9` — use as `down_revision` for the schema migration in Phase 1.

---

## Phase Layout

1. **Schema + Alembic migration** — adds `workspace_kinds`, `task_workspace_requirements`, `workspaces.kind_id`. Seeds system kinds + per-project vault rows. No behavior change yet (`kind_id` populated but unused at runtime).
2. **Model layer** — `WorkspaceKind`, `ResolvedRequirement`, `WorkspaceAttachment`, `WorkspaceAttachmentSet` dataclasses; updates to `Workspace`, `Task`, `TaskContext`.
3. **Workspace-kind queries** — CRUD + resolution (`resolve_kind` with project-scoped → system fallback).
4. **Vault watcher (`WorkspaceKindStore`)** — markdown ↔ DB reconcile, system kind bootstrap, per-project directory init.
5. **Multi-kind acquisition** — `effective_requirements`, `acquire_one_unlocked` (per-dialect), `acquire_for_task`, refactor `_prepare_workspace` to wrapper.
6. **Task requirement intake** — `create_task` accepts `requires_kinds`; playbook stages thread it through; validation against §3.5.
7. **Runtime integration** — `TaskContext.workspace_attachments` populated; runtimes derive cwd + extra dirs from attachments; `add_dirs` dedup.
8. **Prompt builder + commands** — render `Workspaces` block; `list_workspace_kinds` MCP/CLI/Discord; `add_workspace` accepts `kind_id`.

Phases 1–4 leave the system in working single-workspace mode (kind_id populated, but acquisition still uses old code path). Phase 5 flips the switch. Phases 6–8 expose the new capability and polish.

---

## Phase 1 — Schema + Alembic Migration

After this phase, the daemon still uses the old single-workspace acquisition path. The new tables exist and `workspaces.kind_id` is populated; no code reads it yet. Each task ends in a commit so a regression can be bisected.

### Task 1.1: Add new tables to `tables.py`

**Files:**
- Modify: `src/database/tables.py` (add two tables, one column near line 233)

- [ ] **Step 1: Read the surrounding context**

Read `src/database/tables.py:218-266` (current `workspaces` and `agent_profiles` definitions). The new tables go between them.

- [ ] **Step 2: Add `workspace_kinds` table**

Insert this block in `src/database/tables.py` immediately after the `workspaces` table definition (after the closing `)` and `# hooks and hook_runs tables removed` comment):

```python
workspace_kinds = Table(
    "workspace_kinds",
    metadata,
    # Composite PK (project_id, id). project_id uses sentinel '__system__'
    # for system-wide rows so the column can be NOT NULL on Postgres.
    # See docs/specs/design/workspaces-v2.md §3.1.
    Column("project_id", Text, nullable=False, primary_key=True),
    Column("id", Text, nullable=False, primary_key=True),
    Column("description", Text, nullable=False, server_default="''"),
    Column("writable", Boolean, nullable=False, server_default=true()),
    Column("lockable", Boolean, nullable=False, server_default=true()),
    Column("is_git_repo", Boolean, nullable=False, server_default=true()),
    Column("repo_url", Text, nullable=True),
    # Lowercase enum value: 'exclusive' | 'branch_isolated' | 'directory_isolated'
    # — matches WorkspaceMode.value.
    Column("default_lock_mode", Text, nullable=True),
    Column("auto_attach", Boolean, nullable=False, server_default="false"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
```

- [ ] **Step 3: Add `task_workspace_requirements` table**

Insert immediately after `workspace_kinds`:

```python
task_workspace_requirements = Table(
    "task_workspace_requirements",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id"), primary_key=True),
    # kind_id is a soft reference (no FK) — resolution depends on
    # (project_id, kind_id) and may target either the project-scoped
    # row or the system row.  See spec §3.2 / §3.5.
    Column("kind_id", Text, nullable=False, primary_key=True),
    Column("position", Integer, nullable=False, server_default="0", primary_key=True),
    Column("alias", Text, nullable=True),
    Index("idx_task_ws_reqs_task_id", "task_id"),
)
```

- [ ] **Step 4: Add `kind_id` column to `workspaces` table**

In the existing `workspaces` table definition (around line 218), insert this column after the `name` column and before `locked_by_agent_id`:

```python
    Column("kind_id", Text, nullable=True),  # Soft ref; resolved against (project_id, kind_id) at use time. NOT NULL in follow-up migration after one minor version (spec §9.5).
```

- [ ] **Step 5: Verify imports cover what we added**

Confirm `Boolean`, `Text`, `Integer`, `Float`, `ForeignKey`, `Index`, `Table`, `Column`, `true` are already imported at the top of the file (lines 14-27). They are. No new imports needed.

- [ ] **Step 6: Run the test suite to confirm nothing broke (no migration yet)**

Run: `source .venv/bin/activate && pytest tests/test_database.py -x -q 2>&1 | tail -20`
Expected: tests pass — the new table definitions exist in metadata but the dev DB hasn't been migrated. Tests that use `metadata.create_all` will pick them up; tests against an existing dev DB will fail until Task 1.2 runs. If many tests fail with "no such table: workspace_kinds", that confirms create_all is in scope and we're good — the new tables are picked up by tests that build a fresh DB. If tests fail with "no such column: kind_id", same story.

- [ ] **Step 7: Commit**

```bash
git add src/database/tables.py
git commit -m "schema: add workspace_kinds, task_workspace_requirements, workspaces.kind_id

Source-of-truth schema for workspaces-v2 (spec §3). Migration follows
in next commit; existing acquisition path is unchanged."
```

### Task 1.2: Generate the Alembic migration

**Files:**
- Create: `migrations/versions/2026_05_07_workspaces_v2.py`

- [ ] **Step 1: Confirm the head revision**

Run: `source .venv/bin/activate && alembic heads`
Expected: `e4f2a8b1d6c9 (head)` (single head). If anything else, **stop and surface to the operator**.

- [ ] **Step 2: Autogenerate the migration**

Run: `source .venv/bin/activate && alembic revision --autogenerate -m "add workspaces_v2 schema"`
Expected: a new file in `migrations/versions/` with revision id printed. Note the revision id (call it `<NEW_REV>`).

- [ ] **Step 3: Review the autogenerated file**

Open the generated file. Alembic should have created:
- `op.create_table('workspace_kinds', ...)` matching the schema in Task 1.1.
- `op.create_table('task_workspace_requirements', ...)` matching the schema.
- An `op.add_column('workspaces', sa.Column('kind_id', sa.Text(), nullable=True))` and an `op.batch_alter_table` wrap on SQLite.

If autogenerate misses anything (it sometimes does for `Index`), add the missing op manually. Reference: the upgrade and downgrade functions must be symmetric.

- [ ] **Step 4: Add the data-migration block**

In `upgrade()`, after the `create_table` calls, append the following data migration. This seeds system kinds and per-project vault workspaces per spec §9.2.

```python
import time
from sqlalchemy.sql import table, column, select as sa_select

def upgrade() -> None:
    # ... autogenerated schema ops above ...

    # ── Data migration (spec §9.2) ─────────────────────────────────────
    bind = op.get_bind()
    now = time.time()

    # Lightweight table refs for inserts.
    wk = table(
        "workspace_kinds",
        column("project_id"), column("id"), column("description"),
        column("writable"), column("lockable"), column("is_git_repo"),
        column("repo_url"), column("default_lock_mode"), column("auto_attach"),
        column("created_at"), column("updated_at"),
    )
    ws = table(
        "workspaces",
        column("id"), column("project_id"), column("workspace_path"),
        column("source_type"), column("name"), column("kind_id"),
        column("locked_by_agent_id"), column("locked_by_task_id"),
        column("locked_at"), column("lock_mode"), column("enabled"),
        column("created_at"),
    )
    proj = table("projects", column("id"))

    # Step 1: Seed system kinds (idempotent via WHERE NOT EXISTS).
    system_kinds = [
        dict(project_id="__system__", id="project-repo",
             description="Default project repository — single writable, "
                         "exclusively-locked clone of the project repo.",
             writable=True, lockable=True, is_git_repo=True,
             repo_url=None, default_lock_mode="exclusive", auto_attach=False,
             created_at=now, updated_at=now),
        dict(project_id="__system__", id="vault",
             description="Project vault — agent memory, notes, knowledge bases. "
                         "Auto-attached to every task; not lockable.",
             writable=True, lockable=False, is_git_repo=False,
             repo_url=None, default_lock_mode=None, auto_attach=True,
             created_at=now, updated_at=now),
        dict(project_id="__system__", id="readonly-dir",
             description="Read-only reference directory — docs, schemas, peer "
                         "projects. Not writable, not lockable.",
             writable=False, lockable=False, is_git_repo=False,
             repo_url=None, default_lock_mode=None, auto_attach=False,
             created_at=now, updated_at=now),
    ]
    existing = {
        (r[0], r[1])
        for r in bind.execute(sa_select(wk.c.project_id, wk.c.id)).fetchall()
    }
    for k in system_kinds:
        if (k["project_id"], k["id"]) not in existing:
            bind.execute(wk.insert().values(**k))

    # Step 2: Bind existing workspaces to project-repo (idempotent).
    bind.execute(
        ws.update()
          .where(ws.c.kind_id.is_(None))
          .values(kind_id="project-repo")
    )

    # Step 3: Provision per-project vault workspaces (idempotent).
    project_ids = [row[0] for row in bind.execute(sa_select(proj.c.id)).fetchall()]
    existing_vault = {
        row[0]
        for row in bind.execute(
            sa_select(ws.c.project_id).where(ws.c.kind_id == "vault")
        ).fetchall()
    }
    import os
    import uuid
    vault_root = os.path.expanduser("~/.agent-queue/vault/projects")
    for pid in project_ids:
        if pid in existing_vault:
            continue
        bind.execute(ws.insert().values(
            id=f"vault-{pid}-{uuid.uuid4().hex[:8]}",
            project_id=pid,
            workspace_path=os.path.join(vault_root, pid),
            source_type="link",
            name="vault",
            kind_id="vault",
            locked_by_agent_id=None,
            locked_by_task_id=None,
            locked_at=None,
            lock_mode=None,
            enabled=True,
            created_at=now,
        ))
```

In `downgrade()`, the existing autogen `drop_table` calls already remove `task_workspace_requirements` and `workspace_kinds`, and the `kind_id` column drop reverses Task 1.1 step 4. No data-migration downgrade is needed (the data lives in the dropped tables and the nullable column).

- [ ] **Step 5: Apply against the dev database**

Run: `source .venv/bin/activate && alembic upgrade head 2>&1 | tail -10`
Expected: `INFO  [alembic.runtime.migration] Running upgrade e4f2a8b1d6c9 -> <NEW_REV>, add workspaces_v2 schema`. No tracebacks.

- [ ] **Step 6: Verify the schema and seed data**

Run: `python -c "
import sqlite3, os
db = os.path.expanduser('~/.agent-queue/agent_queue.db')
con = sqlite3.connect(db)
cur = con.cursor()
print('workspace_kinds:')
for row in cur.execute('SELECT project_id, id, writable, lockable, is_git_repo, auto_attach FROM workspace_kinds ORDER BY project_id, id'):
    print('  ', row)
print()
print('workspaces.kind_id distribution:')
for row in cur.execute('SELECT kind_id, COUNT(*) FROM workspaces GROUP BY kind_id'):
    print('  ', row)
print()
print('per-project vault workspaces:')
for row in cur.execute(\"SELECT project_id, workspace_path FROM workspaces WHERE kind_id = 'vault' ORDER BY project_id\"):
    print('  ', row)
"`
Expected: three system kinds (`__system__/project-repo`, `__system__/vault`, `__system__/readonly-dir`); existing workspaces have `kind_id='project-repo'`; one `kind_id='vault'` row per project pointing at `~/.agent-queue/vault/projects/<pid>`.

- [ ] **Step 7: Test idempotency by running the migration twice**

Run: `source .venv/bin/activate && alembic downgrade -1 && alembic upgrade head && alembic upgrade head 2>&1 | tail -5`
Expected: the second `alembic upgrade head` is a no-op (already at head). Re-run the verification from step 6 — still only three system kinds, still one vault per project, no duplicates.

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/2026_05_07_workspaces_v2.py
git commit -m "migrate: workspaces v2 schema + seed system kinds + bind existing workspaces

Idempotent data migration per spec §9.2:
1. Seed __system__ kinds (project-repo, vault, readonly-dir)
2. Bind existing workspaces to kind_id='project-repo'
3. Provision per-project vault workspaces (lazy on duplicate)"
```

### Task 1.3: Add a regression test for the migration

**Files:**
- Create: `tests/test_migration_workspaces_v2.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_workspaces_v2.py`:

```python
"""Regression: workspaces v2 migration is idempotent and seeds expected data."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.asyncio
async def test_migration_seeds_system_kinds():
    """After migrating a fresh DB, the three system kinds exist."""
    import sqlalchemy as sa
    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        url = f"sqlite:///{db_path}"
        cfg = _alembic_config(url)
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id FROM workspace_kinds WHERE project_id = '__system__' "
                    "ORDER BY id"
                )
            ).fetchall()
        ids = sorted(row[0] for row in rows)
        assert ids == ["project-repo", "readonly-dir", "vault"], ids


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    """Running the migration twice yields the same data."""
    import sqlalchemy as sa
    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        url = f"sqlite:///{db_path}"
        cfg = _alembic_config(url)
        command.upgrade(cfg, "head")
        # Downgrade one and re-upgrade.
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            count = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM workspace_kinds WHERE project_id = '__system__'"
                )
            ).scalar()
        assert count == 3, f"expected 3 system kinds after re-migration, got {count}"


@pytest.mark.asyncio
async def test_migration_binds_existing_workspaces():
    """Workspaces present before the migration get kind_id='project-repo'."""
    import sqlalchemy as sa
    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        url = f"sqlite:///{db_path}"
        cfg = _alembic_config(url)

        # Migrate to one revision before workspaces_v2 so we can insert a workspace.
        command.upgrade(cfg, "e4f2a8b1d6c9")  # head before this migration

        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, credit_weight, max_concurrent_agents, "
                "status, total_tokens_used, created_at) "
                "VALUES ('p1', 'Test Project', 1.0, 1, 'ACTIVE', 0, 0.0)"
            ))
            conn.execute(sa.text(
                "INSERT INTO workspaces (id, project_id, workspace_path, source_type, "
                "enabled, created_at) "
                "VALUES ('w1', 'p1', '/tmp/ws1', 'clone', 1, 0.0)"
            ))

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            kind = conn.execute(
                sa.text("SELECT kind_id FROM workspaces WHERE id = 'w1'")
            ).scalar()
            vault_count = conn.execute(
                sa.text("SELECT COUNT(*) FROM workspaces WHERE project_id = 'p1' AND kind_id = 'vault'")
            ).scalar()
        assert kind == "project-repo", kind
        assert vault_count == 1, f"expected 1 vault workspace for project, got {vault_count}"
```

- [ ] **Step 2: Run the test**

Run: `source .venv/bin/activate && pytest tests/test_migration_workspaces_v2.py -v 2>&1 | tail -20`
Expected: 3 passes. If autogenerate created the migration with a different revision id than `e4f2a8b1d6c9` for the prior head, update the test's downgrade target to match (use `alembic history` to confirm).

- [ ] **Step 3: Commit**

```bash
git add tests/test_migration_workspaces_v2.py
git commit -m "test: workspaces v2 migration is idempotent and seeds system kinds"
```

---

## Phase 2 — Model Layer

After this phase, the new dataclasses exist and can be constructed/serialized. Nothing reads or writes them at the orchestrator/runtime level yet.

### Task 2.1: Add `WorkspaceKind` dataclass

**Files:**
- Modify: `src/models.py` (add new dataclass + supporting types)

- [ ] **Step 1: Locate insertion point**

Read `src/models.py:360-378` (current `Workspace` dataclass). The new types go immediately after.

- [ ] **Step 2: Add `WorkspaceKind`**

Insert after the `Workspace` dataclass:

```python
SYSTEM_KIND_SCOPE = "__system__"


@dataclass
class WorkspaceKind:
    """Definition of a workspace type. See spec §3.1."""

    project_id: str  # SYSTEM_KIND_SCOPE for system-wide rows
    id: str
    description: str = ""
    writable: bool = True
    lockable: bool = True
    is_git_repo: bool = True
    repo_url: str | None = None
    default_lock_mode: str | None = None  # "exclusive" | "branch_isolated" | "directory_isolated"
    auto_attach: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
```

- [ ] **Step 3: Add `ResolvedRequirement`, `WorkspaceAttachment`, `WorkspaceAttachmentSet`**

Insert immediately after `WorkspaceKind`:

```python
@dataclass
class ResolvedRequirement:
    """A task's request for one workspace of a given kind. See spec §6.1.

    `position` is part of the canonical lock order (kind_id, position).
    `preferred_workspace_id` is only set on the synthesized project-repo
    requirement (carrying `Task.preferred_workspace_id`).
    """

    kind_id: str
    alias: str | None = None
    position: int = 0
    preferred_workspace_id: str | None = None


@dataclass
class WorkspaceAttachment:
    """A workspace bound to a task at acquisition time. See spec §8.1.

    Distinct from `Task.attachments` (file attachments) — see naming
    note in spec §6.2.
    """

    requirement: ResolvedRequirement
    workspace: Workspace
    kind: WorkspaceKind

    @property
    def kind_id(self) -> str:
        return self.requirement.kind_id

    @property
    def alias(self) -> str | None:
        return self.requirement.alias

    @property
    def workspace_path(self) -> str:
        return self.workspace.workspace_path

    @property
    def writable(self) -> bool:
        return self.kind.writable

    @property
    def lockable(self) -> bool:
        return self.kind.lockable


@dataclass
class WorkspaceAttachmentSet:
    """All workspace attachments acquired for a task. See spec §6.2."""

    attachments: list[WorkspaceAttachment] = field(default_factory=list)

    def by_kind(self, kind_id: str) -> list[WorkspaceAttachment]:
        return [a for a in self.attachments if a.kind_id == kind_id]

    def first_of_kind(self, kind_id: str) -> WorkspaceAttachment | None:
        for a in self.attachments:
            if a.kind_id == kind_id:
                return a
        return None

    @property
    def primary_path(self) -> str | None:
        """Path of the project-repo attachment if present (spec §8.1)."""
        a = self.first_of_kind("project-repo")
        return a.workspace_path if a else None
```

- [ ] **Step 4: Add `kind_id` field to `Workspace` dataclass**

Locate the `Workspace` dataclass (around line 360). Add a `kind_id` field:

```python
@dataclass
class Workspace:
    id: str
    project_id: str
    workspace_path: str
    source_type: RepoSourceType
    name: str | None = None
    kind_id: str | None = None  # spec §3.2 — soft ref to workspace_kinds
    locked_by_agent_id: str | None = None
    locked_by_task_id: str | None = None
    locked_at: float | None = None
    lock_mode: WorkspaceMode | None = None
    enabled: bool = True
```

- [ ] **Step 5: Write a smoke test for the dataclasses**

Add to `tests/test_models.py` (or create if absent):

```python
def test_workspace_attachment_set_helpers():
    from src.models import (
        ResolvedRequirement, Workspace, WorkspaceAttachment,
        WorkspaceAttachmentSet, WorkspaceKind, RepoSourceType,
    )
    kind = WorkspaceKind(project_id="__system__", id="project-repo")
    ws = Workspace(
        id="w1", project_id="p1", workspace_path="/tmp/ws1",
        source_type=RepoSourceType.CLONE, kind_id="project-repo",
    )
    req = ResolvedRequirement(kind_id="project-repo", position=0)
    att = WorkspaceAttachment(requirement=req, workspace=ws, kind=kind)
    s = WorkspaceAttachmentSet(attachments=[att])
    assert s.primary_path == "/tmp/ws1"
    assert s.first_of_kind("project-repo") is att
    assert s.first_of_kind("vault") is None
```

- [ ] **Step 6: Run the test**

Run: `source .venv/bin/activate && pytest tests/test_models.py::test_workspace_attachment_set_helpers -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/models.py tests/test_models.py
git commit -m "models: add WorkspaceKind, ResolvedRequirement, WorkspaceAttachment, WorkspaceAttachmentSet

Dataclasses for spec §3 + §6 + §8.  WorkspaceAttachment is intentionally
distinct from Task.attachments (file attachments)."
```

### Task 2.2: Update `_row_to_workspace` to populate `kind_id`

**Files:**
- Modify: `src/database/queries/workspace_queries.py:350-365` (`_row_to_workspace`)
- Modify: `src/database/queries/workspace_queries.py:25-41` (`create_workspace`)

- [ ] **Step 1: Update `_row_to_workspace` to read `kind_id`**

Edit `_row_to_workspace` to include the new column:

```python
@staticmethod
def _row_to_workspace(row) -> Workspace:
    raw_mode = row["lock_mode"]
    return Workspace(
        id=row["id"],
        project_id=row["project_id"],
        workspace_path=row["workspace_path"],
        source_type=RepoSourceType(row["source_type"]),
        name=row["name"],
        kind_id=row["kind_id"],
        locked_by_agent_id=row["locked_by_agent_id"],
        locked_by_task_id=row["locked_by_task_id"],
        locked_at=row["locked_at"],
        lock_mode=WorkspaceMode(raw_mode) if raw_mode else None,
        enabled=bool(row["enabled"]),
    )
```

- [ ] **Step 2: Update `create_workspace` to write `kind_id`**

In `create_workspace`, add `kind_id=workspace.kind_id` to the `.values(...)` block:

```python
async def create_workspace(self, workspace: Workspace) -> None:
    async with self._engine.begin() as conn:
        await conn.execute(
            insert(workspaces).values(
                id=workspace.id,
                project_id=workspace.project_id,
                workspace_path=workspace.workspace_path,
                source_type=workspace.source_type.value,
                name=workspace.name,
                kind_id=workspace.kind_id,
                locked_by_agent_id=workspace.locked_by_agent_id,
                locked_by_task_id=workspace.locked_by_task_id,
                locked_at=workspace.locked_at,
                enabled=workspace.enabled,
                created_at=time.time(),
            )
        )
```

- [ ] **Step 3: Run the existing workspace test suite**

Run: `source .venv/bin/activate && pytest tests/test_database.py -k workspace -v 2>&1 | tail -20`
Expected: all green. If any test breaks because it constructs a Workspace without `kind_id`, that's fine — the field defaults to None — but check the test isn't asserting the row reads back as something other than None.

- [ ] **Step 4: Commit**

```bash
git add src/database/queries/workspace_queries.py
git commit -m "queries: read/write Workspace.kind_id

Pass-through change so legacy code keeps working with the new column.
Acquisition path still ignores kind_id; that comes in Phase 5."
```

---

## Phase 3 — Workspace-Kind Queries

After this phase, code can CRUD `workspace_kinds` and resolve a kind id against the project-scoped → system fallback chain. Watcher integration comes in Phase 4.

### Task 3.1: New file `src/database/queries/workspace_kinds_queries.py`

**Files:**
- Create: `src/database/queries/workspace_kinds_queries.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_workspace_kinds_queries.py`:

```python
"""WorkspaceKindQueryMixin — CRUD + resolution. Spec §3.5."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from src.database import Database
from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind


@pytest_asyncio.fixture
async def db():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_upsert_and_get(db):
    kind = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="project-repo",
        description="Default project repo",
        writable=True, lockable=True, is_git_repo=True,
        default_lock_mode="exclusive",
        created_at=time.time(), updated_at=time.time(),
    )
    await db.upsert_workspace_kind(kind)
    fetched = await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "project-repo")
    assert fetched is not None
    assert fetched.description == "Default project repo"
    assert fetched.lockable is True


@pytest.mark.asyncio
async def test_resolve_project_overrides_system(db):
    sys_kind = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="vault",
        description="system vault",
        writable=True, lockable=False, is_git_repo=False, auto_attach=True,
        created_at=time.time(), updated_at=time.time(),
    )
    await db.upsert_workspace_kind(sys_kind)
    project_kind = WorkspaceKind(
        project_id="p1", id="vault",
        description="project vault override",
        writable=True, lockable=False, is_git_repo=False, auto_attach=True,
        created_at=time.time(), updated_at=time.time(),
    )
    await db.upsert_workspace_kind(project_kind)

    # Project p1 sees its override
    resolved = await db.resolve_workspace_kind("p1", "vault")
    assert resolved is not None
    assert resolved.project_id == "p1"
    assert resolved.description == "project vault override"

    # Project p2 sees the system row
    resolved = await db.resolve_workspace_kind("p2", "vault")
    assert resolved is not None
    assert resolved.project_id == SYSTEM_KIND_SCOPE


@pytest.mark.asyncio
async def test_resolve_returns_none_for_unknown(db):
    resolved = await db.resolve_workspace_kind("p1", "does-not-exist")
    assert resolved is None


@pytest.mark.asyncio
async def test_list_kinds_for_project_includes_system_and_overrides(db):
    sys_kind = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="vault",
        created_at=time.time(), updated_at=time.time(),
    )
    sys_kind2 = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="readonly-dir",
        writable=False, lockable=False, is_git_repo=False,
        created_at=time.time(), updated_at=time.time(),
    )
    project_override = WorkspaceKind(
        project_id="p1", id="vault",
        description="custom",
        created_at=time.time(), updated_at=time.time(),
    )
    project_only = WorkspaceKind(
        project_id="p1", id="package-foo",
        created_at=time.time(), updated_at=time.time(),
    )
    for k in (sys_kind, sys_kind2, project_override, project_only):
        await db.upsert_workspace_kind(k)

    kinds = await db.list_workspace_kinds_for_project("p1")
    by_id = {k.id: k for k in kinds}
    assert "vault" in by_id and by_id["vault"].project_id == "p1"  # override wins
    assert "readonly-dir" in by_id and by_id["readonly-dir"].project_id == SYSTEM_KIND_SCOPE
    assert "package-foo" in by_id and by_id["package-foo"].project_id == "p1"


@pytest.mark.asyncio
async def test_auto_attach_kinds(db):
    auto = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="vault",
        auto_attach=True,
        created_at=time.time(), updated_at=time.time(),
    )
    not_auto = WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="readonly-dir",
        auto_attach=False,
        created_at=time.time(), updated_at=time.time(),
    )
    await db.upsert_workspace_kind(auto)
    await db.upsert_workspace_kind(not_auto)

    autos = await db.list_auto_attach_kinds_for_project("p1")
    assert {k.id for k in autos} == {"vault"}


@pytest.mark.asyncio
async def test_delete_workspace_kind(db):
    kind = WorkspaceKind(
        project_id="p1", id="game-repo",
        created_at=time.time(), updated_at=time.time(),
    )
    await db.upsert_workspace_kind(kind)
    assert await db.get_workspace_kind("p1", "game-repo") is not None
    await db.delete_workspace_kind("p1", "game-repo")
    assert await db.get_workspace_kind("p1", "game-repo") is None
```

- [ ] **Step 2: Run the test (should fail)**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kinds_queries.py -v 2>&1 | tail -15`
Expected: ImportError or AttributeError — the methods don't exist yet.

- [ ] **Step 3: Implement `WorkspaceKindQueryMixin`**

Create `src/database/queries/workspace_kinds_queries.py`:

```python
"""WorkspaceKind CRUD and resolution. Spec §3.5."""

from __future__ import annotations

import time

from sqlalchemy import case, delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import workspace_kinds
from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind


class WorkspaceKindQueryMixin:
    """Query mixin for workspace_kinds. Expects ``self._engine``."""

    async def upsert_workspace_kind(self, kind: WorkspaceKind) -> None:
        """Insert or update a workspace kind, keyed by (project_id, id)."""
        now = time.time()
        values = dict(
            project_id=kind.project_id,
            id=kind.id,
            description=kind.description,
            writable=kind.writable,
            lockable=kind.lockable,
            is_git_repo=kind.is_git_repo,
            repo_url=kind.repo_url,
            default_lock_mode=kind.default_lock_mode,
            auto_attach=kind.auto_attach,
            created_at=kind.created_at or now,
            updated_at=now,
        )
        async with self._engine.begin() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                stmt = sqlite_insert(workspace_kinds).values(**values)
                update_cols = {
                    c: stmt.excluded[c]
                    for c in ("description", "writable", "lockable",
                              "is_git_repo", "repo_url", "default_lock_mode",
                              "auto_attach", "updated_at")
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "id"], set_=update_cols
                )
            elif dialect == "postgresql":
                stmt = pg_insert(workspace_kinds).values(**values)
                update_cols = {
                    c: stmt.excluded[c]
                    for c in ("description", "writable", "lockable",
                              "is_git_repo", "repo_url", "default_lock_mode",
                              "auto_attach", "updated_at")
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "id"], set_=update_cols
                )
            else:
                # Generic fallback: try update, then insert if 0 rows.
                result = await conn.execute(
                    update(workspace_kinds)
                    .where(
                        (workspace_kinds.c.project_id == kind.project_id)
                        & (workspace_kinds.c.id == kind.id)
                    )
                    .values(**{k: v for k, v in values.items()
                               if k not in ("project_id", "id")})
                )
                if result.rowcount == 0:
                    await conn.execute(insert(workspace_kinds).values(**values))
                return
            await conn.execute(stmt)

    async def get_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> WorkspaceKind | None:
        """Fetch a kind by exact (project_id, id) — no fallback."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspace_kinds).where(
                    (workspace_kinds.c.project_id == project_id)
                    & (workspace_kinds.c.id == kind_id)
                )
            )
            row = result.mappings().fetchone()
            return self._row_to_workspace_kind(row) if row else None

    async def resolve_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> WorkspaceKind | None:
        """Look up a kind for a project, falling back to the system row.

        See spec §3.5: project row wins over system row with same id.
        """
        # Try project-scoped row first.
        kind = await self.get_workspace_kind(project_id, kind_id)
        if kind is not None:
            return kind
        # Fall back to system row.
        return await self.get_workspace_kind(SYSTEM_KIND_SCOPE, kind_id)

    async def list_workspace_kinds_for_project(
        self, project_id: str
    ) -> list[WorkspaceKind]:
        """All kinds visible to a project: project rows + system rows whose
        ids are not shadowed by project rows."""
        async with self._engine.begin() as conn:
            project_rows = (await conn.execute(
                select(workspace_kinds).where(
                    workspace_kinds.c.project_id == project_id
                )
            )).mappings().fetchall()
            project_ids = {r["id"] for r in project_rows}
            system_rows = (await conn.execute(
                select(workspace_kinds).where(
                    workspace_kinds.c.project_id == SYSTEM_KIND_SCOPE
                )
            )).mappings().fetchall()
        out = [self._row_to_workspace_kind(r) for r in project_rows]
        for r in system_rows:
            if r["id"] not in project_ids:
                out.append(self._row_to_workspace_kind(r))
        out.sort(key=lambda k: k.id)
        return out

    async def list_auto_attach_kinds_for_project(
        self, project_id: str
    ) -> list[WorkspaceKind]:
        """Auto-attach kinds visible to a project (project rows shadow system)."""
        kinds = await self.list_workspace_kinds_for_project(project_id)
        return [k for k in kinds if k.auto_attach]

    async def delete_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(workspace_kinds).where(
                    (workspace_kinds.c.project_id == project_id)
                    & (workspace_kinds.c.id == kind_id)
                )
            )

    @staticmethod
    def _row_to_workspace_kind(row) -> WorkspaceKind:
        return WorkspaceKind(
            project_id=row["project_id"],
            id=row["id"],
            description=row["description"],
            writable=bool(row["writable"]),
            lockable=bool(row["lockable"]),
            is_git_repo=bool(row["is_git_repo"]),
            repo_url=row["repo_url"],
            default_lock_mode=row["default_lock_mode"],
            auto_attach=bool(row["auto_attach"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
```

- [ ] **Step 4: Wire the mixin into `Database`**

Locate `src/database/__init__.py` (or whatever module composes the mixins — `grep -rn 'WorkspaceQueryMixin' src/database/` to find it). Add `WorkspaceKindQueryMixin` to the `Database` class's MRO alongside `WorkspaceQueryMixin`. If the composition file imports mixins explicitly, add the import.

- [ ] **Step 5: Run the tests**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kinds_queries.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/database/queries/workspace_kinds_queries.py tests/test_workspace_kinds_queries.py src/database/__init__.py
git commit -m "queries: workspace_kinds CRUD + project→system resolution

Implements spec §3.5 resolution: project-scoped kind shadows system row
with same id.  Per-dialect upsert via ON CONFLICT (sqlite/postgres) with
generic fallback for other dialects."
```

---

## Phase 4 — Vault Watcher (`WorkspaceKindStore`)

After this phase, kind definitions in `vault/[projects/<pid>/]workspace-kinds/*.md` are reconciled into the DB on daemon start and on filesystem change. System kinds also get markdown bootstrapped.

### Task 4.1: Markdown parser for workspace kinds

**Files:**
- Create: `src/profiles/workspace_kind_parser.py`
- Create: `tests/test_workspace_kind_parser.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_workspace_kind_parser.py`:

```python
"""WorkspaceKind markdown frontmatter parser. Spec §4.1."""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.profiles.workspace_kind_parser import parse_workspace_kind_file


def test_parse_full_frontmatter(tmp_path: Path):
    md = tmp_path / "game-repo.md"
    md.write_text(textwrap.dedent("""
        ---
        id: game-repo
        description: Atom games monorepo
        writable: true
        lockable: true
        is_git_repo: true
        repo_url: git@github.com:atom/games.git
        default_lock_mode: branch_isolated
        auto_attach: false
        ---

        # game-repo

        Body text used as description fallback.
    """).strip())

    kind = parse_workspace_kind_file(md, project_id="p1")
    assert kind.id == "game-repo"
    assert kind.project_id == "p1"
    assert kind.description == "Atom games monorepo"
    assert kind.writable and kind.lockable and kind.is_git_repo
    assert kind.repo_url == "git@github.com:atom/games.git"
    assert kind.default_lock_mode == "branch_isolated"
    assert kind.auto_attach is False


def test_parse_uses_body_when_description_missing(tmp_path: Path):
    md = tmp_path / "k.md"
    md.write_text(textwrap.dedent("""
        ---
        id: k
        ---

        Body text.

        Second paragraph.
    """).strip())
    kind = parse_workspace_kind_file(md, project_id="__system__")
    assert kind.description == "Body text.\n\nSecond paragraph."


def test_parse_defaults(tmp_path: Path):
    md = tmp_path / "minimal.md"
    md.write_text("---\nid: minimal\n---\n")
    kind = parse_workspace_kind_file(md, project_id="__system__")
    assert kind.writable is True
    assert kind.lockable is True
    assert kind.is_git_repo is True
    assert kind.auto_attach is False
    assert kind.repo_url is None
    assert kind.default_lock_mode is None


def test_parse_rejects_missing_id(tmp_path: Path):
    import pytest
    md = tmp_path / "noid.md"
    md.write_text("---\nwritable: true\n---\n")
    with pytest.raises(ValueError, match="missing.*id"):
        parse_workspace_kind_file(md, project_id="__system__")
```

- [ ] **Step 2: Run the test (should fail)**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kind_parser.py -v 2>&1 | tail -10`
Expected: ImportError.

- [ ] **Step 3: Implement the parser**

Create `src/profiles/workspace_kind_parser.py`:

```python
"""Parse vault/[projects/<pid>/]workspace-kinds/<id>.md into WorkspaceKind."""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from src.models import WorkspaceKind


def parse_workspace_kind_file(path: Path, project_id: str) -> WorkspaceKind:
    """Parse one markdown file into a WorkspaceKind.

    Frontmatter format per spec §4.1.  Body text is used as description
    when the frontmatter `description` field is absent.
    """
    text = path.read_text()
    fm, body = _split_frontmatter(text)
    if "id" not in fm:
        raise ValueError(f"{path}: frontmatter is missing required key 'id'")

    description = fm.get("description") or body.strip()
    now = time.time()
    return WorkspaceKind(
        project_id=project_id,
        id=fm["id"],
        description=description,
        writable=bool(fm.get("writable", True)),
        lockable=bool(fm.get("lockable", True)),
        is_git_repo=bool(fm.get("is_git_repo", True)),
        repo_url=fm.get("repo_url"),
        default_lock_mode=fm.get("default_lock_mode"),
        auto_attach=bool(fm.get("auto_attach", False)),
        created_at=now,
        updated_at=now,
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    fm = yaml.safe_load(fm_text) or {}
    return fm, body
```

- [ ] **Step 4: Run the tests**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kind_parser.py -v 2>&1 | tail -10`
Expected: all 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/profiles/workspace_kind_parser.py tests/test_workspace_kind_parser.py
git commit -m "parse: workspace_kind markdown frontmatter parser

Reads vault/[projects/<pid>/]workspace-kinds/<id>.md into WorkspaceKind.
Falls back to body text when frontmatter description is missing."
```

### Task 4.2: `WorkspaceKindStore` — vault watcher

**Files:**
- Create: `src/profiles/workspace_kind_registry.py`
- Create: `tests/test_workspace_kind_registry.py`

- [ ] **Step 1: Read the existing `mcp_registry.py` for the watcher pattern**

Read `src/profiles/mcp_registry.py`. The new store mirrors this pattern: in-memory cache of parsed entities, filesystem watcher reconciling to DB, system-wide + project-scoped paths.

- [ ] **Step 2: Write the failing test**

Create `tests/test_workspace_kind_registry.py`:

```python
"""WorkspaceKindStore — markdown ↔ DB reconciliation. Spec §4."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from src.database import Database
from src.profiles.workspace_kind_registry import WorkspaceKindStore


@pytest_asyncio.fixture
async def db():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_initial_scan_inserts_system_kinds(tmp_path: Path, db):
    sys_dir = tmp_path / "workspace-kinds"
    sys_dir.mkdir()
    (sys_dir / "project-repo.md").write_text(
        textwrap.dedent("""
            ---
            id: project-repo
            writable: true
            lockable: true
            is_git_repo: true
            default_lock_mode: exclusive
            ---
            Default project repo.
        """).strip()
    )
    store = WorkspaceKindStore(db, vault_root=tmp_path)
    await store.scan()

    kind = await db.resolve_workspace_kind("any-project", "project-repo")
    assert kind is not None
    assert kind.lockable is True


@pytest.mark.asyncio
async def test_initial_scan_inserts_project_overrides(tmp_path: Path, db):
    proj_dir = tmp_path / "projects" / "p1" / "workspace-kinds"
    proj_dir.mkdir(parents=True)
    (proj_dir / "vault.md").write_text(
        textwrap.dedent("""
            ---
            id: vault
            description: project-specific vault
            writable: true
            lockable: false
            is_git_repo: false
            auto_attach: true
            ---
        """).strip()
    )
    store = WorkspaceKindStore(db, vault_root=tmp_path)
    await store.scan()

    kind = await db.resolve_workspace_kind("p1", "vault")
    assert kind is not None
    assert kind.project_id == "p1"
    assert kind.description == "project-specific vault"


@pytest.mark.asyncio
async def test_scan_removes_kinds_for_deleted_files(tmp_path: Path, db):
    proj_dir = tmp_path / "projects" / "p1" / "workspace-kinds"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "extra.md"
    f.write_text("---\nid: extra\n---\n")

    store = WorkspaceKindStore(db, vault_root=tmp_path)
    await store.scan()
    assert await db.get_workspace_kind("p1", "extra") is not None

    f.unlink()
    await store.scan()
    assert await db.get_workspace_kind("p1", "extra") is None


@pytest.mark.asyncio
async def test_bootstrap_creates_missing_system_markdown(tmp_path: Path, db):
    """If the DB has system kinds but no markdown exists, scan() writes the markdown."""
    import time
    from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind
    await db.upsert_workspace_kind(WorkspaceKind(
        project_id=SYSTEM_KIND_SCOPE, id="vault",
        description="Project vault", writable=True, lockable=False,
        is_git_repo=False, auto_attach=True,
        created_at=time.time(), updated_at=time.time(),
    ))

    store = WorkspaceKindStore(db, vault_root=tmp_path)
    await store.bootstrap()

    md = tmp_path / "workspace-kinds" / "vault.md"
    assert md.exists()
    assert "id: vault" in md.read_text()
```

- [ ] **Step 3: Run the test (should fail)**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kind_registry.py -v 2>&1 | tail -10`
Expected: ImportError.

- [ ] **Step 4: Implement the store**

Create `src/profiles/workspace_kind_registry.py`:

```python
"""WorkspaceKindStore — markdown ↔ DB reconciliation. Spec §4."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind
from src.profiles.workspace_kind_parser import parse_workspace_kind_file

logger = logging.getLogger(__name__)


class WorkspaceKindStore:
    """Reconciles `vault/[projects/<pid>/]workspace-kinds/*.md` ↔ DB."""

    def __init__(self, db, vault_root: Path):
        self.db = db
        self.vault_root = Path(vault_root)

    async def scan(self) -> None:
        """Full reconciliation: walk the vault, sync DB, prune deleted files."""
        seen: set[tuple[str, str]] = set()

        # System-wide kinds
        sys_dir = self.vault_root / "workspace-kinds"
        if sys_dir.is_dir():
            for f in sorted(sys_dir.glob("*.md")):
                try:
                    kind = parse_workspace_kind_file(f, project_id=SYSTEM_KIND_SCOPE)
                    await self.db.upsert_workspace_kind(kind)
                    seen.add((SYSTEM_KIND_SCOPE, kind.id))
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", f, e)

        # Project-scoped kinds
        projects_root = self.vault_root / "projects"
        if projects_root.is_dir():
            for proj_dir in sorted(projects_root.iterdir()):
                if not proj_dir.is_dir():
                    continue
                pid = proj_dir.name
                kinds_dir = proj_dir / "workspace-kinds"
                if not kinds_dir.is_dir():
                    continue
                for f in sorted(kinds_dir.glob("*.md")):
                    try:
                        kind = parse_workspace_kind_file(f, project_id=pid)
                        await self.db.upsert_workspace_kind(kind)
                        seen.add((pid, kind.id))
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", f, e)

        # Prune: any DB row not seen on disk gets deleted (per spec §3.5
        # — file delete reconciles to row delete).
        for pid_in_db in await self._all_scoped_pids():
            kinds = await self.db.list_workspace_kinds_for_project(pid_in_db) \
                if pid_in_db != SYSTEM_KIND_SCOPE else []
            # We already use `list_workspace_kinds_for_project` for project IDs.
            # System kinds need their own list.
            if pid_in_db == SYSTEM_KIND_SCOPE:
                kinds = [
                    k for k in await self._list_all_kinds()
                    if k.project_id == SYSTEM_KIND_SCOPE
                ]
            for k in kinds:
                if (k.project_id, k.id) not in seen and k.project_id == pid_in_db:
                    logger.info(
                        "Removing workspace_kind (%s, %s) — no markdown found",
                        k.project_id, k.id,
                    )
                    await self.db.delete_workspace_kind(k.project_id, k.id)

    async def bootstrap(self) -> None:
        """Ensure markdown exists for every kind in the DB.

        Called on daemon start so the operator can edit the files even
        when the migration seeded rows directly.  Spec §9.3.
        """
        for k in await self._list_all_kinds():
            md_path = self._path_for_kind(k.project_id, k.id)
            if md_path.exists():
                continue
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(self._render_kind_markdown(k))

    async def ensure_project_dir(self, project_id: str) -> None:
        """Create vault/projects/<pid>/workspace-kinds/ if missing.

        Called from the project-create flow.  Mirrors the mcp-servers
        convention.
        """
        d = self.vault_root / "projects" / project_id / "workspace-kinds"
        d.mkdir(parents=True, exist_ok=True)

    def _path_for_kind(self, project_id: str, kind_id: str) -> Path:
        if project_id == SYSTEM_KIND_SCOPE:
            return self.vault_root / "workspace-kinds" / f"{kind_id}.md"
        return self.vault_root / "projects" / project_id / "workspace-kinds" / f"{kind_id}.md"

    @staticmethod
    def _render_kind_markdown(k: WorkspaceKind) -> str:
        fm = {
            "id": k.id,
            "description": k.description,
            "writable": k.writable,
            "lockable": k.lockable,
            "is_git_repo": k.is_git_repo,
            "auto_attach": k.auto_attach,
        }
        if k.repo_url:
            fm["repo_url"] = k.repo_url
        if k.default_lock_mode:
            fm["default_lock_mode"] = k.default_lock_mode
        return f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n# {k.id}\n\n{k.description}\n"

    async def _list_all_kinds(self) -> list[WorkspaceKind]:
        """All kinds across all scopes — used by bootstrap and prune."""
        # Inefficient but clear; the table is tiny.
        from sqlalchemy import select
        from src.database.tables import workspace_kinds
        async with self.db._engine.begin() as conn:
            rows = (await conn.execute(select(workspace_kinds))).mappings().fetchall()
        from src.database.queries.workspace_kinds_queries import WorkspaceKindQueryMixin
        return [WorkspaceKindQueryMixin._row_to_workspace_kind(r) for r in rows]

    async def _all_scoped_pids(self) -> set[str]:
        """All project_ids that have at least one kind (system + per-project)."""
        return {k.project_id for k in await self._list_all_kinds()}
```

- [ ] **Step 5: Run the tests**

Run: `source .venv/bin/activate && pytest tests/test_workspace_kind_registry.py -v 2>&1 | tail -15`
Expected: 4 passes. If `bootstrap` test fails because of missing `WorkspaceKindQueryMixin._row_to_workspace_kind` access, refactor that helper to be a module-level function and re-import.

- [ ] **Step 6: Commit**

```bash
git add src/profiles/workspace_kind_registry.py tests/test_workspace_kind_registry.py
git commit -m "watcher: WorkspaceKindStore reconciles vault markdown with DB

Mirrors the mcp_registry pattern.  scan() syncs files into DB and prunes
deleted files; bootstrap() writes markdown for DB rows that have no file
yet (used after the migration seeds system kinds)."
```

### Task 4.3: Wire `WorkspaceKindStore` into daemon startup

**Files:**
- Modify: `src/main.py` or `src/orchestrator.py` (wherever the daemon initializes — `grep -rn 'mcp_registry' src/main.py src/orchestrator.py` to find the parallel hook)

- [ ] **Step 1: Locate the daemon-init hook**

Run: `grep -rn 'mcp_registry\|MCPRegistry' src/main.py src/orchestrator.py | head -10`

The MCP registry is initialized somewhere during orchestrator boot; the workspace kind store hooks in alongside it.

- [ ] **Step 2: Add the store init**

Add a `self.workspace_kind_store = WorkspaceKindStore(self.db, vault_root=...)` next to the MCP registry; call `await self.workspace_kind_store.scan()` and `await self.workspace_kind_store.bootstrap()` during `initialize()`. The vault root comes from config (it's already used for the MCP registry init — copy that pattern).

- [ ] **Step 3: Run the daemon briefly to confirm no boot regressions**

Run: `./run.sh start 2>&1 | head -50; sleep 3; ./run.sh stop 2>&1 | tail -5`
Expected: clean start, "WorkspaceKindStore initialized" or similar in logs (add a log line if absent), clean stop.

- [ ] **Step 4: Verify markdown was bootstrapped**

Run: `ls ~/.agent-queue/vault/workspace-kinds/`
Expected: `project-repo.md`, `vault.md`, `readonly-dir.md`.

- [ ] **Step 5: Commit**

```bash
git add src/main.py src/orchestrator.py
git commit -m "boot: initialize WorkspaceKindStore on daemon start

Scans vault for kind definitions, syncs to DB, then bootstraps markdown
for any DB row missing a file (covers the post-migration case)."
```

---

## Phase 5 — Multi-Kind Acquisition

This is the load-bearing phase. After it lands, single-workspace tasks still work unchanged via the wrapper, but the new code path is in use.

### Task 5.1: `task_workspace_requirements` queries

**Files:**
- Create: `src/database/queries/task_requirements_queries.py`
- Create: `tests/test_task_requirements_queries.py`

- [ ] **Step 1: Write tests for the new queries**

Create `tests/test_task_requirements_queries.py` covering:
- `add_task_workspace_requirements(task_id, [(kind_id, alias), ...])` — assigns positions per `(task_id, kind_id)`.
- `fetch_task_workspace_requirements(task_id)` — returns rows in `(kind_id, position)` order.
- `delete_task_workspace_requirements(task_id)` — clears all rows for a task (used on task delete).

```python
"""task_workspace_requirements CRUD. Spec §3.3."""

from __future__ import annotations

import pytest
import pytest_asyncio

from src.database import Database


@pytest_asyncio.fixture
async def db():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_add_assigns_position_per_kind(db):
    await db.add_task_workspace_requirements("t1", [
        ("game-repo", None),
        ("package-foo", "primary"),
        ("package-foo", "mirror"),
    ])
    rows = await db.fetch_task_workspace_requirements("t1")
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r.kind_id, []).append(r)
    assert {row.position for row in by_kind["game-repo"]} == {0}
    assert {row.position for row in by_kind["package-foo"]} == {0, 1}
    assert {row.alias for row in by_kind["package-foo"]} == {"primary", "mirror"}


@pytest.mark.asyncio
async def test_fetch_orders_by_kind_then_position(db):
    await db.add_task_workspace_requirements("t1", [
        ("zeta", None),
        ("alpha", None),
        ("alpha", "second"),
    ])
    rows = await db.fetch_task_workspace_requirements("t1")
    keys = [(r.kind_id, r.position) for r in rows]
    assert keys == sorted(keys), keys


@pytest.mark.asyncio
async def test_delete_clears_all_rows_for_task(db):
    await db.add_task_workspace_requirements("t1", [("x", None), ("y", None)])
    await db.delete_task_workspace_requirements("t1")
    assert await db.fetch_task_workspace_requirements("t1") == []
```

- [ ] **Step 2: Implement**

Create `src/database/queries/task_requirements_queries.py`:

```python
"""task_workspace_requirements CRUD. Spec §3.3."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select

from src.database.tables import task_workspace_requirements


@dataclass
class TaskRequirementRow:
    task_id: str
    kind_id: str
    position: int
    alias: str | None


class TaskRequirementsQueryMixin:
    """Query mixin for task_workspace_requirements. Expects ``self._engine``."""

    async def add_task_workspace_requirements(
        self,
        task_id: str,
        requirements: list[tuple[str, str | None]],
    ) -> None:
        """Insert rows; assigns position = MAX(position)+1 per (task_id, kind_id)."""
        async with self._engine.begin() as conn:
            counts: dict[str, int] = {}
            # Pre-fetch existing max positions per kind to seed the counter.
            existing = (await conn.execute(
                select(
                    task_workspace_requirements.c.kind_id,
                    func.max(task_workspace_requirements.c.position),
                )
                .where(task_workspace_requirements.c.task_id == task_id)
                .group_by(task_workspace_requirements.c.kind_id)
            )).fetchall()
            for kind_id, max_pos in existing:
                counts[kind_id] = (max_pos or 0) + 1

            rows = []
            for kind_id, alias in requirements:
                pos = counts.get(kind_id, 0)
                counts[kind_id] = pos + 1
                rows.append({
                    "task_id": task_id,
                    "kind_id": kind_id,
                    "position": pos,
                    "alias": alias,
                })
            if rows:
                await conn.execute(insert(task_workspace_requirements), rows)

    async def fetch_task_workspace_requirements(
        self, task_id: str
    ) -> list[TaskRequirementRow]:
        async with self._engine.begin() as conn:
            result = (await conn.execute(
                select(task_workspace_requirements)
                .where(task_workspace_requirements.c.task_id == task_id)
                .order_by(
                    task_workspace_requirements.c.kind_id,
                    task_workspace_requirements.c.position,
                )
            )).mappings().fetchall()
        return [
            TaskRequirementRow(
                task_id=r["task_id"], kind_id=r["kind_id"],
                position=r["position"], alias=r["alias"],
            )
            for r in result
        ]

    async def delete_task_workspace_requirements(self, task_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_workspace_requirements).where(
                    task_workspace_requirements.c.task_id == task_id
                )
            )
```

- [ ] **Step 3: Wire mixin into `Database`**

Add `TaskRequirementsQueryMixin` to the Database class composition (same place as `WorkspaceKindQueryMixin` from Task 3.1 step 4).

- [ ] **Step 4: Run tests**

Run: `source .venv/bin/activate && pytest tests/test_task_requirements_queries.py -v 2>&1 | tail -10`
Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/task_requirements_queries.py tests/test_task_requirements_queries.py src/database/__init__.py
git commit -m "queries: task_workspace_requirements CRUD with auto-position"
```

### Task 5.2: `effective_requirements()` and `acquire_one_unlocked()`

**Files:**
- Create: `src/orchestrator/workspace_attachments.py` (new file housing `effective_requirements`)
- Modify: `src/database/queries/workspace_queries.py` (add `acquire_one_unlocked`, `first_workspace_of_kind`)
- Create: `tests/test_workspace_attachments.py`

- [ ] **Step 1: Write tests for `effective_requirements`**

Create `tests/test_workspace_attachments.py` covering each spec §6.1 branch:
- Empty explicit requirements + project-repo resolves → synthesizes one project-repo requirement carrying preferred_workspace_id.
- Empty explicit requirements + no project-repo kind → returns only auto-attach kinds.
- Explicit requirements present → no synthesis; auto-attach kinds appended.
- Auto-attach kind already in explicit → not duplicated.
- Output is sorted by (kind_id, position).

(Detailed test code omitted for brevity — but write them. ~80 lines.)

- [ ] **Step 2: Implement `effective_requirements`**

Create `src/orchestrator/workspace_attachments.py`:

```python
"""Compute the workspace requirement set for a task. Spec §6.1."""

from __future__ import annotations

from src.models import ResolvedRequirement, Task


async def effective_requirements(db, task: Task) -> list[ResolvedRequirement]:
    """The single load-bearing function that turns a task into requirements.

    See spec §6.1.  Pure relative to DB state — same input → same output.
    Output is sorted by (kind_id, position) for canonical lock order (§6.3).
    """
    explicit_rows = await db.fetch_task_workspace_requirements(task.id)

    base: list[ResolvedRequirement] = []

    if explicit_rows:
        for row in explicit_rows:
            base.append(ResolvedRequirement(
                kind_id=row.kind_id,
                alias=row.alias,
                position=row.position,
                preferred_workspace_id=None,
            ))
    else:
        project_repo = await db.resolve_workspace_kind(task.project_id, "project-repo")
        if project_repo is not None:
            base.append(ResolvedRequirement(
                kind_id="project-repo",
                alias=None,
                position=0,
                preferred_workspace_id=task.preferred_workspace_id,
            ))

    explicit_kind_ids = {r.kind_id for r in base}
    auto_attach = await db.list_auto_attach_kinds_for_project(task.project_id)
    for idx, kind in enumerate(auto_attach):
        if kind.id in explicit_kind_ids:
            continue
        base.append(ResolvedRequirement(
            kind_id=kind.id,
            alias=None,
            position=10_000 + idx,
            preferred_workspace_id=None,
        ))

    base.sort(key=lambda r: (r.kind_id, r.position))
    return base
```

- [ ] **Step 3: Run `effective_requirements` tests**

Run: `source .venv/bin/activate && pytest tests/test_workspace_attachments.py -v 2>&1 | tail -15`
Expected: all pass.

- [ ] **Step 4: Add `acquire_one_unlocked` and `first_workspace_of_kind` to workspace_queries.py**

In `src/database/queries/workspace_queries.py`, add these methods to `WorkspaceQueryMixin`:

```python
async def first_workspace_of_kind(
    self, project_id: str, kind_id: str
) -> Workspace | None:
    """Return the first workspace of a given kind for a project (no lock)."""
    async with self._engine.begin() as conn:
        result = await conn.execute(
            select(workspaces)
            .where(
                (workspaces.c.project_id == project_id)
                & (workspaces.c.kind_id == kind_id)
                & (workspaces.c.enabled.is_(True))
            )
            .order_by(workspaces.c.id)
            .limit(1)
        )
        row = result.mappings().fetchone()
        return self._row_to_workspace(row) if row else None


async def acquire_one_unlocked(
    self,
    project_id: str,
    kind_id: str,
    mode: str | None,
    locked_by_task_id: str,
    locked_by_agent_id: str,
    prefer_workspace_id: str | None = None,
) -> Workspace | None:
    """Atomically acquire one unlocked workspace of a given kind.

    Spec §6.4 per-dialect strategy.  Preserves the path-level conflict
    check from the legacy `acquire_workspace` (BRANCH_ISOLATED only
    conflicts with non-BRANCH_ISOLATED on same path).
    """
    import time
    from src.models import WorkspaceMode

    now = time.time()
    lock_mode_value = mode  # already a string in lowercase, e.g. "exclusive"

    async with self._engine.begin() as conn:
        # Build candidate list: preferred first, then any other unlocked of this kind.
        candidate_ids: list[str] = []
        if prefer_workspace_id:
            row = (await conn.execute(
                select(workspaces.c.id).where(
                    (workspaces.c.id == prefer_workspace_id)
                    & (workspaces.c.project_id == project_id)
                    & (workspaces.c.kind_id == kind_id)
                    & (workspaces.c.locked_by_agent_id.is_(None))
                    & (workspaces.c.enabled.is_(True))
                )
            )).fetchone()
            if row:
                candidate_ids.append(row[0])

        rows = (await conn.execute(
            select(workspaces.c.id)
            .where(
                (workspaces.c.project_id == project_id)
                & (workspaces.c.kind_id == kind_id)
                & (workspaces.c.locked_by_agent_id.is_(None))
                & (workspaces.c.enabled.is_(True))
            )
            .order_by(workspaces.c.id)
        )).fetchall()
        for row in rows:
            if row[0] not in candidate_ids:
                candidate_ids.append(row[0])

        for ws_id in candidate_ids:
            ws_row = (await conn.execute(
                select(workspaces).where(
                    (workspaces.c.id == ws_id)
                    & (workspaces.c.locked_by_agent_id.is_(None))
                )
            )).mappings().fetchone()
            if not ws_row:
                continue

            # Path-level conflict check (preserved from acquire_workspace).
            if mode == WorkspaceMode.BRANCH_ISOLATED.value:
                conflict = (await conn.execute(
                    select(workspaces.c.id).where(
                        (workspaces.c.workspace_path == ws_row["workspace_path"])
                        & (workspaces.c.locked_by_agent_id.isnot(None))
                        & (workspaces.c.id != ws_row["id"])
                        & (workspaces.c.lock_mode != WorkspaceMode.BRANCH_ISOLATED.value)
                    )
                )).fetchone()
            else:
                conflict = (await conn.execute(
                    select(workspaces.c.id).where(
                        (workspaces.c.workspace_path == ws_row["workspace_path"])
                        & (workspaces.c.locked_by_agent_id.isnot(None))
                        & (workspaces.c.id != ws_row["id"])
                    )
                )).fetchone()
            if conflict:
                continue

            # Atomic lock attempt — UPDATE with WHERE locked IS NULL.
            result = await conn.execute(
                update(workspaces)
                .where(
                    (workspaces.c.id == ws_row["id"])
                    & (workspaces.c.locked_by_agent_id.is_(None))
                )
                .values(
                    locked_by_agent_id=locked_by_agent_id,
                    locked_by_task_id=locked_by_task_id,
                    locked_at=now,
                    lock_mode=lock_mode_value,
                )
            )
            if result.rowcount != 1:
                continue

            ws = self._row_to_workspace(ws_row)
            ws.locked_by_agent_id = locked_by_agent_id
            ws.locked_by_task_id = locked_by_task_id
            ws.locked_at = now
            ws.lock_mode = WorkspaceMode(lock_mode_value) if lock_mode_value else None
            return ws

        return None
```

- [ ] **Step 5: Run the workspace queries tests**

Run: `source .venv/bin/activate && pytest tests/test_database.py -k workspace -v 2>&1 | tail -15`
Expected: all green (legacy `acquire_workspace` is still untouched).

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/workspace_attachments.py src/database/queries/workspace_queries.py tests/test_workspace_attachments.py
git commit -m "acquire: effective_requirements + acquire_one_unlocked

Implements spec §6.1 (compute requirement set) and the new per-kind
acquisition primitive that preserves the path-level conflict check from
legacy acquire_workspace.  Not yet wired into _prepare_workspace."
```

### Task 5.3: `acquire_for_task` and refactor `_prepare_workspace`

**Files:**
- Modify: `src/orchestrator/workspace_attachments.py` (add `acquire_for_task`)
- Modify: `src/orchestrator/workspace.py:32-276` (`_prepare_workspace` becomes a wrapper)
- Create/extend: `tests/test_workspace_attachments.py`

- [ ] **Step 1: Tests for `acquire_for_task`**

Add tests covering spec §14 cases:
- Single-kind project (back-compat) — task with no requirements → synthesized project-repo, locks correctly.
- Multi-kind task with all instances available → all locked.
- Multi-kind task, partial availability → AcquisitionFailed, no partial holds (verify all rows still unlocked after).
- Concurrent same-kind: spawn two tasks racing for one instance → exactly one wins.
- Auto-attach kind appears in result without explicit declaration.

- [ ] **Step 2: Implement `acquire_for_task`**

Add to `src/orchestrator/workspace_attachments.py`:

```python
class AcquisitionFailed(Exception):
    def __init__(self, kind_id: str):
        self.kind_id = kind_id
        super().__init__(f"could not acquire workspace of kind '{kind_id}'")


async def acquire_for_task(
    db, task: Task, agent_id: str
) -> WorkspaceAttachmentSet:
    """Acquire all required workspaces for a task atomically. Spec §6.2.

    Raises AcquisitionFailed (and rolls back any partial locks) if any
    required lockable kind has no available instance.
    """
    requirements = await effective_requirements(db, task)
    acquired: list[WorkspaceAttachment] = []

    try:
        for req in requirements:
            kind = await db.resolve_workspace_kind(task.project_id, req.kind_id)
            if kind is None:
                raise AcquisitionFailed(req.kind_id)

            if kind.lockable:
                ws = await db.acquire_one_unlocked(
                    project_id=task.project_id,
                    kind_id=kind.id,
                    mode=kind.default_lock_mode,
                    locked_by_task_id=task.id,
                    locked_by_agent_id=agent_id,
                    prefer_workspace_id=req.preferred_workspace_id,
                )
                if ws is None:
                    raise AcquisitionFailed(req.kind_id)
            else:
                ws = await db.first_workspace_of_kind(
                    project_id=task.project_id, kind_id=kind.id,
                )
                if ws is None:
                    raise AcquisitionFailed(req.kind_id)

            acquired.append(WorkspaceAttachment(
                requirement=req, workspace=ws, kind=kind,
            ))
        return WorkspaceAttachmentSet(attachments=acquired)
    except Exception:
        # Roll back any locks we managed to take.
        for att in acquired:
            if att.lockable:
                await db.release_workspace(att.workspace.id)
        raise
```

Note: this implementation acquires per-kind in separate transactions (each `acquire_one_unlocked` opens its own transaction). The all-or-nothing guarantee is enforced by the explicit rollback loop above. A future refactor can wrap the whole acquisition in a single `db.transaction()` if/when SQLAlchemy supports nested DML across helper methods cleanly; for now the rollback loop is correct and readable.

- [ ] **Step 3: Refactor `_prepare_workspace` to delegate**

In `src/orchestrator/workspace.py`, restructure `_prepare_workspace` per spec §6.5/§6.6:

```python
async def _prepare_workspace(self, task: Task, agent) -> str | None:
    from src.orchestrator.workspace_attachments import (
        AcquisitionFailed, acquire_for_task,
    )

    project = await self.db.get_project(task.project_id)
    lock_mode = task.workspace_mode or WorkspaceMode.EXCLUSIVE
    if lock_mode == WorkspaceMode.DIRECTORY_ISOLATED:
        raise RuntimeError(...)  # unchanged

    try:
        attachment_set = await acquire_for_task(self.db, task, agent.id)
    except AcquisitionFailed as e:
        # Branch-isolated fallback: try to share an existing locked workspace
        # via worktree if we couldn't acquire a project-repo lock.
        if e.kind_id == "project-repo" and lock_mode == WorkspaceMode.BRANCH_ISOLATED:
            ws = await self._create_branch_isolated_worktree(task, agent, project)
            if not ws:
                return None
            # Synthesize a one-attachment set so the rest of the pipeline works.
            kind = await self.db.resolve_workspace_kind(task.project_id, "project-repo")
            attachment_set = WorkspaceAttachmentSet(attachments=[
                WorkspaceAttachment(
                    requirement=ResolvedRequirement(kind_id="project-repo", position=0),
                    workspace=ws,
                    kind=kind,
                ),
            ])
        else:
            return None

    # Stash the attachment set on the task for later runtime use (Phase 7).
    self._task_attachments[task.id] = attachment_set

    primary = attachment_set.first_of_kind("project-repo")
    if primary is None:
        # Tasks with no project-repo (e.g. Supervisor tasks with a vault attachment)
        # have no primary workspace path — return None to signal tool-call-only mode.
        return None

    workspace = primary.workspace_path
    ws = primary.workspace
    is_worktree = ws.source_type == RepoSourceType.WORKTREE

    # ... (rest of _prepare_workspace unchanged: git mutex registration,
    #      sentinel handling, git provisioning, plan cleanup) ...
    return workspace
```

The body below `# ... rest of _prepare_workspace unchanged ...` is the existing code from line 96 onward, lifted verbatim. The branch-isolated fallback now wraps the new `acquire_for_task` call. `self._task_attachments` is a new orchestrator-level dict added in Task 5.4.

- [ ] **Step 4: Add `self._task_attachments` to orchestrator init**

In `src/orchestrator.py` (or wherever the orchestrator is constructed), add `self._task_attachments: dict[str, WorkspaceAttachmentSet] = {}` to `__init__`.

- [ ] **Step 5: Run the orchestrator tests**

Run: `source .venv/bin/activate && pytest tests/test_orchestrator.py -v -x 2>&1 | tail -30`
Expected: all green. Single-workspace path is unchanged in behavior; the new code path only adds the attachment-set computation around the existing call.

- [ ] **Step 6: Run the §14 tests**

Run: `source .venv/bin/activate && pytest tests/test_workspace_attachments.py -v 2>&1 | tail -25`
Expected: all spec §14 scenarios pass.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/workspace_attachments.py src/orchestrator/workspace.py src/orchestrator.py tests/test_workspace_attachments.py
git commit -m "acquire: acquire_for_task wired into _prepare_workspace

Single-kind back-compat preserved via the project-repo synthesis path.
Branch-isolated worktree fallback unchanged.  Attachment set stashed on
the orchestrator for Phase 7 runtime integration."
```

---

## Phase 6 — Task Requirement Intake

After this phase, callers can pass `requires_kinds` when creating tasks; defaults still apply when omitted.

### Task 6.1: `create_task` accepts `requires_kinds`

**Files:**
- Modify: `src/commands/task_commands.py` (or wherever `_cmd_create_task` lives — `grep -rn 'def _cmd_create_task' src/`)
- Modify: `src/tools/definitions.py` (MCP tool schema for `create_task`)
- Create: `tests/test_create_task_requires_kinds.py`

- [ ] **Step 1: Find and read the existing `create_task` handler**

Run: `grep -rn 'def _cmd_create_task\|"create_task"' src/commands/ src/tools/definitions.py | head -10`

Read the handler and its tool definition.

- [ ] **Step 2: Write the failing test**

Test that creating a task with `requires_kinds=["game-repo", "package-foo"]` writes corresponding rows to `task_workspace_requirements`. Test that omitting it writes no rows. Test that an unknown kind id raises a clear error per spec §5.4.

- [ ] **Step 3: Implement**

Add a `requires_kinds: list[str | dict] | None = None` parameter to the handler. Normalize strings to dicts. Validate each via `db.resolve_workspace_kind` (project-scoped + system fallback). Write rows via the Task 5.1 helper.

- [ ] **Step 4: Update the MCP tool definition**

Add `requires_kinds` to the schema in `src/tools/definitions.py`. Mark it optional.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_create_task_requires_kinds.py -v`
Expected: passes.

- [ ] **Step 6: Commit**

```bash
git add src/commands/task_commands.py src/tools/definitions.py tests/test_create_task_requires_kinds.py
git commit -m "create_task: accept requires_kinds parameter

Validates each kind via resolve_workspace_kind (project-scoped→system
fallback).  Unknown kinds fail at create time with a clear error."
```

### Task 6.2: Playbook stage requirements

**Files:**
- Modify: `src/playbooks/models.py` (stage definition)
- Modify: `src/playbooks/runner.py` (or wherever stages spawn tasks)
- Test: extend `tests/test_playbooks_*.py`

- [ ] **Step 1: Find playbook stage definitions**

Run: `grep -rn 'class.*Stage\|requires_kinds' src/playbooks/ | head -10`

- [ ] **Step 2: Add `requires_kinds: list[str | dict] = []` to stage model**

Update the dataclass. Update the YAML/markdown parser if needed.

- [ ] **Step 3: Pipe through to spawned tasks**

When a stage spawns a task, copy `requires_kinds` into the create_task call.

- [ ] **Step 4: Test**

Add a playbook test that spawns a task with `requires_kinds` declared and verifies the rows land in `task_workspace_requirements`.

- [ ] **Step 5: Commit**

```bash
git commit -am "playbooks: stages can declare requires_kinds for spawned tasks"
```

---

## Phase 7 — Runtime Integration

After this phase, runtimes see the workspace_attachments and expose them to the agent.

### Task 7.1: Extend `TaskContext`

**Files:**
- Modify: `src/models.py` (TaskContext dataclass)
- Modify: `src/runtimes/claude_sdk.py` and `src/runtimes/acpx.py` (consumers)
- Modify: `src/orchestrator/execution.py` (where TaskContext is built)

- [ ] **Step 1: Add fields to TaskContext**

```python
@dataclass
class TaskContext:
    # ... existing fields ...
    workspace_attachments: list[WorkspaceAttachment] = field(default_factory=list)
    primary_path: str | None = None  # alias for checkout_path; spec §8.1
```

- [ ] **Step 2: Build TaskContext from the AttachmentSet**

In `src/orchestrator/execution.py` (where the runtime's `start(task)` call assembles the context), pass `workspace_attachments=attachment_set.attachments` and `primary_path=attachment_set.primary_path`.

- [ ] **Step 3: Update runtimes to derive cwd + extra dirs**

In `claude_sdk.py` and `acpx.py`, change cwd selection to prefer `task.primary_path` (falling back to `task.checkout_path` for back-compat); compute `add_dirs ∪ {a.workspace_path for a in workspace_attachments if a.workspace_path != cwd}` (spec §7.1 dedup).

- [ ] **Step 4: Tests**

Add a test that creates a task with multi-kind requirements, confirms the runtime is invoked with the right cwd and the right add_dirs (no dupes, vault included once).

- [ ] **Step 5: Commit**

```bash
git commit -am "runtime: propagate workspace_attachments through TaskContext"
```

---

## Phase 8 — Prompt Builder + Commands

### Task 8.1: Render `## Workspaces` block

**Files:**
- Modify: `src/prompt_builder.py`
- Test: extend `tests/test_prompt_builder.py`

- [ ] Render attachments as a markdown block per spec §8.3.

### Task 8.2: `list_workspace_kinds` command + tool

**Files:**
- Modify: `src/commands/system_commands.py` (or wherever new commands live)
- Modify: `src/tools/definitions.py` and `src/tools/registry.py`

- [ ] Implement, register, test.

### Task 8.3: `add_workspace` accepts `kind_id`

**Files:**
- Modify: `src/commands/workspace_commands.py` (or the existing `add_workspace` handler)

- [ ] Add `kind_id: str | None = None` parameter; default to `'project-repo'` when the project resolves it; otherwise require explicit value with a clear error.

### Task 8.4: Documentation

- [ ] Update `docs/specs/design/agent-coordination.md` §7 to reference workspaces-v2.
- [ ] Add a paragraph to `CLAUDE.md` Quick Reference under "Workspaces" — point at the new spec.

---

## Out-of-Plan Follow-Ons

These are explicit non-goals for this plan but are tracked for future work:

- **Tighten `workspaces.kind_id` to NOT NULL.** Follow-up Alembic revision after one minor version (spec §9.5).
- **Auto-clone of missing kind instances.** When a task acquires a kind with no provisioned instances and the kind has `is_git_repo=true` + `repo_url`, auto-clone before failing. Spec §12 open question.
- **Tagged / affinity-based instance selection.** Spec §11 non-goal.
- **DIRECTORY_ISOLATED implementation.** Spec §11 non-goal.
