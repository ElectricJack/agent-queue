"""Tests for pr_merge command (dv2 Phase 2, Task 1).

Tests GitManager.amerge_pr and CommandHandler._cmd_pr_merge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# GitManager.amerge_pr unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merge_command_shells_gh_and_returns_success(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[:3] == ["gh", "pr", "merge"]
        assert "--squash" in cmd
        assert "https://github.com/org/repo/pull/42" in cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "Merged\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pr_merge_command_reports_gh_failure(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "not mergeable: conflicts"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is False
    assert "conflicts" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_invalid_method(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    result = await gm.amerge_pr(
        "/some/checkout", "https://github.com/org/repo/pull/42", method="fast-forward"
    )
    assert result["success"] is False
    assert "invalid method" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_parses_sha_from_output(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    sha = "a" * 40

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 0
        r.stdout = f"Merged pull request #42 ({sha})\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["sha"] == sha


# ---------------------------------------------------------------------------
# CommandHandler._cmd_pr_merge integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "pm.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    await d.create_workspace(
        Workspace(
            id="w1",
            project_id="p1",
            workspace_path="/tmp/p1",
            source_type=RepoSourceType.CLONE,
        )
    )
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "pm.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


@pytest.mark.asyncio
async def test_cmd_pr_merge_routes_through_git_manager(monkeypatch, handler):
    calls = {}

    async def fake_amerge(checkout_path, pr_url, method="squash"):
        calls["args"] = (checkout_path, pr_url, method)
        return {"success": True, "sha": "abc123", "error": None}

    monkeypatch.setattr(handler.orchestrator.git, "amerge_pr", fake_amerge)
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "p1",
            "pr_url": "https://github.com/o/r/pull/1",
            "method": "squash",
        },
    )
    assert result["success"] is True
    assert result["sha"] == "abc123"
    assert calls["args"] == ("/tmp/p1", "https://github.com/o/r/pull/1", "squash")


@pytest.mark.asyncio
async def test_cmd_pr_merge_rejects_unknown_project(handler):
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "nope",
            "pr_url": "https://github.com/o/r/pull/1",
        },
    )
    assert result["success"] is False
    assert "project" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_pr_url(handler):
    result = await handler.execute(
        "pr_merge",
        {"project_id": "p1"},
    )
    assert result["success"] is False
    assert "pr_url" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_project_id(handler):
    result = await handler.execute(
        "pr_merge",
        {"pr_url": "https://github.com/o/r/pull/1"},
    )
    assert result["success"] is False
    assert "project_id" in result["error"].lower()
