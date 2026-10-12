"""The daily branch sweep deletes only refs with exact Git proof."""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.database import Database
from src.database.tables import projects
from src.git.manager import GitManager
from src.integration.branch_sweep import SweepSafety, sweep_checkout
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(cwd: Path, message: str, filename: str, content: str) -> None:
    (cwd / filename).write_text(content)
    _git(cwd, "add", filename)
    _git(cwd, "commit", "-m", message)


@pytest.mark.asyncio
async def test_sweep_deletes_proven_refs_and_keeps_held_unique_and_stashed_work(tmp_path):
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)], check=True, capture_output=True
    )
    _commit(checkout, "initial", "README", "initial\n")
    _git(checkout, "push", "-u", "origin", "main")

    # A differently-hashed commit with the same patch as dev.
    _git(checkout, "switch", "-c", "takeover/equivalent", "main")
    _commit(checkout, "same patch", "landed.txt", "landed\n")
    equivalent_sha = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "push", "origin", "takeover/equivalent")

    _git(checkout, "switch", "main")
    _git(checkout, "switch", "-c", "dev")
    _commit(checkout, "same patch", "landed.txt", "landed\n")
    _git(checkout, "push", "origin", "dev")
    dev_sha = _git(checkout, "rev-parse", "HEAD")

    # An ancestor candidate and a live task ref whose content is also landed.
    _git(checkout, "branch", "land/already-on-dev", dev_sha)
    _git(checkout, "push", "origin", "land/already-on-dev")
    _git(checkout, "branch", "aq/live-task", dev_sha)
    _git(checkout, "push", "origin", "aq/live-task")

    # Unique work must remain even under preserved and non-AQ branch names.
    _git(checkout, "switch", "main")
    _git(checkout, "switch", "-c", "aq/preserved/unique")
    _commit(checkout, "unique preserved work", "preserved.txt", "keep me\n")
    preserved_sha = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "push", "origin", "aq/preserved/unique")
    _git(checkout, "switch", "main")
    _git(checkout, "switch", "-c", "hotfix/unique")
    _commit(checkout, "unique hotfix work", "hotfix.txt", "keep me too\n")
    hotfix_sha = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "push", "origin", "hotfix/unique")
    _git(checkout, "switch", "main")
    _git(checkout, "push", "origin", "main")
    _git(checkout, "push", "origin", "main:staging")

    # The report-only stash audit must leave the stash in place.
    (checkout / "README").write_text("stashed change\n")
    _git(checkout, "stash", "push", "-m", "operator stash")

    backup = tmp_path / "vault" / "branch-sweep.tsv"
    report = await sweep_checkout(
        GitManager(),
        str(checkout),
        repository_url=str(origin),
        default_branch="main",
        holds={"aq/live-task": "task is in progress"},
        backup_path=backup,
    )

    assert {name for name, _, _ in report.local_deleted} == {
        "takeover/equivalent", "land/already-on-dev"
    }
    assert {name for name, _, _ in report.remote_deleted} == {
        "takeover/equivalent", "land/already-on-dev"
    }
    assert ("aq/preserved/unique", preserved_sha) in report.unique_unmerged
    assert ("hotfix/unique", hotfix_sha) in report.unique_unmerged
    assert ("aq/preserved/unique", preserved_sha) in report.preserved_kept
    assert "aq/live-task" in _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert "main" in _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    assert "dev" in _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    assert "staging" in _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    assert report.stashes and "operator stash" in report.stashes[0]
    assert "operator stash" in _git(checkout, "stash", "list")

    backup_rows = backup.read_text().splitlines()
    assert any("refs/heads/takeover/equivalent" in row and equivalent_sha in row for row in backup_rows)
    assert any("refs/heads/land/already-on-dev" in row for row in backup_rows)


@pytest.mark.asyncio
async def test_daily_sweep_scheduler_runs_only_when_git_first_is_active(tmp_path):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.config = SimpleNamespace(
        data_dir=str(tmp_path), integration=SimpleNamespace(git_first="shadow")
    )
    orchestrator._last_git_branch_sweep_date = None
    orchestrator._git_branch_sweep_task = None
    orchestrator._run_daily_git_branch_sweep = AsyncMock()

    orchestrator._schedule_daily_git_branch_sweep()
    assert orchestrator._git_branch_sweep_task is None

    orchestrator.config.integration.git_first = "active"
    orchestrator._schedule_daily_git_branch_sweep()
    await orchestrator._git_branch_sweep_task
    orchestrator._run_daily_git_branch_sweep.assert_awaited_once()

    orchestrator._git_branch_sweep_task = None
    orchestrator._schedule_daily_git_branch_sweep()
    assert orchestrator._git_branch_sweep_task is None


@pytest.mark.asyncio
async def test_daily_sweep_includes_non_active_registered_projects(tmp_path, monkeypatch):
    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield object()

    project = SimpleNamespace(id="project-archived", name="Archived", status="ARCHIVED")
    db = SimpleNamespace(
        _engine=Engine(),
        list_projects=AsyncMock(return_value=[project]),
        list_repos=AsyncMock(return_value=[]),
        list_workspaces=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.integration.delivery_branches.live_branch_references",
        AsyncMock(return_value={}),
    )
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = db
    orchestrator.git = AsyncMock()
    orchestrator.config = SimpleNamespace(
        data_dir=str(tmp_path / "state"),
        vault_projects=str(tmp_path / "vault"),
    )
    orchestrator._command_handler = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True})
    )

    await orchestrator._run_daily_git_branch_sweep("2026-10-08")

    db.list_workspaces.assert_awaited_once_with(project_id=project.id)
    orchestrator._command_handler.execute.assert_awaited_once()
    command, args = orchestrator._command_handler.execute.call_args.args
    assert command == "message_send"
    assert args["project_id"] == project.id
    assert args["to_id"] == "supervisor-project-archived"
    assert (tmp_path / "state/maintenance/git-branch-sweep-last-date").read_text().strip() == (
        "2026-10-08"
    )


@pytest.fixture
async def guarded_repo(tmp_path):
    origin, checkout = tmp_path / "origin.git", tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(checkout))
    _commit(checkout, "base", "README", "base\n")
    _git(checkout, "push", "origin", "main")
    _git(checkout, "branch", "aq/candidate")
    _git(checkout, "push", "origin", "aq/candidate")
    db = Database(lease_dsn("branch-sweep"))
    await db.initialize()
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin),
        checkout_base_path=str(checkout),
    ))
    yield db, GitManager(), checkout, origin
    await db.close()


@pytest.mark.parametrize("hold", ["task", "owner", "flow", "attached"])
async def test_guarded_sweep_never_deletes_live_or_protected_work(guarded_repo, tmp_path, hold):
    db, git, checkout, origin = guarded_repo
    if hold == "task":
        await db.create_task(Task(
            id="candidate", project_id="p", title="live", description="", repo_id="repo",
            branch_name="aq/candidate", status=TaskStatus.READY,
        ))
    elif hold == "owner":
        await BranchLock(db).acquire(
            BranchKey(repository_id="repo", branch="aq/candidate"), "live-owner",
        )
    elif hold == "flow":
        async with db.immediate() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                promotion_flow=[{"id": "release", "source": "main", "target": "aq/candidate"}],
            ))
    else:
        _git(checkout, "worktree", "add", str(tmp_path / "attached"), "aq/candidate")
    safety = SweepSafety(db, "repo", default_branch="main", git=git, checkout=str(checkout))
    backup = tmp_path / "audit.tsv"
    report = await sweep_checkout(
        git, str(checkout), repository_url=str(origin), default_branch="main",
        holds={}, backup_path=backup, deletion_guard=safety.deletion,
    )
    assert report.local_deleted == report.remote_deleted == []
    assert report.local_held == report.remote_held == 1
    assert await git.arev_parse(str(checkout), "aq/candidate")
    assert await git.arev_parse(str(origin), "refs/heads/aq/candidate")
    assert not backup.exists()


async def test_daily_backstop_reports_each_project_and_recovers_terminal_intents(tmp_path):
    projects = [SimpleNamespace(id="p", name="One"), SimpleNamespace(id="q", name="Two")]
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = SimpleNamespace(
        list_projects=AsyncMock(return_value=projects), list_repos=AsyncMock(return_value=[]),
        list_workspaces=AsyncMock(side_effect=[[], RuntimeError("unavailable")]),
    )
    orchestrator.config = SimpleNamespace(data_dir=str(tmp_path), vault_projects=str(tmp_path))
    orchestrator.integration_cleanup_service = SimpleNamespace(reconcile=AsyncMock())
    orchestrator.branch_retirement_service = SimpleNamespace(
        reconcile_project=AsyncMock(return_value=[("complete", None), ("pending", "live owner")]),
    )
    orchestrator._command_handler = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True}),
    )
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    orchestrator.integration_cleanup_service.reconcile.assert_awaited_once()
    assert [call.args for call in
            orchestrator.branch_retirement_service.reconcile_project.await_args_list] == [
        ("p",), ("q",),
    ]
    messages = [call.args[1] for call in orchestrator._command_handler.execute.await_args_list]
    assert [m["to_id"] for m in messages] == ["supervisor-p", "supervisor-q"]
    assert all("1 complete, 1 held/conflicted" in m["body"] for m in messages)
    assert "unavailable" in messages[1]["body"]


async def test_failed_supervisor_report_does_not_mark_day_complete(tmp_path):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[SimpleNamespace(id="p", name="One")]),
        list_repos=AsyncMock(return_value=[]), list_workspaces=AsyncMock(return_value=[]),
    )
    orchestrator.config = SimpleNamespace(data_dir=str(tmp_path), vault_projects=str(tmp_path))
    orchestrator._command_handler = SimpleNamespace(execute=AsyncMock(return_value={
        "success": False, "error": "message storage unavailable",
    }))
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    assert orchestrator._last_git_branch_sweep_date is None
    assert not (tmp_path / "maintenance/git-branch-sweep-last-date").exists()


async def test_daily_retry_skips_projects_already_reported_before_restart(tmp_path):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[SimpleNamespace(id="p", name="One"),
                                             SimpleNamespace(id="q", name="Two")]),
        list_repos=AsyncMock(return_value=[]), list_workspaces=AsyncMock(return_value=[]),
    )
    orchestrator.config = SimpleNamespace(data_dir=str(tmp_path), vault_projects=str(tmp_path))
    orchestrator._command_handler = SimpleNamespace(execute=AsyncMock(side_effect=[
        {"success": True}, {"success": False, "error": "temporarily unavailable"},
        {"success": True},
    ]))
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    assert orchestrator._last_git_branch_sweep_date is None
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    assert [call.args[1]["project_id"] for call in
            orchestrator._command_handler.execute.await_args_list] == ["p", "q", "q"]
    assert (tmp_path / "maintenance/git-branch-sweep-last-date").read_text().strip() == "2026-10-09"


def _reporting_orchestrator(tmp_path, result):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[SimpleNamespace(id="p", name="One")]),
        list_repos=AsyncMock(return_value=[]), list_workspaces=AsyncMock(return_value=[]),
    )
    orchestrator.config = SimpleNamespace(data_dir=str(tmp_path), vault_projects=str(tmp_path))
    orchestrator._command_handler = SimpleNamespace(execute=AsyncMock(return_value=result))
    return orchestrator


async def test_queued_supervisor_report_marks_day_complete(tmp_path):
    # message_send answers a queued send with no "success" key; reading that
    # as a refusal re-sent every project's report on each tick (2026-10-10).
    queued = {"message_id": "msg-1", "state": "queued", "message": {"id": "msg-1"}}
    await _reporting_orchestrator(tmp_path, queued)._run_daily_git_branch_sweep("2026-10-09")
    assert (tmp_path / "maintenance/git-branch-sweeps/p").read_text().strip() == "2026-10-09"
    assert (tmp_path / "maintenance/git-branch-sweep-last-date").read_text().strip() == (
        "2026-10-09"
    )
    restarted = _reporting_orchestrator(tmp_path, queued)
    await restarted._run_daily_git_branch_sweep("2026-10-09")
    restarted._command_handler.execute.assert_not_awaited()


async def test_refused_supervisor_report_without_success_key_is_retried(tmp_path):
    orchestrator = _reporting_orchestrator(tmp_path, {"error": "Project 'p' not found"})
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    assert orchestrator._last_git_branch_sweep_date is None
    assert not (tmp_path / "maintenance/git-branch-sweeps/p").exists()
    assert not (tmp_path / "maintenance/git-branch-sweep-last-date").exists()


async def test_daily_backstop_sweeps_registered_checkout_with_fresh_holds(guarded_repo, tmp_path):
    db, git, checkout, origin = guarded_repo
    await db.create_task(Task(
        id="candidate", project_id="p", title="live", description="", repo_id="repo",
        branch_name="aq/candidate", status=TaskStatus.IN_PROGRESS,
    ))
    _git(checkout, "branch", "aq/missed-landing")
    _git(checkout, "push", "origin", "aq/missed-landing")
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db, orchestrator.git = db, git
    orchestrator.config = SimpleNamespace(
        data_dir=str(tmp_path / "state"), vault_projects=str(tmp_path / "vault"),
    )
    orchestrator._command_handler = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True}),
    )
    await orchestrator._run_daily_git_branch_sweep("2026-10-09")
    assert await git.arev_parse(str(origin), "refs/heads/aq/candidate")
    assert await git.arev_parse(str(checkout), "aq/candidate")
    assert await git.arev_parse(str(origin), "refs/heads/aq/missed-landing") is None
    assert await git.arev_parse(str(checkout), "aq/missed-landing") is None
    report = orchestrator._command_handler.execute.call_args.args[1]["body"]
    assert "deleted 1 Git-proven local refs" in report
    assert "deleted 1 Git-proven remote refs" in report


async def test_sweep_rechecks_task_that_resumes_after_git_proof(guarded_repo, tmp_path, monkeypatch):
    from src.integration import branch_sweep

    db, git, checkout, origin = guarded_repo
    await db.create_task(Task(
        id="candidate", project_id="p", title="completed", description="", repo_id="repo",
        branch_name="aq/candidate", status=TaskStatus.COMPLETED,
    ))
    original = branch_sweep._proof

    async def resume_after_proof(*args):
        proof = await original(*args)
        await db.update_task("candidate", status=TaskStatus.READY)
        return proof

    monkeypatch.setattr(branch_sweep, "_proof", resume_after_proof)
    safety = SweepSafety(db, "repo", default_branch="main")
    report = await sweep_checkout(
        git, str(checkout), repository_url=str(origin), default_branch="main",
        holds={}, backup_path=tmp_path / "audit.tsv", deletion_guard=safety.deletion,
    )
    assert report.local_deleted == report.remote_deleted == []
    assert report.local_held == report.remote_held == 1


async def test_sweep_lists_stashes_of_a_bare_store_read_only(tmp_path):
    origin, checkout, store = tmp_path / "origin.git", tmp_path / "checkout", tmp_path / "store.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(checkout))
    _commit(checkout, "base", "README", "base\n")
    _git(checkout, "push", "origin", "main")
    _git(tmp_path, "clone", "--bare", str(origin), str(store))
    sweep = {"repository_url": str(origin), "default_branch": "main", "holds": {},
             "backup_path": tmp_path / "audit.tsv"}

    assert (await sweep_checkout(GitManager(), str(store), **sweep)).stashes == []

    # A linked worktree's stash lives in the bare store's shared refs/stash.
    linked = tmp_path / "linked"
    _git(store, "worktree", "add", "--detach", str(linked), "main")
    (linked / "README").write_text("stashed change\n")
    _git(linked, "stash", "push", "-m", "linked stash")
    expected = _git(linked, "stash", "list", "--format=%gd %H %s").splitlines()
    report = await sweep_checkout(GitManager(), str(store), **sweep)
    assert report.stashes == expected and "linked stash" in expected[0]
    assert _git(linked, "stash", "list", "--format=%gd %H %s").splitlines() == expected


async def test_daily_backstop_skips_job_snapshots_and_reports_unregistered_origin_once(tmp_path):
    other, snapshot_source = tmp_path / "other.git", tmp_path / "snapshot-source.git"
    for bare in (other, snapshot_source):
        _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    paths = {name: tmp_path / name for name in ("first", "second", "snapshot")}
    _git(tmp_path, "clone", str(other), str(paths["first"]))
    _git(tmp_path, "clone", str(other), str(paths["second"]))
    _git(tmp_path, "clone", str(snapshot_source), str(paths["snapshot"]))
    workspaces = [
        SimpleNamespace(workspace_path=str(paths["snapshot"]), kind_id="job-snapshot",
                        is_slot=False, enabled=False),
        SimpleNamespace(workspace_path=str(paths["first"]), kind_id=None, is_slot=False),
        SimpleNamespace(workspace_path=str(paths["second"]), kind_id=None, is_slot=False),
    ]
    registered = SimpleNamespace(id="repo", url=str(tmp_path / "registered.git"),
                                 checkout_base_path=str(tmp_path / "absent"),
                                 default_branch="main")
    orchestrator = object.__new__(Orchestrator)
    orchestrator.db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[SimpleNamespace(id="p", name="One")]),
        list_repos=AsyncMock(return_value=[registered]),
        list_workspaces=AsyncMock(return_value=workspaces),
        resolve_workspace_kind=AsyncMock(side_effect=AssertionError("snapshot inspected")),
    )
    orchestrator.git = GitManager()
    orchestrator.config = SimpleNamespace(data_dir=str(tmp_path / "state"),
                                          vault_projects=str(tmp_path / "vault"))
    orchestrator._command_handler = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True}),
    )

    await orchestrator._run_daily_git_branch_sweep("2026-10-09")

    body = orchestrator._command_handler.execute.call_args.args[1]["body"]
    assert "Failures (1): " in body
    assert body.count("origin has no unique registered repository") == 1
    assert "skipped 2 checkouts of it" in body
    assert str(paths["snapshot"]) not in body
