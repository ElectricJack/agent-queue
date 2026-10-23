"""A separately filed repair's passing head fast-forwards its root PR's branch.

Real Git repositories and the retained-store fetch path; GitHub is a double.
The case this control exists for: a repair task filed for an open root PR
completes on its own branch, and the PR branch it repairs belongs to nobody
who may push it (``aq integration fast-forward-pr``).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import events, projects, repos, task_completion_records, tasks
from src.git.manager import GitManager
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.pr_repair_publication import (
    PR_REPAIR_PUBLISHED_EVENT,
    PRRepairPublication,
)
from src.integration.repository_git import RepositoryGit
from src.models import Project, RepoConfig, RepoSourceType

PR = 41
PR_URL = f"https://github.com/o/r/pull/{PR}"


def _git(*args: str, cwd=None, committer=("Agent Queue", "agent-queue@localhost")) -> str:
    # The fallback project identity, so the repair's commits pass the publish policy;
    # explicit because a worker session's own GIT_* identity overrides repository config.
    name, email = committer
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class _GitHub:
    def __init__(self) -> None:
        self.pulls: dict[int, dict] = {}

    def open(self, number: int, branch: str, sha: str, *, repo_id: int = 7):
        self.pulls[number] = {
            "number": number,
            "state": "open",
            "merged_at": None,
            "head": {"ref": branch, "sha": sha, "repo": {"id": repo_id}},
            "base": {"ref": "main", "repo": {"id": 7}},
        }

    async def pull_request(self, pr_url: str) -> dict:
        assert pr_url.startswith("https://github.com/o/r/pull/")
        return copy.deepcopy(self.pulls[int(pr_url.rsplit("/", 1)[1])])


@pytest.fixture
async def env(reuse_database, tmp_path, monkeypatch):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("clone", str(remote), str(work))
    (work / "shared.txt").write_text("base\n")
    _git("add", "shared.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("push", "origin", "main", cwd=work)

    db = await reuse_database("integration-pr-repair-publication")
    await db.create_project(Project(id="p", name="PR repair"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    )
    await db.update_project(
        "p", integration_repository_id="repo", hierarchical_integration_mode="train"
    )
    github = _GitHub()
    git = GitManager()

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: github)
    promotion = RepositoryGit(db, data_dir=tmp_path / "data", git_manager=git)
    yield SimpleNamespace(
        db=db, work=work, remote=remote, github=github, promotion=promotion,
        control=PRRepairPublication(db, promotion, None),
    )


def _commit(env, path: str, text: str) -> str:
    (env.work / path).write_text(text)
    _git("add", path, cwd=env.work)
    _git("commit", "-m", path, cwd=env.work)
    return _git("rev-parse", "HEAD", cwd=env.work)


async def _task(env, task_id: str, branch: str, *, status="COMPLETED", pr_url=None, **values):
    now = time.time()
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(tasks).values(
            id=task_id, project_id="p", repo_id="repo", title=task_id, description="",
            status=status, branch_name=branch, pr_url=pr_url, created_at=now, updated_at=now,
            **values,
        ))


async def _completion(env, task_id: str, branch: str, head: str, *, outcome="pass"):
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(task_completion_records).values(
            id=f"{task_id}-completion", task_id=task_id, outcome=outcome, branch=branch,
            commits=json.dumps([head]), completed_at=time.time(),
        ))


async def _scenario(env, *, repair_start="aq/root"):
    """Root PR ``aq/root`` (completed, open) and repair ``aq/repair`` (passing, pushed)."""
    _git("switch", "-c", "aq/root", "main", cwd=env.work)
    old = _commit(env, "root.txt", "root\n")
    _git("switch", "-c", "aq/repair", repair_start, cwd=env.work)
    tip = _commit(env, "repair.txt", "repair\n")
    _git("push", "origin", "aq/root", "aq/repair", cwd=env.work)
    await _task(env, "root", "aq/root", pr_url=PR_URL)
    await _task(env, "repair", "aq/repair")
    await _completion(env, "repair", "aq/repair", tip)
    env.github.open(PR, "aq/root", old)
    return old, tip


def _remote_head(env, branch: str) -> str:
    return _git("rev-parse", f"refs/heads/{branch}", cwd=env.remote)


async def _apply(env, old: str, **overrides):
    args = {"dry_run": False, "expected_head_sha": old, "reason": "adopt repair",
            "operator_id": "human:local-operator", **overrides}
    return await env.control.run("repair", PR, **args)


async def _published_events(env) -> list[dict]:
    async with env.db._engine.connect() as conn:
        rows = (await conn.execute(
            select(events.c.payload).where(events.c.event_type == PR_REPAIR_PUBLISHED_EVENT)
        )).scalars().all()
    return [json.loads(row) for row in rows]


async def test_dry_run_reports_heads_without_pushing(env):
    old, tip = await _scenario(env)

    result = await env.control.run("repair", PR)

    assert result["outcome"] == "would_publish"
    assert (result["previous_oid"], result["published_oid"]) == (old, tip)
    assert (result["target_branch"], result["root_task_id"]) == ("aq/root", "root")
    assert _remote_head(env, "aq/root") == old
    assert await _published_events(env) == []


async def test_apply_fast_forwards_the_pr_branch_and_audits(env):
    old, tip = await _scenario(env)

    result = await _apply(env, old)

    assert result["outcome"] == "published", result["reason"]
    assert _remote_head(env, "aq/root") == tip
    [event] = await _published_events(env)
    assert event["published_oid"] == tip and event["previous_oid"] == old
    assert event["completion_id"] == "repair-completion"
    assert (event["operator_id"], event["reason"]) == ("human:local-operator", "adopt repair")
    # The lease is released: a worker can take the branch again.
    await BranchLock(env.db).acquire(BranchKey(repository_id="repo", branch="aq/root"), "worker")


async def test_a_pr_already_at_the_repair_head_reports_already_published(env):
    old, tip = await _scenario(env)
    await _apply(env, old)
    env.github.open(PR, "aq/root", tip)

    assert (await env.control.run("repair", PR))["outcome"] == "already_published"
    assert (await _apply(env, tip))["outcome"] == "already_published"


@pytest.mark.parametrize("target", ["main", "dev", "staging"])
async def test_protected_targets_refuse(env, target):
    _, tip = await _scenario(env)
    env.github.open(PR, target, tip)

    result = await _apply(env, tip)

    assert result["outcome"] == "not_eligible"
    assert "promotion branches" in result["reason"]
    assert _remote_head(env, "main") != tip


@pytest.mark.parametrize("protect", [
    (projects, {"promotion_flow": [{"id": "release", "source": "main", "target": "aq/root"}]}),
    (repos, {"default_branch": "aq/root"}),
])
async def test_promotion_and_repository_default_targets_refuse(env, protect):
    old, _ = await _scenario(env)
    table, values = protect
    async with env.db._engine.begin() as conn:
        await conn.execute(update(table).values(**values))

    result = await _apply(env, old)

    assert result["outcome"] == "not_eligible"
    assert "promotion branches" in result["reason"]
    assert _remote_head(env, "aq/root") == old


async def test_the_repairs_own_branch_refuses(env):
    _, tip = await _scenario(env)
    env.github.open(PR, "aq/repair", tip)

    result = await env.control.run("repair", PR)

    assert result["outcome"] == "not_eligible"
    assert "own branch" in result["reason"]


@pytest.mark.parametrize("pull", [
    {"state": "closed"},
    {"merged_at": "2026-10-01T00:00:00Z"},
    {"head": {"ref": "aq/root", "sha": "f" * 40, "repo": {"id": 8}}},
])
async def test_a_closed_merged_or_cross_repository_pr_refuses(env, pull):
    await _scenario(env)
    env.github.pulls[PR].update(pull)

    result = await env.control.run("repair", PR)

    assert result["outcome"] == "not_eligible"
    assert "must be open" in result["reason"]


@pytest.mark.parametrize("change", [
    {"status": "IN_PROGRESS"},
    {"outcome": "fail"},
    {"branch": "aq/elsewhere"},
])
async def test_a_repair_without_an_exact_passing_completion_refuses(env, change):
    await _scenario(env)
    async with env.db._engine.begin() as conn:
        if "status" in change:
            await conn.execute(update(tasks).where(tasks.c.id == "repair").values(**change))
        else:
            await conn.execute(update(task_completion_records).values(**change))

    result = await env.control.run("repair", PR)

    assert result["outcome"] == "not_eligible"
    assert "passing completion" in result["reason"]


async def test_a_pr_no_completed_root_tracks_refuses(env):
    await _scenario(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "root").values(parent_task_id="repair"))

    result = await env.control.run("repair", PR)

    assert result["outcome"] == "not_eligible"
    assert "no completed root" in result["reason"]


@pytest.mark.parametrize("writer", [
    {"pr_url": PR_URL, "branch": "aq/root-child"},
    {"pr_url": None, "branch": "aq/root"},
    {"pr_url": None, "branch": "aq/repair"},
])
async def test_live_writers_on_the_pr_or_repair_block(env, writer):
    old, _ = await _scenario(env)
    await _task(env, "writer", writer["branch"], status="IN_PROGRESS", pr_url=writer["pr_url"])

    result = await _apply(env, old)

    assert result["outcome"] == "blocked"
    assert "writer" in result["reason"]
    assert _remote_head(env, "aq/root") == old


async def test_a_repair_that_does_not_descend_from_the_pr_head_refuses(env):
    old, _ = await _scenario(env, repair_start="main")

    result = await _apply(env, old)

    assert result["outcome"] == "not_eligible"
    assert "does not descend" in result["reason"]
    assert _remote_head(env, "aq/root") == old


@pytest.mark.parametrize("branch", ["aq/root", "aq/repair"])
async def test_remote_movement_refuses(env, branch):
    old, _ = await _scenario(env)
    _git("switch", branch, cwd=env.work)
    moved = _commit(env, "moved.txt", "moved\n")
    _git("push", "origin", branch, cwd=env.work)

    result = await _apply(env, old)

    assert result["outcome"] == "changed"
    assert moved in result["reason"]
    assert _remote_head(env, branch) == moved


async def test_a_pr_head_other_than_the_dry_runs_refuses(env):
    old, tip = await _scenario(env)

    result = await _apply(env, tip)

    assert result["outcome"] == "changed"
    assert _remote_head(env, "aq/root") == old


async def test_an_identity_change_before_the_lock_refuses(env, monkeypatch):
    from src.integration import pr_repair_publication

    old, _ = await _scenario(env)
    policy = pr_repair_publication.task_publish_policy

    async def renamed_meanwhile(*args, **kwargs):
        await env.db.update_task("repair", branch_name="aq/renamed")
        return await policy(*args, **kwargs)

    monkeypatch.setattr(pr_repair_publication, "task_publish_policy", renamed_meanwhile)

    result = await _apply(env, old)

    assert result["outcome"] == "changed"
    assert "identity changed" in result["reason"]
    assert _remote_head(env, "aq/root") == old


async def test_a_foreign_branch_lease_blocks(env):
    old, _ = await _scenario(env)
    await BranchLock(env.db).acquire(
        BranchKey(repository_id="repo", branch="aq/root"), "someone-else")

    result = await _apply(env, old)

    assert result["outcome"] == "blocked"
    assert result["reason"].startswith("BranchBusy")
    assert _remote_head(env, "aq/root") == old


async def test_a_foreign_committer_is_refused_by_the_publish_identity(env):
    old, _ = await _scenario(env)
    _git("switch", "aq/repair", cwd=env.work)
    _git("commit", "--allow-empty", "-m", "foreign", cwd=env.work,
         committer=("Someone", "someone@example.test"))
    tip = _git("rev-parse", "HEAD", cwd=env.work)
    _git("push", "origin", "aq/repair", cwd=env.work)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(task_completion_records).values(commits=json.dumps([tip])))

    result = await _apply(env, old)

    assert result["outcome"] == "blocked"
    assert "Git identity" in result["reason"]
    assert _remote_head(env, "aq/root") == old


async def test_a_project_outside_train_mode_refuses(env):
    await _scenario(env)
    await env.db.update_project("p", hierarchical_integration_mode="disabled")

    assert (await env.control.run("repair", PR))["outcome"] == "not_eligible"


async def test_command_authorizes_validates_and_runs(env):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context

    old, tip = await _scenario(env)

    class Handler(IntegrationCommandsMixin):
        db = env.db
        config = None
        orchestrator = None

        def _integration_repository_git(self):
            return env.promotion

    handler = Handler()
    invalid = await handler._cmd_integration_fast_forward_pr(
        {"task_id": "repair", "pr_number": PR, "dry_run": False})
    assert (invalid["success"], invalid["outcome"]) == (False, "invalid")
    with principal_context(ExecutionPrincipal.service("unrelated")):
        refused = await handler._cmd_integration_fast_forward_pr(
            {"task_id": "repair", "pr_number": PR})
    assert refused["outcome"] == "unauthorized"
    missing = await handler._cmd_integration_fast_forward_pr({"task_id": "nope", "pr_number": PR})
    assert (missing["success"], missing["outcome"]) == (False, "not_found")

    preview = await handler._cmd_integration_fast_forward_pr({"task_id": "repair", "pr_number": PR})
    assert (preview["success"], preview["outcome"], preview["dry_run"]) == (
        True, "would_publish", True)
    applied = await handler._cmd_integration_fast_forward_pr({
        "task_id": "repair", "pr_number": PR, "dry_run": False,
        "expected_head_sha": old, "reason": "adopt repair",
    })
    assert (applied["success"], applied["outcome"]) == (True, "published")
    assert _remote_head(env, "aq/root") == tip
    [event] = await _published_events(env)
    assert event["operator_id"] == "human:local-operator"
