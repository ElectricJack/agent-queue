"""add_workspace kind_id parameter + list_workspace_kinds. Spec §10."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.database import Database
from src.models import Project, WorkspaceKind


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test", repo_url=""))

    # Add a project-scoped kind for the kind_id-acceptance tests.
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id="p1",
            id="game-repo",
            description="Game repo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
            created_at=time.time(),
            updated_at=time.time(),
        )
    )

    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    orch.git = MagicMock()
    orch.git.acreate_checkout = AsyncMock()
    orch.git.aget_remote_url = AsyncMock(return_value=None)

    config = MagicMock()
    config.workspace_dir = str(tmp_path / "workspaces")

    handler = CommandHandler(orch, config)
    handler._active_project_id = "p1"

    yield handler, db, tmp_path
    await db.close()


class TestAddWorkspaceKindId:
    async def test_default_is_project_repo(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-default"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
        })
        assert "created" in result, result
        assert result["kind_id"] == "project-repo"
        ws = await db.get_workspace(result["created"])
        assert ws.kind_id == "project-repo"

    async def test_explicit_kind_id_is_persisted(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-game"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
            "kind_id": "game-repo",
        })
        assert "created" in result, result
        assert result["kind_id"] == "game-repo"
        ws = await db.get_workspace(result["created"])
        assert ws.kind_id == "game-repo"

    async def test_unknown_kind_fails(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-bogus"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
            "kind_id": "does-not-exist",
        })
        assert "error" in result
        assert "does-not-exist" in result["error"]


class TestListWorkspaceKinds:
    async def test_lists_system_plus_project(self, setup):
        handler, db, _ = setup
        result = await handler._cmd_list_workspace_kinds({"project_id": "p1"})
        assert result["project_id"] == "p1"
        ids = {k["id"] for k in result["workspace_kinds"]}
        # system kinds (from migration)
        assert {"project-repo", "vault", "readonly-dir"}.issubset(ids)
        # project-scoped kind (from fixture)
        assert "game-repo" in ids

    async def test_marks_scope_correctly(self, setup):
        handler, db, _ = setup
        result = await handler._cmd_list_workspace_kinds({"project_id": "p1"})
        by_id = {k["id"]: k for k in result["workspace_kinds"]}
        assert by_id["project-repo"]["scope"] == "system"
        assert by_id["game-repo"]["scope"] == "project"

    async def test_no_project_returns_system_only(self, setup):
        handler, db, _ = setup
        handler._active_project_id = None  # clear active project
        result = await handler._cmd_list_workspace_kinds({})
        # No project context: only system rows.
        ids = {k["id"] for k in result["workspace_kinds"]}
        assert ids == {"project-repo", "vault", "readonly-dir"}
        # game-repo (project-scoped) should not appear.
        assert "game-repo" not in ids

    async def test_active_project_inherited(self, setup):
        handler, db, _ = setup
        # _active_project_id is "p1" from the fixture; no explicit project_id.
        result = await handler._cmd_list_workspace_kinds({})
        assert result["project_id"] == "p1"
        assert any(k["id"] == "game-repo" for k in result["workspace_kinds"])
