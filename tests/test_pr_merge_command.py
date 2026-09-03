"""Tests for pr_merge command (dv2 Phase 2, Task 1).

Tests GitManager.amerge_pr and CommandHandler._cmd_pr_merge.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.git.manager import GitError
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# GitManager.amerge_pr unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merge_command_shells_gh_and_returns_success(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[:3] == ["gh", "pr", "merge"]
        assert "--squash" in cmd
        assert "https://github.com/org/repo/pull/42" in cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "Merged\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pr_merge_command_reports_gh_failure(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "not mergeable: conflicts"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is False
    assert "conflicts" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_invalid_method(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    result = await gm.amerge_pr(
        "/some/checkout", "https://github.com/org/repo/pull/42", method="fast-forward"
    )
    assert result["success"] is False
    assert "invalid method" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_parses_sha_from_output(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )
    sha = "a" * 40

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 0
        r.stdout = f"Merged pull request #42 ({sha})\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["sha"] == sha


@pytest.mark.asyncio
async def test_pr_merge_pins_the_validated_head_oid(monkeypatch):
    """The reviewed head is passed to gh, not re-resolved by a mutable PR URL."""
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    head_oid = "b" * 40
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", head_oid)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd == [
            "gh",
            "pr",
            "merge",
            "https://github.com/org/repo/pull/42",
            "--squash",
            "--match-head-commit",
            head_oid,
            "--delete-branch",
        ]
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid=head_oid,
    )
    assert result["success"] is True


def _pr_identity_payload(
    *, base_oid: str = "a" * 40, head_oid: str = "b" * 40, number: int = 42
) -> str:
    """The subset of GitHub's REST ``pulls/{n}`` resource the identity reads."""
    import json

    return json.dumps(
        {
            "number": number,
            "base": {"ref": "main", "sha": base_oid, "repo": {"full_name": "org/repo"}},
            "head": {"ref": "feature/guard", "sha": head_oid, "repo": {"full_name": "org/repo"}},
        }
    )


_PR_IDENTITY_CMD = ["gh", "api", "--hostname", "github.com", "repos/org/repo/pulls/42"]


def _is_identity_call(cmd: list[str]) -> bool:
    """``gh api repos/{owner}/{repo}/pulls/{n}`` — not the paginated files query."""
    return cmd[:2] == ["gh", "api"] and "--paginate" not in cmd


#: ``gh pr view --json`` fields as gh 2.45.0 lists them in its "Unknown JSON
#: field" error — the minimum gh this project supports (``_cmd_pr_merge`` was
#: verified against it).  ``baseRefOid`` (gh >= 2.46) and ``baseRepository``
#: (no version) are deliberately absent.
_GH_2_45_PR_VIEW_FIELDS = frozenset(
    {
        "additions", "assignees", "author", "autoMergeRequest", "baseRefName", "body",
        "changedFiles", "closed", "closedAt", "comments", "commits", "createdAt",
        "deletions", "files", "headRefName", "headRefOid", "headRepository",
        "headRepositoryOwner", "id", "isCrossRepository", "isDraft", "labels",
        "latestReviews", "maintainerCanModify", "mergeCommit", "mergeStateStatus",
        "mergeable", "mergedAt", "mergedBy", "milestone", "number",
        "potentialMergeCommit", "projectCards", "projectItems", "reactionGroups",
        "reviewDecision", "reviewRequests", "reviews", "state", "statusCheckRollup",
        "title", "updatedAt", "url",
    }
)  # fmt: skip


def test_every_gh_pr_view_json_field_exists_on_the_minimum_supported_gh():
    """Regression for the merge path asking gh for fields it does not have.

    ``aget_pr_identity`` used to request ``baseRefOid`` and ``baseRepository``;
    gh 2.45 rejects both with ``Unknown JSON field`` and every ``aq pr merge``
    failed closed.  The suite missed it because the subprocess was faked, so
    this pins every ``gh pr view --json`` literal in the manager to the field
    list the minimum supported gh actually serves.
    """
    import re
    from pathlib import Path

    from src.git import manager

    source = Path(manager.__file__).read_text()
    specs = re.findall(r'"--json",\s*"([A-Za-z0-9,]+)"', source)
    assert specs, "expected at least one gh pr view --json call in the manager"
    for spec in specs:
        unknown = set(spec.split(",")) - _GH_2_45_PR_VIEW_FIELDS
        assert not unknown, f"gh 2.45 has no pr view --json field(s) {sorted(unknown)}"


@pytest.mark.asyncio
async def test_pr_identity_is_read_from_the_rest_pull_resource(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    commands: list[list[str]] = []

    async def fake_arun_subprocess(cmd, cwd, timeout):
        commands.append(cmd)
        return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    identity = await gm.aget_pr_identity(
        "/some/checkout", "https://github.com/org/repo/pull/42#issuecomment-1"
    )

    assert commands == [_PR_IDENTITY_CMD]
    assert identity.repository == "org/repo"
    assert identity.number == 42
    assert (identity.base_ref, identity.head_ref) == ("main", "feature/guard")
    assert (identity.base_oid, identity.head_oid) == ("a" * 40, "b" * 40)


@pytest.mark.asyncio
async def test_pr_identity_uses_the_host_from_the_url(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    commands: list[list[str]] = []

    async def fake_arun_subprocess(cmd, cwd, timeout):
        commands.append(cmd)
        return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    await gm.aget_pr_identity("/some/checkout", "https://ghe.example.com/org/repo/pull/42")

    assert commands == [["gh", "api", "--hostname", "ghe.example.com", "repos/org/repo/pulls/42"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pr_url",
    [
        "https://github.com/org/repo",
        "https://github.com/org/repo/issues/42",
        "https://github.com/org/repo/pull/0",
        "https://github.com/org/pull/42",
        "not a url",
    ],
)
async def test_pr_identity_rejects_urls_that_are_not_a_pull_request(monkeypatch, pr_url):
    from src.git.manager import GitManager

    gm = GitManager()

    async def never(cmd, cwd, timeout):  # pragma: no cover - must not be reached
        raise AssertionError(f"gh must not run for {pr_url!r}: {cmd}")

    monkeypatch.setattr(gm, "_arun_subprocess", never)
    with pytest.raises(GitError, match="pull request URL"):
        await gm.aget_pr_identity("/some/checkout", pr_url)


@pytest.mark.asyncio
async def test_pr_identity_fails_closed_when_the_resource_is_a_different_pr(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def other_pr(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout=_pr_identity_payload(number=41), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", other_pr)
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.aget_pr_identity("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pr_identity_resolves_against_a_real_gh():
    """The faked-subprocess tests cannot catch a gh field gh does not serve.

    Needs a ``gh`` on PATH that is logged in and can reach github.com; skips
    otherwise.  cli/cli#1 is a merged, immutable public PR.
    """
    import asyncio
    import shutil

    from src.git.manager import GitManager

    if shutil.which("gh") is None:
        pytest.skip("gh is not installed")
    auth = await asyncio.create_subprocess_exec(
        "gh", "auth", "status", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    if await auth.wait() != 0:
        pytest.skip("gh is not authenticated")

    identity = await GitManager().aget_pr_identity(os.getcwd(), "https://github.com/cli/cli/pull/1")
    assert identity.repository == "cli/cli"
    assert identity.number == 1
    assert identity.base_ref == "prototype"
    assert identity.head_ref == "gh-pr"
    assert identity.base_oid == "8ebaf1d3aaf3eef03b349d20338c83157b0bcfd7"
    assert identity.head_oid == "e9a3253762e768badaa1d4a5b3d267416d1e42f4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "path"),
    [
        ("added", ".aq/claim.json"),
        ("modified", ".aq-worktree.json"),
        ("deleted", ".codex/config.json"),
    ],
)
async def test_pr_validation_rejects_added_modified_and_deleted_reserved_paths(
    monkeypatch, operation, path
):
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")
        assert cmd[:4] == ["gh", "api", "--paginate", "repos/org/repo/pulls/42/files"]
        return MagicMock(returncode=0, stdout=f"work.py\n{path}\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    # GitHub's PR-files endpoint exposes the filename for each status, and
    # safety must not vary with whether the reserved path was added, modified,
    # or deleted.
    assert operation in {"added", "modified", "deleted"}
    with pytest.raises(GitError, match="reserved daemon bookkeeping"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_for_malformed_or_unavailable_identity(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def malformed(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout='{"headRefOid": "not-an-oid"}', stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", malformed)
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    async def unavailable(cmd, cwd, timeout):
        return MagicMock(returncode=1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(gm, "_arun_subprocess", unavailable)
    with pytest.raises(GitError, match="could not resolve PR identity"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    calls = 0

    async def unavailable_diff(cmd, cwd, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")
        return MagicMock(returncode=1, stdout="", stderr="API unavailable")

    monkeypatch.setattr(gm, "_arun_subprocess", unavailable_diff)
    with pytest.raises(GitError, match="could not inspect PR delivery diff"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
async def test_pr_validation_accepts_clean_paths_and_detects_a_changed_head(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    views = iter([_pr_identity_payload(), _pr_identity_payload()])

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=next(views), stderr="")
        return MagicMock(returncode=0, stdout="work.py\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    views = iter([_pr_identity_payload(), _pr_identity_payload(head_oid="c" * 40)])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    views = iter([_pr_identity_payload(), _pr_identity_payload(base_oid="e" * 40)])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
async def test_pr_merge_refuses_when_head_changes_after_ci_validation(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    merge_called = False

    async def fake_arun_subprocess(cmd, cwd, timeout):
        nonlocal merge_called
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(head_oid="c" * 40), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            merge_called = True
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid="b" * 40,
        expected_base_oid="a" * 40,
    )

    assert result["success"] is False
    assert "identity changed" in result["error"]
    assert merge_called is False


@pytest.mark.asyncio
async def test_direct_manager_merge_validates_and_pins_identity(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    gm.avalidate_pr_for_merge = AsyncMock(return_value=identity)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[-3:] == ["--match-head-commit", "b" * 40, "--delete-branch"]
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")

    assert result["success"] is True
    gm.avalidate_pr_for_merge.assert_awaited_once_with(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )


# ---------------------------------------------------------------------------
# CommandHandler._cmd_pr_merge integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "pm.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    await d.create_workspace(
        Workspace(
            id="w1",
            project_id="p1",
            workspace_path="/tmp/p1",
            source_type=RepoSourceType.CLONE,
        )
    )
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "pm.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


@pytest.mark.asyncio
async def test_cmd_pr_merge_routes_through_git_manager(monkeypatch, handler):
    calls = {}

    head_oid = "c" * 40

    async def fake_amerge(
        checkout_path, pr_url, method="squash", *, expected_head_oid=None, expected_base_oid=None
    ):
        calls["args"] = (checkout_path, pr_url, method)
        calls["head_oid"] = expected_head_oid
        calls["base_oid"] = expected_base_oid
        return {"success": True, "sha": "abc123", "error": None}

    handler.orchestrator.git.avalidate_pr_for_merge = AsyncMock(
        return_value=MagicMock(head_oid=head_oid)
    )
    monkeypatch.setattr(handler.orchestrator.git, "amerge_pr", fake_amerge)
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "p1",
            "pr_url": "https://github.com/o/r/pull/1",
            "method": "squash",
        },
    )
    assert result["success"] is True
    assert result["sha"] == "abc123"
    # gh runs outside any checkout: given a full PR URL it resolves the repo
    # from the URL, so pr_merge no longer borrows the project's base clone
    # (which is routinely the operator's own working tree).
    checkout_path, url, method = calls["args"]
    assert (url, method) == ("https://github.com/o/r/pull/1", "squash")
    assert checkout_path == handler.config.data_dir
    assert checkout_path != "/tmp/p1"
    assert calls["head_oid"] == head_oid


@pytest.mark.asyncio
async def test_cmd_pr_merge_rejects_unknown_project(handler):
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "nope",
            "pr_url": "https://github.com/o/r/pull/1",
        },
    )
    assert result["success"] is False
    assert "project" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_pr_url(handler):
    result = await handler.execute(
        "pr_merge",
        {"project_id": "p1"},
    )
    assert result["success"] is False
    assert "pr_url" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_project_id(handler):
    result = await handler.execute(
        "pr_merge",
        {"pr_url": "https://github.com/o/r/pull/1"},
    )
    assert result["success"] is False
    assert "project_id" in result["error"].lower()
