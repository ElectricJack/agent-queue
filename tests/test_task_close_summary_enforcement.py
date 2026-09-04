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
    assert await db.get_task_completion(tid) is None


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


@pytest.mark.asyncio
async def test_close_captures_auto_commit_sha_after_completion_pipeline(handler, db, monkeypatch):
    """Auto-remediation in the pipeline must be reflected in the durable commit."""
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    created = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = created["created"]
    await db.update_task(tid, branch_name="feature/auto-remediated")
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
    pipeline_finished = False
    original_complete = handler.orchestrator.complete_session_task

    async def complete_with_auto_commit(*args, **kwargs):
        nonlocal pipeline_finished
        result = await original_complete(*args, **kwargs)
        pipeline_finished = True
        return result

    async def final_sha(checkout_path, ref):
        return "after-pipeline" if pipeline_finished else "before-pipeline"

    monkeypatch.setattr(handler.orchestrator, "complete_session_task", complete_with_auto_commit)
    monkeypatch.setattr(handler.orchestrator.git, "arev_parse", final_sha)

    result = await handler.execute(
        "task_close",
        {"task_id": tid, "outcome": "pass", "summary": "auto-remediated"},
    )

    assert result["success"] is True
    assert await db.get_task_meta(tid, "work_commit_auto") == "after-pipeline"
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.commits == ["after-pipeline"]


@pytest.mark.asyncio
async def test_close_persists_structured_completion_story(handler, db):
    """A successful close must save the normalized completion account."""
    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker", needs_workspace=True))
    created = await handler.execute(
        "create_task", {"project_id": "p", "title": "t", "profile_id": "worker"}
    )
    tid = created["created"]
    await db.update_task(
        tid,
        branch_name="feature/completion-story",
        pr_url="https://github.com/example/repo/pull/17",
    )
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "work_outcome": "shipped",
            "summary": "Added durable completion records.",
            "changes": "Added the model, persistence, and task detail surfaces.",
            "verification": "Focused backend and dashboard tests passed.",
            "tests": ["pytest tests/test_task_close_summary_enforcement.py -q"],
            "commands": ["npm test -- task-detail", "ruff check src tests"],
            "commit": "abc123",
            "notes": "Ready for reviewer.",
        },
    )

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.task_id == tid
    assert completion.outcome == "pass"
    assert completion.work_outcome == "shipped"
    assert completion.changes == "Added the model, persistence, and task detail surfaces."
    assert completion.verification == "Focused backend and dashboard tests passed."
    assert completion.tests == ["pytest tests/test_task_close_summary_enforcement.py -q"]
    assert completion.commands == ["npm test -- task-detail", "ruff check src tests"]
    assert completion.branch == "feature/completion-story"
    assert completion.commits == ["abc123"]
    assert completion.pr_url == "https://github.com/example/repo/pull/17"
    assert completion.summary == "Added durable completion records."
    assert completion.notes == "Ready for reviewer."


@pytest.mark.asyncio
async def test_close_accepts_declared_file_deliverables_and_records_their_evaluation(handler, db):
    """A passing close preserves positive deliverable evidence for review."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "model", "kind": "file", "target": "src/models.py"}],
        },
    )
    tid = created["created"]
    assert (await db.get_task(tid)).deliverables == [
        {"id": "model", "kind": "file", "target": "src/models.py"}
    ]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.deliverables == [
        {
            "id": "model",
            "kind": "file",
            "target": "src/models.py",
            "met": True,
            "reason": "",
        }
    ]


@pytest.mark.asyncio
async def test_close_rejects_unmet_deliverable_without_an_explicit_reason(handler, db):
    """A worker cannot silently pass a task with a declared missing file."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "missing", "kind": "file", "target": "does-not-exist.py"}],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute("task_close", {"task_id": tid, "outcome": "pass"})

    assert result["success"] is False
    assert result["code"] == "deliverables.unmet"
    assert "missing" in result["error"]
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS
    assert await db.get_task_completion(tid) is None


@pytest.mark.asyncio
async def test_close_records_an_explicit_reason_for_an_unmet_deliverable(handler, db):
    """A deliberate scope exception is allowed but cannot be hidden from review."""
    await db.create_project(Project(id="p", name="P"))
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [{"id": "missing", "kind": "file", "target": "does-not-exist.py"}],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "deliverable_unmet": ["missing: explicitly deferred to the follow-up task"],
        },
    )

    assert result["success"] is True
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert completion.deliverables == [
        {
            "id": "missing",
            "kind": "file",
            "target": "does-not-exist.py",
            "met": False,
            "reason": "explicitly deferred to the follow-up task",
        }
    ]


@pytest.mark.asyncio
async def test_close_accepts_test_and_command_deliverables_declared_as_shell_commands(
    handler, db
):
    """A test suite declared as its ``aq test`` command line and a lint step
    declared as ``ruff check <changed files>`` are met by the recorded
    ``--test`` / ``--command`` values (regression for azure-current-84)."""
    await db.create_project(Project(id="p", name="P"))
    suite = "aq test tests/test_deliverables.py tests/test_task_close_summary_enforcement.py"
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "t",
            "deliverables": [
                {"id": "focused-suite", "kind": "test", "target": suite},
                {"id": "ruff", "kind": "command", "target": "ruff check <changed files>"},
            ],
        },
    )
    tid = created["created"]
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="test")

    refused = await handler.execute(
        "task_close", {"task_id": tid, "outcome": "pass", "tests": [suite]}
    )
    assert refused["success"] is False
    assert refused["code"] == "deliverables.unmet"
    assert [item["id"] for item in refused["unmet_deliverables"]] == ["ruff"]

    result = await handler.execute(
        "task_close",
        {
            "task_id": tid,
            "outcome": "pass",
            "tests": [suite],
            "commands": ["ruff check src/deliverables.py"],
        },
    )

    assert result["success"] is True, result
    completion = await db.get_task_completion(tid)
    assert completion is not None
    assert [(item["id"], item["met"]) for item in completion.deliverables] == [
        ("focused-suite", True),
        ("ruff", True),
    ]
