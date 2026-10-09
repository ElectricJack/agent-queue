"""The daily branch sweep deletes only refs with exact Git proof."""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.git.manager import GitManager
from src.integration.branch_sweep import sweep_checkout
from src.orchestrator import Orchestrator


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
    orchestrator._emit_text_notify = AsyncMock()

    await orchestrator._run_daily_git_branch_sweep("2026-10-08")

    db.list_workspaces.assert_awaited_once_with(project_id=project.id)
    orchestrator._emit_text_notify.assert_awaited_once()
