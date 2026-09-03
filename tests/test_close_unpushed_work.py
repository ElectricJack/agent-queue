"""A failing close must never strand commits (``aq task close --outcome fail``).

Evidence this exists for (task ``solid-harbor.43``, 2026-09-03): a worker left
five commits in its slot worktree, never pushed them, and closed ``blocked``.
The close was accepted, the slot was reset, and three re-runs of the task each
started from ``main``, could not find the work, and closed ``blocked`` again.

These tests drive the real ``task_close`` command against a real git
repository, because the whole question is what git says about the workspace.
Only the completion pipeline (which a failing close never runs anyway) and
resource release are stubbed.
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitManager
from src.models import (
    AgentProfile,
    Project,
    RepoSourceType,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: str, name: str) -> str:
    pathlib.Path(repo, name).write_text(name)
    _git(["add", name], cwd=repo)
    _git(["commit", "-m", f"add {name}"], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def repo(tmp_path):
    """A bare "origin" plus a clone with one pushed commit on ``main``."""
    origin = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", origin],
        check=True,
        capture_output=True,
    )
    clone = str(tmp_path / "slot")
    subprocess.run(["git", "clone", origin, clone], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=clone)
    _git(["config", "user.email", "t@t.com"], cwd=clone)
    _commit(clone, "README.md")
    _git(["push", "origin", "main"], cwd=clone)
    return {"origin": origin, "clone": clone}


@pytest.fixture(params=["sqlite", "postgres"])
async def handler(request, tmp_path, repo):
    """SQLite always; PostgreSQL when ``POSTGRES_TEST_DSN`` is set (CI).

    The close path writes task metadata and a completion row, so both
    dialects are exercised rather than assumed equivalent.
    """
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "close.db"))
        await db.initialize()
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "close.db"),
        data_dir=str(tmp_path / "d"),
    )
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = GitManager()
    orch.bus = MagicMock()
    orch.bus.emit = AsyncMock()
    orch.command_handler = CommandHandler(orch, cfg)

    async def _noop_release(task_id, *, agent_id=None, workspace_path=None,
                            expect_claim_epoch=None):
        return None

    orch.release_session_task_resources = _noop_release

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    yield orch.command_handler
    await db.close()


async def _task_on_workspace(h: CommandHandler, repo: dict, task_title="Do work") -> str:
    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": task_title, "profile_id": "worker"},
        )
    )["created"]
    await h.db.create_workspace(
        Workspace(
            id=f"ws-{task_id}",
            project_id="p",
            workspace_path=repo["clone"],
            source_type=RepoSourceType.CLONE,
            locked_by_task_id=task_id,
        )
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")
    return task_id


async def test_failing_close_pushes_unpushed_commits(handler, repo):
    """The core rule: a ``fail`` close puts the commits on ``origin`` first."""
    h = handler
    task_id = await _task_on_workspace(h, repo)
    _git(["checkout", "-b", f"aq/{task_id}"], cwd=repo["clone"])
    sha = _commit(repo["clone"], "work.py")

    result = await h.execute(
        "task_close",
        {"task_id": task_id, "outcome": "fail", "summary": "could not finish"},
    )

    assert result.get("success"), result
    assert result["unmerged_branch"] == f"aq/{task_id}"
    assert _git(["rev-parse", f"refs/heads/aq/{task_id}"], cwd=repo["origin"]) == sha
    assert await h.db.get_task_meta(task_id, "unmerged_branch") == f"aq/{task_id}"
    assert await h.db.get_task_meta(task_id, "unmerged_commit") == sha


async def test_failing_close_records_the_branch_in_the_completion_summary(handler, repo):
    """The summary is what a human and the next agent read — put it there."""
    h = handler
    task_id = await _task_on_workspace(h, repo)
    _git(["checkout", "-b", f"aq/{task_id}"], cwd=repo["clone"])
    _commit(repo["clone"], "work.py")

    await h.execute(
        "task_close",
        {"task_id": task_id, "outcome": "fail", "summary": "blocked on the schema"},
    )

    completion = await h.db.get_task_completion(task_id)
    assert completion is not None
    assert "blocked on the schema" in completion.summary
    assert f"origin/aq/{task_id}" in completion.summary
    assert "1 commit(s) preserved" in completion.summary


async def test_close_is_refused_when_the_commits_cannot_be_pushed(handler, repo, tmp_path):
    """No silent close: the task stays claimed and the agent is told why."""
    h = handler
    task_id = await _task_on_workspace(h, repo)
    _git(["checkout", "-b", f"aq/{task_id}"], cwd=repo["clone"])
    _commit(repo["clone"], "work.py")
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=repo["clone"])

    session = await _live_session(h, task_id, repo["clone"])
    result = await h.execute(
        "task_close",
        {
            "task_id": task_id,
            "outcome": "fail",
            "summary": "gave up",
            "session_id": session,
        },
    )

    assert result["success"] is False
    assert result["result"] == "verification_failed"
    assert "commits no remote branch has" in result["error"]
    assert result["unmerged"]["count"] == 1
    task = await h.db.get_task(task_id)
    assert task.status == TaskStatus.IN_PROGRESS, "a refused close must change nothing"
    assert await h.db.get_task_completion(task_id) is None


async def test_clean_failing_close_is_unaffected(handler, repo):
    """Nothing unpushed → the close behaves exactly as it always did."""
    h = handler
    task_id = await _task_on_workspace(h, repo)

    result = await h.execute(
        "task_close", {"task_id": task_id, "outcome": "fail", "summary": "no-op"}
    )

    assert result.get("success"), result
    assert "unmerged_branch" not in result
    assert await h.db.get_task_meta(task_id, "unmerged_branch") is None


async def test_passing_close_is_left_to_the_verification_pipeline(handler, repo):
    """A ``pass`` close keeps its existing auto-push/verify path, not this one."""
    h = handler
    orch = h.orchestrator

    async def _noop_pipeline(ctx):
        return ("", True)

    orch._run_completion_pipeline = _noop_pipeline
    task_id = await _task_on_workspace(h, repo)
    _git(["checkout", "-b", f"aq/{task_id}"], cwd=repo["clone"])
    _commit(repo["clone"], "work.py")

    result = await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )

    assert result.get("success"), result
    assert "unmerged_branch" not in result


async def _live_session(h: CommandHandler, task_id: str, work_dir: str) -> str:
    """A running session row holding *task_id* — the "agent is still there" case."""
    import time

    from src.models import SessionRecord

    session = SessionRecord(
        id=f"sess-{task_id}",
        name=f"n-{task_id}",
        project_id="p",
        profile_id="worker",
        harness="claude",
        provider="fake",
        lifecycle="task",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        task_id=task_id,
        state="running",
        work_dir=work_dir,
    )
    await h.db.create_session(session)
    return session.id
