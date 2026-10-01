"""An open PR closes only once Git proves its work is on the default branch.

Real Git repositories and the retained-store fetch path; GitHub is a
recording double.  The legacy cases this control exists for: an untracked
operator PR re-delivered as a patch-identical commit that main later edited
(#687), work that reached main under a merge, and PRs nothing delivered.
"""

from __future__ import annotations

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.database.tables import events, tasks
from src.git.manager import GitManager
from src.integration.pr_delivery import (
    PR_CLOSED_DELIVERED_EVENT,
    DeliveredPullRequestClosure,
)
from src.integration.promotion import PromotionService
from src.models import Project, RepoConfig, RepoSourceType


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class _GitHub:
    """Pull requests by number; records comments and closures."""

    def __init__(self) -> None:
        self.pulls: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}
        self.closed: list[int] = []
        self.moved_before_close: dict[int, str] = {}

    def open(self, number: int, branch: str, sha: str, *, base: str = "main", repo_id: int = 7):
        self.pulls[number] = {
            "number": number,
            "state": "open",
            "merged_at": None,
            "head": {"ref": branch, "sha": sha, "repo": {"id": repo_id}},
            "base": {"ref": base, "repo": {"id": 7}},
        }

    async def pull_request(self, pr_url: str) -> dict:
        assert pr_url.startswith("https://github.com/o/r/pull/")
        return copy.deepcopy(self.pulls[int(pr_url.rsplit("/", 1)[1])])

    async def has_comment_marker(self, *, number: int, marker: str) -> bool:
        return any(marker in body for body in self.comments.get(number, []))

    async def comment_pull_request(self, *, number: int, marker: str, body: str) -> None:
        assert marker in body
        self.comments.setdefault(number, []).append(body)

    async def exact_pull_request(self, *, number: int) -> dict:
        pull = self.pulls[number]
        return {
            "repository_numeric_id": 7,
            "repository_full_name": "o/r",
            "head_sha": self.moved_before_close.get(number, pull["head"]["sha"]),
            "state": pull["state"],
        }

    async def close_pull_request(self, *, number: int) -> None:
        self.pulls[number]["state"] = "closed"
        self.closed.append(number)


@pytest.fixture
async def env(reuse_database, tmp_path, monkeypatch):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("clone", str(remote), str(work))
    _git("config", "user.name", "PR Delivery Test", cwd=work)
    _git("config", "user.email", "pr-delivery@example.test", cwd=work)
    (work / "shared.txt").write_text("one\ntwo\nthree\n")
    _git("add", "shared.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("push", "origin", "main", cwd=work)

    db = await reuse_database("integration-pr-delivery")
    await db.create_project(Project(id="p", name="PR delivery"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    )
    await db.update_project("p", integration_repository_id="repo")
    github = _GitHub()
    git = GitManager()

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: github)
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=git)
    clock = iter(float(n) for n in range(1000, 2000))
    yield SimpleNamespace(
        db=db, work=work, github=github,
        control=DeliveredPullRequestClosure(db, promotion, clock=lambda: next(clock)),
    )


def _commit(env, path: str, text: str, message: str) -> str:
    (env.work / path).write_text(text)
    _git("add", path, cwd=env.work)
    _git("commit", "-m", message, cwd=env.work)
    return _git("rev-parse", "HEAD", cwd=env.work)


def _branch(env, name: str, start: str = "main") -> None:
    _git("switch", "-c", name, start, cwd=env.work)


def _push(env, *refs: str) -> None:
    _git("push", "origin", *refs, cwd=env.work)


async def _events(env) -> list[dict]:
    async with env.db._engine.connect() as conn:
        rows = (await conn.execute(
            select(events).where(events.c.event_type == PR_CLOSED_DELIVERED_EVENT)
        )).mappings().all()
    return [{**row, "payload": json.loads(row["payload"])} for row in rows]


async def test_a_head_already_on_main_is_proven_by_ancestry_and_closed_once(env):
    _branch(env, "aq/feature")
    head = _commit(env, "feature.txt", "feature\n", "feature")
    _push(env, "aq/feature")
    _git("switch", "main", cwd=env.work)
    _git("merge", "--no-ff", "-m", "carrier merge", head, cwd=env.work)
    _push(env, "main")
    env.github.open(11, "aq/feature", head)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", title="Legacy", description="", status="COMPLETED",
            pr_url="https://github.com/o/r/pull/11", created_at=1.0, updated_at=1.0,
        ))

    dry = await env.control.run("p", 11)
    assert dry["outcome"] == "would_close"
    assert dry["proof"] == {"kind": "ancestor"}
    assert (dry["head_sha"], dry["branch"], dry["task_ids"]) == (head, "aq/feature", ["legacy"])
    assert dry["target_sha"] == _git("rev-parse", "main", cwd=env.work)
    assert env.github.comments == {} and env.github.closed == []

    closed = await env.control.run(
        "p", 11, dry_run=False, expected_head_sha=head, reason="carrier delivered", operator_id="op",
    )
    assert closed["outcome"] == "closed"
    assert env.github.closed == [11]
    [comment] = env.github.comments[11]
    assert f"<!-- aq-delivered-pr:11:{head} -->" in comment and "(ancestor)" in comment
    assert "carrier delivered" not in comment
    [event] = await _events(env)
    assert event["task_id"] == "legacy" and event["project_id"] == "p"
    assert event["payload"]["proof"] == {"kind": "ancestor"}
    assert (event["payload"]["operator_id"], event["payload"]["reason"]) == (
        "op", "carrier delivered",
    )
    assert (await env.control.run("p", 11))["outcome"] == "nothing_to_close"
    async with env.db._engine.connect() as conn:
        assert (await conn.execute(select(tasks.c.status).where(tasks.c.id == "legacy"))
                ).scalar_one() == "COMPLETED"


async def test_a_cherry_picked_series_main_later_edited_is_patch_equivalent(env):
    _branch(env, "fix/operator")
    first = _commit(env, "shared.txt", "one\nTWO\nthree\n", "operator fix")
    second = _commit(env, "extra.txt", "extra\n", "operator follow-up")
    _push(env, "fix/operator")
    _git("switch", "main", cwd=env.work)
    _commit(env, "main.txt", "main\n", "main moves first")
    _git("cherry-pick", first, second, cwd=env.work)
    picked = _git("rev-list", "--reverse", "HEAD~2..HEAD", cwd=env.work).split()
    # Main moves on over the same hunk: merging the PR head would now conflict.
    _commit(env, "shared.txt", "one\nTWO (refined)\nthree\n", "main refines the fix")
    _push(env, "main")
    env.github.open(12, "fix/operator", second)

    dry = await env.control.run("p", 12)
    assert dry["outcome"] == "would_close"
    assert dry["proof"]["kind"] == "patch_equivalent"
    assert dry["proof"]["commits"] == [first, second]
    assert sorted(dry["proof"]["equivalent_commits"]) == sorted(picked)
    assert dry["task_ids"] == []
    closed = await env.control.run(
        "p", 12, dry_run=False, expected_head_sha=second, reason="operator PR re-delivered",
    )
    assert closed["outcome"] == "closed"
    [comment] = env.github.comments[12]
    assert all(sha in comment for sha in picked)
    [event] = await _events(env)
    assert event["task_id"] is None


async def test_work_merged_through_other_commits_is_content_equivalent(env):
    _branch(env, "aq/merged")
    change = _commit(env, "merged.txt", "work\n", "work")
    _git("merge", "--no-ff", "-m", "refresh from main", "main", cwd=env.work)
    _git("switch", "main", cwd=env.work)
    _commit(env, "other.txt", "other\n", "unrelated main work")
    _git("cherry-pick", change, cwd=env.work)
    _git("switch", "aq/merged", cwd=env.work)
    _git("merge", "--no-ff", "-m", "refresh again", "main", cwd=env.work)
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "main", "aq/merged")
    env.github.open(13, "aq/merged", head)

    dry = await env.control.run("p", 13)
    assert dry["outcome"] == "would_close"
    assert dry["proof"]["kind"] == "content_equivalent"
    assert dry["proof"]["commits"][0] == change


async def test_undelivered_work_stays_open_and_says_what_is_missing(env):
    _branch(env, "aq/partial")
    picked = _commit(env, "picked.txt", "picked\n", "delivered half")
    head = _commit(env, "missing.txt", "missing\n", "undelivered half")
    _push(env, "aq/partial")
    _git("switch", "main", cwd=env.work)
    _commit(env, "main.txt", "main\n", "main moves first")
    _git("cherry-pick", picked, cwd=env.work)
    _push(env, "main")
    env.github.open(14, "aq/partial", head)

    dry = await env.control.run("p", 14)
    assert dry["outcome"] == "undelivered"
    assert dry["undelivered"]["conflict"] is False
    assert dry["undelivered"]["files"] == ["missing.txt"]
    assert dry["undelivered"]["commits"] == [picked, head]
    applied = await env.control.run(
        "p", 14, dry_run=False, expected_head_sha=head, reason="not delivered",
    )
    assert applied["outcome"] == "undelivered"
    assert env.github.closed == [] and env.github.comments == {}
    assert await _events(env) == []


async def test_a_conflicting_undelivered_pr_reports_the_conflict(env):
    _branch(env, "aq/conflict")
    head = _commit(env, "shared.txt", "one\nbranch\nthree\n", "branch edit")
    _push(env, "aq/conflict")
    _git("switch", "main", cwd=env.work)
    _commit(env, "shared.txt", "one\nmain\nthree\n", "main edit")
    _push(env, "main")
    env.github.open(15, "aq/conflict", head)

    dry = await env.control.run("p", 15)
    assert dry["outcome"] == "undelivered"
    assert dry["undelivered"]["conflict"] is True


async def test_a_moved_or_mismatched_head_is_never_closed(env):
    _branch(env, "aq/feature")
    head = _commit(env, "feature.txt", "feature\n", "feature")
    _push(env, "aq/feature")
    _git("switch", "main", cwd=env.work)
    _git("merge", "--ff-only", head, cwd=env.work)
    _push(env, "main")
    env.github.open(16, "aq/feature", head)

    stale = await env.control.run(
        "p", 16, dry_run=False, expected_head_sha="f" * 40, reason="stale dry run",
    )
    assert stale["outcome"] == "changed"
    env.github.moved_before_close[16] = "e" * 40
    raced = await env.control.run(
        "p", 16, dry_run=False, expected_head_sha=head, reason="raced push",
    )
    assert raced["outcome"] == "changed"
    assert env.github.closed == []
    assert await _events(env) == []
    # The PR names a head the remote branch no longer has.
    _git("switch", "aq/feature", cwd=env.work)
    _commit(env, "feature.txt", "feature v2\n", "pushed after the PR read")
    _push(env, "aq/feature")
    assert (await env.control.run("p", 16))["outcome"] == "changed"


@pytest.mark.parametrize(
    ("base", "repo_id", "state", "merged_at", "outcome"),
    [
        ("release", 7, "open", None, "not_eligible"),
        ("main", 99, "open", None, "not_eligible"),
        ("main", 7, "closed", None, "nothing_to_close"),
        ("main", 7, "closed", "2026-10-01T00:00:00Z", "nothing_to_close"),
    ],
)
async def test_only_open_prs_of_the_designated_repository_to_main_qualify(
    env, base, repo_id, state, merged_at, outcome,
):
    _branch(env, "aq/feature")
    head = _commit(env, "feature.txt", "feature\n", "feature")
    _push(env, "aq/feature")
    env.github.open(17, "aq/feature", head, base=base, repo_id=repo_id)
    env.github.pulls[17].update(state=state, merged_at=merged_at)
    result = await env.control.run("p", 17)
    assert result["outcome"] == outcome
    if merged_at:
        assert result["state"] == "merged"
    assert env.github.closed == []


async def test_a_project_without_a_designated_repository_is_not_eligible(env):
    await env.db.create_project(Project(id="bare", name="No repository"))
    assert (await env.control.run("bare", 1))["outcome"] == "not_eligible"
    assert (await env.control.run("missing", 1))["outcome"] == "not_found"
