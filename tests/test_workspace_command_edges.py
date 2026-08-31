"""Failure-branch tests for the workspace mutation commands.

``_cmd_add_workspace`` and ``_cmd_edit_workspace`` carry a dense matrix of
refusals — unknown kind, cross-project link collision, clone/init failure,
locked workspace, duplicate realpath, forced relocation.  Wrong state here
points a worker at another project's tree, so each refusal must also leave the
database untouched (test-coverage plan, commands 21–22).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from src.models import Agent, Project, RepoSourceType, Task, Workspace


@pytest.fixture
async def ws_handler(command_handler_factory):
    handler = await command_handler_factory()
    handler.orchestrator.git.acreate_checkout = AsyncMock()
    handler.orchestrator.git.ainit_repo = AsyncMock()
    handler.orchestrator.git.aget_remote_url = AsyncMock(return_value=None)
    await handler.db.create_project(Project(id="p1", name="One", repo_url=""))
    await handler.db.create_project(Project(id="p2", name="Two", repo_url=""))
    return handler


async def _workspace_ids(handler) -> list[str]:
    return sorted(ws.id for ws in await handler.db.list_workspaces())


# ---------------------------------------------------------------------------
# 21: add_workspace refusals
# ---------------------------------------------------------------------------


async def test_add_workspace_rejects_unknown_kind_cross_project_link_and_clone_failure(
    ws_handler, tmp_path
):
    handler = ws_handler
    assert await _workspace_ids(handler) == []

    # Unknown project.
    missing_project = await handler._cmd_add_workspace({"project_id": "ghost"})
    assert missing_project == {"error": "Project 'ghost' not found"}
    assert await _workspace_ids(handler) == []

    # Unknown kind — no project-scoped or system definition.
    unknown_kind = await handler._cmd_add_workspace(
        {"project_id": "p1", "kind_id": "not-a-kind", "source": "link", "path": str(tmp_path)}
    )
    assert "Kind 'not-a-kind' is not defined for project 'p1'" in unknown_kind["error"]
    assert await _workspace_ids(handler) == []

    # Link without a path.
    no_path = await handler._cmd_add_workspace({"project_id": "p1", "source": "link"})
    assert no_path == {"error": "Link workspaces require a 'path' parameter"}
    assert await _workspace_ids(handler) == []

    # Link to a path that is not a directory.
    ghost_dir = tmp_path / "nope"
    bad_path = await handler._cmd_add_workspace(
        {"project_id": "p1", "source": "link", "path": str(ghost_dir)}
    )
    assert bad_path == {
        "error": f"Path '{os.path.realpath(ghost_dir)}' does not exist or is not a directory"
    }
    assert await _workspace_ids(handler) == []

    # Cross-project link collision: p2 already owns this directory.
    shared = tmp_path / "shared"
    shared.mkdir()
    await handler.db.create_workspace(
        Workspace(
            id="ws-p2",
            project_id="p2",
            workspace_path=str(shared),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )
    collision = await handler._cmd_add_workspace(
        {"project_id": "p1", "source": "link", "path": str(shared)}
    )
    assert collision["error"] == (
        f"Path '{os.path.realpath(shared)}' is already a workspace for project 'p2'"
    )
    assert await _workspace_ids(handler) == ["ws-p2"]

    # Clone failure: the git call raises, so no row is written.
    await handler.db.update_project("p1", repo_url="https://example.test/r.git")
    handler.orchestrator.git.acreate_checkout = AsyncMock(side_effect=RuntimeError("no network"))
    clone_failed = await handler._cmd_add_workspace(
        {"project_id": "p1", "source": "clone", "path": str(tmp_path / "clone")}
    )
    assert clone_failed == {"error": "Clone failed: no network"}
    assert await _workspace_ids(handler) == ["ws-p2"]

    # init failure: same contract.
    handler.orchestrator.git.ainit_repo = AsyncMock(side_effect=RuntimeError("bad perms"))
    init_failed = await handler._cmd_add_workspace(
        {"project_id": "p1", "source": "init", "path": str(tmp_path / "init")}
    )
    assert init_failed == {"error": "git init failed: bad perms"}
    assert await _workspace_ids(handler) == ["ws-p2"]

    # Unsupported source strings fail cleanly with the same error contract
    # whether the string is a RepoSourceType this command does not provision
    # (worktree slots are orchestrator-managed, never created here) or not an
    # enum member at all.  Either way no workspace row is written.
    for unsupported in ("worktree", "teleport"):
        refused = await handler._cmd_add_workspace(
            {"project_id": "p1", "source": unsupported}
        )
        assert refused == {"error": f"Unsupported workspace source_type '{unsupported}'"}
        through_execute = await handler.execute(
            "add_workspace", {"project_id": "p1", "source": unsupported}
        )
        assert through_execute == {
            "error": f"Unsupported workspace source_type '{unsupported}'"
        }
    assert await _workspace_ids(handler) == ["ws-p2"]


async def test_add_workspace_generates_a_path_and_auto_detects_the_remote(ws_handler, tmp_path):
    """Positive control for the refusal cases above."""
    handler = ws_handler
    handler.orchestrator.git.aget_remote_url = AsyncMock(
        return_value="https://example.test/detected.git"
    )

    result = await handler._cmd_add_workspace(
        {"project_id": "p1", "source": "init", "name": "main-checkout"}
    )

    assert "error" not in result
    assert result["project_id"] == "p1"
    assert result["kind_id"] == "project-repo"
    # Path generated under workspace_dir/<project>/<name>.
    assert result["workspace_path"] == os.path.realpath(
        os.path.join(handler.config.workspace_dir, "p1", "main-checkout")
    )
    # The project had no repo_url, so the detected remote is written back.
    assert result["auto_detected_repo_url"] == "https://example.test/detected.git"
    project = await handler.db.get_project("p1")
    assert project.repo_url == "https://example.test/detected.git"
    assert await _workspace_ids(handler) == [result["created"]]


# ---------------------------------------------------------------------------
# 22: edit_workspace lock / duplicate / forced relocation
# ---------------------------------------------------------------------------


async def test_edit_workspace_refuses_locked_or_duplicate_path_but_allows_forced_missing_path(
    ws_handler, tmp_path
):
    handler = ws_handler
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    await handler.db.create_workspace(
        Workspace(
            id="ws-a",
            project_id="p1",
            workspace_path=os.path.realpath(a_dir),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )
    await handler.db.create_workspace(
        Workspace(
            id="ws-b",
            project_id="p2",
            workspace_path=os.path.realpath(b_dir),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )

    assert await handler._cmd_edit_workspace({}) == {"error": "workspace_id is required"}
    assert await handler._cmd_edit_workspace({"workspace_id": "ghost"}) == {
        "error": "Workspace 'ghost' not found"
    }

    # Empty / non-string path.
    for bad in ("", "   ", 7, None):
        result = await handler._cmd_edit_workspace({"workspace_id": "ws-a", "workspace_path": bad})
        assert result == {"error": "workspace_path must be a non-empty string"}

    # Unsupported source_type.
    assert await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "source_type": "teleport"}
    ) == {"error": "Unsupported source_type 'teleport'"}

    # Duplicate realpath: ws-b already lives there.
    duplicate = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "workspace_path": str(b_dir)}
    )
    assert duplicate["error"] == (
        f"Path '{os.path.realpath(b_dir)}' is already a workspace (ws-b in project 'p2')."
    )
    assert (await handler.db.get_workspace("ws-a")).workspace_path == os.path.realpath(a_dir)

    # Missing directory without force.
    missing = tmp_path / "not-yet"
    not_forced = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "workspace_path": str(missing)}
    )
    assert "does not exist" in not_forced["error"]
    assert "force=true" in not_forced["error"]
    assert (await handler.db.get_workspace("ws-a")).workspace_path == os.path.realpath(a_dir)

    # Locked workspace: path edits refused even when the target is valid.
    await handler.db.create_task(Task(id="t1", project_id="p1", title="t1", description=""))
    await handler.db.create_agent(Agent(id="agent-1", name="agent-1", profile_id="generic"))
    await handler.db.update_workspace("ws-a", locked_by_agent_id="agent-1", locked_by_task_id="t1")
    locked = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "workspace_path": str(missing), "force": True}
    )
    assert "is locked by task 't1'" in locked["error"]
    assert (await handler.db.get_workspace("ws-a")).workspace_path == os.path.realpath(a_dir)

    # Unlocked + force: the canonical path is written.
    await handler.db.update_workspace("ws-a", locked_by_agent_id=None, locked_by_task_id=None)
    forced = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "workspace_path": str(missing), "force": True}
    )
    assert forced["fields"] == ["workspace_path"]
    assert forced["workspace_path"] == os.path.realpath(missing)
    assert (await handler.db.get_workspace("ws-a")).workspace_path == os.path.realpath(missing)

    # A no-op edit reports no changed fields and writes nothing.
    noop = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "workspace_path": str(missing), "force": True}
    )
    assert noop["fields"] == []
    assert noop["workspace_path"] == os.path.realpath(missing)

    # name / enabled / source_type edits do not need force or an existing dir.
    edited = await handler._cmd_edit_workspace(
        {"workspace_id": "ws-a", "name": "renamed", "enabled": False, "source_type": "init"}
    )
    assert sorted(edited["fields"]) == ["enabled", "name", "source_type"]
    assert edited["enabled"] is False
    row = await handler.db.get_workspace("ws-a")
    assert row.name == "renamed"
    assert row.enabled is False
    assert row.source_type == RepoSourceType.INIT
