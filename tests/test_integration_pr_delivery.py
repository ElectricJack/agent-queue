"""An open PR closes only once Git proves its work is on the default branch.

Real Git repositories and the retained-store fetch path; GitHub is a
recording double.  The legacy cases this control exists for: an untracked
operator PR re-delivered as a patch-identical commit that main later edited
(#687), work that reached main under a merge, and PRs nothing delivered.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import agents, events, sessions, tasks, workspaces
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, GitManager
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
            "html_url": f"https://github.com/o/r/pull/{number}",
            "created_at": "2026-10-01T00:00:00Z",
        }

    async def paged_list(self, path, *, max_pages):
        assert path.startswith("/repositories/7/pulls?state=open")
        assert max_pages == 100
        return copy.deepcopy(list(self.pulls.values()))

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
    # The bare remote on disk stands in for the GitHub repository.
    monkeypatch.setattr("src.integration.pr_cleanup.lacks_pull_request_host", lambda _url: False)
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


@pytest.mark.parametrize("status", ["READY", "IN_PROGRESS", "PAUSED", "BLOCKED", "WAITING_INPUT"])
@pytest.mark.parametrize("tracking", ["pr_url", "branch"])
async def test_a_live_task_keeps_its_pr_open_even_when_its_head_is_already_on_main(
    env, status, tracking,
):
    _branch(env, "aq/live")
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "aq/live")
    env.github.open(14, "aq/live", head)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="live", project_id="p", repo_id="repo", title="Live", description="",
            status=status, created_at=1.0, updated_at=1.0,
            pr_url="https://github.com/o/r/pull/14" if tracking == "pr_url" else None,
            branch_name="aq/live" if tracking == "branch" else None,
        ))
    for dry_run in (True, False):
        held = await env.control.run(
            "p", 14, dry_run=dry_run, expected_head_sha=head, reason="already equivalent",
        )
        assert held["outcome"] == "not_eligible"
        assert held["task_ids"] == held["live_task_ids"] == ["live"]
    assert env.github.closed == [] and env.github.comments == {}
    assert await _events(env) == []


@pytest.mark.parametrize("attachment", ["session", "workspace", "assigned_agent"])
async def test_a_completed_task_with_an_attached_writer_keeps_its_pr_open(env, attachment):
    _branch(env, "aq/attached")
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "aq/attached")
    env.github.open(15, "aq/attached", head)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="attached", project_id="p", repo_id="repo", title="Attached", description="",
            status="COMPLETED", branch_name="aq/attached", created_at=1.0, updated_at=1.0,
        ))
        if attachment == "session":
            await conn.execute(insert(sessions).values(
                id="writer", task_id="attached", project_id="p", profile_id="worker",
                harness="codex", provider="fake", name="writer", lifecycle="pool",
                state="running", desired_state="running", work_dir=str(env.work),
                epoch="e", instance_token="writer-token", started_at=1.0,
            ))
        elif attachment == "workspace":
            await conn.execute(insert(workspaces).values(
                id="writer", project_id="p", workspace_path=str(env.work),
                locked_by_task_id="attached", created_at=1.0,
            ))
        else:
            await conn.execute(insert(agents).values(
                id="writer", name="writer", profile_id="worker", created_at=1.0,
            ))
            await conn.execute(update(tasks).where(tasks.c.id == "attached").values(
                assigned_agent_id="writer",
            ))
    held = await env.control.run("p", 15)
    assert held["outcome"] == "not_eligible" and held["live_task_ids"] == ["attached"]
    assert env.github.closed == [] and env.github.comments == {}


async def test_task_reopening_during_apply_invalidates_delivery_proof_before_any_write(
    env, monkeypatch,
):
    _branch(env, "aq/reopened")
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "aq/reopened")
    env.github.open(16, "aq/reopened", head)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="reopened", project_id="p", repo_id="repo", title="Reopened", description="",
            status="COMPLETED", branch_name="refs/heads/aq/reopened",
            created_at=1.0, updated_at=1.0,
        ))
    assert (await env.control.run("p", 16))["outcome"] == "would_close"
    observe = env.control._observe

    async def reopen_after_observation(*args):
        observed, client = await observe(*args)
        assert observed["outcome"] == "would_close"
        async with env.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "reopened").values(
                status="READY", updated_at=2.0,
            ))
        return observed, client

    monkeypatch.setattr(env.control, "_observe", reopen_after_observation)
    result = await env.control.run(
        "p", 16, dry_run=False, expected_head_sha=head, reason="stale cleanup preview",
    )
    assert result["outcome"] == "not_eligible" and result["live_task_ids"] == ["reopened"]
    assert env.github.closed == [] and env.github.comments == {}
    assert await _events(env) == []


async def test_a_live_branch_in_another_repository_does_not_hold_this_pr(env):
    _branch(env, "aq/shared-name")
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "aq/shared-name")
    env.github.open(17, "aq/shared-name", head)
    await env.db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/o/other",
    ))
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="other-task", project_id="p", repo_id="other", title="Other", description="",
            status="IN_PROGRESS", branch_name="aq/shared-name", created_at=1.0, updated_at=1.0,
        ))
    dry = await env.control.run("p", 17)
    assert dry["outcome"] == "would_close" and dry["task_ids"] == []


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


async def test_periodic_sweep_uses_real_delivery_proof_and_retries_failed_close(env, monkeypatch):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.integration.pr_cleanup import PullRequestReconciler
    from src.git.manager import GitError

    _branch(env, "aq/delivered")
    head = _commit(env, "done.txt", "done\n", "delivered")
    _push(env, "aq/delivered")
    _git("switch", "main", cwd=env.work)
    _commit(env, "main.txt", "main\n", "diverge")
    _git("cherry-pick", head, cwd=env.work)
    _push(env, "main")
    env.github.open(21, "aq/delivered", head)
    _branch(env, "aq/undelivered")
    missing = _commit(env, "missing.txt", "missing\n", "pending")
    _push(env, "aq/undelivered")
    env.github.open(22, "aq/undelivered", missing)
    env.github.open(23, "human/branch", head)

    class Handler(IntegrationCommandsMixin):
        db = env.db

        def _integration_repository_git(self):
            return env.control.promotion

        async def execute(self, command, args):
            assert command == "integration_close_delivered_pr"
            return await self._cmd_integration_close_delivered_pr(args)

    close = env.github.close_pull_request
    failures = [True]

    async def flaky_close(*, number):
        if failures:
            failures.pop()
            raise GitError("GitHub temporarily unavailable")
        await close(number=number)

    monkeypatch.setattr(env.github, "close_pull_request", flaky_close)
    sweep = PullRequestReconciler(env.db, env.control.git, commands=Handler, page_size=1)
    await sweep.tick(1000)
    assert env.github.closed == []
    await sweep.tick(1001)
    assert env.github.closed == [] and not sweep.pending
    await sweep.tick(1200)  # inventory interval has not elapsed
    assert env.github.closed == []
    await sweep.tick(1301)
    await sweep.tick(1302)
    assert env.github.closed == [21]
    assert len(env.github.comments[21]) == 1
    assert env.github.pulls[22]["state"] == "open"
    assert env.github.pulls[23]["state"] == "open"
    [event] = await _events(env)
    assert event["payload"]["operator_id"] == "integration-pr-cleanup"
    assert event["payload"]["proof"]["kind"] == "patch_equivalent"


async def test_settled_pr_closure_is_idempotent_and_rejects_a_moved_head(env):
    from src.integration.pr_cleanup import SettledTaskPullRequestClosure

    head = _git("rev-parse", "HEAD", cwd=env.work)
    env.github.open(31, "aq/obsolete", head)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="obsolete", project_id="p", repo_id="repo", title="obsolete", description="",
            status="COMPLETED", branch_name="aq/obsolete",
            pr_url="https://github.com/o/r/pull/31", created_at=1.0, updated_at=1.0,
        ))
    service = SettledTaskPullRequestClosure(env.db, env.control.git)
    env.github.moved_before_close[31] = "e" * 40
    refused = await service.run("obsolete", reason="superseded by PR #32", principal="operator")
    assert refused["outcome"] == "blocked" and env.github.closed == []
    env.github.moved_before_close.clear()
    closed = await service.run("obsolete", reason="superseded by PR #32", principal="operator")
    replay = await service.run("obsolete", reason="superseded by PR #32", principal="operator")
    assert closed["outcome"] == "closed" and replay["outcome"] == "nothing_to_close"
    assert env.github.closed == [31] and len(env.github.comments[31]) == 1
    assert "superseded by PR #32" in env.github.comments[31][0]


async def test_periodic_sweep_visits_inventory_beyond_one_hundred_prs(env):
    from src.integration.pr_cleanup import PullRequestReconciler

    for number in range(1, 151):
        env.github.open(number, f"aq/task-{number}", "b" * 40)
    visited = []

    class Handler:
        async def execute(self, command, args):
            assert command == "integration_close_delivered_pr" and "dry_run" not in args
            visited.append(args["pr_number"])
            if args["pr_number"] == 1:
                raise RuntimeError("one PR is unreadable")
            return {"outcome": "undelivered"}

    sweep = PullRequestReconciler(env.db, env.control.git, commands=Handler, page_size=20)
    await sweep.tick(1000)
    assert len(visited) == 20
    for now in range(1001, 1008):
        await sweep.tick(now)
    assert visited == list(range(1, 151)) and not sweep.pending
    assert env.github.closed == [] and env.github.comments == {}


async def test_periodic_service_authority_is_named_and_limited_to_aq_prs(env):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context
    from src.integration.pr_cleanup import SWEEP_PRINCIPAL

    _branch(env, "human/delivered")
    head = _git("rev-parse", "HEAD", cwd=env.work)
    _push(env, "human/delivered")
    env.github.open(151, "human/delivered", head)

    class Handler(IntegrationCommandsMixin):
        db = env.db

        def _integration_repository_git(self):
            return env.control.promotion

    handler = Handler()
    with principal_context(ExecutionPrincipal.service("unrelated")):
        refused = await handler._cmd_integration_close_delivered_pr({"project_id": "p",
                                                                     "pr_number": 151})
    assert refused["outcome"] == "unauthorized"
    with principal_context(ExecutionPrincipal.service(SWEEP_PRINCIPAL)):
        refused = await handler._cmd_integration_close_delivered_pr({"project_id": "p",
                                                                     "pr_number": 151})
    assert refused["outcome"] == "not_eligible"
    assert env.github.closed == [] and env.github.comments == {}


async def test_periodic_sweep_times_out_one_pr_and_continues_to_the_next(env):
    import asyncio
    from src.integration.pr_cleanup import PullRequestReconciler

    env.github.open(161, "aq/stuck", "b" * 40)
    env.github.open(162, "aq/next", "b" * 40)
    visited = []
    stopped = []

    class Handler:
        async def execute(self, command, args):
            visited.append(args["pr_number"])
            if args["pr_number"] == 161:
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.append(161)
            return {"outcome": "undelivered"}

    sweep = PullRequestReconciler(env.db, env.control.git, commands=Handler,
                                 item_timeout_seconds=0.05)
    await sweep.tick(1000)
    assert visited == [161, 162] and stopped == [161]
    assert not sweep.pending and env.github.closed == []


async def test_adoption_cleanup_retains_failure_and_refuses_a_new_settlement(env, monkeypatch):
    from src.integration.pr_cleanup import queue_settled_task_prs_on, retry_settled_task_prs
    from src.database.tables import task_metadata
    from src.git.manager import GitError

    env.github.open(32, "aq/adopted", "c" * 40)
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="adopted", project_id="p", repo_id="repo", title="adopted", description="",
            status="COMPLETED", branch_name="aq/adopted",
            pr_url="https://github.com/o/r/pull/32", created_at=1.0, updated_at=1.0,
        ))
        await queue_settled_task_prs_on(env.db, conn, ["adopted"], reason="adopted at main",
                                        principal="operator")
    close = env.github.close_pull_request

    async def unavailable(*, number):
        raise GitError("unavailable")

    monkeypatch.setattr(env.github, "close_pull_request", unavailable)
    assert (await retry_settled_task_prs(env.db, env.control.git))[0]["outcome"] == "blocked"
    assert (await env.db.get_task("adopted")).status.value == "COMPLETED"
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == "adopted"))
    monkeypatch.setattr(env.github, "close_pull_request", close)
    async with env.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "adopted").values(updated_at=2.0))
    refused = await retry_settled_task_prs(env.db, env.control.git)
    assert refused[0]["outcome"] == "blocked" and env.github.closed == []
    async with env.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "adopted").values(updated_at=1.0))
    assert (await retry_settled_task_prs(env.db, env.control.git))[0]["outcome"] == "closed"
    assert len(env.github.comments[32]) == 1
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == "adopted")) is None


async def test_old_orphan_prs_exclude_live_tasks_reservations_and_batch_members(env):
    from src.integration.pr_cleanup import orphaned_pull_requests
    from src.database.tables import (
        integration_batch_members, integration_batches, integration_branch_owners,
        integration_review_evidence, integration_repair_operations, integration_parent_episodes,
    )

    for number, branch in [(41, "aq/orphan"), (42, "aq/task"), (43, "aq/owner"),
                           (44, "aq/recent"), (45, "human/old"), (46, "aq/fork"),
                           (47, "aq/batch"), (48, "aq/member"), (49, "aq/active-parent")]:
        env.github.open(number, branch, "b" * 40, repo_id=99 if number == 46 else 7)
    env.github.pulls[44]["created_at"] = "2026-10-03T12:00:00Z"
    async with env.db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="live", project_id="p", repo_id="repo", title="live", description="",
            status="PAUSED", branch_name="aq/task", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="refs/heads/aq/owner", owner_id="live",
            owner_role="worker", handoff_state="reserved", fence_token=1,
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_batches).values(
            id="active-batch", project_id="p", repository_id="repo", request_id="request",
            source_manifest_digest="sha256:" + "a" * 64, base_sha="a" * 40,
            lifecycle="sealing", integration_branch="refs/heads/aq/batch",
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_review_evidence).values(
            id="review", source_task_id="member", repository_id="repo", source_base="a" * 40,
            reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40, reviewer_task_id="reviewer",
            review_kind="leaf", generation=1, verdict="approved", evidence={}, created_at=1.0,
        ))
        await conn.execute(insert(integration_batch_members).values(
            batch_id="active-batch", ordinal=0, task_id="member", repository_id="repo",
            source_base_sha="a" * 40, reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40,
            source_ref="refs/heads/aq/member", source_ref_retention="retain",
            review_evidence_id="review", review_evidence={},
        ))
        await conn.execute(update(integration_batches).values(lifecycle="human_blocked"))
        await conn.execute(insert(tasks).values(
            id="parent", project_id="p", repo_id="repo", title="parent", description="",
            status="COMPLETED", branch_name="aq/active-parent", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_parent_episodes).values(
            id="episode", parent_task_id="parent", repository_id="repo", generation=1,
            pre_collection_checkpoint_sha="a" * 40, created_at=1.0,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id="parent-operation", target_kind="parent", parent_task_id="parent",
            episode_id="episode", state="human_required", policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks-v1", created_at=1.0, updated_at=1.0,
        ))
    from datetime import datetime

    now = datetime.fromisoformat("2026-10-03T13:00:00+00:00").timestamp()
    findings = await orphaned_pull_requests(env.db, env.control.git, "p", now=now)
    assert [item["pr_number"] for item in findings] == [41]
    assert findings[0]["age_seconds"] > 24 * 3600
    assert env.github.closed == [] and env.github.comments == {}


async def test_inventory_skips_a_repository_that_cannot_host_a_pull_request():
    """fresh-quest-25: a bare remote on disk has no PRs to inventory, so no bind is tried."""
    from unittest.mock import AsyncMock

    from src.integration.pr_cleanup import open_aq_pull_requests

    repo = SimpleNamespace(
        id="repo", project_id="p", url="/home/u/.agent-queue/local-remotes/web.git",
    )
    db = SimpleNamespace(
        get_project=AsyncMock(return_value=SimpleNamespace(integration_repository_id="repo")),
        get_repo=AsyncMock(return_value=repo),
    )
    git = SimpleNamespace(bind_github_repository=AsyncMock(side_effect=AssertionError("bound")))
    assert await open_aq_pull_requests(db, git, "p") == []
    git.bind_github_repository.assert_not_awaited()


async def test_periodic_sweep_logs_a_classified_inventory_failure_on_one_line(
    env, monkeypatch, caplog,
):
    from src.integration import pr_cleanup

    async def refused(*_args):
        try:
            raise GitHubAccessError("transient", "GitHub request failed (transient, HTTP 502)",
                                    http_status=502)
        except GitHubAccessError as cause:
            raise GitError(str(cause)) from cause

    monkeypatch.setattr(pr_cleanup, "open_aq_pull_requests", refused)
    caplog.set_level(logging.INFO, logger="src.integration.pr_cleanup")
    sweep = pr_cleanup.PullRequestReconciler(env.db, env.control.git, commands=object)
    await sweep.tick(1000)

    [record] = [r for r in caplog.records if "inventory" in r.getMessage()]
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert record.getMessage() == (
        "Could not inventory PRs for p: GitError: GitHub request failed (transient, HTTP 502) "
        "[code=transient, http=502]"
    )
