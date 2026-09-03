"""Worktree-execution schema: migration + model/parser round-trip.

Covers worktree-execution implementation spec §3.1–3.6 and §10's
``tests/test_worktree_migration.py`` row.

The DDL itself shipped in the Wave 0 substrate revision
(``93a8a9e48fb8``) rather than a lane-owned one — see that revision's
docstring.  These tests pin the properties the spec cares about:

* the columns and the ``merge_slots`` table exist after ``upgrade head``;
* the partial unique index on ``(base_workspace_id, slot_index)`` really is
  partial (many NULL/NULL rows allowed, one row per populated pair);
* every kind row that existed *before* the revision is backfilled to
  ``exclusive-clone`` so no install changes provisioning strategy on
  upgrade;
* downgrade/re-upgrade is clean and the backfill re-applies;
* pre-existing ``workspaces`` rows are untouched (``slot_index`` NULL).

``tmp_path`` is used rather than ``tempfile.TemporaryDirectory`` because
Windows cannot unlink the SQLite file while the engine still holds it —
the pre-existing failures in ``tests/test_migration_workspaces_v2.py`` are
exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from src.models import (
    KIND_MODE_EXCLUSIVE_CLONE,
    KIND_MODE_WORKTREE,
    WORKSPACE_KIND_MODES,
    MergeSlot,
    Workspace,
    WorkspaceKind,
    WorktreeSentinel,
    worktree_setup_hash,
)

pytestmark = pytest.mark.migration

# The Wave 0 substrate revision that carries the worktree-execution DDL.
SUBSTRATE_REVISION = "93a8a9e48fb8"
# Its parent — the point just before the worktree columns exist.
PRE_SUBSTRATE_REVISION = "e252a41eb210"


def _alembic_config(async_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


def _urls(tmp_path: Path, name: str = "wt.db") -> tuple[Config, str]:
    """(alembic config on the async URL, sync URL for verification)."""
    db_path = tmp_path / name
    return _alembic_config(f"sqlite+aiosqlite:///{db_path}"), f"sqlite:///{db_path}"


def _columns(conn, table: str) -> dict[str, dict]:
    return {
        r[1]: {"type": r[2], "notnull": r[3], "default": r[4]}
        for r in conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    }


# ─────────────────────────────── schema shape ────────────────────────────


def test_upgrade_head_adds_worktree_columns(tmp_path: Path):
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            kinds = _columns(conn, "workspace_kinds")
            assert "mode" in kinds and kinds["mode"]["notnull"] == 1
            assert "worktree_setup" in kinds
            assert kinds["worktree_setup"]["notnull"] == 1

            ws = _columns(conn, "workspaces")
            assert "slot_index" in ws and ws["slot_index"]["notnull"] == 0
            assert "base_workspace_id" in ws
            assert ws["base_workspace_id"]["notnull"] == 0

            merge = _columns(conn, "merge_slots")
            assert set(merge) == {
                "project_id",
                "holder_task_id",
                "acquired_at",
                "expires_at",
                "updated_at",
            }
    finally:
        engine.dispose()


def test_partial_unique_index_is_partial(tmp_path: Path):
    """Many NULL/NULL rows are fine; one row per populated (base, slot)."""
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO projects (id, name, repo_url, status, created_at) "
                    "VALUES ('p1', 'p1', '', 'active', 0)"
                )
            )

            def _ins(wid, path, slot, base):
                conn.execute(
                    sa.text(
                        "INSERT INTO workspaces "
                        "(id, project_id, workspace_path, source_type, kind_id, "
                        " enabled, slot_index, base_workspace_id, created_at) "
                        "VALUES (:i, 'p1', :p, 'clone', 'project-repo', 1, "
                        ":s, :b, 0)"
                    ),
                    {"i": wid, "p": path, "s": slot, "b": base},
                )

            # Three clone rows: all NULL/NULL — the partial index must ignore them.
            _ins("w1", "/r/a", None, None)
            _ins("w2", "/r/b", None, None)
            _ins("w3", "/r/c", None, None)

            # Slots under one base: distinct indices are fine.
            _ins("s0", "/r/a/.aq/worktrees/slot-0", 0, "w1")
            _ins("s1", "/r/a/.aq/worktrees/slot-1", 1, "w1")
            # Same index under a *different* base is also fine.
            _ins("t0", "/r/b/.aq/worktrees/slot-0", 0, "w2")

        with engine.begin() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO workspaces "
                        "(id, project_id, workspace_path, source_type, kind_id, "
                        " enabled, slot_index, base_workspace_id, created_at) "
                        "VALUES ('dup', 'p1', '/r/a/dup', 'clone', "
                        "'project-repo', 1, 0, 'w1', 0)"
                    )
                )
    finally:
        engine.dispose()


# ──────────────────────────── the one data step ──────────────────────────


def test_pre_existing_kinds_are_backfilled_to_exclusive_clone(tmp_path: Path):
    """A row that existed before the revision keeps clone behavior."""
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, PRE_SUBSTRATE_REVISION)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO workspace_kinds "
                    "(project_id, id, description, writable, lockable, "
                    " is_git_repo, auto_attach, created_at, updated_at) "
                    "VALUES ('proj-x', 'custom-repo', '', 1, 1, 1, 0, 0, 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            modes = dict(
                conn.execute(
                    sa.text("SELECT id || '@' || project_id, mode FROM workspace_kinds")
                ).fetchall()
            )
        # Every row present at revision time — the three seeded system kinds
        # and the operator's own — is exclusive-clone.
        assert modes, "expected seeded kinds"
        assert set(modes.values()) == {KIND_MODE_EXCLUSIVE_CLONE}, modes
        assert modes["custom-repo@proj-x"] == KIND_MODE_EXCLUSIVE_CLONE
    finally:
        engine.dispose()


def test_new_rows_get_the_shipped_worktree_default(tmp_path: Path):
    """The column's server_default is 'worktree' — only pre-existing rows
    were backfilled, so a row inserted after the migration opts in."""
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO workspace_kinds "
                    "(project_id, id, description, writable, lockable, "
                    " is_git_repo, auto_attach, created_at, updated_at) "
                    "VALUES ('__system__', 'fresh-repo', '', 1, 1, 1, 0, 0, 0)"
                )
            )
        with engine.connect() as conn:
            mode = conn.execute(
                sa.text(
                    "SELECT mode FROM workspace_kinds WHERE id = 'fresh-repo'"
                )
            ).scalar()
            setup = conn.execute(
                sa.text(
                    "SELECT worktree_setup FROM workspace_kinds "
                    "WHERE id = 'fresh-repo'"
                )
            ).scalar()
        assert mode == KIND_MODE_WORKTREE
        assert json.loads(setup) == []
    finally:
        engine.dispose()


def test_existing_workspaces_untouched_by_the_revision(tmp_path: Path):
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, PRE_SUBSTRATE_REVISION)

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO projects (id, name, repo_url, status, created_at) "
                    "VALUES ('p1', 'p1', '', 'active', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO workspaces "
                    "(id, project_id, workspace_path, source_type, kind_id, "
                    " enabled, created_at) "
                    "VALUES ('w1', 'p1', '/r/a', 'clone', 'project-repo', 1, 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT workspace_path, slot_index, base_workspace_id "
                    "FROM workspaces WHERE id = 'w1'"
                )
            ).fetchone()
        assert row == ("/r/a", None, None)
    finally:
        engine.dispose()


def test_downgrade_then_reupgrade_reapplies_the_backfill(tmp_path: Path):
    cfg, url = _urls(tmp_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, PRE_SUBSTRATE_REVISION)

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            cols = _columns(conn, "workspace_kinds")
            assert "mode" not in cols
            tables = {
                r[0]
                for r in conn.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            assert "merge_slots" not in tables
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            modes = [
                r[0]
                for r in conn.execute(
                    sa.text("SELECT mode FROM workspace_kinds")
                ).fetchall()
            ]
            n_kinds = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM workspace_kinds "
                    "WHERE project_id = '__system__'"
                )
            ).scalar()
        assert n_kinds == 3, "re-upgrade must not duplicate the seeded kinds"
        assert set(modes) == {KIND_MODE_EXCLUSIVE_CLONE}
    finally:
        engine.dispose()


# The single-head guard moved to ``tests/test_migration_single_head.py``.  It needs no
# database, and this module's ``pytestmark = pytest.mark.migration`` kept it out
# of the default selection — which is how ``main`` acquired two heads unnoticed.


# ───────────────────────────── models (§3.5) ─────────────────────────────


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
