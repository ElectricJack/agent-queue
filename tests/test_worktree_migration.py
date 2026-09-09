"""Current PostgreSQL worktree schema and model/parser contracts.

The pre-substrate backfill and downgrade paths were retired with the historical
revision chain. The baseline still promises nullable slot fields, uniqueness
within a base workspace, and the worktree default for newly inserted kinds.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from src.database import Database
from tests.db_fixtures import lease_dsn
from src.models import (
    KIND_MODE_WORKTREE,
    WORKSPACE_KIND_MODES,
    MergeSlot,
    Workspace,
    WorkspaceKind,
    WorktreeSentinel,
    worktree_setup_hash,
)

pytestmark = pytest.mark.migration


@pytest.fixture
async def database():
    db = Database(lease_dsn("worktree-schema"))
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


async def test_upgrade_head_adds_worktree_columns(database):
    async with database._engine.connect() as conn:
        async def columns(table):
            return await conn.run_sync(
                lambda sync: {
                    col["name"]: col for col in sa.inspect(sync).get_columns(table)
                }
            )

        kinds = await columns("workspace_kinds")
        assert kinds["mode"]["nullable"] is False
        assert kinds["worktree_setup"]["nullable"] is False
        ws = await columns("workspaces")
        assert ws["slot_index"]["nullable"] is True
        assert ws["base_workspace_id"]["nullable"] is True
        assert set(await columns("merge_slots")) == {
            "project_id", "holder_task_id", "acquired_at", "expires_at", "updated_at"
        }


async def test_partial_unique_index_is_partial(database):
    """Many NULL pairs are allowed, but each populated (base, slot) is unique."""
    async with database._engine.begin() as conn:
        await conn.execute(sa.text(
            "INSERT INTO projects (id, name, created_at) VALUES ('p1', 'p1', 0)"
        ))
        statement = sa.text(
            "INSERT INTO workspaces "
            "(id, project_id, workspace_path, source_type, kind_id, enabled, "
            "slot_index, base_workspace_id, created_at) "
            "VALUES (:id, 'p1', :path, 'clone', 'project-repo', true, :slot, :base, 0)"
        )
        for wid, slot, base in [
            ("w1", None, None), ("w2", None, None), ("w3", None, None),
            ("s0", 0, "w1"), ("s1", 1, "w1"), ("t0", 0, "w2"),
        ]:
            await conn.execute(statement, {
                "id": wid, "path": f"/r/{wid}", "slot": slot, "base": base,
            })
        with pytest.raises(sa.exc.IntegrityError):
            async with conn.begin_nested():
                await conn.execute(statement, {
                    "id": "duplicate", "path": "/r/duplicate", "slot": 0, "base": "w1",
                })


async def test_new_rows_get_the_shipped_worktree_default(database):
    async with database._engine.begin() as conn:
        await conn.execute(sa.text(
            "INSERT INTO workspace_kinds "
            "(project_id, id, description, writable, lockable, is_git_repo, "
            "auto_attach, created_at, updated_at) "
            "VALUES ('__system__', 'fresh-repo', '', true, true, true, false, 0, 0)"
        ))
        mode, setup = (await conn.execute(sa.text(
            "SELECT mode, worktree_setup FROM workspace_kinds WHERE id = 'fresh-repo'"
        ))).one()
        assert mode == KIND_MODE_WORKTREE
        assert json.loads(setup) == []


def test_workspace_kind_model_defaults():
    k = WorkspaceKind(project_id="__system__", id="project-repo")
    assert k.mode == KIND_MODE_WORKTREE
    assert k.worktree_setup == []
    # Independent per instance — the classic mutable-default trap.
    k.worktree_setup.append("npm ci")
    assert WorkspaceKind(project_id="__system__", id="other").worktree_setup == []


def test_workspace_kind_modes_enumerated():
    assert WORKSPACE_KIND_MODES == {
        "worktree",
        "exclusive-clone",
        "directory-isolated",
    }


def test_workspace_slot_fields_default_to_none():
    from src.models import RepoSourceType

    ws = Workspace(
        id="w1",
        project_id="p1",
        workspace_path="/r/a",
        source_type=RepoSourceType.CLONE,
    )
    assert ws.slot_index is None
    assert ws.base_workspace_id is None
    assert ws.is_slot is False

    slot = Workspace(
        id="s0",
        project_id="p1",
        workspace_path="/r/a/.aq/worktrees/slot-0",
        source_type=RepoSourceType.WORKTREE,
        slot_index=0,
        base_workspace_id="w1",
    )
    assert slot.is_slot is True


def test_merge_slot_lease_semantics():
    free = MergeSlot(project_id="p1")
    assert not free.is_held(now=100.0)

    held = MergeSlot(
        project_id="p1", holder_task_id="tsk-1", acquired_at=0.0, expires_at=200.0
    )
    assert held.is_held(now=100.0)
    assert not held.is_held(now=300.0)

    # A holder with no expiry is held forever — only an explicit release frees it.
    forever = MergeSlot(project_id="p1", holder_task_id="tsk-1")
    assert forever.is_held(now=1e9)


def test_worktree_sentinel_round_trip():
    s = WorktreeSentinel(
        slot="slot-1",
        slot_index=1,
        base_workspace_id="ws-a1b2",
        project_id="atom-claude",
        workspace_id="ws-slot1",
        task_id="tsk-9f3e",
        branch="aq/tsk-9f3e",
        created_at=1755590400.0,
        assigned_at=1755612300.0,
        daemon_epoch="2026-08-19T10:00:00Z",
        setup_hash=worktree_setup_hash(["npm ci"]),
    )
    assert WorktreeSentinel.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_worktree_sentinel_from_dict_is_tolerant():
    s = WorktreeSentinel.from_dict(
        {"slot": "slot-0", "slot_index": 0, "unknown_key": "ignored"}
    )
    assert s.slot == "slot-0"
    assert s.slot_index == 0
    assert s.task_id is None
    assert s.setup_hash == ""


def test_worktree_setup_hash_is_order_sensitive_and_stable():
    a = worktree_setup_hash(["npm ci", "make build"])
    b = worktree_setup_hash(["make build", "npm ci"])
    assert a != b
    assert a == worktree_setup_hash(["npm ci", "make build"])
    assert worktree_setup_hash([]) == worktree_setup_hash(None)
    assert WorkspaceKind(project_id="s", id="k").setup_hash() == worktree_setup_hash([])
