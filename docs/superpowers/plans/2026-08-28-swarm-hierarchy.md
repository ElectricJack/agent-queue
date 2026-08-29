# Swarm Work Model — Plan 1: Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `parent-child` edge the single authority for task hierarchy, mint dotted child ids everywhere (including the graph creator), settle containers event-driven instead of by a per-tick scan, and expose `children` / `progress` / `reparent` — with the schema for Plans 2 and 3 landed in the same migration.

**Architecture:** A new `HierarchyQueryMixin` owns every write that touches hierarchy (`set_parent`, `settle_containers`, `create_task_under`) and runs on a caller-supplied connection so membership, `is_blocked` recompute and container settlement commit together. `transition_task` is split into a connection-aware `_apply_transition` plus a wrapper that emits after commit. The `parent_task_id` column stays as a derived cache written only by `set_parent`. Reads use one recursive CTE.

**Tech Stack:** Python 3.12, SQLAlchemy Core 2.x (async, `sqlite+aiosqlite` / `postgresql+asyncpg`), Alembic, pytest + pytest-asyncio (auto mode), Click CLI, ruff (line-length 100).

**Spec:** `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` — Part I (§4–§8), plus §9 schema, §16 tests, §17 migration. Executors read the spec alongside this plan.

## Global Constraints

- Exactly one parent per task; `parent_task_id` is written **only** by `set_parent` (spec D2).
- Ids are immutable; children are `<parent>.<n>` from `tasks.next_child_ordinal`; naming depth cap 3, structural depth cap 3 (spec §4, §6).
- No raw `UPDATE tasks SET status` anywhere new — all status changes go through `_apply_transition` (spec §7).
- Every mutation helper in the new mixin takes `conn` and never opens its own transaction (spec §5).
- Postgres semantics first; SQLite must pass the same tests (spec D10). Recursive CTEs require SQLite ≥ 3.8.3 (bundled SQLite is far newer).
- Migrations must work on SQLite (batch mode) and PostgreSQL (spec §17); **never** edit `tables.py` without a migration (CLAUDE.md).
- Commands return `{"success": bool, ...}` or `{"error": ...}` dicts; all state changes go through `CommandHandler` (CLAUDE.md).
- Async-first; no `subprocess.run` in production code (CLAUDE.md).
- Run `pytest tests/ -n auto` before every commit that touches the query layer; run `ruff check src tests` before every commit.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/database/tables.py` | New columns/indexes/table (spec §9, §17 revision A) |
| `src/models.py` | `Task` gains `created_by_kind`, `created_by_id`, `claim_epoch`, `filed_count`; `AgentState.RETIRED` |
| `migrations/versions/a1b2c3d4e5f6_swarm_ddl.py` | Revision A — DDL only |
| `src/task_names.py` | `naming_depth`, `reserve_child_ordinal(conn, parent)`, conn-aware `child_task_id` |
| `src/database/queries/hierarchy_queries.py` (**new**) | `HierarchyQueryMixin`: `HierarchyError`, `mark_container`, `set_parent`, `settle_containers`, `create_task_under`, `subtree_ids`, `structural_depth`, `subtree_height`, `get_children`, `get_task_tree` (CTE), `get_children_summary` |
| `src/database/queries/task_queries.py` | `_apply_transition` split; settlement listener; `delete_task` refuse/cascade; drop old `get_task_tree` |
| `src/database/queries/dependency_queries.py` | `add_dependency`/`remove_dependency` delegate `parent-child` to `set_parent` |
| `src/database/queries/archive_queries.py` | subtree-atomic `archive_task`; `archive_old_terminal_tasks` selects subtree roots |
| `src/database/adapters/{sqlite,postgresql}.py` | Register `HierarchyQueryMixin` first in MRO |
| `src/task_graph/creator.py` | Dotted ids, `parent_id` (existing container), container flag |
| `src/commands/task_commands.py` | `create_task --parent` via `create_task_under`; `_cmd_task_children`, `_cmd_task_progress`, `_cmd_reparent_task`; `delete --cascade`; `create_task_graph parent_id`; open-children refusal in `skip`/`set_task_status` |
| `src/commands/surface_commands.py` | `task_show` gains `parent` + `children` summary |
| `src/commands/session_commands.py` | `task_close` open-children refusal + `abandon_children` |
| `src/orchestrator/monitoring.py`, `src/orchestrator/core.py` | Delete `_check_plan_parent_completion`; add `_sweep_container_completion`; settlement listener → events/notify/workflow-stage |
| `src/config.py` | `WorkGraphConfig.container_sweep_interval_seconds` |
| `src/tools/definitions.py`, `src/api/models/task.py`, `src/cli/tasks.py`, `src/cli/formatter_registry.py` | Surface for the three new commands and the new flags |
| `src/event_schemas.py` | `task.reparented` |
| `src/database/hierarchy_migration.py` (**new**) | Canonicalisation used by preflight and revision B |
| `migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py` | Revision B — data step + partial unique index |
| `src/commands/ops_commands.py` | `_cmd_db_preflight_hierarchy` |
| `src/doctor/hierarchy_checks.py` (**new**), `src/doctor/__init__.py` | `hierarchy.*` checks |
| `tests/test_hierarchy_ids.py`, `tests/test_hierarchy_queries.py`, `tests/test_hierarchy_settlement.py`, `tests/test_hierarchy_commands.py`, `tests/test_hierarchy_archive_delete.py`, `tests/test_hierarchy_graph_creator.py`, `tests/test_hierarchy_migration.py`, `tests/test_hierarchy_doctor.py`, `tests/perf/test_hierarchy_statements.py` | Tests, one file per task cluster |

**Shared test fixtures** (copy into each new test file — they mirror `tests/test_work_graph_commands.py`):

```python
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid
```

---

### Task 1: Schema and model fields

**Files:**
- Modify: `src/database/tables.py:63-113` (tasks), `:144-160` (task_dependencies), `:529-561` (sessions), `:473-527` (agent_profiles), `:652-690` (archived_tasks)
- Modify: `src/models.py:200-213` (`AgentState`), `:342-396` (`Task`)
- Modify: `src/database/queries/task_queries.py:40-85` (`create_task`), `:762-802` (`_row_to_task`)
- Modify: `src/database/queries/archive_queries.py:31-140` (copy the two provenance columns)
- Test: `tests/test_hierarchy_queries.py`

**Interfaces:**
- Produces: `tasks.next_child_ordinal`, `tasks.created_by_kind`, `tasks.created_by_id`, `tasks.claim_epoch`, `tasks.filed_count`; `Task.created_by_kind: str | None`, `Task.created_by_id: str | None`, `Task.claim_epoch: int`, `Task.filed_count: int`; `AgentState.RETIRED`; table `hierarchy_migration_rejects`; `sessions.{claims, agent_id, claim_phase, claim_phase_at, last_claim_epoch, last_claim_result}`; `agent_profiles.{min_active, max_active, max_claims_per_session}`; index `idx_tasks_ready_by_profile`.
- Note: the partial unique index `uq_task_deps_single_parent` is **not** declared in `tables.py` in this task — revision B creates it after canonicalisation (Task 12). Declaring it here would make `Database.initialize()` (which uses `metadata.create_all`) enforce it in tests, which is fine, but it would also make revision A's autogenerate try to create it before the data is clean. It is added to `tables.py` in Task 12.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hierarchy_queries.py
"""HierarchyQueryMixin — spec Part I (§4–§8)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentState, Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


class TestSchemaFields:
    async def test_new_task_columns_have_defaults(self, db):
        await mktask(db, "a")
        t = await db.get_task("a")
        assert t.created_by_kind is None
        assert t.created_by_id is None
        assert t.claim_epoch == 0
        assert t.filed_count == 0

    async def test_created_by_round_trips(self, db):
        await mktask(db, "a", created_by_kind="session", created_by_id="s-1")
        t = await db.get_task("a")
        assert (t.created_by_kind, t.created_by_id) == ("session", "s-1")

    def test_agent_state_has_retired(self):
        assert AgentState.RETIRED.value == "RETIRED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hierarchy_queries.py -v`
Expected: FAIL — `TypeError: Task.__init__() got an unexpected keyword argument 'created_by_kind'` and `AttributeError: RETIRED`.

- [ ] **Step 3: Add the columns to `tables.py`**

In the `tasks` table, after the `discord_thread_id` column:

```python
    # Hierarchy (swarm-work-model §4, §6): per-parent ordinal counter for
    # dotted child ids.  Incremented atomically by
    # ``task_names.reserve_child_ordinal``; never read for anything else.
    Column("next_child_ordinal", Integer, nullable=False, server_default="1"),
    # Provenance (swarm-work-model §9): who created the row.  Stamped by
    # CommandHandler.execute from the request scope (Plan 2); nullable so
    # rows created by legacy paths stay valid.
    Column("created_by_kind", Text, nullable=True),
    Column("created_by_id", Text, nullable=True),
    # Per-claim fence (swarm-work-model §10).  Plan 2 increments it.
    Column("claim_epoch", Integer, nullable=False, server_default="0"),
    # Worker-filing quota counter (swarm-work-model §12).  Plan 2 reserves it.
    Column("filed_count", Integer, nullable=False, server_default="0"),
```

and after `Index("idx_tasks_parent", "parent_task_id")`:

```python
    # Pool work query (swarm-work-model §10): ready tasks for a profile.
    Index("idx_tasks_ready_by_profile", "project_id", "profile_id", "status", "is_blocked"),
```

In `archived_tasks`, add beside the other copied columns:

```python
    Column("created_by_kind", Text, nullable=True),
    Column("created_by_id", Text, nullable=True),
```

In `sessions`, after `desired_state`:

```python
    # Pool lifecycle (swarm-work-model §9–§11).  Plan 2 writes these.
    Column("claims", Integer, nullable=False, server_default="0"),
    Column("agent_id", Text, nullable=True),
    Column("claim_phase", Text, nullable=True),
    Column("claim_phase_at", Float, nullable=True),
    Column("last_claim_epoch", Integer, nullable=True),
    Column("last_claim_result", Text, nullable=True),
```

In `agent_profiles`, after `max_session_age`:

```python
    # lifecycle: pool (swarm-work-model §9).  NULL = unlimited claims.
    Column("min_active", Integer, nullable=True),
    Column("max_active", Integer, nullable=True),
    Column("max_claims_per_session", Integer, nullable=True),
```

New table, placed after `task_labels`:

```python
hierarchy_migration_rejects = Table(
    "hierarchy_migration_rejects",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("parent_id", Text, nullable=True),
    Column("source", Text, nullable=False),  # duplicate_edge | column_only | edge
    Column("reason", Text, nullable=False),  # cross_project | cycle | depth | not_found | duplicate
    Column("detail", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("idx_hier_rejects_run", "run_id"),
)
```

- [ ] **Step 4: Add the model fields**

`src/models.py` — in `AgentState` add `RETIRED = "RETIRED"` after `ERROR`. In `Task`, after `intelligence_class`:

```python
    # Provenance and swarm counters (swarm-work-model §9).  ``claim_epoch``
    # and ``filed_count`` are written by Plan 2; they ride on the model so
    # ``_row_to_task`` is complete from the first migration.
    created_by_kind: str | None = None
    created_by_id: str | None = None
    claim_epoch: int = 0
    filed_count: int = 0
```

- [ ] **Step 5: Persist and read the fields**

`task_queries.create_task` — add to the `insert(tasks).values(...)` call:

```python
                    created_by_kind=task.created_by_kind,
                    created_by_id=task.created_by_id,
```

`_row_to_task` — add before `created_at=`:

```python
            created_by_kind=row.get("created_by_kind"),
            created_by_id=row.get("created_by_id"),
            claim_epoch=int(row.get("claim_epoch") or 0),
            filed_count=int(row.get("filed_count") or 0),
```

`archive_queries.archive_task` — add to the archive insert `.values(...)`:

```python
                    created_by_kind=task.created_by_kind,
                    created_by_id=task.created_by_id,
```

and to `_row_to_archived_task` (line ~236) add `"created_by_kind": row.get("created_by_kind"), "created_by_id": row.get("created_by_id"),`.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_hierarchy_queries.py tests/test_database.py tests/test_task_queries.py -v -n auto`
Expected: PASS (the existing archive/round-trip tests still pass).

- [ ] **Step 7: Commit**

```bash
git add src/database/tables.py src/models.py src/database/queries/task_queries.py src/database/queries/archive_queries.py tests/test_hierarchy_queries.py
git commit -m "feat(hierarchy): schema and model fields for the swarm work model

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Migration revision A (DDL only)

**Files:**
- Create: `migrations/versions/a1b2c3d4e5f6_swarm_ddl.py`
- Test: `tests/test_hierarchy_migration.py`

**Interfaces:**
- Consumes: the `tables.py` changes from Task 1.
- Produces: revision id `a1b2c3d4e5f6` (Task 12's revision B sets `down_revision` to it).

- [ ] **Step 1: Confirm the current head**

Run: `alembic heads`
Expected: a single head. Write its id down; it is `down_revision` below. (At the time of writing it is `4e925610d7a6`.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_hierarchy_migration.py
"""Revisions A (DDL) and B (canonicalise) — spec §17."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DATABASE_URL=f"sqlite:///{db_path}")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "mig.db")


class TestRevisionA:
    def test_upgrade_adds_columns_and_table(self, db_path):
        res = _alembic(db_path, "upgrade", "a1b2c3d4e5f6")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        task_cols = {c["name"] for c in insp.get_columns("tasks")}
        assert {"next_child_ordinal", "created_by_kind", "created_by_id", "claim_epoch",
                "filed_count"} <= task_cols
        sess_cols = {c["name"] for c in insp.get_columns("sessions")}
        assert {"claims", "agent_id", "claim_phase", "claim_phase_at",
                "last_claim_epoch", "last_claim_result"} <= sess_cols
        prof_cols = {c["name"] for c in insp.get_columns("agent_profiles")}
        assert {"min_active", "max_active", "max_claims_per_session"} <= prof_cols
        assert "hierarchy_migration_rejects" in insp.get_table_names()
        idx = {i["name"] for i in insp.get_indexes("tasks")}
        assert "idx_tasks_ready_by_profile" in idx

    def test_downgrade_round_trips(self, db_path):
        assert _alembic(db_path, "upgrade", "a1b2c3d4e5f6").returncode == 0
        res = _alembic(db_path, "downgrade", "-1")
        assert res.returncode == 0, res.stderr
        insp = inspect(create_engine(f"sqlite:///{db_path}"))
        assert "next_child_ordinal" not in {c["name"] for c in insp.get_columns("tasks")}
        assert "hierarchy_migration_rejects" not in insp.get_table_names()
```

Check how `migrations/env.py` reads the database URL (grep `AGENT_QUEUE_DATABASE_URL\|sqlalchemy.url` in `migrations/env.py` and `alembic.ini`) and adjust the env var name in `_alembic` to match. If `env.py` reads from `load_config()` instead, set `AGENT_QUEUE_CONFIG` to a temp config file whose `database_path` is `db_path`; `tests/test_migration_work_graph.py` shows the pattern this repo already uses — copy it.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_hierarchy_migration.py -v`
Expected: FAIL — `Can't locate revision identified by 'a1b2c3d4e5f6'`.

- [ ] **Step 4: Write the revision**

```python
# migrations/versions/a1b2c3d4e5f6_swarm_ddl.py
"""swarm work model — DDL (revision A)

Revision ID: a1b2c3d4e5f6
Revises: 4e925610d7a6
Create Date: 2026-08-28

DDL only (spec §17).  The hierarchy data step and the single-parent partial
unique index are revision B, so that a rejected canonicalisation never rolls
back the columns the preflight report lives in.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "4e925610d7a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as b:
        b.add_column(sa.Column("next_child_ordinal", sa.Integer(), server_default="1", nullable=False))
        b.add_column(sa.Column("created_by_kind", sa.Text(), nullable=True))
        b.add_column(sa.Column("created_by_id", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_epoch", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("filed_count", sa.Integer(), server_default="0", nullable=False))
        b.create_index(
            "idx_tasks_ready_by_profile",
            ["project_id", "profile_id", "status", "is_blocked"],
        )
    with op.batch_alter_table("archived_tasks", schema=None) as b:
        b.add_column(sa.Column("created_by_kind", sa.Text(), nullable=True))
        b.add_column(sa.Column("created_by_id", sa.Text(), nullable=True))
    with op.batch_alter_table("sessions", schema=None) as b:
        b.add_column(sa.Column("claims", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("agent_id", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_phase", sa.Text(), nullable=True))
        b.add_column(sa.Column("claim_phase_at", sa.Float(), nullable=True))
        b.add_column(sa.Column("last_claim_epoch", sa.Integer(), nullable=True))
        b.add_column(sa.Column("last_claim_result", sa.Text(), nullable=True))
    with op.batch_alter_table("agent_profiles", schema=None) as b:
        b.add_column(sa.Column("min_active", sa.Integer(), nullable=True))
        b.add_column(sa.Column("max_active", sa.Integer(), nullable=True))
        b.add_column(sa.Column("max_claims_per_session", sa.Integer(), nullable=True))
    op.create_table(
        "hierarchy_migration_rejects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("idx_hier_rejects_run", "hierarchy_migration_rejects", ["run_id"])


def downgrade() -> None:
    op.drop_index("idx_hier_rejects_run", table_name="hierarchy_migration_rejects")
    op.drop_table("hierarchy_migration_rejects")
    with op.batch_alter_table("agent_profiles", schema=None) as b:
        b.drop_column("max_claims_per_session")
        b.drop_column("max_active")
        b.drop_column("min_active")
    with op.batch_alter_table("sessions", schema=None) as b:
        for col in ("last_claim_result", "last_claim_epoch", "claim_phase_at",
                    "claim_phase", "agent_id", "claims"):
            b.drop_column(col)
    with op.batch_alter_table("archived_tasks", schema=None) as b:
        b.drop_column("created_by_id")
        b.drop_column("created_by_kind")
    with op.batch_alter_table("tasks", schema=None) as b:
        b.drop_index("idx_tasks_ready_by_profile")
        for col in ("filed_count", "claim_epoch", "created_by_id", "created_by_kind",
                    "next_child_ordinal"):
            b.drop_column(col)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_hierarchy_migration.py tests/test_database.py -v`
Expected: PASS. Also run `alembic upgrade head` against your local dev DB (backed up first) and confirm `aq doctor` reports `db.migrations` OK.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/a1b2c3d4e5f6_swarm_ddl.py tests/test_hierarchy_migration.py
git commit -m "feat(migrations): swarm work model DDL (revision A)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Dotted ids from an atomic counter

**Files:**
- Modify: `src/task_names.py:83-200`
- Test: `tests/test_hierarchy_ids.py`

**Interfaces:**
- Produces: `MAX_NAMING_DEPTH = 3`, `MAX_STRUCTURAL_DEPTH = 3`, `naming_depth(task_id) -> int`, `async reserve_child_ordinal(conn, parent_id) -> int`, `async child_task_id(conn, parent_id) -> tuple[str, bool]` (**now takes a connection**, not `db`), `async fresh_root_id(conn) -> str`. `generate_task_id(db, parent_id=None)` keeps its signature for the graph creator's root id; its `parent_id` branch opens a transaction and delegates.
- Removes: `_next_child_ordinal` (sibling scan), `_MAX_HIERARCHY_DEPTH`, `_hierarchy_depth`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_ids.py
"""Dotted child ids from tasks.next_child_ordinal — spec §6."""

from __future__ import annotations

import asyncio

import pytest

from src import task_names
from src.database import Database
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid))


def test_naming_depth():
    assert task_names.naming_depth("swift-falcon") == 1
    assert task_names.naming_depth("swift-falcon.2") == 2
    assert task_names.naming_depth("swift-falcon.2.1") == 3


class TestReserveChildOrdinal:
    async def test_ordinals_are_sequential_and_never_reused(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            first = await task_names.reserve_child_ordinal(conn, "p")
            second = await task_names.reserve_child_ordinal(conn, "p")
        assert (first, second) == (1, 2)
        # A deleted sibling's ordinal is not reused.
        async with db._engine.begin() as conn:
            third = await task_names.reserve_child_ordinal(conn, "p")
        assert third == 3

    async def test_unknown_parent_raises(self, db):
        async with db._engine.begin() as conn:
            with pytest.raises(KeyError):
                await task_names.reserve_child_ordinal(conn, "nope")


class TestChildTaskId:
    async def test_dotted_id_under_root(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "p")
        assert (cid, capped) == ("p.1", False)

    async def test_naming_depth_cap_falls_back_to_root_id(self, db):
        await mktask(db, "a.1.1")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "a.1.1")
        assert capped is True
        assert "." not in cid

    async def test_concurrent_reservations_are_unique(self, db):
        await mktask(db, "p")

        async def one():
            async with db._engine.begin() as conn:
                return await task_names.reserve_child_ordinal(conn, "p")

        ords = await asyncio.gather(*(one() for _ in range(10)))
        assert sorted(ords) == list(range(1, 11))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_ids.py -v`
Expected: FAIL — `AttributeError: module 'src.task_names' has no attribute 'naming_depth'`.

- [ ] **Step 3: Replace the hierarchy helpers in `task_names.py`**

Delete everything from the `_MAX_RETRIES = 10` line through the end of `generate_task_id`, and write:

```python
_MAX_RETRIES = 10

#: Naming depth cap (swarm-work-model §4): a parent whose id already has this
#: many dot-segments mints *root* ids for its children (plus a
#: ``discovered-from`` edge, added by the caller).  Naming depth never blocks
#: a structural operation; structural depth is enforced by the query layer.
MAX_NAMING_DEPTH = 3

#: Structural depth cap — the live ``parent-child`` chain length, root = 1.
MAX_STRUCTURAL_DEPTH = 3


def naming_depth(task_id: str) -> int:
    """Number of dot-separated segments in *task_id*."""
    return task_id.count(".") + 1


async def fresh_root_id(conn) -> str:
    """A fresh adjective-noun root id, collision-checked on *conn*."""
    from sqlalchemy import select

    from src.database.tables import tasks

    async def _exists(name: str) -> bool:
        row = (await conn.execute(select(tasks.c.id).where(tasks.c.id == name))).fetchone()
        return row is not None

    for _ in range(_MAX_RETRIES):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        if not await _exists(name):
            return name
    while True:
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(10, 99)}"
        if not await _exists(name):
            return name


async def reserve_child_ordinal(conn, parent_id: str) -> int:
    """Atomically take the next child ordinal from the parent row (spec §6).

    ``UPDATE … RETURNING`` on both dialects (SQLite ≥ 3.35 supports
    RETURNING; the bundled library is newer).  The row update is the
    serialisation point: two concurrent reservations cannot return the same
    number because the second UPDATE sees the first's increment.  Ordinals
    are never reused — deletes leave gaps on purpose (an id must never be
    re-minted).
    """
    from sqlalchemy import update

    from src.database.tables import tasks

    stmt = (
        update(tasks)
        .where(tasks.c.id == parent_id)
        .values(next_child_ordinal=tasks.c.next_child_ordinal + 1)
        .returning(tasks.c.next_child_ordinal)
    )
    row = (await conn.execute(stmt)).fetchone()
    if row is None:
        raise KeyError(parent_id)
    return int(row[0]) - 1


async def child_task_id(conn, parent_id: str) -> tuple[str, bool]:
    """Return ``(id, capped)`` for a new child of *parent_id* (spec §6).

    ``capped=False`` — ``f"{parent_id}.{n}"`` with *n* reserved atomically.
    ``capped=True``  — the parent is at :data:`MAX_NAMING_DEPTH`; a fresh
    root id is returned and the caller adds a ``discovered-from`` edge so
    provenance survives without extending the dotted chain.
    """
    if naming_depth(parent_id) >= MAX_NAMING_DEPTH:
        logging.getLogger(__name__).info(
            "child_task_id: parent '%s' at naming depth cap — minting a root id", parent_id
        )
        return (await fresh_root_id(conn), True)
    n = await reserve_child_ordinal(conn, parent_id)
    return (f"{parent_id}.{n}", False)


async def generate_task_id(db, parent_id: str | None = None) -> str:
    """Generate a unique task id.

    With *parent_id* this opens its own transaction and delegates to
    :func:`child_task_id`; callers that already hold a connection (the
    hierarchy mixin, the graph creator) call that directly instead.
    """
    async with db._engine.begin() as conn:
        if parent_id is not None:
            cid, _capped = await child_task_id(conn, parent_id)
            return cid
        return await fresh_root_id(conn)
```

Add `import logging` at the top of the module (keep `import random`).

- [ ] **Step 4: Fix the one existing caller**

`src/commands/task_commands.py:1027` calls `child_task_id(self.db, parent_id)`. It is rewritten in Task 4 (creation becomes one transaction). For now, to keep the suite green, change that line to:

```python
            async with self.db._engine.begin() as _conn:
                task_id, depth_cap_fallback = await child_task_id(_conn, parent_id)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_hierarchy_ids.py tests/test_work_graph_commands.py tests/test_task_queries.py -v -n auto`
Expected: PASS. If any existing test asserted the old sibling-scan behaviour (`grep -rn "_next_child_ordinal\|_MAX_HIERARCHY_DEPTH" tests/`), update it to the counter semantics (ordinals are sequential from 1 and never reused).

- [ ] **Step 6: Commit**

```bash
git add src/task_names.py src/commands/task_commands.py tests/test_hierarchy_ids.py
git commit -m "feat(hierarchy): dotted child ids from an atomic per-parent counter

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `HierarchyQueryMixin` — `set_parent`, `mark_container`, `create_task_under`

**Files:**
- Create: `src/database/queries/hierarchy_queries.py`
- Modify: `src/database/queries/__init__.py` (export), `src/database/adapters/sqlite.py:57-80`, `src/database/adapters/postgresql.py:57-80` (register the mixin **first** in the bases so its `get_task_tree` overrides `TaskQueryMixin`'s in Task 8)
- Modify: `src/database/queries/dependency_queries.py:28-56` (`add_dependency`), `:339-360` (`remove_dependency`)
- Modify: `src/commands/task_commands.py:1015-1090` (`_cmd_create_task` parent path)
- Test: `tests/test_hierarchy_queries.py`

**Interfaces:**
- Produces:
  - `class HierarchyError(Exception)` with `.code: str` ∈ `{not_found, cross_project, cycle, depth, self_parent, container_closed}` and `.detail: str`.
  - `async mark_container(self, task_id, *, conn) -> None` — upsert `task_metadata(task_id, 'container', 'true')`.
  - `async is_container(self, task_id, *, conn) -> bool`.
  - `async structural_depth(self, task_id, *, conn) -> int` (root = 1).
  - `async subtree_height(self, task_id, *, conn) -> int` (leaf = 1).
  - `async subtree_ids(self, root_id, *, conn) -> list[str]` (root first, breadth order).
  - `async set_parent(self, task_id, parent_id, *, conn) -> tuple[set[str], list[str]]` — returns `(flipped, settled)`: blocked-state flips and the container ids `settle_containers` completed (Task 5 implements settlement; until then the stub returns `[]`). Callers forward `settled` to `_notify_settled` after commit (Task 5 adds that helper).
  - `async create_task_under(self, task: Task, parent_id: str) -> tuple[str, bool]` — one transaction: reserve id, insert, `set_parent` (or `discovered-from` at the naming cap). Returns `(task_id, capped)`. `task.id` is ignored and overwritten.
- Consumes: `task_names.child_task_id(conn, parent_id)`, `BlockedStateMixin.recompute_blocked(seeds, conn=conn)` and `_collect_affected(seeds, conn)`, `state_machine.validate_dag_with_new_edge`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_hierarchy_queries.py`)

```python
from src.database.queries.hierarchy_queries import HierarchyError


class TestSetParent:
    async def test_writes_edge_and_pointer_together(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
        assert (await db.get_task("c")).parent_task_id == "p"
        assert await db.get_typed_dependencies("c") == [("p", "parent-child")]
        async with db._engine.begin() as conn:
            assert await db.is_container("p", conn=conn) is True

    async def test_reparent_replaces_the_single_edge(self, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p1", conn=conn)
            await db.set_parent("c", "p2", conn=conn)
        assert (await db.get_task("c")).parent_task_id == "p2"
        assert await db.get_typed_dependencies("c") == [("p2", "parent-child")]

    async def test_to_root_clears_both(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
            await db.set_parent("c", None, conn=conn)
        assert (await db.get_task("c")).parent_task_id is None
        assert await db.get_typed_dependencies("c") == []

    async def test_child_is_withheld_while_container_is_defined(self, db):
        await mktask(db, "p")  # DEFINED
        await mktask(db, "c", status=TaskStatus.READY)
        async with db._engine.begin() as conn:
            await db.set_parent("c", "p", conn=conn)
        assert (await db.get_task("c")).is_blocked is True

    @pytest.mark.parametrize(
        "setup, code",
        [
            ("self", "self_parent"),
            ("cycle", "cycle"),
            ("cross_project", "cross_project"),
            ("depth", "depth"),
            ("missing", "not_found"),
        ],
    )
    async def test_rejections(self, db, setup, code):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "b", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            if setup == "self":
                args = ("a", "a")
            elif setup == "cycle":
                await db.set_parent("b", "a", conn=conn)
                args = ("a", "b")
            elif setup == "cross_project":
                await db.create_project(Project(id="other", name="o"))
                await db.create_task(Task(id="x", project_id="other", title="x", description="x"))
                args = ("x", "a")
            elif setup == "depth":
                await mktask(db, "c", status=TaskStatus.IN_PROGRESS)
                await mktask(db, "d")
                await db.set_parent("b", "a", conn=conn)
                await db.set_parent("c", "b", conn=conn)
                args = ("d", "c")  # would be structural depth 4
            else:
                args = ("a", "ghost")
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent(*args, conn=conn)
        assert exc.value.code == code

    async def test_completed_container_refuses_children(self, db):
        await mktask(db, "p", status=TaskStatus.COMPLETED)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            with pytest.raises(HierarchyError) as exc:
                await db.set_parent("c", "p", conn=conn)
        assert exc.value.code == "container_closed"


class TestCreateTaskUnder:
    async def test_mints_dotted_id_and_links(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        task = Task(id="ignored", project_id=PROJECT_ID, title="t", description="t")
        tid, capped = await db.create_task_under(task, "p")
        assert (tid, capped) == ("p.1", False)
        t = await db.get_task("p.1")
        assert t.parent_task_id == "p"
        assert await db.get_typed_dependencies("p.1") == [("p", "parent-child")]

    async def test_naming_cap_uses_discovered_from(self, db):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        b, _ = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="b", description="b"), "a")
        c, _ = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="c", description="c"), b)
        # c is "a.1.1" — naming depth 3.  Its child gets a root id + discovered-from.
        await db.transition_task(b, TaskStatus.IN_PROGRESS)
        await db.transition_task(c, TaskStatus.IN_PROGRESS)
        d, capped = await db.create_task_under(Task(id="", project_id=PROJECT_ID, title="d", description="d"), c)
        assert capped is True and "." not in d
        assert (await db.get_task(d)).parent_task_id is None
        assert await db.get_typed_dependencies(d) == [(c, "discovered-from")]


class TestDependencyDelegation:
    async def test_add_dependency_parent_child_sets_pointer(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        assert (await db.get_task("c")).parent_task_id == "p"

    async def test_remove_dependency_parent_child_clears_pointer(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        await db.remove_dependency("c", "p", "parent-child")
        assert (await db.get_task("c")).parent_task_id is None
        assert await db.get_typed_dependencies("c") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_queries.py -v`
Expected: FAIL — `ModuleNotFoundError: src.database.queries.hierarchy_queries`.

- [ ] **Step 3: Create the mixin**

```python
# src/database/queries/hierarchy_queries.py
"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import and_, delete, exists, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import task_dependencies, task_metadata, tasks
from src.models import DepType, Task, TaskStatus
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

logger = logging.getLogger(__name__)

#: Container statuses that withhold their children (work-graph §3.1).
WITHHOLDING_PARENT_STATUSES = (
    TaskStatus.DEFINED.value,
    TaskStatus.AWAITING_PLAN_APPROVAL.value,
)

CONTAINER_KEY = "container"
CONTAINER_VALUE = "true"  # json.dumps(True); matches set_task_meta's encoding


class HierarchyError(Exception):
    """A rejected hierarchy mutation.  ``code`` is the stable machine string."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class HierarchyQueryMixin:
    """Expects ``self._engine`` plus BlockedStateMixin and TaskQueryMixin."""

    # -- container flag -------------------------------------------------

    async def mark_container(self, task_id: str, *, conn) -> None:
        """Set ``task_metadata.container = true`` (idempotent).  Never cleared."""
        dialect = conn.dialect.name
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        await conn.execute(
            ins(task_metadata)
            .values(task_id=task_id, key=CONTAINER_KEY, value=CONTAINER_VALUE)
            .on_conflict_do_nothing()
        )

    async def is_container(self, task_id: str, *, conn) -> bool:
        row = (
            await conn.execute(
                select(literal(1)).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == CONTAINER_KEY,
                        task_metadata.c.value == CONTAINER_VALUE,
                    )
                )
            )
        ).fetchone()
        return row is not None

    # -- structure reads (CTE) -------------------------------------------

    def _ancestor_cte(self, task_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == task_id)
            .cte("ancestors", recursive=True)
        )
        parent = tasks.alias("parent")
        rec = select(parent.c.id, parent.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            parent.c.id == base.c.parent_task_id
        )
        return base.union_all(rec)

    def _descendant_cte(self, root_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == root_id)
            .cte("descendants", recursive=True)
        )
        child = tasks.alias("child")
        rec = select(child.c.id, child.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            child.c.parent_task_id == base.c.id
        )
        return base.union_all(rec)

    async def structural_depth(self, task_id: str, *, conn) -> int:
        """Live parent-child chain length from *task_id* to its root (root = 1)."""
        cte = self._ancestor_cte(task_id)
        row = (await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))).fetchone()
        return int(row[0]) if row else 0

    async def subtree_height(self, task_id: str, *, conn) -> int:
        """Height of the subtree rooted at *task_id* (leaf = 1)."""
        cte = self._descendant_cte(task_id)
        row = (await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))).fetchone()
        return int(row[0]) if row else 0

    async def subtree_ids(self, root_id: str, *, conn) -> list[str]:
        """Every id in the subtree, root first, shallow before deep."""
        cte = self._descendant_cte(root_id)
        rows = (await conn.execute(select(cte.c.id).order_by(cte.c.depth, cte.c.id))).fetchall()
        return [r[0] for r in rows]

    # -- the single writer ----------------------------------------------

    async def set_parent(
        self, task_id: str, parent_id: str | None, *, conn
    ) -> tuple[set[str], list[str]]:
        """Move *task_id* under *parent_id* (``None`` = root).  Spec §5.

        Same transaction: delete any existing parent-child edge, insert the
        new one, write ``tasks.parent_task_id``, recompute ``is_blocked``
        over the affected set, mark the new parent a container, settle both
        the old and the new container.  Returns the blocked-state flips.
        """
        task_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id == task_id
                )
            )
        ).fetchone()
        if task_row is None:
            raise HierarchyError("not_found", task_id)
        old_parent = task_row.parent_task_id

        if parent_id is not None:
            if parent_id == task_id:
                raise HierarchyError("self_parent", task_id)
            parent_row = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id, tasks.c.status).where(
                        tasks.c.id == parent_id
                    )
                )
            ).fetchone()
            if parent_row is None:
                raise HierarchyError("not_found", parent_id)
            if parent_row.project_id != task_row.project_id:
                raise HierarchyError(
                    "cross_project", f"{task_id} is in {task_row.project_id}, {parent_id} in {parent_row.project_id}"
                )
            if parent_row.status == TaskStatus.COMPLETED.value:
                raise HierarchyError("container_closed", parent_id)
            # Cycle: the new parent must not be inside task_id's subtree.
            if parent_id in await self.subtree_ids(task_id, conn=conn):
                raise HierarchyError("cycle", f"{parent_id} is a descendant of {task_id}")
            depth = await self.structural_depth(parent_id, conn=conn)
            height = await self.subtree_height(task_id, conn=conn)
            if depth + height > MAX_STRUCTURAL_DEPTH:
                raise HierarchyError(
                    "depth", f"parent depth {depth} + subtree height {height} > {MAX_STRUCTURAL_DEPTH}"
                )
            # Blocking-edge DAG check (waits-for / blocks edges could loop
            # through the new parent-child edge).
            deps = await self._blocking_edges(conn)
            try:
                validate_dag_with_new_edge(deps, task_id, parent_id, DepType.PARENT_CHILD.value)
            except CyclicDependencyError as exc:
                raise HierarchyError("cycle", str(exc)) from exc

        affected = await self._collect_affected({task_id}, conn)
        if old_parent:
            affected.add(old_parent)
        if parent_id:
            affected.add(parent_id)

        await conn.execute(
            delete(task_dependencies).where(
                and_(
                    task_dependencies.c.task_id == task_id,
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        if parent_id is not None:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id=task_id, depends_on_task_id=parent_id,
                    dep_type=DepType.PARENT_CHILD.value,
                )
            )
            await self.mark_container(parent_id, conn=conn)
        await conn.execute(
            update(tasks).where(tasks.c.id == task_id).values(
                parent_task_id=parent_id, updated_at=time.time()
            )
        )
        affected |= await self._collect_affected({task_id}, conn)
        flipped = await self.recompute_blocked(affected, conn=conn)
        settled = await self.settle_containers(
            {p for p in (old_parent, parent_id) if p}, conn=conn
        )
        return flipped, settled

    async def _blocking_edges(self, conn) -> dict[str, set[str]]:
        from src.models import BLOCKING_DEP_TYPES

        rows = (
            await conn.execute(
                select(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id).where(
                    task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES))
                )
            )
        ).fetchall()
        deps: dict[str, set[str]] = {}
        for tid, dep in rows:
            deps.setdefault(tid, set()).add(dep)
        return deps

    # -- settlement (filled in by Task 5) ---------------------------------

    async def settle_containers(self, seeds: set[str], *, conn) -> list[str]:
        """Complete every seeded container whose children are all done (spec §7)."""
        return []  # replaced in Task 5

    # -- creation -------------------------------------------------------

    async def create_task_under(self, task: Task, parent_id: str) -> tuple[str, bool]:
        """Insert *task* as a child of *parent_id* in one transaction (spec §6).

        Reserves the dotted id, inserts the row, links it via
        :meth:`set_parent` — or, at the naming cap, gives it a root id and a
        ``discovered-from`` edge.  Returns ``(task_id, capped)``.
        """
        async with self._engine.begin() as conn:
            task_id, capped = await child_task_id(conn, parent_id)
            task.id = task_id
            task.parent_task_id = None  # set_parent owns the pointer
            await self._insert_task_row(task, conn=conn)
            if capped:
                await conn.execute(
                    insert(task_dependencies).values(
                        task_id=task_id, depends_on_task_id=parent_id,
                        dep_type=DepType.DISCOVERED_FROM.value,
                    )
                )
            else:
                await self.set_parent(task_id, parent_id, conn=conn)
        return task_id, capped
```

`_insert_task_row(task, conn=conn)` does not exist yet: refactor `TaskQueryMixin.create_task` so its `insert(tasks).values(...)` body lives in `async def _insert_task_row(self, task, *, conn)` and `create_task` becomes:

```python
    async def create_task(self, task: Task) -> None:
        """Insert a new task row."""
        async with self._engine.begin() as conn:
            await self._insert_task_row(task, conn=conn)
```

- [ ] **Step 4: Register the mixin**

`src/database/queries/__init__.py` — add `from src.database.queries.hierarchy_queries import HierarchyQueryMixin` and put `"HierarchyQueryMixin"` in `__all__`. In both adapters, add `HierarchyQueryMixin,` as the **first** base of the adapter class (before `ProjectQueryMixin`).

- [ ] **Step 5: Delegate parent-child edges in `dependency_queries.py`**

At the top of `add_dependency`, before opening the transaction:

```python
        if dep_type == DepType.PARENT_CHILD.value:
            async with self._engine.begin() as conn:
                flipped, _settled = await self.set_parent(task_id, depends_on, conn=conn)
            await self.log_blocked_flips(flipped)
            return
```

At the top of `remove_dependency`:

```python
        if dep_type == DepType.PARENT_CHILD.value:
            async with self._engine.begin() as conn:
                current = (
                    await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
                ).fetchone()
                if current is None or current[0] != depends_on:
                    return
                flipped, _settled = await self.set_parent(task_id, None, conn=conn)
            await self.log_blocked_flips(flipped)
            return
```

When `dep_type is None` in `remove_dependency` (remove every kind between the pair), keep the existing delete but add, before it, the same pointer clearing: if `parent_task_id == depends_on`, call `set_parent(task_id, None, conn=conn)` inside the same transaction and exclude `parent-child` from the subsequent delete (it is already gone).

- [ ] **Step 6: Route `_cmd_create_task --parent` through `create_task_under`**

In `_cmd_create_task` (`task_commands.py:1015-1070`): delete the `if parent_id: … child_task_id …` block and the `else: task_id = await generate_task_id(self.db)` line. Build the `Task` with `id=""` and `parent_task_id=None` when `parent_id` is set, then replace `await self.db.create_task(task)` with:

```python
        depth_cap_fallback = False
        if parent_id:
            try:
                task_id, depth_cap_fallback = await self.db.create_task_under(task, parent_id)
            except HierarchyError as exc:
                return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}
        else:
            task_id = await generate_task_id(self.db)
            task.id = task_id
            await self.db.create_task(task)
```

(`from src.database.queries.hierarchy_queries import HierarchyError` at the top of the module.) Keep the existing `edges` loop, but drop the two lines that appended a `parent-child` / `discovered-from` edge for `parent_id` — `create_task_under` owns that now. The `initial_status` computation still sees `has_blocking_edge`; a parented task must start `DEFINED` — set `has_blocking_edge = True` when `parent_id` is given.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_hierarchy_queries.py tests/test_hierarchy_ids.py tests/test_work_graph_commands.py tests/test_work_graph_cascade.py tests/test_task_queries.py -v -n auto`
Expected: PASS. The `depth` rejection test needs `settle_containers` to be a no-op (it is) and `a`,`b`,`c` in `IN_PROGRESS` so `container_closed` does not fire.

- [ ] **Step 8: Commit**

```bash
git add src/database/queries/hierarchy_queries.py src/database/queries/__init__.py src/database/adapters/sqlite.py src/database/adapters/postgresql.py src/database/queries/dependency_queries.py src/database/queries/task_queries.py src/commands/task_commands.py tests/test_hierarchy_queries.py
git commit -m "feat(hierarchy): single-writer set_parent, create_task_under, edge delegation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `_apply_transition` split, `settle_containers`, settlement listener

**Files:**
- Modify: `src/database/queries/task_queries.py:216-308` (`transition_task`)
- Modify: `src/database/queries/hierarchy_queries.py` (replace the `settle_containers` stub)
- Test: `tests/test_hierarchy_settlement.py`

**Interfaces:**
- Produces:
  - `@dataclass TransitionResult: flipped: set[str]; settled: list[str]` (in `task_queries.py`).
  - `async _apply_transition(self, conn, task_id, new_status, *, context="", event=None, force=False, **kwargs) -> TransitionResult` — the old `transition_task` body on a caller-owned connection; when `new_status == COMPLETED` and the task has a parent, calls `settle_containers({parent}, conn=conn)` and merges its result.
  - `transition_task(...)` keeps its signature and **return type (`set[str]`)** for every existing caller; internally opens the transaction, calls `_apply_transition`, then after commit: `log_blocked_flips(flipped)` and `await self._notify_settled(settled)`.
  - `set_settlement_listener(self, cb: Callable[[list[str]], Awaitable[None]] | None)`; `_settlement_listener` default `None`.
  - `async settle_containers(self, seeds, *, conn) -> list[str]` — spec §7 predicate; uses `_apply_transition(conn, id, COMPLETED, context="subtasks_completed")` per hit; walks up ≤ 3 levels.
  - Constant `LIVE_SESSION_STATES = ("starting", "running", "draining")` in `hierarchy_queries.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_settlement.py
"""Container settlement — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.models import Project, SessionRecord, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def family(db, n=2):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    kids = []
    for i in range(n):
        await mktask(db, f"c{i}", status=TaskStatus.READY)
        await db.add_dependency(f"c{i}", "p", "parent-child")
        kids.append(f"c{i}")
    return kids


class TestSettlement:
    async def test_last_child_completion_completes_container_in_same_call(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS
        await db.transition_task(kids[1], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED

    async def test_listener_receives_settled_ids(self, db):
        seen = []

        async def cb(ids):
            seen.append(list(ids))

        db.set_settlement_listener(cb)
        kids = await family(db, n=1)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert seen == [["p"]]

    async def test_live_session_guard(self, db):
        kids = await family(db, n=1)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1", task_id="p", project_id=PROJECT_ID, profile_id="worker",
                harness="claude", provider="fake", name="s-p", lifecycle="task",
                state="running", work_dir="/tmp", epoch="e", instance_token="t",
                started_at=now, last_activity=now,
            )
        )
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_failed_child_does_not_settle(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        await db.transition_task(kids[1], TaskStatus.FAILED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_settles_up_to_three_levels(self, db):
        await mktask(db, "g", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("p", "g", "parent-child")
        await db.add_dependency("c", "p", "parent-child")
        await db.transition_task("c", TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("g")).status == TaskStatus.COMPLETED

    async def test_emptied_container_settles_on_reparent(self, db):
        kids = await family(db, n=1)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.set_parent(kids[0], "p2", conn=conn)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p2")).status == TaskStatus.IN_PROGRESS

    async def test_non_container_in_progress_leaf_is_untouched(self, db):
        await mktask(db, "leaf", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            settled = await db.settle_containers({"leaf"}, conn=conn)
        assert settled == []
        assert (await db.get_task("leaf")).status == TaskStatus.IN_PROGRESS
```

Check `SessionRecord`'s required fields in `src/models.py:1197-1240` and `create_session` in `src/database/queries/session_queries.py`; adjust the constructor call to the exact required set (the fields above are the ones the model listed on 2026-08-28).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_settlement.py -v`
Expected: FAIL — `AttributeError: 'SQLiteDatabaseAdapter' object has no attribute 'set_settlement_listener'`, and the completion tests fail because the container stays `IN_PROGRESS`.

- [ ] **Step 3: Split `transition_task`**

In `task_queries.py`, add near the top:

```python
from dataclasses import dataclass, field


@dataclass
class TransitionResult:
    """What one status write changed besides the row itself."""

    flipped: set[str] = field(default_factory=set)
    settled: list[str] = field(default_factory=list)
```

Rename the existing `transition_task` to `_apply_transition` with signature
`async def _apply_transition(self, conn, task_id: str, new_status: TaskStatus, *, context: str = "", event=None, force: bool = False, **kwargs) -> TransitionResult:` and make these edits inside it:

1. Remove the `async with self._engine.begin() as conn:` line and dedent the body one level — the caller owns `conn`.
2. Replace `return set()` (task-not-found branch) with `return TransitionResult()`.
3. Track `result = TransitionResult()`; wherever the old code assigned `flipped = …`, assign `result.flipped = …`.
4. After the terminal-gate block (`if new_status.value in self._TERMINAL_TASK_STATUSES: await self.expire_satisfied_gates(...)`), add:

```python
                if new_status == TaskStatus.COMPLETED:
                    parent = (
                        await conn.execute(
                            select(tasks.c.parent_task_id).where(tasks.c.id == task_id)
                        )
                    ).scalar()
                    if parent:
                        result.settled.extend(await self.settle_containers({parent}, conn=conn))
```

5. Remove the trailing `await self.log_blocked_flips(flipped)`; end with `return result`.

Then add the public wrapper and listener:

```python
    _settlement_listener = None

    def set_settlement_listener(self, cb) -> None:
        """Register the post-commit callback for settled containers (spec §7)."""
        self._settlement_listener = cb

    async def _notify_settled(self, settled: list[str]) -> None:
        if settled and self._settlement_listener is not None:
            try:
                await self._settlement_listener(list(settled))
            except Exception:  # a listener failure must not fail the transition
                logger.exception("settlement listener failed for %s", settled)

    async def transition_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        event=None,
        force: bool = False,
        **kwargs,
    ) -> set[str]:
        """Public status write: one transaction, then post-commit emission.

        Returns the blocked-state flips (unchanged contract).  Settled
        containers are delivered to the settlement listener after commit.
        """
        async with self._engine.begin() as conn:
            result = await self._apply_transition(
                conn, task_id, new_status, context=context, event=event, force=force, **kwargs
            )
        await self.log_blocked_flips(result.flipped)
        await self._notify_settled(result.settled)
        return result.flipped
```

Also update the two `set_parent` callers in `dependency_queries.py` (Task 4) to forward
settlement: rename `_settled` to `settled` and add `await self._notify_settled(settled)`
immediately after each `await self.log_blocked_flips(flipped)`.

- [ ] **Step 4: Implement `settle_containers`**

Replace the stub in `hierarchy_queries.py`:

```python
LIVE_SESSION_STATES = ("starting", "running", "draining")


    async def settle_containers(self, seeds: set[str], *, conn) -> list[str]:
        """Complete every seeded container whose children are all done (spec §7).

        Predicate: container flag ∧ status = IN_PROGRESS ∧ no live session holds
        it ∧ no non-COMPLETED child (vacuously true when empty).  Each hit goes
        through ``_apply_transition``, which seeds its own parent, so the walk
        climbs at most ``MAX_STRUCTURAL_DEPTH`` levels.
        """
        from src.database.tables import sessions

        settled: list[str] = []
        pending = {s for s in seeds if s}
        rounds = 0
        while pending and rounds < MAX_STRUCTURAL_DEPTH:
            rounds += 1
            child = tasks.alias("child")
            stmt = select(tasks.c.id).where(
                and_(
                    tasks.c.id.in_(sorted(pending)),
                    tasks.c.status == TaskStatus.IN_PROGRESS.value,
                    exists(
                        select(literal(1)).where(
                            and_(
                                task_metadata.c.task_id == tasks.c.id,
                                task_metadata.c.key == CONTAINER_KEY,
                                task_metadata.c.value == CONTAINER_VALUE,
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                sessions.c.task_id == tasks.c.id,
                                sessions.c.state.in_(LIVE_SESSION_STATES),
                            )
                        )
                    ),
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                child.c.parent_task_id == tasks.c.id,
                                child.c.status != TaskStatus.COMPLETED.value,
                            )
                        )
                    ),
                )
            )
            hits = [r[0] for r in (await conn.execute(stmt)).fetchall()]
            pending = set()
            for cid in hits:
                # _apply_transition seeds the container's own parent via
                # settle_containers, so grandparents are handled by recursion;
                # collect everything it settled.
                res = await self._apply_transition(
                    conn, cid, TaskStatus.COMPLETED, context="subtasks_completed"
                )
                settled.append(cid)
                settled.extend(res.settled)
        return settled
```

Because `_apply_transition` already recurses through `settle_containers` for the parent, the `while` loop only iterates once in practice; it stays as a bound.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_hierarchy_settlement.py tests/test_hierarchy_queries.py tests/test_work_graph_cascade.py tests/test_work_graph_outcomes.py tests/test_orchestrator.py -v -n auto`
Expected: PASS. `grep -rn "transition_task(" src | wc -l` — every caller still gets a `set[str]`.

- [ ] **Step 6: Commit**

```bash
git add src/database/queries/task_queries.py src/database/queries/hierarchy_queries.py tests/test_hierarchy_settlement.py
git commit -m "feat(hierarchy): connection-aware _apply_transition and container settlement

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Container-close semantics on the command surface

**Files:**
- Modify: `src/commands/session_commands.py:435-600` (`_cmd_task_close`)
- Modify: `src/commands/task_commands.py:3044-3070` (`_cmd_set_task_status`, `_cmd_skip_task`)
- Modify: `src/orchestrator/core.py:1125-1160` (`skip_task`)
- Modify: `src/database/queries/hierarchy_queries.py` (add `open_children`, `live_descendant_sessions`, `abandon_subtree`)
- Test: `tests/test_hierarchy_commands.py`

**Interfaces:**
- Produces:
  - `async open_children(self, task_id, *, conn=None) -> list[str]` — non-terminal children (status ∉ COMPLETED/FAILED).
  - `async live_descendant_sessions(self, task_id, *, conn) -> list[tuple[str, str]]` — `(session_id, task_id)` for live sessions holding any descendant; on Postgres the select uses `.with_for_update()`.
  - `async abandon_subtree(self, task_id, *, conn) -> list[str]` — locks the subtree rows (`.with_for_update()` on Postgres), closes each non-terminal descendant `COMPLETED` with `task_metadata.work_outcome = "abandoned"` via `_apply_transition`, returns the ids. Caller must have checked `live_descendant_sessions` on the same `conn` first.
  - Error codes returned by commands: `hierarchy.open_children`, `hierarchy.live_descendants` (with `sessions: [...]`).
  - `_cmd_task_close` accepts `abandon_children: bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_commands.py
"""Container-close semantics and the hierarchy command surface — spec §7, §14."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.complete_session_task = AsyncMock(return_value={"status": "COMPLETED"})
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


async def container_with_open_child(db):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "c", status=TaskStatus.READY)
    await db.add_dependency("c", "p", "parent-child")


class TestCloseRefusals:
    async def test_task_close_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close({"task_id": "p", "outcome": "pass", "summary": "x"})
        assert res["success"] is False
        assert res["code"] == "hierarchy.open_children"
        assert res["open_children"] == ["c"]

    async def test_set_task_status_refuses_open_children(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_set_task_status({"task_id": "p", "status": "COMPLETED"})
        assert res.get("code") == "hierarchy.open_children"

    async def test_skip_refuses_open_children(self, handler, db):
        await mktask(db, "p", status=TaskStatus.BLOCKED)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        res = await handler._cmd_skip_task({"task_id": "p"})
        assert "open_children" in res["error"]


class TestAbandonChildren:
    async def test_abandons_when_no_live_descendants(self, handler, db):
        await container_with_open_child(db)
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["success"] is True
        assert res["abandoned"] == ["c"]
        assert (await db.get_task("c")).status == TaskStatus.COMPLETED
        assert await db.get_task_meta("c", "work_outcome") == "abandoned"

    async def test_refused_while_descendant_has_live_session(self, handler, db):
        await container_with_open_child(db)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1", task_id="c", project_id=PROJECT_ID, profile_id="worker",
                harness="claude", provider="fake", name="s-c", lifecycle="task",
                state="running", work_dir="/tmp", epoch="e", instance_token="t",
                started_at=now, last_activity=now,
            )
        )
        res = await handler._cmd_task_close(
            {"task_id": "p", "outcome": "pass", "summary": "x", "abandon_children": True}
        )
        assert res["code"] == "hierarchy.live_descendants"
        assert res["sessions"] == [{"session_id": "s1", "task_id": "c"}]
        assert (await db.get_task("c")).status == TaskStatus.READY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_commands.py -v`
Expected: FAIL — `_cmd_task_close` returns success / the skip and set_status paths have no refusal.

- [ ] **Step 3: Add the query helpers** (append to `HierarchyQueryMixin`)

```python
    async def open_children(self, task_id: str, *, conn=None) -> list[str]:
        """Direct children not yet terminal (spec §7 close rule)."""
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        stmt = (
            select(tasks.c.id)
            .where(and_(tasks.c.parent_task_id == task_id, tasks.c.status.notin_(terminal)))
            .order_by(tasks.c.id)
        )
        if conn is not None:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]
        async with self._engine.begin() as c:
            return [r[0] for r in (await c.execute(stmt)).fetchall()]

    async def live_descendant_sessions(self, task_id: str, *, conn) -> list[tuple[str, str]]:
        """Live sessions holding any task in *task_id*'s subtree.

        Lock order is sessions-before-tasks to match the claim path (spec §7):
        on Postgres the rows are taken ``FOR UPDATE`` so a session cannot
        start holding a descendant between this check and the abandonment.
        """
        from src.database.tables import sessions

        ids = await self.subtree_ids(task_id, conn=conn)
        stmt = select(sessions.c.id, sessions.c.task_id).where(
            and_(sessions.c.task_id.in_(ids), sessions.c.state.in_(LIVE_SESSION_STATES))
        )
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return [(r[0], r[1]) for r in (await conn.execute(stmt)).fetchall()]

    async def abandon_subtree(self, task_id: str, *, conn) -> list[str]:
        """Close every non-terminal descendant as ``abandoned`` (spec §7)."""
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        rows = (await conn.execute(stmt)).fetchall()
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        # Deepest first so each container settles naturally after its children.
        depth = {tid: i for i, tid in enumerate(ids)}
        open_ids = sorted((r[0] for r in rows if r[1] not in terminal), key=lambda t: -depth[t])
        abandoned: list[str] = []
        for tid in open_ids:
            await self._upsert_meta(tid, "work_outcome", "abandoned", conn=conn)
            await self._apply_transition(
                conn, tid, TaskStatus.COMPLETED, context="abandoned_by_container",
                assigned_agent_id=None,
            )
            abandoned.append(tid)
        return abandoned

    async def _upsert_meta(self, task_id: str, key: str, value, *, conn) -> None:
        import json

        encoded = json.dumps(value)
        res = await conn.execute(
            update(task_metadata)
            .where(and_(task_metadata.c.task_id == task_id, task_metadata.c.key == key))
            .values(value=encoded)
        )
        if res.rowcount == 0:
            await conn.execute(insert(task_metadata).values(task_id=task_id, key=key, value=encoded))
```

- [ ] **Step 4: Refuse in `_cmd_task_close`**

Right after the `caller_session_id` validation block and before the summary check, insert:

```python
        # Container-close semantics (swarm-work-model §7).
        open_children = await self.db.open_children(task_id)
        abandoned: list[str] = []
        if open_children:
            if not args.get("abandon_children"):
                return {
                    "success": False,
                    "code": "hierarchy.open_children",
                    "error": (
                        f"task {task_id} has {len(open_children)} open child(ren); close them "
                        "first or pass abandon_children=true"
                    ),
                    "open_children": open_children,
                }
            immediate = getattr(self.db, "immediate", None) or self.db._engine.begin
            async with immediate() as conn:
                live = await self.db.live_descendant_sessions(task_id, conn=conn)
                if live:
                    return {
                        "success": False,
                        "code": "hierarchy.live_descendants",
                        "error": "descendants are held by live sessions; stop them first "
                                 "(aq task stop <id> / aq session kill <name>)",
                        "sessions": [{"session_id": s, "task_id": t} for s, t in live],
                    }
                abandoned = await self.db.abandon_subtree(task_id, conn=conn)
```

and include `"abandoned": abandoned` in the success dict at the end. `self.db.immediate` is the `BEGIN IMMEDIATE` context manager Plan 2 adds; until then `getattr` falls back to `begin()` (the SQLite test suite is single-connection, so it is serialised anyway).

- [ ] **Step 5: Refuse in `set_task_status` and `skip_task`**

`_cmd_set_task_status` — before `transition_task`:

```python
        if new_status == TaskStatus.COMPLETED.value:
            open_children = await self.db.open_children(task_id)
            if open_children:
                return {
                    "error": f"task {task_id} has open children: {', '.join(open_children)}",
                    "code": "hierarchy.open_children",
                    "open_children": open_children,
                }
```

`Orchestrator.skip_task` (`core.py:1140`) — after the status check:

```python
        open_children = await self.db.open_children(task_id)
        if open_children:
            return (
                f"Task has open_children ({', '.join(open_children)}); close or "
                "abandon them first.",
                [],
            )
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_hierarchy_commands.py tests/test_session_commands.py tests/test_human_in_the_loop.py -v -n auto`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/database/queries/hierarchy_queries.py src/commands/session_commands.py src/commands/task_commands.py src/orchestrator/core.py tests/test_hierarchy_commands.py
git commit -m "feat(hierarchy): container-close semantics — refuse open children, abandon_children

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Delete (refuse / cascade) and subtree-atomic archive

**Files:**
- Modify: `src/database/queries/task_queries.py:379-475` (`delete_task`)
- Modify: `src/database/queries/archive_queries.py:31-140` (`archive_task`), `:158-182` (`archive_old_terminal_tasks`)
- Modify: `src/commands/task_commands.py:1880-1890` (`_cmd_delete_task`), `:1897-2007` (`_cmd_archive_task` single mode)
- Test: `tests/test_hierarchy_archive_delete.py`

**Interfaces:**
- Produces:
  - `delete_task(task_id, *, cascade: bool = False)` — raises `HierarchyError("has_children")` when the task has children and `cascade` is false; with `cascade`, deletes the subtree deepest-first in one transaction and settles the deleted root's parent.
  - `archive_task(task_id)` — raises `HierarchyError("open_descendants", detail)` unless every descendant is terminal; archives the whole subtree in one transaction, deepest first, root last. The `UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = :id` line is removed.
  - `archive_old_terminal_tasks(statuses, older_than_seconds)` selects only tasks that are **subtree roots of terminal subtrees**: terminal, older than cutoff, `parent_task_id IS NULL`, and with no non-terminal descendant. Children are archived by the root's subtree archive, not selected individually.
  - `_cmd_delete_task` accepts `cascade: bool`; error code `hierarchy.has_children`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_archive_delete.py
"""Delete refuse/cascade and subtree-atomic archive — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def tree(db, statuses=("IN_PROGRESS", "READY", "READY")):
    await mktask(db, "p", status=TaskStatus(statuses[0]))
    await mktask(db, "c1", status=TaskStatus(statuses[1]))
    await mktask(db, "c2", status=TaskStatus(statuses[2]))
    await db.add_dependency("c1", "p", "parent-child")
    await db.add_dependency("c2", "p", "parent-child")


class TestDelete:
    async def test_refuses_container_with_children(self, db):
        await tree(db)
        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p")
        assert exc.value.code == "has_children"
        assert await db.get_task("p") is not None

    async def test_cascade_deletes_subtree(self, db):
        await tree(db)
        await db.delete_task("p", cascade=True)
        assert await db.get_task("p") is None
        assert await db.get_task("c1") is None
        assert await db.get_task("c2") is None

    async def test_deleting_last_child_settles_container(self, db):
        await tree(db, statuses=("IN_PROGRESS", "COMPLETED", "READY"))
        await db.delete_task("c2")
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED


class TestArchive:
    async def test_refuses_open_descendants(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("p")
        assert exc.value.code == "open_descendants"

    async def test_archives_subtree_together(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "FAILED"))
        assert await db.archive_task("p") is True
        for tid in ("p", "c1", "c2"):
            assert await db.get_task(tid) is None
            assert (await db.get_archived_task(tid)) is not None
        assert (await db.get_archived_task("c1"))["parent_task_id"] == "p"

    async def test_sweep_selects_only_terminal_subtree_roots(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        await mktask(db, "lone", status=TaskStatus.COMPLETED)
        # Make every row old enough.
        async with db._engine.begin() as conn:
            from sqlalchemy import update

            from src.database.tables import tasks

            await conn.execute(update(tasks).values(updated_at=time.time() - 10_000))
        archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
        assert archived == ["lone"]
        assert await db.get_task("p") is not None
        assert await db.get_task("c1") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_archive_delete.py -v`
Expected: FAIL — `delete_task() got an unexpected keyword argument 'cascade'`, and archive leaves `c1`/`c2` behind / nulls their pointer.

- [ ] **Step 3: Rewrite `delete_task`**

Extract the existing per-task body into `async def _delete_one(self, task_id, *, conn)` (everything from the `task_results` delete through `delete(tasks)`, **without** the `_collect_affected` snapshot and the final recompute — those move to the wrapper). Then:

```python
    async def delete_task(self, task_id: str, *, cascade: bool = False) -> None:
        """Delete a task; with *cascade*, its whole subtree (spec §7).

        Refuses a container with children unless *cascade*.  One transaction:
        dependents are snapshotted while the edges exist, the subtree is
        removed deepest-first, the former container is settled, and the
        projection is recomputed.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        async with self._engine.begin() as conn:
            parent = (
                await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
            ).scalar()
            ids = await self.subtree_ids(task_id, conn=conn)
            if len(ids) > 1 and not cascade:
                raise HierarchyError("has_children", f"{task_id} has {len(ids) - 1} descendant(s)")
            affected = await self._collect_affected(set(ids), conn)
            affected -= set(ids)
            if parent:
                affected.add(parent)
            for tid in reversed(ids):  # deepest first (subtree_ids is shallow→deep)
                await self._delete_one(tid, conn=conn)
            flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
            settled = await self.settle_containers({parent} if parent else set(), conn=conn)
        await self.log_blocked_flips(flipped)
        await self._notify_settled(settled)
```

- [ ] **Step 4: Rewrite `archive_task`**

Extract the existing per-task body (archive insert, timestamp copy, child-row deletes, agent/workspace unlinks, `delete(tasks)`) into `async def _archive_one(self, task: Task, *, conn)`. **Delete** the `update(tasks).where(tasks.c.parent_task_id == task_id).values(parent_task_id=None)` statement from it. Then:

```python
    async def archive_task(self, task_id: str) -> bool:
        """Archive *task_id* and its whole subtree atomically (spec §7).

        Refuses unless every descendant is terminal.  Deepest first, root
        last, so an archived child never points at a live parent.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
        async with self._engine.begin() as conn:
            ids = await self.subtree_ids(task_id, conn=conn)
            if not ids:
                return False
            rows = (
                await conn.execute(select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids)))
            ).fetchall()
            open_ids = [r[0] for r in rows if r[1] not in terminal and r[0] != task_id]
            if open_ids:
                raise HierarchyError("open_descendants", ", ".join(sorted(open_ids)))
            parent = (
                await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
            ).scalar()
            affected = await self._collect_affected(set(ids), conn)
            affected -= set(ids)
            for tid in reversed(ids):
                task = await self._get_task_conn(tid, conn=conn)
                if task is not None:
                    await self._archive_one(task, conn=conn)
            flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
            settled = await self.settle_containers({parent} if parent else set(), conn=conn)
        await self.log_blocked_flips(flipped)
        await self._notify_settled(settled)
        return True
```

Add to `TaskQueryMixin`: `async def _get_task_conn(self, task_id, *, conn) -> Task | None` — the `get_task` body on a supplied connection (`select(tasks).where(...)` → `_row_to_task`).

- [ ] **Step 5: Make the sweep select subtree roots**

Replace the select in `archive_old_terminal_tasks` with:

```python
        child = tasks.alias("child")
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.status.in_(statuses),
                tasks.c.updated_at <= cutoff,
                tasks.c.parent_task_id.is_(None),
                ~exists(
                    select(literal(1)).where(
                        and_(
                            child.c.parent_task_id == tasks.c.id,
                            child.c.status.notin_(
                                (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
                                 TaskStatus.BLOCKED.value)
                            ),
                        )
                    )
                ),
            )
        )
```

(import `exists`, `literal` from sqlalchemy). The `EXISTS` only sees direct children; open grandchildren are caught by `archive_task`'s own subtree check, which raises — catch `HierarchyError` in the loop, log at debug, and skip that root.

- [ ] **Step 6: Command surface**

`_cmd_delete_task`: read `cascade = bool(args.get("cascade", False))`; wrap `await self.db.delete_task(args["task_id"], cascade=cascade)` in `try/except HierarchyError as exc: return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}`. In the single-task branch of `_cmd_archive_task`, wrap the `archive_task` call the same way (code `hierarchy.open_descendants`).

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_hierarchy_archive_delete.py tests/test_archive*.py tests/test_task_commands*.py -v -n auto`
Expected: PASS. If an existing archive test relied on children being orphaned (`parent_task_id = NULL`), rewrite it to expect the subtree to archive together.

- [ ] **Step 8: Commit**

```bash
git add src/database/queries/task_queries.py src/database/queries/archive_queries.py src/commands/task_commands.py tests/test_hierarchy_archive_delete.py
git commit -m "feat(hierarchy): delete refuses/cascades, archive is subtree-atomic

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Reads — CTE tree, `get_children`, progress extras, `task_show` summary

**Files:**
- Modify: `src/database/queries/hierarchy_queries.py` (add `get_children`, `get_children_summary`, `get_task_tree`)
- Modify: `src/database/queries/task_queries.py:613-707` (delete the old `get_task_tree`; extend `get_group_progress`)
- Modify: `src/commands/surface_commands.py:128+` (`_cmd_task_show`), `src/commands/task_commands.py:1386-1396` (`_cmd_get_task`)
- Test: `tests/perf/test_hierarchy_statements.py`, plus cases in `tests/test_hierarchy_queries.py`

**Interfaces:**
- Produces:
  - `async get_children(self, parent_id, *, recursive=False, status: str | None = None, limit: int | None = None, offset: int = 0) -> list[Task]`.
  - `async get_children_summary(self, task_id) -> dict | None` → `{"total", "done", "ready", "blocked", "in_progress"}` or `None` when the task has no children (one aggregate statement).
  - `async get_task_tree(self, root_task_id, *, max_depth: int = 4) -> dict | None` — same nested `{"task", "children"}` shape as before, built from **one** recursive CTE (max_depth bounds the CTE).
  - `get_group_progress` payload gains `"max_parallelism": int` and `"depth": int`.
  - `_cmd_task_show` result gains `"parent": {"id","title","status"} | None` and `"children": <summary> | None`; `_cmd_get_task` gains `"children"` the same way (keeps `subtasks`).
- Test fixture `count_statements(db)` — an async context manager that counts SQLAlchemy `before_cursor_execute` events on `db._engine.sync_engine`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/perf/test_hierarchy_statements.py
"""Statement budgets for hierarchy reads — spec §15.2 (size-independent)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event

from src.database import Database
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "perf.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@asynccontextmanager
async def count_statements(db):
    counter = {"n": 0}

    def _hook(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        yield counter
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)


async def build_wide_tree(db, width: int):
    await db.create_task(Task(id="root", project_id=PROJECT_ID, title="r", description="r",
                              status=TaskStatus.IN_PROGRESS))
    for i in range(width):
        cid = f"root.{i + 1}"
        await db.create_task(Task(id=cid, project_id=PROJECT_ID, title=cid, description=cid,
                                  status=TaskStatus.READY, parent_task_id=None))
        await db.add_dependency(cid, "root", "parent-child")
        gid = f"{cid}.1"
        await db.create_task(Task(id=gid, project_id=PROJECT_ID, title=gid, description=gid,
                                  status=TaskStatus.COMPLETED))
        await db.add_dependency(gid, cid, "parent-child")


@pytest.mark.parametrize("width", [3, 60])
async def test_tree_children_progress_are_size_independent(db, width):
    await build_wide_tree(db, width)
    async with count_statements(db) as c:
        await db.get_task_tree("root")
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_children("root", recursive=True)
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_group_progress("root")
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_children_summary("root")
    assert c["n"] <= 1
```

And in `tests/test_hierarchy_queries.py`:

```python
class TestReads:
    async def test_tree_shape_and_depth_bound(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1.1")
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.1.1", "r.1", "parent-child")
        tree = await db.get_task_tree("r")
        assert tree["task"].id == "r"
        assert tree["children"][0]["task"].id == "r.1"
        assert tree["children"][0]["children"][0]["task"].id == "r.1.1"
        shallow = await db.get_task_tree("r", max_depth=1)
        assert shallow["children"][0]["children"] == []

    async def test_children_filters(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.READY)
        await mktask(db, "r.2", status=TaskStatus.COMPLETED)
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.2", "r", "parent-child")
        assert [t.id for t in await db.get_children("r", status="READY")] == ["r.1"]
        assert [t.id for t in await db.get_children("r", limit=1, offset=1)] == ["r.2"]
        summary = await db.get_children_summary("r")
        assert summary == {"total": 2, "done": 1, "ready": 1, "blocked": 0, "in_progress": 0}
        assert await db.get_children_summary("r.1") is None

    async def test_progress_extras(self, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        for c in ("r.1", "r.2", "r.3"):
            await mktask(db, c, status=TaskStatus.READY)
            await db.add_dependency(c, "r", "parent-child")
        await db.add_dependency("r.3", "r.1", "blocks")
        p = await db.get_group_progress("r")
        assert p["waves"] == [["r.1", "r.2"], ["r.3"]]
        assert p["max_parallelism"] == 2
        assert p["depth"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/perf/test_hierarchy_statements.py tests/test_hierarchy_queries.py::TestReads -v`
Expected: FAIL — the old recursive `get_task_tree` issues `1 + N` statements; `get_children`/`get_children_summary` do not exist. (Create `tests/perf/__init__.py` if pytest needs it — check whether other test dirs have one.)

- [ ] **Step 3: Implement the reads** (append to `HierarchyQueryMixin`)

```python
    async def get_children(
        self,
        parent_id: str,
        *,
        recursive: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Direct (or recursive) children, ordered by depth then id."""
        if recursive:
            cte = self._descendant_cte(parent_id)
            stmt = (
                select(tasks, cte.c.depth)
                .join(cte, cte.c.id == tasks.c.id)
                .where(cte.c.id != parent_id)
                .order_by(cte.c.depth, tasks.c.id)
            )
        else:
            stmt = select(tasks).where(tasks.c.parent_task_id == parent_id).order_by(tasks.c.id)
        if status:
            stmt = stmt.where(tasks.c.status == status)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_children_summary(self, task_id: str) -> dict | None:
        """One aggregate over the direct children; ``None`` when there are none."""
        from sqlalchemy import case, func

        s = tasks.c.status
        stmt = select(
            func.count().label("total"),
            func.sum(case((s == TaskStatus.COMPLETED.value, 1), else_=0)).label("done"),
            func.sum(
                case(
                    (and_(s == TaskStatus.READY.value, tasks.c.is_blocked == 0), 1), else_=0
                )
            ).label("ready"),
            func.sum(case((tasks.c.is_blocked == 1, 1), else_=0)).label("blocked"),
            func.sum(
                case(
                    (s.in_((TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)), 1),
                    else_=0,
                )
            ).label("in_progress"),
        ).where(tasks.c.parent_task_id == task_id)
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        if not row or not row["total"]:
            return None
        return {k: int(row[k] or 0) for k in ("total", "done", "ready", "blocked", "in_progress")}

    async def get_task_tree(self, root_task_id: str, *, max_depth: int = 4) -> dict | None:
        """Nested ``{"task", "children"}`` from one recursive CTE (spec §8)."""
        cte = self._descendant_cte(root_task_id)
        stmt = (
            select(tasks, cte.c.depth)
            .join(cte, cte.c.id == tasks.c.id)
            .where(cte.c.depth <= max_depth + 1)
            .order_by(cte.c.depth, tasks.c.id)
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        if not rows:
            return None
        nodes: dict[str, dict] = {}
        root: dict | None = None
        for r in rows:
            task = self._row_to_task(r)
            node = {"task": task, "children": []}
            nodes[task.id] = node
            if task.id == root_task_id:
                root = node
            elif task.parent_task_id in nodes:
                nodes[task.parent_task_id]["children"].append(node)
        return root
```

Delete `TaskQueryMixin.get_task_tree` (lines 693-706). In `get_group_progress`, after computing `waves`, add to the returned dict:

```python
            "max_parallelism": max((len(w) for w in waves), default=0),
            "depth": len(waves),
```

- [ ] **Step 4: Surface in `task_show` and `get_task`**

In `_cmd_task_show` (`surface_commands.py`), where the result dict is assembled, add:

```python
        parent = None
        if task.parent_task_id:
            p = await self.db.get_task(task.parent_task_id)
            if p:
                parent = {"id": p.id, "title": p.title, "status": p.status.value}
        result["parent"] = parent
        result["children"] = await self.db.get_children_summary(task.id)
```

In `_cmd_get_task` (`task_commands.py:1386`), after the `subtasks` block: `info["children"] = await self.db.get_children_summary(task.id)`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/perf/test_hierarchy_statements.py tests/test_hierarchy_queries.py tests/test_task_commands*.py tests/test_api_graph.py -v -n auto`
Expected: PASS. `_cmd_get_task_tree` (`task_commands.py:743`) already passes `max_depth` to the formatter; now also pass it to `self.db.get_task_tree(task_id, max_depth=max_depth)`.

- [ ] **Step 6: Commit**

```bash
git add src/database/queries/hierarchy_queries.py src/database/queries/task_queries.py src/commands/surface_commands.py src/commands/task_commands.py tests/perf tests/test_hierarchy_queries.py
git commit -m "feat(hierarchy): CTE tree, get_children, children summary, progress extras

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Graph creator — dotted ids, `parent_id`, container flag

**Files:**
- Modify: `src/task_graph/creator.py:57-70` (`assign_child_ids`), `:142-200` (`build_plan`), `:251-280` (`write_plan`), `:281-300` (`build_report`), `:310-340` (`create_graph`)
- Modify: `src/commands/task_commands.py:1237-1318` (`_cmd_create_task_graph`)
- Modify: `src/cli/tasks.py:82` (`_create_task_graph` — add `--parent`)
- Test: `tests/test_hierarchy_graph_creator.py`

**Interfaces:**
- Produces:
  - `build_plan(db, graph, *, project_id, parent_id: str | None = None) -> GraphPlan`. Without `parent_id`: a new container row with `next_child_ordinal = len(nodes) + 1`, node ids `<container>.1..N` in document order — no DB round trips for ids. With `parent_id`: `plan.parent_id = parent_id`, `plan.parent_row = None`, node ids are `provisional` (`<parent>.?`) until `write_plan` reserves them; `GraphPlan.provisional: bool`.
  - `write_plan(db, plan)` — one transaction: inserts the container (if new) with the container flag, reserves ordinals when `provisional`, rewrites ids in every row, inserts nodes, and for each node calls `set_parent(node_id, container_id, conn=conn)` (which writes edge + pointer + recompute + settle), then inserts the remaining edges/context/criteria/labels.
  - `build_report` includes `"provisional": bool` and, when provisional, ids rendered as `<parent>.?`.
  - `_cmd_create_task_graph` accepts `parent_id`; validates it exists, same project, not `COMPLETED`, structural depth allows `+1` (`hierarchy.depth` otherwise).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_graph_creator.py
"""Dotted ids and --parent in the graph creator — spec §6."""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import Project, Task, TaskStatus
from src.task_graph import parse_graph
from src.task_graph.creator import build_plan, write_plan

PROJECT_ID = "proj"

GRAPH = {
    "version": 1,
    "parent": {"title": "Epic"},
    "nodes": [
        {"key": "a", "title": "A"},
        {"key": "b", "title": "B", "needs": [{"on": "a"}]},
    ],
}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


class TestNewContainer:
    async def test_dotted_ids_known_at_plan_time(self, db):
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        assert plan.provisional is False
        assert plan.task_ids == [f"{plan.parent_id}.1", f"{plan.parent_id}.2"]
        assert plan.parent_row["next_child_ordinal"] == 3

    async def test_write_links_children_and_marks_container(self, db):
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
        await write_plan(db, plan)
        a, b = plan.task_ids
        assert (await db.get_task(a)).parent_task_id == plan.parent_id
        assert (plan.parent_id, "parent-child") in await db.get_typed_dependencies(b)
        assert (a, "blocks") in await db.get_typed_dependencies(b)
        async with db._engine.begin() as conn:
            assert await db.is_container(plan.parent_id, conn=conn)


class TestExistingParent:
    async def test_provisional_ids_then_reserved_on_write(self, db):
        await db.create_task(Task(id="epic", project_id=PROJECT_ID, title="e", description="e",
                                  status=TaskStatus.IN_PROGRESS))
        plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID, parent_id="epic")
        assert plan.provisional is True
        assert plan.task_ids == ["epic.?", "epic.?"]
        assert plan.parent_row is None
        await write_plan(db, plan)
        assert plan.task_ids == ["epic.1", "epic.2"]
        assert (await db.get_task("epic.2")).parent_task_id == "epic"
        assert ("epic.1", "blocks") in await db.get_typed_dependencies("epic.2")

    async def test_dry_run_reserves_nothing(self, db):
        await db.create_task(Task(id="epic", project_id=PROJECT_ID, title="e", description="e",
                                  status=TaskStatus.IN_PROGRESS))
        await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID, parent_id="epic")
        assert (await db.get_task("epic")).parent_task_id is None
        async with db._engine.begin() as conn:
            from src.task_names import reserve_child_ordinal

            assert await reserve_child_ordinal(conn, "epic") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_graph_creator.py -v`
Expected: FAIL — `build_plan() got an unexpected keyword argument 'parent_id'`; ids are flat slugs.

- [ ] **Step 3: Rewrite id assignment and the plan**

Replace `assign_child_ids` and edit `GraphPlan`/`build_plan`/`write_plan`:

```python
PROVISIONAL_SUFFIX = ".?"


def assign_child_ids(parent_id: str, keys: list[str], *, provisional: bool) -> dict[str, str]:
    """Dotted ids for every key (spec §6).

    A *new* container's children are numbered ``1..N`` in document order with
    no round trip.  Under an *existing* parent the ordinals are reserved in
    ``write_plan``'s transaction, so the plan shows ``<parent>.?``.
    """
    if provisional:
        return {key: f"{parent_id}{PROVISIONAL_SUFFIX}" for key in keys}
    return {key: f"{parent_id}.{i + 1}" for i, key in enumerate(keys)}
```

`GraphPlan` gains `provisional: bool = False` and `parent_row: dict | None`. In `build_plan`:

```python
async def build_plan(db, graph, *, project_id: str, parent_id: str | None = None) -> GraphPlan:
    now = time.time()
    provisional = parent_id is not None
    container_id = parent_id or await generate_task_id(db)
    ids = assign_child_ids(container_id, graph.node_keys(), provisional=provisional)
    parent = graph.parent
    parent_row = None
    if not provisional:
        parent_title = (parent.title if parent else "") or "Task graph"
        parent_row = {
            "id": container_id,
            ... (existing fields unchanged) ...
            "next_child_ordinal": len(graph.nodes) + 1,
            "created_at": now,
            "updated_at": now,
        }
    plan = GraphPlan(parent_id=container_id, parent_row=parent_row, ids=ids, provisional=provisional)
```

Node rows are built with `"parent_task_id": None` (set_parent writes it). Keep the `parent` label rows only when `parent_row` is not None.

`write_plan` becomes:

```python
async def write_plan(db, plan: GraphPlan) -> None:
    async with db._engine.begin() as conn:
        if plan.parent_row is not None:
            await _insert_task(conn, plan.parent_row)
            await db.mark_container(plan.parent_id, conn=conn)
        if plan.provisional:
            real: dict[str, str] = {}
            for key in plan.ids:
                real[key] = f"{plan.parent_id}.{await reserve_child_ordinal(conn, plan.parent_id)}"
            _rewrite_ids(plan, real)
        for row in plan.node_rows:
            await _insert_task(conn, row)
        for row in plan.node_rows:
            await db.set_parent(row["id"], plan.parent_id, conn=conn)
        if plan.dependency_rows:
            await conn.execute(insert(task_dependencies), plan.dependency_rows)
        ... (context / criteria / labels unchanged) ...


def _rewrite_ids(plan: GraphPlan, real: dict[str, str]) -> None:
    """Replace provisional ids with reserved ones in every row of *plan*.

    ``build_plan`` stamps each row with the graph key(s) it came from
    (``_key``, ``_task_key``, ``_dep_key``) so this is a lookup, never a
    reverse search over ids.
    """
    plan.ids = dict(real)
    for row in plan.node_rows:
        row["id"] = real[row["_key"]]
    for row in plan.dependency_rows:
        row["task_id"] = real.get(row.get("_task_key"), row["task_id"])
        row["depends_on_task_id"] = real.get(row.get("_dep_key"), row["depends_on_task_id"])
    for coll in (plan.context_rows, plan.criteria_rows, plan.label_rows):
        for row in coll:
            row["task_id"] = real.get(row.get("_key"), row["task_id"])
```

and have `build_plan` stamp `_key` / `_task_key` / `_dep_key` (the graph key, or `None` for an edge onto an existing task id) on every row it creates. `_insert_task` and the bulk inserts must strip keys starting with `_` first: `{k: v for k, v in row.items() if not k.startswith("_")}`.

`reserve_child_ordinal` import: `from src.task_names import generate_task_id, reserve_child_ordinal`. Delete `PARENT_STATUS`'s docstring reference to `_check_plan_parent_completion` (Task 10 removes it) — the container now settles event-driven.

- [ ] **Step 4: Command and CLI**

`_cmd_create_task_graph`: read `parent_id = args.get("parent_id")`; if set, validate:

```python
        if parent_id:
            parent = await self.db.get_task(parent_id)
            if parent is None:
                return {"error": f"Parent task '{parent_id}' not found", "code": "hierarchy.not_found"}
            if parent.project_id != project_id:
                return {"error": "parent is in another project", "code": "hierarchy.cross_project"}
            if parent.status == TaskStatus.COMPLETED:
                return {"error": "parent is COMPLETED", "code": "hierarchy.container_closed"}
            async with self.db._engine.begin() as conn:
                depth = await self.db.structural_depth(parent_id, conn=conn)
            if depth + 1 > MAX_STRUCTURAL_DEPTH:
                return {"error": f"parent at structural depth {depth}; cap is {MAX_STRUCTURAL_DEPTH}",
                        "code": "hierarchy.depth"}
```

and pass `parent_id=parent_id` to `create_graph(...)` → `build_plan(...)`. `build_report` adds `"provisional": plan.provisional`.

`src/cli/tasks.py` `task_create`: add `@click.option("--parent", "parent_id", default=None, help="Create under this container (single task or graph)")`; pass `params["parent_id"] = parent_id` for single tasks and thread it through `_create_task_graph(..., parent_id=parent_id)` → `args["parent_id"]`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_hierarchy_graph_creator.py tests/test_task_graph.py tests/test_create_task_graph_command.py tests/test_cli*.py -v -n auto`
Expected: PASS. Update `tests/test_task_graph.py` fakes if they assert flat ids (they should now expect `<parent>.N`).

- [ ] **Step 6: Commit**

```bash
git add src/task_graph/creator.py src/commands/task_commands.py src/cli/tasks.py tests/test_hierarchy_graph_creator.py tests/test_task_graph.py
git commit -m "feat(task-graph): dotted child ids, --parent onto an existing container

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Cascade — delete the per-tick scan, add the settlement listener and backstop

**Files:**
- Modify: `src/orchestrator/monitoring.py:303-347` (delete `_check_plan_parent_completion`; add `_sweep_container_completion`, `_on_containers_settled`)
- Modify: `src/orchestrator/core.py:1281-1345` (`initialize` — register the listener), `:2435-2445` (cascade call site), `:20-30` (docstring cascade list)
- Modify: `src/config.py:1244-1275` (`WorkGraphConfig.container_sweep_interval_seconds`)
- Modify: `src/database/queries/hierarchy_queries.py` (add `settle_candidates`)
- Test: `tests/test_hierarchy_settlement.py` (orchestrator cases)

**Interfaces:**
- Produces:
  - `WorkGraphConfig.container_sweep_interval_seconds: int = 60` (validate `>= 0`; `0` disables).
  - `async settle_candidates(self) -> list[str]` — one aggregate statement returning every container matching the §7 predicate (no seeds).
  - `Orchestrator._on_containers_settled(ids: list[str])` — for each id: `_emit_task_event("task.completed", task)`, `_emit_text_notify("**Container completed:** …")`, `write_task_summary`, `_check_workflow_stage_completion(task)` — everything the deleted scan used to do.
  - `_sweep_container_completion()` — throttled by the interval; `settle_candidates()` then `settle_containers` in one transaction; logs each hit at WARNING as `container settlement backstop hit` (it should find nothing).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_hierarchy_settlement.py`)

```python
from unittest.mock import AsyncMock, MagicMock

from src.config import AppConfig, DiscordConfig
from src.orchestrator import Orchestrator


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def orch(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    o.bus = MagicMock()
    o.bus.emit = AsyncMock()
    o._emit_text_notify = AsyncMock()
    o._check_workflow_stage_completion = AsyncMock()
    o.register_settlement_listener()
    return o


class TestOrchestratorSettlement:
    async def test_listener_emits_task_completed_and_notifies(self, orch, db):
        kids = await family(db, n=1)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert "task.completed" in emitted
        orch._emit_text_notify.assert_awaited()
        orch._check_workflow_stage_completion.assert_awaited()

    async def test_no_per_tick_scan_method_remains(self, orch):
        assert not hasattr(orch, "_check_plan_parent_completion")

    async def test_backstop_sweep_settles_and_warns(self, orch, db, caplog):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.COMPLETED)
        # Bypass the event path: create the edge with a raw insert so nothing settled.
        from sqlalchemy import insert

        from src.database.tables import task_dependencies

        async with db._engine.begin() as conn:
            await conn.execute(insert(task_dependencies).values(
                task_id="c", depends_on_task_id="p", dep_type="parent-child"))
            await db.mark_container("p", conn=conn)
            from sqlalchemy import update

            from src.database.tables import tasks

            await conn.execute(update(tasks).where(tasks.c.id == "c").values(parent_task_id="p"))
        orch._last_container_sweep = 0.0
        with caplog.at_level("WARNING"):
            await orch._sweep_container_completion()
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert "backstop" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_settlement.py::TestOrchestratorSettlement -v`
Expected: FAIL — `register_settlement_listener` missing; `_check_plan_parent_completion` still exists.

- [ ] **Step 3: Config**

In `WorkGraphConfig` add `container_sweep_interval_seconds: int = 60` with docstring line "Backstop cadence for container settlement (swarm-work-model §7); ``0`` disables." and in `validate()`:

```python
        if self.container_sweep_interval_seconds < 0:
            errors.append(ConfigError("work_graph", "container_sweep_interval_seconds", "must be >= 0"))
```

Add the key to `src/config_editor.py`'s schema for the `work_graph` section (grep `gate_sweep_interval_seconds` there and mirror it).

- [ ] **Step 4: Query helper** (append to `HierarchyQueryMixin`)

```python
    async def settle_candidates(self) -> list[str]:
        """Every container the §7 predicate would settle right now (backstop)."""
        from src.database.tables import sessions

        child = tasks.alias("child")
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                exists(select(literal(1)).where(and_(
                    task_metadata.c.task_id == tasks.c.id,
                    task_metadata.c.key == CONTAINER_KEY,
                    task_metadata.c.value == CONTAINER_VALUE,
                ))),
                ~exists(select(literal(1)).where(and_(
                    sessions.c.task_id == tasks.c.id,
                    sessions.c.state.in_(LIVE_SESSION_STATES),
                ))),
                ~exists(select(literal(1)).where(and_(
                    child.c.parent_task_id == tasks.c.id,
                    child.c.status != TaskStatus.COMPLETED.value,
                ))),
            )
        )
        async with self._engine.begin() as conn:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]
```

- [ ] **Step 5: Orchestrator**

In `monitoring.py`, delete `_check_plan_parent_completion` entirely and add:

```python
    def register_settlement_listener(self) -> None:
        """Wire ``db.transition_task``'s post-commit settlement callback to us."""
        self.db.set_settlement_listener(self._on_containers_settled)

    async def _on_containers_settled(self, ids: list[str]) -> None:
        """Post-commit fan-out for containers completed by settlement (spec §7).

        Everything the old per-tick scan did after the transition: bus event,
        operator notification, vault summary, workflow-stage check.
        """
        for cid in ids:
            task = await self.db.get_task(cid)
            if task is None:
                continue
            try:
                await self._emit_task_event("task.completed", task)
            except Exception:
                logger.exception("task.completed emit failed for container %s", cid)
            try:
                result = await self.db.get_task_result(cid)
                write_task_summary(self.config.vault_root, task, result)
            except Exception as e:
                logger.warning("Failed to write task summary for %s: %s", cid, e)
            await self._emit_text_notify(
                f"**Container completed:** `{task.id}` — {task.title} (all children finished).",
                project_id=task.project_id,
            )
            await self._check_workflow_stage_completion(task)

    _last_container_sweep: float = 0.0

    async def _sweep_container_completion(self) -> None:
        """Low-cadence backstop for the event-driven settlement (spec §7)."""
        interval = self.config.work_graph.container_sweep_interval_seconds
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_container_sweep < interval:
            return
        self._last_container_sweep = now
        candidates = await self.db.settle_candidates()
        if not candidates:
            return
        async with self.db._engine.begin() as conn:
            settled = await self.db.settle_containers(set(candidates), conn=conn)
        for cid in settled:
            logger.warning("container settlement backstop hit: %s (event path missed it)", cid)
        await self.db._notify_settled(settled)
```

In `core.py`: in `initialize()` right after `self.db` is ready (before `adopt_on_start`), call `self.register_settlement_listener()`; at the cascade call site replace `await self._check_plan_parent_completion()` with `await self._sweep_container_completion()`; update the step-list docstring at the top of the file (`_check_plan_parent_completion` → `_sweep_container_completion  # backstop only`). Also `grep -rn "_check_plan_parent_completion" src tests docs` and fix every remaining reference (there is one in `core.py:2082`'s comment and `task_graph/creator.py`'s docstring).

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_hierarchy_settlement.py tests/test_orchestrator.py tests/test_orphan_workflow_scenarios.py tests/test_config.py -v -n auto`
Expected: PASS. Any existing test that called `_check_plan_parent_completion` directly is rewritten to complete the last child through `transition_task` and assert the parent settled.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/monitoring.py src/orchestrator/core.py src/config.py src/config_editor.py src/database/queries/hierarchy_queries.py src/task_graph/creator.py tests/test_hierarchy_settlement.py
git commit -m "feat(orchestrator): event-driven container completion with a backstop sweep

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Commands and surface — `task_children`, `task_progress`, `reparent_task`, tool defs, API models, CLI, formatters, events

**Files:**
- Modify: `src/commands/task_commands.py` (three new `_cmd_*` next to `_cmd_get_task_tree`)
- Modify: `src/tools/definitions.py` (three definitions + `parent_id` on `create_task`, `cascade` on `delete_task`, `abandon_children` on `task_close`, `parent_id` on `create_task_graph`; `_TOOL_CATEGORIES` entries)
- Modify: `src/api/models/task.py` (`TaskChildrenResponse`, `TaskProgressResponse`, `ReparentTaskResponse`; register in `RESPONSE_MODELS`)
- Modify: `src/cli/formatter_registry.py:153-460` (formatters for `task_children`, `task_progress`)
- Modify: `src/event_schemas.py:63-124` (`task.reparented`)
- Modify: `src/api/scope.py:14-31` (`AGENT_COMMAND_SET` += `task_children`, `task_progress`)
- Modify: `src/commands/surface_commands.py:39-62` (`get_schema` — add `hierarchy_error` codes)
- Test: `tests/test_hierarchy_commands.py`

**Interfaces:**
- Produces:
  - `_cmd_task_children(args)` → `{"success": True, "task_id", "children": [task dicts], "count"}`; args `task_id`, `recursive?`, `status?`, `limit?`, `offset?`.
  - `_cmd_task_progress(args)` → `{"success": True, **get_group_progress}`.
  - `_cmd_reparent_task(args)` → `{"success": True, "task_id", "old_parent", "new_parent"}`; args `task_id`, `parent_id | None` (`root: true` clears). Errors: `hierarchy.<code>`. Emits `task.reparented {task_id, project_id, title, old_parent, new_parent}`.
  - CLI: `aq task children <id> [--recursive] [--status S] [--limit N] [--offset N]`, `aq task progress <id>`, `aq task reparent <id> (--parent <id> | --root)`, `aq task delete <id> [--cascade]` (auto-generated from the tool schemas; the `task_` prefix strips to `children`/`progress`, `reparent_task` strips to `reparent`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_hierarchy_commands.py`)

```python
class TestHierarchyCommands:
    async def test_children_flat_and_recursive(self, handler, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1.1", status=TaskStatus.READY)
        await db.add_dependency("r.1", "r", "parent-child")
        await db.add_dependency("r.1.1", "r.1", "parent-child")
        flat = await handler._cmd_task_children({"task_id": "r"})
        assert [c["id"] for c in flat["children"]] == ["r.1"]
        deep = await handler._cmd_task_children({"task_id": "r", "recursive": True})
        assert [c["id"] for c in deep["children"]] == ["r.1", "r.1.1"]

    async def test_progress(self, handler, db):
        await mktask(db, "r", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "r.1", status=TaskStatus.READY)
        await db.add_dependency("r.1", "r", "parent-child")
        res = await handler._cmd_task_progress({"task_id": "r"})
        assert res["success"] is True
        assert res["total"] == 1 and res["ready"] == 1 and res["max_parallelism"] == 1

    async def test_reparent_and_root(self, handler, db):
        await mktask(db, "p1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p1", "parent-child")
        res = await handler._cmd_reparent_task({"task_id": "c", "parent_id": "p2"})
        assert res["success"] is True and res["old_parent"] == "p1"
        assert (await db.get_task("c")).parent_task_id == "p2"
        res = await handler._cmd_reparent_task({"task_id": "c", "root": True})
        assert (await db.get_task("c")).parent_task_id is None

    async def test_reparent_error_codes(self, handler, db):
        await mktask(db, "a", status=TaskStatus.IN_PROGRESS)
        res = await handler._cmd_reparent_task({"task_id": "a", "parent_id": "a"})
        assert res["code"] == "hierarchy.self_parent"

    async def test_schema_lists_hierarchy_codes(self, handler):
        res = await handler._cmd_get_schema({})
        assert "container_closed" in res["enums"]["hierarchy_error"]

    async def test_agent_scope_includes_reads(self):
        from src.api.scope import AGENT_COMMAND_SET

        assert {"task_children", "task_progress"} <= AGENT_COMMAND_SET
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_commands.py::TestHierarchyCommands -v`
Expected: FAIL — `AttributeError: _cmd_task_children`.

- [ ] **Step 3: Commands** (add after `_cmd_get_task_tree` in `task_commands.py`)

```python
    async def _cmd_task_children(self, args: dict) -> dict:
        """Direct or recursive children of a task.  Backs ``aq task children``."""
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        if await self.db.get_task(task_id) is None:
            return {"error": f"Task '{task_id}' not found"}
        children = await self.db.get_children(
            task_id,
            recursive=bool(args.get("recursive", False)),
            status=args.get("status"),
            limit=args.get("limit"),
            offset=int(args.get("offset") or 0),
        )
        return {
            "success": True,
            "task_id": task_id,
            "count": len(children),
            "children": [self._task_to_dict(t) for t in children],
        }

    async def _cmd_task_progress(self, args: dict) -> dict:
        """Computed group progress (counts, waves, max parallelism).  Backs ``aq task progress``."""
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        if await self.db.get_task(task_id) is None:
            return {"error": f"Task '{task_id}' not found"}
        progress = await self.db.get_group_progress(task_id)
        return {"success": True, **progress}

    async def _cmd_reparent_task(self, args: dict) -> dict:
        """Move a task under another container, or to root.  Backs ``aq task reparent``."""
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        if bool(args.get("root")) == bool(args.get("parent_id")):
            return {"error": "exactly one of parent_id or root is required"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"error": f"Task '{task_id}' not found"}
        new_parent = None if args.get("root") else args["parent_id"]
        old_parent = task.parent_task_id
        try:
            async with self.db._engine.begin() as conn:
                flipped, settled = await self.db.set_parent(task_id, new_parent, conn=conn)
        except HierarchyError as exc:
            return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}
        await self.db.log_blocked_flips(flipped)
        await self.db._notify_settled(settled)
        try:
            await self.orchestrator._emit_task_event(
                "task.reparented", task, old_parent=old_parent or "", new_parent=new_parent or ""
            )
        except AttributeError:
            pass
        return {"success": True, "task_id": task_id, "old_parent": old_parent, "new_parent": new_parent}
```

`set_parent` returns `(flipped, settled)` (Task 4); a reparent that empties the old container settles it inside the transaction, and the settled ids are handed to the post-commit listener here exactly as `transition_task` does.

- [ ] **Step 4: Events, scope, schema**

`event_schemas.py` `_TASK_SCHEMAS`: add

```python
    "task.reparented": {
        "required": ["task_id", "project_id", "title"],
        "optional": ["old_parent", "new_parent"],
    },
```

`api/scope.py`: add `"task_children"`, `"task_progress"` to `AGENT_COMMAND_SET`.

`_cmd_get_schema`: add `"hierarchy_error": ["not_found", "cross_project", "cycle", "depth", "self_parent", "container_closed", "has_children", "open_children", "open_descendants", "live_descendants"]`.

- [ ] **Step 5: Tool definitions, API models, formatters**

`src/tools/definitions.py` — add three entries to `_ALL_TOOL_DEFINITIONS` (mirror `get_task_tree`'s shape at line ~604):

```python
    {
        "name": "task_children",
        "description": "List the children of a task (direct, or the whole subtree with recursive=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
                "status": {"type": "string", "description": "Filter by status"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_progress",
        "description": "Computed progress for a container: counts, Kahn waves, max parallelism.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "reparent_task",
        "description": "Move a task under another container (parent_id) or to the root (root=true). Ids never change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "parent_id": {"type": "string"},
                "root": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    },
```

Add `"task_children": "task", "task_progress": "task", "reparent_task": "task"` to `_TOOL_CATEGORIES`. On the existing `create_task` definition add `"parent_id": {"type": "string", "description": "Create as a child of this container; the id becomes <parent>.<n>"}`; on `delete_task` add `"cascade": {"type": "boolean", "default": False}`; on `task_close` add `"abandon_children": {"type": "boolean", "default": False}`; on `create_task_graph` add `"parent_id": {"type": "string"}`.

`src/api/models/task.py`:

```python
class TaskChildrenResponse(BaseModel):
    success: bool
    task_id: str
    count: int
    children: list[TaskDict]


class TaskProgressResponse(BaseModel):
    success: bool
    parent_id: str
    total: int
    done: int
    ready: int
    blocked: int
    in_progress: int
    waves: list[list[str]]
    max_parallelism: int
    depth: int


class ReparentTaskResponse(BaseModel):
    success: bool
    task_id: str
    old_parent: str | None = None
    new_parent: str | None = None
```

and register them in `RESPONSE_MODELS` under `"task_children"`, `"task_progress"`, `"reparent_task"`. Add `parent: dict | None = None` and `children: dict | None = None` to `TaskDetail`.

`src/cli/formatter_registry.py` — in `_register_all()`:

```python
    FORMATTERS["task_children"] = FormatterSpec(
        render=format_task_table,
        extract="children",
        proxy=task_proxy,
        many=True,
        empty_message="No children.",
    )

    def _render_progress(p):
        lines = [
            f"{p.parent_id}: {p.done}/{p.total} done, {p.ready} ready, "
            f"{p.blocked} blocked, {p.in_progress} in progress",
            f"waves: {p.depth}, max parallelism: {p.max_parallelism}",
        ]
        for i, wave in enumerate(p.waves or [], 1):
            lines.append(f"  {i}. " + ", ".join(wave))
        return "\n".join(lines)

    FORMATTERS["task_progress"] = FormatterSpec(render=_render_progress, extract=None, many=False)
```

Then regenerate the clients: `python scripts/generate_openapi.py` (or whatever `dashboard/CLAUDE.md` names — grep `generate:ts-client` in `package.json` and `openapi` in `scripts/`), and commit `openapi.json` + `packages/aq-client` + `packages/aq-ts-client` changes.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_hierarchy_commands.py tests/test_emit_schema_compliance.py tests/test_event_schema_registry_validation.py tests/test_cli*.py tests/test_api*.py -v -n auto`
Expected: PASS. Then `aq task children --help`, `aq task progress --help`, `aq task reparent --help` against a running daemon show the generated options.

- [ ] **Step 7: Commit**

```bash
git add src/commands src/tools/definitions.py src/api/models/task.py src/api/scope.py src/cli/formatter_registry.py src/event_schemas.py src/database/queries/hierarchy_queries.py src/database/queries/dependency_queries.py openapi.json packages tests/test_hierarchy_commands.py
git commit -m "feat(hierarchy): children/progress/reparent commands across CLI, API, MCP

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Canonicalisation, preflight command, migration revision B

**Files:**
- Create: `src/database/hierarchy_migration.py`
- Create: `migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py`
- Modify: `src/database/tables.py:144-160` (add the partial unique index to `task_dependencies`)
- Modify: `src/commands/ops_commands.py` (`_cmd_db_preflight_hierarchy`), `src/tools/definitions.py` (definition, category `system`)
- Test: `tests/test_hierarchy_migration.py`

**Interfaces:**
- Produces (in `hierarchy_migration.py`, **sync** SQLAlchemy Core so Alembic can call it on its own connection):
  - `@dataclass Reject: task_id: str; parent_id: str | None; source: str; reason: str; detail: str`
  - `@dataclass CanonicalPlan: parents: dict[str, str]  # child → canonical parent; rejects: list[Reject]; ordinals: dict[str, int]  # parent prefix → next ordinal`
  - `def canonicalise(conn) -> CanonicalPlan` — steps 1–3 of spec §17 from an immutable snapshot; read-only.
  - `def apply(conn, plan: CanonicalPlan) -> None` — step 4: rewrite edges/pointers, container flags, ordinal backfill.
  - `def persist_rejects(conn, run_id: str, rejects: list[Reject]) -> None`.
  - `def write_report(path: str, run_id: str, plan: CanonicalPlan) -> None` (JSON).
  - Env flag `AQ_MIGRATION_ALLOW_REJECTS=1`.
  - Command `db_preflight_hierarchy` → `{"success": bool, "run_id", "rejects": [...], "report_path", "parents_resolved": int}`; `success` is `False` when rejects exist (the CLI exits non-zero on `success: false` via the envelope's error path).
- `tables.py` gains, in `task_dependencies`: `Index("uq_task_deps_single_parent", "task_id", unique=True, sqlite_where=text("dep_type = 'parent-child'"), postgresql_where=text("dep_type = 'parent-child'"))` (import `text`). Tests that use `Database.initialize()` get the index from `create_all`; existing tests that added two `parent-child` edges to one task must be updated to expect an `IntegrityError` — grep `parent-child` in `tests/` and fix any that do.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_hierarchy_migration.py`)

```python
import time

from sqlalchemy import text as sqltext

from src.database import hierarchy_migration as hm


def _seed(engine, rows, edges):
    """rows: (id, project, parent_col, status); edges: (task, parent)."""
    with engine.begin() as c:
        for pid in {r[1] for r in rows}:
            c.execute(sqltext("INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (:i, :i, 0)"),
                      {"i": pid})
        for tid, proj, parent_col, status in rows:
            c.execute(sqltext(
                "INSERT INTO tasks (id, project_id, parent_task_id, title, description, status, "
                "created_at, updated_at) VALUES (:i, :p, :pc, :i, :i, :s, :t, :t)"),
                {"i": tid, "p": proj, "pc": parent_col, "s": status, "t": time.time()})
        for t, p in edges:
            c.execute(sqltext(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type) "
                "VALUES (:t, :p, 'parent-child')"), {"t": t, "p": p})


@pytest.fixture
def engine_at_a(db_path):
    assert _alembic(db_path, "upgrade", "a1b2c3d4e5f6").returncode == 0
    return create_engine(f"sqlite:///{db_path}")


class TestCanonicalise:
    def test_column_breaks_duplicate_edge_tie(self, engine_at_a):
        _seed(engine_at_a,
              [("p1", "x", None, "IN_PROGRESS"), ("p2", "x", None, "IN_PROGRESS"), ("c", "x", "p2", "READY")],
              [("c", "p1"), ("c", "p2")])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert plan.parents["c"] == "p2"
        assert [r.reason for r in plan.rejects] == ["duplicate"]
        assert plan.rejects[0].parent_id == "p1"

    def test_column_only_becomes_edge(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "x", "p", "READY")], [])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert plan.parents == {"c": "p"}
        assert plan.rejects == []

    def test_cross_project_parent_is_rejected(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        assert "c" not in plan.parents
        assert plan.rejects[0].reason == "cross_project"

    def test_cycle_and_depth_rejected(self, engine_at_a):
        _seed(engine_at_a,
              [("a", "x", None, "IN_PROGRESS"), ("b", "x", None, "IN_PROGRESS"),
               ("d1", "x", None, "IN_PROGRESS"), ("d2", "x", None, "IN_PROGRESS"),
               ("d3", "x", None, "IN_PROGRESS"), ("d4", "x", None, "READY")],
              [("a", "b"), ("b", "a"), ("d2", "d1"), ("d3", "d2"), ("d4", "d3")])
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
        reasons = {(r.task_id, r.reason) for r in plan.rejects}
        assert ("d4", "depth") in reasons
        assert any(t in ("a", "b") and r == "cycle" for t, r in reasons)

    def test_ordinals_backfill_by_id_prefix_across_archive(self, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("p.3", "x", None, "READY")], [])
        with engine_at_a.begin() as c:
            c.execute(sqltext(
                "INSERT INTO archived_tasks (id, project_id, title, description, status, "
                "created_at, updated_at, archived_at) VALUES ('p.7', 'x', 'a', 'a', 'COMPLETED', 0, 0, 0)"))
        with engine_at_a.begin() as conn:
            plan = hm.canonicalise(conn)
            hm.apply(conn, plan)
        with engine_at_a.begin() as conn:
            n = conn.execute(sqltext("SELECT next_child_ordinal FROM tasks WHERE id='p'")).scalar()
        assert n == 8


class TestRevisionB:
    def test_fails_on_rejects_but_keeps_report(self, db_path, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        res = _alembic(db_path, "upgrade", "b2c3d4e5f6a7")
        assert res.returncode != 0
        with engine_at_a.begin() as conn:
            rows = conn.execute(sqltext("SELECT reason FROM hierarchy_migration_rejects")).fetchall()
        assert rows == [("cross_project",)]
        # Schema unchanged: no unique index yet.
        insp = inspect(engine_at_a)
        assert not any(i["name"] == "uq_task_deps_single_parent"
                       for i in insp.get_indexes("task_dependencies"))

    def test_allow_rejects_env_proceeds(self, db_path, engine_at_a, monkeypatch):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "y", "p", "READY")], [])
        monkeypatch.setenv("AQ_MIGRATION_ALLOW_REJECTS", "1")
        res = _alembic(db_path, "upgrade", "b2c3d4e5f6a7")
        assert res.returncode == 0, res.stderr
        with engine_at_a.begin() as conn:
            assert conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar() is None
        insp = inspect(engine_at_a)
        assert any(i["name"] == "uq_task_deps_single_parent" for i in insp.get_indexes("task_dependencies"))

    def test_clean_data_migrates_and_flags_containers(self, db_path, engine_at_a):
        _seed(engine_at_a, [("p", "x", None, "IN_PROGRESS"), ("c", "x", None, "READY")], [("c", "p")])
        assert _alembic(db_path, "upgrade", "b2c3d4e5f6a7").returncode == 0
        with engine_at_a.begin() as conn:
            assert conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar() == "p"
            assert conn.execute(sqltext(
                "SELECT value FROM task_metadata WHERE task_id='p' AND key='container'")).scalar() == "true"
```

`_alembic` must pass `monkeypatch`'s env through — it builds `env` from `os.environ`, so a `monkeypatch.setenv` before the call is inherited. The `projects` insert uses `INSERT OR IGNORE` (SQLite); the perf/Postgres suite is Plan 2's concern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_migration.py -v`
Expected: FAIL — `ModuleNotFoundError: src.database.hierarchy_migration`.

- [ ] **Step 3: Write the canonicalisation module**

```python
# src/database/hierarchy_migration.py
"""Hierarchy canonicalisation — spec §17 (preflight + revision B).

Sync SQLAlchemy Core on purpose: Alembic hands us a sync connection, and
the preflight command runs the same code through ``run_sync``.  Steps:

1. snapshot column pointers and parent-child edges into memory;
2. choose one canonical parent per task (single edge; else the edge equal
   to the column; else the oldest edge; else the column alone);
3. validate the whole candidate graph (parent exists, same project, no
   cycle, structural depth ≤ 3) and drop offenders into ``rejects``;
4. apply: rewrite edges + pointers, flag containers, backfill ordinals.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import text

MAX_STRUCTURAL_DEPTH = 3
ALLOW_REJECTS_ENV = "AQ_MIGRATION_ALLOW_REJECTS"
_ORDINAL_RE = re.compile(r"^(?P<prefix>.+)\.(?P<n>\d+)$")


@dataclass
class Reject:
    task_id: str
    parent_id: str | None
    source: str  # duplicate_edge | column_only | edge
    reason: str  # duplicate | cross_project | cycle | depth | not_found
    detail: str = ""


@dataclass
class CanonicalPlan:
    parents: dict[str, str] = field(default_factory=dict)
    rejects: list[Reject] = field(default_factory=list)
    ordinals: dict[str, int] = field(default_factory=dict)


def _snapshot(conn):
    tasks = {
        r.id: (r.project_id, r.parent_task_id)
        for r in conn.execute(text("SELECT id, project_id, parent_task_id FROM tasks"))
    }
    edges: dict[str, list[tuple[str, float]]] = {}
    # task_dependencies has no created_at; rowid order is insertion order on
    # SQLite and ctid-ish on Postgres — "oldest" means "first inserted".
    for i, r in enumerate(
        conn.execute(
            text(
                "SELECT task_id, depends_on_task_id FROM task_dependencies "
                "WHERE dep_type = 'parent-child'"
            )
        )
    ):
        edges.setdefault(r.task_id, []).append((r.depends_on_task_id, float(i)))
    archived_ids = [r[0] for r in conn.execute(text("SELECT id FROM archived_tasks"))]
    return tasks, edges, archived_ids


def canonicalise(conn) -> CanonicalPlan:
    tasks, edges, archived_ids = _snapshot(conn)
    plan = CanonicalPlan()
    candidates: dict[str, tuple[str, str]] = {}  # child -> (parent, source)

    for tid, (_proj, col) in tasks.items():
        es = sorted(edges.get(tid, []), key=lambda e: e[1])
        if len(es) == 1:
            candidates[tid] = (es[0][0], "edge")
        elif len(es) > 1:
            chosen = col if col in {p for p, _ in es} else es[0][0]
            candidates[tid] = (chosen, "edge")
            for p, _ in es:
                if p != chosen:
                    plan.rejects.append(Reject(tid, p, "duplicate_edge", "duplicate",
                                               f"kept {chosen}"))
        elif col:
            candidates[tid] = (col, "column_only")

    # Validate: existence, project, cycle, depth.
    parents = {c: p for c, (p, _) in candidates.items()}

    def reject(child, reason, detail=""):
        p, src = candidates[child]
        plan.rejects.append(Reject(child, p, src, reason, detail))
        parents.pop(child, None)

    for child, (p, _src) in list(candidates.items()):
        if p not in tasks:
            reject(child, "not_found")
        elif tasks[p][0] != tasks[child][0]:
            reject(child, "cross_project", f"{tasks[child][0]} vs {tasks[p][0]}")

    # Cycles: walk up from each node; a revisit means a cycle — reject the
    # edge that closes it (every member of the cycle loses its parent).
    for start in list(parents):
        seen = []
        cur = start
        while cur in parents and cur not in seen:
            seen.append(cur)
            cur = parents[cur]
        if cur in seen:
            for member in seen[seen.index(cur):]:
                if member in parents:
                    reject(member, "cycle", " -> ".join(seen[seen.index(cur):] + [cur]))

    def depth(node):
        d = 1
        while node in parents:
            node = parents[node]
            d += 1
        return d

    # Deepest violators first so a single reject fixes a whole chain.
    for child in sorted(parents, key=depth, reverse=True):
        if child in parents and depth(child) > MAX_STRUCTURAL_DEPTH:
            reject(child, "depth", f"structural depth {depth(child)}")

    plan.parents = dict(parents)

    # Ordinals by id prefix across live and archived ids.
    for tid in list(tasks) + archived_ids:
        m = _ORDINAL_RE.match(tid)
        if m:
            prefix, n = m.group("prefix"), int(m.group("n"))
            plan.ordinals[prefix] = max(plan.ordinals.get(prefix, 1), n + 1)
    return plan


def apply(conn, plan: CanonicalPlan) -> None:
    conn.execute(text("DELETE FROM task_dependencies WHERE dep_type = 'parent-child'"))
    conn.execute(text("UPDATE tasks SET parent_task_id = NULL"))
    for child, parent in plan.parents.items():
        conn.execute(
            text(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type) "
                "VALUES (:c, :p, 'parent-child')"
            ),
            {"c": child, "p": parent},
        )
        conn.execute(text("UPDATE tasks SET parent_task_id = :p WHERE id = :c"), {"c": child, "p": parent})
    for parent in set(plan.parents.values()):
        exists = conn.execute(
            text("SELECT 1 FROM task_metadata WHERE task_id = :p AND key = 'container'"), {"p": parent}
        ).fetchone()
        if not exists:
            conn.execute(
                text("INSERT INTO task_metadata (task_id, key, value) VALUES (:p, 'container', 'true')"),
                {"p": parent},
            )
    for prefix, n in plan.ordinals.items():
        conn.execute(
            text("UPDATE tasks SET next_child_ordinal = :n WHERE id = :p AND next_child_ordinal < :n"),
            {"p": prefix, "n": n},
        )


def persist_rejects(conn, run_id: str, rejects: list[Reject]) -> None:
    now = time.time()
    for r in rejects:
        conn.execute(
            text(
                "INSERT INTO hierarchy_migration_rejects "
                "(run_id, task_id, parent_id, source, reason, detail, created_at) "
                "VALUES (:run, :t, :p, :s, :r, :d, :c)"
            ),
            {"run": run_id, "t": r.task_id, "p": r.parent_id, "s": r.source, "r": r.reason,
             "d": r.detail, "c": now},
        )


def write_report(path: str, run_id: str, plan: CanonicalPlan) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "run_id": run_id,
                "parents_resolved": len(plan.parents),
                "rejects": [r.__dict__ for r in plan.rejects],
                "ordinals": plan.ordinals,
            },
            fh,
            indent=2,
        )


def allow_rejects() -> bool:
    return os.environ.get(ALLOW_REJECTS_ENV) == "1"
```

- [ ] **Step 4: Revision B**

```python
# migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py
"""hierarchy canonicalise + single-parent index (revision B)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-28

Runs the preflight on a separate autocommit connection first so the rejects
report survives an abort (spec §17).
"""
import os
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.database import hierarchy_migration as hm

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    run_id = uuid.uuid4().hex[:12]

    # Preflight on its own connection: the report commits even if we abort.
    with bind.engine.connect() as pre:
        with pre.begin():
            plan = hm.canonicalise(pre)
            hm.persist_rejects(pre, run_id, plan.rejects)
    report = os.path.expanduser(f"~/.agent-queue/logs/hierarchy-preflight-{run_id}.json")
    try:
        hm.write_report(report, run_id, plan)
    except OSError:
        pass

    if plan.rejects and not hm.allow_rejects():
        raise RuntimeError(
            f"hierarchy canonicalisation found {len(plan.rejects)} reject(s); "
            f"see hierarchy_migration_rejects run_id={run_id} and {report}. "
            f"Fix the data or set {hm.ALLOW_REJECTS_ENV}=1 to proceed."
        )

    hm.apply(bind, plan)
    with op.batch_alter_table("task_dependencies", schema=None) as b:
        b.create_index(
            "uq_task_deps_single_parent",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("dep_type = 'parent-child'"),
            postgresql_where=sa.text("dep_type = 'parent-child'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("task_dependencies", schema=None) as b:
        b.drop_index("uq_task_deps_single_parent")
```

If `bind.engine.connect()` reuses the in-flight connection on SQLite's `StaticPool` (the preflight commit would then be swallowed by the outer rollback), open a second engine instead: `sa.create_engine(str(bind.engine.url))` — write the test first and see which one the assertion `rows == [("cross_project",)]` needs. Migration tests run through the `alembic` CLI (subprocess), where the pool is the default, so `connect()` is a fresh connection.

- [ ] **Step 5: Preflight command**

In `src/commands/ops_commands.py`:

```python
    async def _cmd_db_preflight_hierarchy(self, args: dict) -> dict:
        """Dry-run hierarchy canonicalisation; commit the rejects report (spec §17)."""
        import os
        import uuid

        from src.database import hierarchy_migration as hm

        run_id = uuid.uuid4().hex[:12]
        holder: dict = {}

        def _run(sync_conn):
            plan = hm.canonicalise(sync_conn)
            hm.persist_rejects(sync_conn, run_id, plan.rejects)
            holder["plan"] = plan

        async with self.db._engine.begin() as conn:
            await conn.run_sync(_run)
        plan = holder["plan"]
        report = os.path.join(
            os.path.expanduser(self.config.data_dir), "logs", f"hierarchy-preflight-{run_id}.json"
        )
        hm.write_report(report, run_id, plan)
        return {
            "success": not plan.rejects,
            "run_id": run_id,
            "parents_resolved": len(plan.parents),
            "rejects": [r.__dict__ for r in plan.rejects],
            "report_path": report,
        }
```

Tool definition `db_preflight_hierarchy` (category `system`, no args) so it surfaces as `aq system db-preflight-hierarchy`; add a hand-crafted alias `aq db preflight hierarchy` only if a `db` group already exists in `src/cli/` (grep `@cli.group("db")`); otherwise document the `system` form in the CLI help.

- [ ] **Step 6: Add the index to `tables.py`**

In `task_dependencies`, after the two composite indexes:

```python
    # Exactly one parent per task (swarm-work-model §4): a partial unique
    # index over parent-child edges only.  Created by revision B after the
    # data is canonicalised.
    Index(
        "uq_task_deps_single_parent",
        "task_id",
        unique=True,
        sqlite_where=text("dep_type = 'parent-child'"),
        postgresql_where=text("dep_type = 'parent-child'"),
    ),
```

(`from sqlalchemy import text` at the top). Run `alembic revision --autogenerate -m check` against a DB at revision B and confirm it generates **no** operations for this index; delete the check revision.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_hierarchy_migration.py tests/test_hierarchy_queries.py tests/test_database.py -v -n auto`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/database/hierarchy_migration.py migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py src/database/tables.py src/commands/ops_commands.py src/tools/definitions.py tests/test_hierarchy_migration.py
git commit -m "feat(migrations): hierarchy canonicalisation, preflight, single-parent index (revision B)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: Doctor checks

**Files:**
- Create: `src/doctor/hierarchy_checks.py`
- Modify: `src/doctor/__init__.py:30-40` (`default_registry` registers them), `src/doctor/models.py:120-125` (`RESERVED_CHECK_IDS`)
- Test: `tests/test_hierarchy_doctor.py`

**Interfaces:**
- Produces `hierarchy_checks() -> list[DoctorCheck]` with ids:
  - `hierarchy.parent_pointer` — column ⇔ edge disagreement; `fix` rewrites the column from the edges (`--fix`).
  - `hierarchy.single_parent` — tasks with > 1 `parent-child` out-edge (should be impossible after revision B; ERROR if any).
  - `hierarchy.depth` — any task with structural depth > 3.
  - `hierarchy.closed_container_children` — `COMPLETED` containers with a non-terminal child (invariant 6).
  - `hierarchy.migration_rejects` — rows in `hierarchy_migration_rejects` (WARN with count and latest `run_id`).
- Consumes: `DoctorContext.db` (the async adapter), `CheckResult`, `Severity`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hierarchy_doctor.py
"""hierarchy.* doctor checks — spec §16."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, text, update

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.database.tables import task_dependencies, tasks
from src.doctor.hierarchy_checks import hierarchy_checks
from src.doctor.models import DoctorContext, Severity
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@pytest.fixture
def ctx(db, tmp_path):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path))
    return DoctorContext(config=cfg, db=db)


def check(cid):
    return next(c for c in hierarchy_checks() if c.id == cid)


async def mktask(db, tid, status=TaskStatus.DEFINED):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status))


class TestParentPointer:
    async def test_ok_when_consistent(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        await db.add_dependency("c", "p", "parent-child")
        res = await check("hierarchy.parent_pointer").run(ctx)
        assert res.severity == Severity.OK

    async def test_detects_and_fixes_drift(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c")
        async with db._engine.begin() as conn:
            await conn.execute(insert(task_dependencies).values(
                task_id="c", depends_on_task_id="p", dep_type="parent-child"))
        res = await check("hierarchy.parent_pointer").run(ctx)
        assert res.severity == Severity.ERROR and res.fixable
        fixed = await check("hierarchy.parent_pointer").fix(ctx)
        assert fixed.fix_applied
        assert (await db.get_task("c")).parent_task_id == "p"


class TestOthers:
    async def test_closed_container_children(self, db, ctx):
        await mktask(db, "p", TaskStatus.IN_PROGRESS)
        await mktask(db, "c", TaskStatus.READY)
        await db.add_dependency("c", "p", "parent-child")
        async with db._engine.begin() as conn:  # bypass the guard on purpose
            await conn.execute(update(tasks).where(tasks.c.id == "p").values(status="COMPLETED"))
        res = await check("hierarchy.closed_container_children").run(ctx)
        assert res.severity == Severity.ERROR and res.data["containers"] == ["p"]

    async def test_migration_rejects_warns(self, db, ctx):
        async with db._engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO hierarchy_migration_rejects (run_id, task_id, parent_id, source, reason, "
                "detail, created_at) VALUES ('r1', 'x', 'y', 'edge', 'cycle', '', 0)"))
        res = await check("hierarchy.migration_rejects").run(ctx)
        assert res.severity == Severity.WARN and res.data["count"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hierarchy_doctor.py -v`
Expected: FAIL — `ModuleNotFoundError: src.doctor.hierarchy_checks`.

- [ ] **Step 3: Write the checks**

```python
# src/doctor/hierarchy_checks.py
"""hierarchy.* doctor checks (spec §16).  Registered by the core registry."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, literal, select, text, update

from src.database.tables import task_dependencies, task_metadata, tasks
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus


async def _rows(ctx: DoctorContext, stmt):
    async with ctx.db._engine.begin() as conn:
        return (await conn.execute(stmt)).fetchall()


async def _check_parent_pointer(ctx: DoctorContext) -> CheckResult:
    pc = task_dependencies.alias()
    edge_parent = (
        select(pc.c.depends_on_task_id)
        .where(and_(pc.c.task_id == tasks.c.id, pc.c.dep_type == "parent-child"))
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(tasks.c.id, tasks.c.parent_task_id, edge_parent.label("edge")).where(
        func.coalesce(tasks.c.parent_task_id, "") != func.coalesce(edge_parent, "")
    )
    bad = await _rows(ctx, stmt)
    if not bad:
        return CheckResult(id="hierarchy.parent_pointer", severity=Severity.OK, detail="column matches edges")
    return CheckResult(
        id="hierarchy.parent_pointer",
        severity=Severity.ERROR,
        detail=f"{len(bad)} task(s) whose parent_task_id disagrees with the parent-child edge",
        fixable=True,
        data={"tasks": [{"id": r[0], "column": r[1], "edge": r[2]} for r in bad[:50]]},
    )


async def _fix_parent_pointer(ctx: DoctorContext) -> CheckResult:
    pc = task_dependencies.alias()
    edge_parent = (
        select(pc.c.depends_on_task_id)
        .where(and_(pc.c.task_id == tasks.c.id, pc.c.dep_type == "parent-child"))
        .limit(1)
        .scalar_subquery()
    )
    async with ctx.db._engine.begin() as conn:
        res = await conn.execute(update(tasks).values(parent_task_id=edge_parent))
    return CheckResult(
        id="hierarchy.parent_pointer",
        severity=Severity.OK,
        detail=f"rewrote parent_task_id from edges ({res.rowcount} row(s) touched)",
        fixable=True,
        fix_applied=True,
    )


async def _check_single_parent(ctx: DoctorContext) -> CheckResult:
    stmt = (
        select(task_dependencies.c.task_id, func.count())
        .where(task_dependencies.c.dep_type == "parent-child")
        .group_by(task_dependencies.c.task_id)
        .having(func.count() > 1)
    )
    bad = await _rows(ctx, stmt)
    sev = Severity.ERROR if bad else Severity.OK
    return CheckResult(id="hierarchy.single_parent", severity=sev,
                       detail=f"{len(bad)} task(s) with more than one parent",
                       data={"tasks": [r[0] for r in bad[:50]]})


async def _check_depth(ctx: DoctorContext) -> CheckResult:
    # Four joins up is depth > 3.
    t1, t2, t3, t4 = (tasks.alias(f"t{i}") for i in range(1, 5))
    stmt = (
        select(t1.c.id)
        .select_from(
            t1.join(t2, t2.c.id == t1.c.parent_task_id)
            .join(t3, t3.c.id == t2.c.parent_task_id)
            .join(t4, t4.c.id == t3.c.parent_task_id)
        )
    )
    bad = await _rows(ctx, stmt)
    return CheckResult(id="hierarchy.depth", severity=Severity.ERROR if bad else Severity.OK,
                       detail=f"{len(bad)} task(s) deeper than 3", data={"tasks": [r[0] for r in bad[:50]]})


async def _check_closed_container_children(ctx: DoctorContext) -> CheckResult:
    child = tasks.alias("child")
    terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
    stmt = select(tasks.c.id).where(
        and_(
            tasks.c.status == TaskStatus.COMPLETED.value,
            exists(select(literal(1)).where(and_(child.c.parent_task_id == tasks.c.id,
                                                 child.c.status.notin_(terminal)))),
        )
    )
    bad = await _rows(ctx, stmt)
    return CheckResult(
        id="hierarchy.closed_container_children",
        severity=Severity.ERROR if bad else Severity.OK,
        detail=f"{len(bad)} COMPLETED container(s) with open children (invariant 6)",
        data={"containers": [r[0] for r in bad[:50]]},
    )


async def _check_migration_rejects(ctx: DoctorContext) -> CheckResult:
    rows = await _rows(ctx, text(
        "SELECT run_id, COUNT(*) FROM hierarchy_migration_rejects GROUP BY run_id ORDER BY run_id DESC"))
    count = sum(int(r[1]) for r in rows)
    if not count:
        return CheckResult(id="hierarchy.migration_rejects", severity=Severity.OK, detail="none")
    return CheckResult(
        id="hierarchy.migration_rejects",
        severity=Severity.WARN,
        detail=f"{count} rejected parent edge(s) from canonicalisation; re-attach with aq task reparent",
        data={"count": count, "latest_run_id": rows[0][0]},
    )


def hierarchy_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="hierarchy.parent_pointer", run=_check_parent_pointer, fix=_fix_parent_pointer),
        DoctorCheck(id="hierarchy.single_parent", run=_check_single_parent),
        DoctorCheck(id="hierarchy.depth", run=_check_depth),
        DoctorCheck(id="hierarchy.closed_container_children", run=_check_closed_container_children),
        DoctorCheck(id="hierarchy.migration_rejects", run=_check_migration_rejects),
    ]
```

Register: in `src/doctor/__init__.py` `default_registry()`, after the builtin loop add `for check in hierarchy_checks(): registry.register(check)` (import from `src.doctor.hierarchy_checks`). Add the five ids to `RESERVED_CHECK_IDS` with owner `"swarm-work-model"`. Check `Severity`'s member names in `src/doctor/models.py` (`OK`/`WARN`/`ERROR`/`INFO`) and match them.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_hierarchy_doctor.py tests/test_doctor*.py -v -n auto`
Expected: PASS; `aq doctor` lists the five checks.

- [ ] **Step 5: Commit**

```bash
git add src/doctor/hierarchy_checks.py src/doctor/__init__.py src/doctor/models.py tests/test_hierarchy_doctor.py
git commit -m "feat(doctor): hierarchy consistency checks

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: Docs, full-suite verification, dev-DB migration

**Files:**
- Modify: `docs/specs/design/work-graph.md:237-241` (§13 — point at the swarm spec Part I; note the container flag, single parent, event-driven settlement)
- Modify: `docs/specs/implementation/work-graph.md:270` (tick "Hierarchical child ids; `get_group_progress` + command")
- Modify: `CLAUDE.md` Quick Reference — add `hierarchy_queries.py` to the Core files line and `hierarchy_migration.py` under Database Migrations
- Modify: `src/skills/aq-tasks/SKILL.md` — document `--parent`, `children`, `progress`, `reparent`, `--cascade`, `--abandon-children`, and that ids never change
- Modify: `docs/specs/database.md` — the new columns and table

- [ ] **Step 1: Update the documents** with the facts above (each is a paragraph; copy the exact command syntax from Task 11's interface block and the schema from Task 1).

- [ ] **Step 2: Full verification**

Run, in order, and paste the summary lines into the PR description:

```bash
ruff check src tests
pytest tests/ -n auto
pytest tests/perf -v
alembic upgrade head            # against a COPY of the dev DB: cp ~/.agent-queue/agent_queue.db /tmp/aq-copy.db first, point AGENT_QUEUE_DATABASE_URL at it
aq system db-preflight-hierarchy --json
aq doctor
```

Expected: ruff clean; suite green; perf budgets hold; preflight `success: true` (or a rejects list you resolve with `aq task reparent` before migrating the real DB); doctor shows the five `hierarchy.*` checks OK.

- [ ] **Step 3: Commit**

```bash
git add docs CLAUDE.md src/skills/aq-tasks/SKILL.md
git commit -m "docs(hierarchy): specs, skill, and quick reference for the hierarchy model

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage (Part I + the schema/migration/tests the plan owns):**

| Spec item | Task |
|---|---|
| §4 schema (`next_child_ordinal`, `created_by_*`, rejects table, partial unique index) | 1, 2, 12 |
| §4 two depths, invariants 1–6 | 3 (naming), 4 (structural, single parent via `set_parent`), 12 (index), 13 (doctor) |
| §5 `set_parent` single writer, `conn` mandatory, callers list | 4 (create/add/remove), 7 (delete/archive), 9 (graph creator), 11 (reparent) |
| §6 ids from counter; graph creator new-container / `--parent` / dry-run provisional; depth rejected before write | 3, 9 |
| §7 `_apply_transition` split; container flag; settlement fixpoint; backstop; close semantics (open children, `--abandon-children` atomic with live-descendant check, `container_closed`, settle on removal) | 5, 6, 10 |
| §7 delete refuse/cascade; archive subtree-atomic; sweep roots only | 7 |
| §8 reads (CTE tree, children, summary, progress extras) | 8 |
| §9 events `task.reparented`; `AgentState.RETIRED` | 11, 1 |
| §14 surface rows for `create --parent`, `children`, `progress`, `reparent`, `delete --cascade`, `close --abandon-children`, `db preflight hierarchy`, `aq schema` codes | 9, 11, 12 |
| §15 statement budgets for reads | 8 (`tests/perf`) |
| §16 doctor checks (`hierarchy.*`) | 13 |
| §17 revisions A/B, preflight, `AQ_MIGRATION_ALLOW_REJECTS`, id-prefix ordinal backfill | 2, 12 |

Out of this plan (by design): everything in spec Part II/III — claim, pools, worker loop, `created_by_*` stamping, quota reservation, formulas, `BEGIN IMMEDIATE` engine helper (Plan 2 adds `db.immediate()`; Task 6 falls back to `begin()` until then).

**Type consistency:** `set_parent` returns `tuple[set[str], list[str]]` — Task 4 defines it that way (see the note in Task 11, applied at Task 4 time), `add_dependency`/`remove_dependency`/`create_task_under` unpack it, `_cmd_reparent_task` forwards `settled` to `_notify_settled`. `child_task_id(conn, parent_id)` takes a connection everywhere. `HierarchyError.code` strings match `aq schema`'s `hierarchy_error` list.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-28-swarm-hierarchy.md`. Plans 2 (claim/pools/worker loop/filing) and 3 (formulas) follow the same shape once this one lands; both consume the schema this plan's revision A creates.
