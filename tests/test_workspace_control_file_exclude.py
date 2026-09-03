"""Daemon control files must never be committed into the project repo.

``task_claim`` writes ``<work_dir>/.aq/claim.json`` and the worktree machinery
writes ``<work_dir>/.aq-worktree.json``; the exclusive-clone prep path writes
``<work_dir>/.agent-queue-lock``.  All three are untracked files inside the
git workspace.  With ``worktrees.enabled: false`` the managed
``.git/info/exclude`` block used to be written only on worktree-slot
provisioning, so an exclusive clone reported them in ``git status`` and the
verify phase's auto-remediation committed them onto the task branch — and
from there, in direct mode, onto the project's default branch.

Observed after a Tier 1 e2e run: the sample repo's ``main`` tracked
``.aq/claim.json`` under ``auto-commit: uncommitted changes from task ...``.
The same commit defeated the empty-branch guards, arming the close-time PR
gate for tasks that produced no code.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import EXCLUDE_BEGIN, EXCLUDE_END


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit_file(repo: str, filename: str, content: str, message: str) -> str:
    path = os.path.join(repo, filename)
    with open(path, "w") as f:
        f.write(content)
    _git(["add", filename], cwd=repo)
    _git(
        ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message],
        cwd=repo,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def git_repo(tmp_path):
    """A bare remote with one commit on ``main`` and a clone of it."""
    remote = str(tmp_path / "remote.git")
    clone = str(tmp_path / "clone")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", remote],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "clone", remote, clone], check=True, capture_output=True)
    _commit_file(clone, "README.md", "seed\n", "seed")
    _git(["push", "origin", "main"], cwd=clone)
    return {"remote": remote, "clone": clone}


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    # The defect is specific to the exclusive-clone path.
    config.worktrees.enabled = False
    o = Orchestrator(config)
    await o.initialize()
    yield o
    await o.shutdown()


async def _prepare(orch: Orchestrator, git_repo, *, source_type: RepoSourceType) -> str:
    db = orch.db
    await db.create_project(
        Project(
            id="p-1",
            name="alpha",
            repo_url=git_repo["remote"],
            repo_default_branch="main",
        )
    )
    await db.create_workspace(
        Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path=git_repo["clone"],
            source_type=source_type,
        )
    )
    agent = Agent(id="a-1", name="agent-1", profile_id="claude")
    await db.create_agent(agent)
    task = Task(
        id="t-1",
        project_id="p-1",
        title="Task One",
        description="first",
        status=TaskStatus.IN_PROGRESS,
        assigned_agent_id="a-1",
        integration_mode="direct",
    )
    await db.create_task(task)
    workspace = await orch._prepare_workspace(task, agent)
    assert workspace == git_repo["clone"]
    return workspace


def _write_control_files(workspace: str) -> None:
    os.makedirs(os.path.join(workspace, ".aq"), exist_ok=True)
    with open(os.path.join(workspace, ".aq", "claim.json"), "w") as f:
        json.dump({"task_id": "t-1", "epoch": 1}, f)
    with open(os.path.join(workspace, ".aq-worktree.json"), "w") as f:
        json.dump({"slot": "n/a"}, f)


class TestExclusiveCloneExcludesControlFiles:
    @pytest.mark.parametrize("source_type", [RepoSourceType.CLONE, RepoSourceType.LINK])
    async def test_prep_writes_managed_exclude_block(self, orch, git_repo, source_type):
        """Every git workspace gets the managed block, not only worktree slots."""
        workspace = await _prepare(orch, git_repo, source_type=source_type)

        exclude = Path(workspace) / ".git" / "info" / "exclude"
        text = exclude.read_text(encoding="utf-8", errors="surrogateescape")
        assert EXCLUDE_BEGIN in text and EXCLUDE_END in text
        assert "/.aq/" in text
        assert "/.aq-worktree.json" in text
        assert "/.agent-queue-lock" in text

        _write_control_files(workspace)
        # The prep path itself left ``.agent-queue-lock`` behind; together with
        # the claim file and the worktree sentinel, none of it may be visible.
        assert os.path.exists(os.path.join(workspace, ".agent-queue-lock"))
        assert _git(["status", "--porcelain"], cwd=workspace) == ""

    async def test_auto_remediation_leaves_no_commit_behind(self, orch, git_repo):
        """The verify-phase fallback must not commit the daemon's own files.

        Regression for the observed e2e symptom: after auto-remediation the
        branch has no commits ahead of its base and the tree is clean.
        """
        workspace = await _prepare(orch, git_repo, source_type=RepoSourceType.CLONE)
        base_sha = _git(["rev-parse", "origin/main"], cwd=workspace)
        _write_control_files(workspace)

        still_dirty = await orch._auto_remediate_uncommitted(
            workspace, "t-1", "main", project_id="p-1", agent_id="a-1"
        )

        assert still_dirty is False
        assert _git(["status", "--porcelain"], cwd=workspace) == ""
        assert _git(["rev-parse", "HEAD"], cwd=workspace) == base_sha
        assert _git(["rev-list", "--count", "origin/main..HEAD"], cwd=workspace) == "0"
        assert ".aq/claim.json" not in _git(["ls-files"], cwd=workspace)
        # The control files are still there for the daemon — excluded, not deleted.
        assert os.path.exists(os.path.join(workspace, ".aq", "claim.json"))

    async def test_separate_git_dir_writes_the_exact_exclude_path(self, orch, tmp_path):
        """A `.git` file must not make the helper append another `.git` directory."""
        workspace = tmp_path / "workspace"
        git_dir = tmp_path / "git-metadata"
        subprocess.run(
            [
                "git",
                "init",
                "--initial-branch=main",
                f"--separate-git-dir={git_dir}",
                str(workspace),
            ],
            check=True,
            capture_output=True,
        )

        await orch._ensure_control_files_excluded(str(workspace))

        exact_exclude = git_dir / "info" / "exclude"
        assert EXCLUDE_BEGIN in exact_exclude.read_text(encoding="utf-8")
        assert not (git_dir / ".git" / "info" / "exclude").exists()
