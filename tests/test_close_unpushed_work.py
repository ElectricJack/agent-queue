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
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database.queries.result_queries import PENDING_COMPLETION_KEY
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.git.manager import GitError, GitManager
from src.git.process import ProcessCleanupError
from src.models import (
    AgentProfile,
    Project,
    RepoSourceType,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.orchestrator.stranded_work import StrandedWork
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

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


@pytest.fixture
async def handler(tmp_path, repo, reuse_database):
    """Real command handler and Git, backed by isolated PostgreSQL."""
    db = await reuse_database("close.db")
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database=DatabaseConfig(url=lease_dsn("close.db")),
        data_dir=str(tmp_path / "d"),
    )
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = GitManager()
    orch.bus = MagicMock()
    orch.bus.emit = AsyncMock()
    orch.set_command_handler(CommandHandler(orch, cfg))

    async def _noop_release(
        task_id, *, agent_id=None, workspace_path=None, expect_claim_epoch=None
    ):
        return None

    orch.release_session_task_resources = _noop_release

    await db.create_project(Project(id="p", name="P"))
    await db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    yield orch._command_handler


async def _task_on_workspace(h: CommandHandler, repo: dict, task_title="Do work") -> str:
    # Filed with hints only (mandatory task routing §5.1), then routed.
    task_id = (
        await h.execute("create_task", {"project_id": "p", "title": task_title})
    )["created"]
    assert await h.db.update_task_routing(
        task_id, profile_id="worker",
        route_source="legacy", intelligence_class=None, preferred_workspace_id=None
    )
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


@pytest.mark.parametrize("live", [False, True])
async def test_clean_failing_close_is_unaffected(handler, repo, live):
    """Nothing unpushed → the close behaves exactly as it always did."""
    h = handler
    task_id = await _task_on_workspace(h, repo)
    session = await _live_session(h, task_id, repo["clone"]) if live else None

    result = await h.execute(
        "task_close",
        {"task_id": task_id, "outcome": "fail", "summary": "no-op", "session_id": session},
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


@pytest.mark.parametrize("failure", ["git_error", "cleanup_error", "unknown", "count_unknown",
                                    "remote_unknown", "head_unknown", "missing_checkout"])
async def test_unknown_preservation_refuses_live_close_without_releasing_work(
    handler, repo, monkeypatch, failure
):
    h = handler
    orch = h.orchestrator
    task_id = await _task_on_workspace(h, repo)
    branch = f"aq/{task_id}"
    _git(["checkout", "-b", branch], cwd=repo["clone"])
    sha = _commit(repo["clone"], "work.py")
    await h.db.update_task(task_id, branch_name=branch)
    pathlib.Path(repo["clone"], "work.py").write_text("unfinished changes")
    pathlib.Path(repo["clone"], "untracked.py").write_text("untracked work")
    dirty = _git(["status", "--porcelain"], cwd=repo["clone"])
    session = await _live_session(h, task_id, repo["clone"])
    before = await h.db.get_task(task_id)
    release = AsyncMock()
    monkeypatch.setattr(orch, "release_session_task_resources", release)
    revoke = AsyncMock()
    monkeypatch.setattr(orch, "token_store", MagicMock(revoke_session=revoke))
    args = {"task_id": task_id, "outcome": "fail", "summary": "blocked", "session_id": session}

    with monkeypatch.context() as injected:
        if failure in {"git_error", "cleanup_error", "unknown"}:
            error = (GitError("inspection failed") if failure == "git_error"
                     else ProcessCleanupError("writers could not be proved stopped"))
            probe = (AsyncMock(return_value=StrandedWork(status="unknown"))
                     if failure == "unknown" else AsyncMock(side_effect=error))
            injected.setattr("src.orchestrator.stranded_work.preserve_unpushed_work", probe)
        else:
            method = {"count_unknown": "acount_commits_not_on_any_remote",
                      "remote_unknown": "ahas_remote", "head_unknown": "arev_parse",
                      "missing_checkout": "avalidate_checkout"}[failure]
            injected.setattr(orch.git, method, AsyncMock(
                return_value=False if failure == "missing_checkout" else None
            ))
        result = await h.execute("task_close", args)

    assert result["success"] is False, result
    assert result["result"] == "verification_failed"
    assert result["unmerged"]["status"] == "unknown"
    assert "preservation could not be proved" in result["error"]
    assert "same claim" in result["feedback"]
    if failure in {"git_error", "cleanup_error"}:
        assert type(error).__name__ in result["feedback"]
        assert str(error) in result["feedback"]
    after = await h.db.get_task(task_id)
    assert (after.status, after.retry_count, after.branch_name, after.claim_epoch) == (
        before.status, before.retry_count, branch, before.claim_epoch
    )
    assert (await h.db.get_session(session)).state == "running"
    assert (await h.db.get_workspace_for_task(task_id)).locked_by_task_id == task_id
    assert await h.db.get_task_completion(task_id) is None
    assert await h.db.get_task_meta(task_id, ACCEPTED_CLOSE_KEY) is None
    assert await h.db.get_task_meta(task_id, PENDING_COMPLETION_KEY) is None
    release.assert_not_awaited()
    revoke.assert_not_awaited()
    assert _git(["rev-parse", "HEAD"], cwd=repo["clone"]) == sha
    assert _git(["branch", "--show-current"], cwd=repo["clone"]) == branch
    assert _git(["status", "--porcelain"], cwd=repo["clone"]) == dirty
    assert pathlib.Path(repo["clone"], "work.py").read_text() == "unfinished changes"
    assert pathlib.Path(repo["clone"], "untracked.py").read_text() == "untracked work"
    assert _git(["for-each-ref", "--format=%(refname)", "refs/heads/aq"],
                cwd=repo["origin"]) == ""

    # The same session can retry once inspection works; the actual HEAD is saved.
    accepted = await h.execute("task_close", args)
    assert accepted["success"], accepted
    assert _git(["rev-parse", f"refs/heads/{branch}"], cwd=repo["origin"]) == sha


async def test_live_failure_without_assigned_workspace_can_close(handler, repo):
    h = handler
    task_id = (await h.execute("create_task", {"project_id": "p", "title": "No checkout"}))[
        "created"
    ]
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")
    session = await _live_session(h, task_id, repo["clone"])
    result = await h.execute("task_close", {
        "task_id": task_id, "outcome": "fail", "summary": "no workspace assigned",
        "session_id": session,
    })
    assert result["success"], result
    assert await h.db.get_task_completion(task_id) is not None


async def test_live_failure_with_no_remote_records_local_head(handler, repo):
    h = handler
    task_id = await _task_on_workspace(h, repo)
    branch = f"aq/{task_id}"
    _git(["checkout", "-b", branch], cwd=repo["clone"])
    sha = _commit(repo["clone"], "work.py")
    _git(["remote", "remove", "origin"], cwd=repo["clone"])
    session = await _live_session(h, task_id, repo["clone"])
    result = await h.execute("task_close", {
        "task_id": task_id, "outcome": "fail", "summary": "local only", "session_id": session,
    })
    assert result["success"], result
    assert result["unmerged_branch"] == branch
    assert await h.db.get_task_meta(task_id, "unmerged_commit") == sha
    assert sha[:12] in (await h.db.get_task_completion(task_id)).summary


@pytest.mark.parametrize("remote_state", ["absent", "behind", "different_branch", "unreachable"])
async def test_live_passing_close_requires_exact_task_branch_on_origin(handler, repo, remote_state):
    h = handler

    async def pipeline(ctx):
        return "", True

    h.orchestrator._run_completion_pipeline = pipeline
    task_id = await _task_on_workspace(h, repo)
    branch = f"aq/{task_id}"
    _git(["checkout", "-b", branch], cwd=repo["clone"])
    await h.db.update_task(task_id, branch_name=branch)
    if remote_state == "behind":
        _git(["push", "origin", branch], cwd=repo["clone"])
    sha = _commit(repo["clone"], "work.py")
    if remote_state == "different_branch":
        _git(["push", "origin", "HEAD:aq/other"], cwd=repo["clone"])
    if remote_state == "unreachable":
        _git(["remote", "set-url", "origin", repo["origin"] + ".missing"], cwd=repo["clone"])
    session = await _live_session(h, task_id, repo["clone"])
    args = {"task_id": task_id, "outcome": "pass", "summary": "done", "session_id": session}
    refused = await h.execute("task_close", args)
    assert refused["success"] is False, refused
    assert refused["result"] == "verification_failed"
    assert "aq git push" in refused["error"]
    assert (await h.db.get_task(task_id)).status == TaskStatus.IN_PROGRESS
    assert (await h.db.get_session(session)).state == "running"
    assert await h.db.get_task_completion(task_id) is None
    _git(["remote", "set-url", "origin", repo["origin"]], cwd=repo["clone"])
    _git(["push", "origin", branch], cwd=repo["clone"])
    assert _git(["rev-parse", f"refs/heads/{branch}"], cwd=repo["origin"]) == sha
    accepted = await h.execute("task_close", args)
    assert accepted["success"], accepted
    assert (await h.db.get_task(task_id)).status == TaskStatus.COMPLETED
