"""Tests for close-with-summary enforcement (Dv2 Phase 2 Task 2).

Verifies:
- Tasks whose profile has ``needs_workspace=True`` must supply a ``summary``
  on close; omitting it returns a structured error.
- Tasks with ``needs_workspace=False`` (or no profile) may omit summary.
- When no explicit ``commit`` is passed but the task has a ``branch_name``,
  the SHA is captured via ``GitManager.arev_parse`` into ``work_commit_auto``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _StubOrchestrator:
    """Minimal orchestrator stub sufficient for _cmd_task_close."""

    def __init__(self, db):
        self.db = db
        self.plugin_registry = None
        self.token_store = None
        self.git = MagicMock()

    async def complete_session_task(self, task, **kwargs):
        status = TaskStatus.COMPLETED if kwargs.get("outcome") == "pass" else TaskStatus.FAILED
        await self.db.transition_task(task.id, status, context="session_close")
        return {"status": status.value, "pr_url": None, "pipeline_ok": True}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t2.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.messages.enabled = False
    return cfg


@pytest.fixture
def handler(db, config):
    orch = _StubOrchestrator(db)
    return CommandHandler(orch, config)


# ---------------------------------------------------------------------------
# Test 1: summary required for workspace-needing profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_rejects_missing_summary_for_workspace_profile(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = task["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is False
    assert "summary" in result["error"].lower()


# ---------------------------------------------------------------------------
# Test 2: summary not required for non-workspace profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_allows_missing_summary_for_non_workspace_profile(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="chat", name="Chat", needs_workspace=False))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "chat"}
    )
    tid = task["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 3: summary not required for tasks with no profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_allows_missing_summary_for_profileless_task(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})

    assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 4: summary succeeds and is written to task_meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_summary_succeeds_and_stores_meta(handler, db):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = task["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "did the thing"},
    )

    assert result["success"] is True
    assert await db.get_task_meta(tid, "summary") == "did the thing"


# ---------------------------------------------------------------------------
# Test 5: commit hash captured from branch_name via arev_parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_captures_commit_from_branch(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = task["created"]
    await db.update_task(tid, branch_name="feature/x")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    fake_sha = "deadbeef" * 5  # 40 chars

    async def fake_arev_parse(checkout_path, ref):
        assert ref == "feature/x"
        return fake_sha

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    # We also need get_project_workspace_path to return a non-None path.
    # The project was created without a workspace row, so we create one here.
    from src.models import RepoSourceType, Workspace

    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p",
            workspace_path="/tmp/p",
            source_type=RepoSourceType.LINK,
        )
    )

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "did the thing"},
    )
    assert result["success"] is True
    meta = await db.get_task_meta(tid, "work_commit_auto")
    assert meta == fake_sha


# ---------------------------------------------------------------------------
# Test 6: explicit commit skips arev_parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_skips_auto_commit_when_explicit_commit_provided(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = task["created"]
    await db.update_task(tid, branch_name="feature/y")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    arev_called = []

    async def fake_arev_parse(checkout_path, ref):
        arev_called.append(ref)
        return "a" * 40

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "summary": "done",
            "commit": "explicit-sha",
        },
    )
    assert result["success"] is True
    assert arev_called == [], "arev_parse must NOT be called when caller supplies commit"
    assert await db.get_task_meta(tid, "work_commit") == "explicit-sha"
    assert await db.get_task_meta(tid, "work_commit_auto") is None


# ---------------------------------------------------------------------------
# Test 7: arev_parse failure is non-fatal (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_succeeds_even_when_arev_parse_returns_none(handler, db, monkeypatch):
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    task = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = task["created"]
    await db.update_task(tid, branch_name="feature/z")
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    from src.models import RepoSourceType, Workspace

    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p",
            workspace_path="/tmp/p",
            source_type=RepoSourceType.LINK,
        )
    )

    async def fake_arev_parse(checkout_path, ref):
        return None  # simulate git failure / unknown ref

    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", fake_arev_parse)

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "completed"},
    )
    assert result["success"] is True
    assert await db.get_task_meta(tid, "work_commit_auto") is None
