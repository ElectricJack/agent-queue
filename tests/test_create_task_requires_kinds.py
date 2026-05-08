"""create_task accepts requires_kinds. See spec §5.1, §5.4."""

from __future__ import annotations

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
    await db.create_project(Project(id="p1", name="test"))

    # Add a project-scoped kind for the create_task tests.
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

    # Build a minimal orchestrator stub so CommandHandler properties work.
    # The notify path is mocked because we don't have a real event bus.
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()

    config = MagicMock()
    config.workspace_dir = "/tmp/test"

    handler = CommandHandler(orch, config)
    handler._active_project_id = "p1"

    yield handler, db
    await db.close()


class TestRequiresKinds:
    async def test_string_form_creates_requirement_row(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "task with explicit kind",
            "requires_kinds": ["game-repo"],
        })
        assert "created" in result
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert [r.kind_id for r in rows] == ["game-repo"]
        assert rows[0].alias is None

    async def test_dict_form_with_alias(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "task with aliases",
            "requires_kinds": [
                {"kind": "game-repo", "alias": "primary"},
            ],
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert [r.alias for r in rows] == ["primary"]

    async def test_unknown_kind_fails_create(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "task with bogus kind",
            "requires_kinds": ["does-not-exist"],
        })
        assert "error" in result
        assert "does-not-exist" in result["error"]

    async def test_system_kind_resolves_for_any_project(self, setup):
        """A kind defined only at __system__ scope is usable by any project."""
        handler, db = setup
        # 'project-repo' is a system kind seeded by the migration.
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "system kind task",
            "requires_kinds": ["project-repo"],
        })
        assert "created" in result, result
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert [r.kind_id for r in rows] == ["project-repo"]

    async def test_omitted_writes_no_requirement_rows(self, setup):
        """When requires_kinds is omitted, no rows are written.  Defaults
        come from effective_requirements at acquisition time (spec §5.2)."""
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "task without kinds",
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert rows == []

    async def test_dict_missing_kind_fails(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "bad dict",
            "requires_kinds": [{"alias": "primary"}],
        })
        assert "error" in result
        assert "kind" in result["error"]

    async def test_invalid_entry_type_fails(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "bad type",
            "requires_kinds": [42],
        })
        assert "error" in result

    async def test_multiple_of_same_kind_with_aliases(self, setup):
        handler, db = setup
        # Add a second project-scoped kind.
        await db.upsert_workspace_kind(
            WorkspaceKind(
                project_id="p1",
                id="package-foo",
                description="Package",
                writable=True,
                lockable=True,
                is_git_repo=True,
                default_lock_mode="exclusive",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "cross-repo merge",
            "requires_kinds": [
                {"kind": "package-foo", "alias": "primary"},
                {"kind": "package-foo", "alias": "mirror"},
            ],
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert {r.position for r in rows} == {0, 1}
        assert {r.alias for r in rows} == {"primary", "mirror"}

    async def test_result_echoes_normalized_requirements(self, setup):
        handler, db = setup
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "echo test",
            "requires_kinds": [
                "game-repo",
                {"kind": "project-repo", "alias": "main"},
            ],
        })
        assert result["requires_kinds"] == [
            {"kind": "game-repo", "alias": None},
            {"kind": "project-repo", "alias": "main"},
        ]
