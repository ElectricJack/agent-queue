"""Tests for pr_merge command (dv2 Phase 2, Task 1).

Tests GitManager.amerge_pr and CommandHandler._cmd_pr_merge.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError
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
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)
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
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)
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
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)
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
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", head_oid, 1)
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
    *,
    base_ref: str = "main",
    base_oid: str = "a" * 40,
    head_oid: str = "b" * 40,
    number: int = 42,
    changed_files: object = 1,
) -> str:
    """The subset of GitHub's REST ``pulls/{n}`` resource the identity reads.

    ``changed_files=None`` omits the count entirely.
    """
    import json

    payload: dict[str, object] = {
        "number": number,
        "base": {"ref": base_ref, "sha": base_oid, "repo": {"full_name": "org/repo"}},
        "head": {"ref": "feature/guard", "sha": head_oid, "repo": {"full_name": "org/repo"}},
    }
    if changed_files is not None:
        payload["changed_files"] = changed_files
    return json.dumps(payload)


_PR_URL = "https://github.com/org/repo/pull/42"
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


# ---------------------------------------------------------------------------
# The pinned-OID delivery diff (gate stark-impact-60.10, M2)
# ---------------------------------------------------------------------------

_HEAD_A = "b" * 40
_HEAD_B = "c" * 40
_BASE = "a" * 40
_MERGE_BASE = "d" * 40
_CACHE = "/some/checkout/pr-diff-cache/github.com/org/repo.git"
_CLONE_URL = "https://github.com/org/repo.git"


def _diff(*paths: str) -> str:
    """What ``git diff-tree --name-only -z`` prints for ``paths``."""
    return "".join(path + "\0" for path in paths)


class _FakeGit:
    """A faked ``git`` for the pinned-OID delivery diff.

    Models a remote whose PR head is ``remote_head`` while the guard runs.
    A fetch that names an OID yields exactly that commit (or fails when the
    OID is not in ``fetchable``), while a fetch that names a ref
    (``refs/pull/<n>/head``) yields whatever the remote head is right now.
    ``diffs`` maps a commit OID to the ``diff-tree`` output of that commit
    against the merge-base, so an implementation that diffs a ref or
    ``FETCH_HEAD`` instead of the pinned OID sees the *remote head's* diff.
    """

    def __init__(
        self,
        diffs: dict[str, str],
        *,
        remote_head: str = _HEAD_A,
        fetchable: set[str] | None = None,
        merge_base: str = _MERGE_BASE,
    ) -> None:
        self.diffs = diffs
        self.remote_head = remote_head
        self.fetchable = fetchable
        self.merge_base = merge_base
        self.calls: list[tuple[list[str], str | None, int | None]] = []
        self.available: set[str] = set()
        self.fetch_head: str | None = None

    @staticmethod
    def subcommand(args: list[str]) -> str:
        it = iter(args)
        for arg in it:
            if arg == "-c":
                next(it, None)
                continue
            if arg.startswith("-"):
                continue
            return arg
        raise AssertionError(f"no git subcommand in {args}")

    def commands(self, sub: str) -> list[list[str]]:
        return [args for args, _, _ in self.calls if self.subcommand(args) == sub]

    def _resolve(self, rev: str) -> str:
        rev = rev.split("^")[0]
        if rev == "FETCH_HEAD":
            assert self.fetch_head is not None, "nothing fetched yet"
            return self.fetch_head
        if rev.startswith("refs/"):
            return self.remote_head
        return rev

    async def __call__(self, args, cwd=None, timeout=None):
        self.calls.append((list(args), cwd, timeout))
        sub = self.subcommand(args)
        if sub == "init":
            return ""
        if sub == "fetch":
            url_at = next(i for i, a in enumerate(args) if a.startswith("https://"))
            for want in args[url_at + 1 :]:
                if want.startswith("refs/"):
                    self.available.add(self.remote_head)
                    self.fetch_head = self.remote_head
                elif self.fetchable is None or want in self.fetchable:
                    self.available.add(want)
                    self.fetch_head = want
                else:
                    raise GitError(
                        f"git fetch failed: remote error: upload-pack: not our ref {want}"
                    )
            return ""
        if sub == "rev-parse":
            oid = self._resolve(args[-1])
            if oid not in self.available:
                raise GitError(f"git rev-parse failed: {args[-1]}")
            return oid
        if sub == "merge-base":
            return self.merge_base
        if sub == "diff-tree":
            return self.diffs.get(self._resolve(args[-1]), "")
        raise AssertionError(f"unexpected git call: {args}")


def _gm_with_git(monkeypatch, git: _FakeGit, *, views: list[str] | None = None):
    """A GitManager whose gh serves ``views`` identity snapshots and whose git is ``git``."""
    from src.git.manager import GitManager

    gm = GitManager()
    snapshots = iter(views if views is not None else [_pr_identity_payload()] * 2)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert _is_identity_call(cmd), f"the guard must not list the PR's files: {cmd}"
        return MagicMock(returncode=0, stdout=next(snapshots), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    monkeypatch.setattr(gm, "_arun", git)
    return gm


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [".aq/claim.json", ".aq-worktree.json", ".codex/config.json"])
async def test_pr_validation_rejects_reserved_paths_in_the_pinned_diff(monkeypatch, reserved):
    # ``--no-renames`` reports a rename as a deletion plus an addition, so a
    # reserved path leaving or entering the tree is listed under its reserved
    # name whichever direction it moved; added, modified and deleted paths
    # are indistinguishable in a name-only diff and equally refused.
    import re

    git = _FakeGit({_HEAD_A: _diff("work.py", reserved)})
    gm = _gm_with_git(monkeypatch, git)
    with pytest.raises(
        GitError, match="reserved daemon bookkeeping.*" + re.escape(reserved)
    ) as excinfo:
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    assert "work.py" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_pr_validation_derives_the_diff_from_the_pinned_oids(monkeypatch):
    """The delivery diff is git's diff from the pinned base/head merge-base to the pinned head.

    Both commits are fetched *by OID* into a daemon-owned, blob-less bare
    cache under the directory ``pr_merge`` runs in — which is not a checkout
    — with gh's credentials.  A content-addressed fetch either yields exactly
    that commit or fails, so nothing addressed by the mutable PR number is
    ever diffed, and neither GitHub's 3000-entry listing cap nor its
    changed-file count is relied on.
    """
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git)

    identity = await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)

    assert identity.head_oid == _HEAD_A
    assert [git.subcommand(args) for args, _, _ in git.calls] == [
        "init", "fetch", "rev-parse", "rev-parse", "merge-base", "diff-tree",
    ]  # fmt: skip
    (init,) = git.commands("init")
    assert init == ["init", "--bare", "--quiet", _CACHE]
    (fetch,) = git.commands("fetch")
    credentials = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    assert fetch[:4] == credentials
    assert fetch[4:] == [
        "fetch", "--quiet", "--no-tags", "--filter=blob:none", _CLONE_URL, _HEAD_A, _BASE,
    ]  # fmt: skip
    assert all(cwd == _CACHE for args, cwd, _ in git.calls if args[0] != "init")
    # Fetch is the one slow step: it gets its own generous window.
    assert next(timeout for args, _, timeout in git.calls if "fetch" in args) >= 600
    assert git.commands("rev-parse") == [
        ["rev-parse", "--verify", "--quiet", f"{_HEAD_A}^{{commit}}"],
        ["rev-parse", "--verify", "--quiet", f"{_BASE}^{{commit}}"],
    ]
    assert git.commands("merge-base") == [["merge-base", _BASE, _HEAD_A]]
    assert git.commands("diff-tree") == [
        ["diff-tree", "-r", "--no-renames", "--name-only", "-z", _MERGE_BASE, _HEAD_A]
    ]


@pytest.mark.asyncio
async def test_pr_validation_refuses_a_head_that_flipped_while_the_diff_was_derived(monkeypatch):
    """Gate stark-impact-60.10 M2: a head force-pushed A -> B -> A must not merge B's diff as A.

    Both identity reads see A and agree on the changed-file count, so the
    pin compares equal and ``--match-head-commit A`` would succeed; only the
    diff itself can tell.  The faked remote's head is B while the diff is
    derived: anything addressed by the PR (``refs/pull/42/head``,
    ``FETCH_HEAD``) resolves to B, whose diff is clean, while A adds a
    reserved path.  The guard must inspect A's diff and refuse.
    """
    git = _FakeGit(
        {_HEAD_A: _diff("work.py", ".aq/claim.json"), _HEAD_B: _diff("work.py")},
        remote_head=_HEAD_B,
    )
    gm = _gm_with_git(monkeypatch, git)
    with pytest.raises(GitError, match=r"reserved daemon bookkeeping.*\.aq/claim\.json"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    (diff,) = git.commands("diff-tree")
    assert diff[-1] == _HEAD_A


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_when_the_pinned_head_cannot_be_fetched(monkeypatch):
    """The flip left A unreachable: fetching it by OID fails, and so must the merge."""
    git = _FakeGit({_HEAD_B: _diff("work.py")}, remote_head=_HEAD_B, fetchable={_HEAD_B, _BASE})
    gm = _gm_with_git(monkeypatch, git)
    with pytest.raises(GitError, match="could not inspect PR delivery diff.*not our ref"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    assert not git.commands("diff-tree")


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_when_a_fetched_commit_is_missing(monkeypatch):
    """A fetch that exits 0 without delivering the pinned commit is not trusted."""

    class _SilentFetch(_FakeGit):
        async def __call__(self, args, cwd=None, timeout=None):
            if self.subcommand(args) == "fetch":
                self.calls.append((list(args), cwd, timeout))
                return ""
            return await super().__call__(args, cwd, timeout)

    git = _SilentFetch({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git)
    with pytest.raises(GitError, match="could not inspect PR delivery diff"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    assert not git.commands("diff-tree")


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_base", ["", "not-an-oid", "d" * 40 + "\n" + "e" * 40])
async def test_pr_validation_fails_closed_without_a_single_merge_base_oid(monkeypatch, merge_base):
    git = _FakeGit({_HEAD_A: _diff("work.py")}, merge_base=merge_base)
    gm = _gm_with_git(monkeypatch, git)
    with pytest.raises(GitError, match="could not inspect PR delivery diff.*merge-base"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    assert not git.commands("diff-tree")


@pytest.mark.asyncio
async def test_pr_validation_caches_per_host_and_repository(monkeypatch):
    """The cache is keyed by the PR URL's host and GitHub's repository name."""
    import json

    payload = json.loads(_pr_identity_payload())
    payload["base"]["repo"]["full_name"] = "Org/Repo.Name"
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git, views=[json.dumps(payload)] * 2)

    await gm.avalidate_pr_for_merge("/data", "https://ghe.example.com/org/repo/pull/42")

    (init,) = git.commands("init")
    assert init[-1] == "/data/pr-diff-cache/ghe.example.com/Org/Repo.Name.git"
    (fetch,) = git.commands("fetch")
    assert "https://ghe.example.com/Org/Repo.Name.git" in fetch


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["../evil", "org/..", "./x", "org/."])
async def test_pr_validation_refuses_a_repository_name_that_escapes_the_cache(
    monkeypatch, repository
):
    import json

    payload = json.loads(_pr_identity_payload())
    payload["base"]["repo"]["full_name"] = repository
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git, views=[json.dumps(payload)] * 2)
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.avalidate_pr_for_merge("/data", _PR_URL)
    assert not git.calls


@pytest.mark.asyncio
async def test_pr_validation_serializes_fetches_into_one_cache(monkeypatch):
    """Two merges of the same repository share one cache and never fetch into it at once."""
    import asyncio

    class _SlowFetch(_FakeGit):
        active = 0
        overlap = False

        async def __call__(self, args, cwd=None, timeout=None):
            if self.subcommand(args) == "fetch":
                _SlowFetch.active += 1
                _SlowFetch.overlap |= _SlowFetch.active > 1
                await asyncio.sleep(0)
                _SlowFetch.active -= 1
            return await super().__call__(args, cwd, timeout)

    git = _SlowFetch({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git, views=[_pr_identity_payload()] * 4)

    await asyncio.gather(
        gm.avalidate_pr_for_merge("/some/checkout", _PR_URL),
        gm.avalidate_pr_for_merge("/some/checkout", _PR_URL),
    )
    assert len(git.commands("fetch")) == 2
    assert _SlowFetch.overlap is False


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_for_malformed_or_unavailable_identity(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    monkeypatch.setattr(gm, "_arun", git)

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
    assert not git.calls


@pytest.mark.asyncio
async def test_pr_validation_accepts_clean_paths_and_detects_a_changed_head(monkeypatch):
    git = _FakeGit({_HEAD_A: _diff("work.py")})

    gm = _gm_with_git(monkeypatch, git)
    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    gm = _gm_with_git(
        monkeypatch, git, views=[_pr_identity_payload(), _pr_identity_payload(head_oid="c" * 40)]
    )
    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    # The base OID moves on every push to the default branch, which says
    # nothing about this PR: the delivery diff is a merge-base diff, so what
    # the PR introduces is unchanged.  Concurrent delivery must not turn
    # validation into a coin toss.
    gm = _gm_with_git(
        monkeypatch, git, views=[_pr_identity_payload(), _pr_identity_payload(base_oid="e" * 40)]
    )
    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    # Retargeting the PR onto another branch is an identity change: the
    # commits would land somewhere the review never looked at.
    gm = _gm_with_git(
        monkeypatch, git, views=[_pr_identity_payload(), _pr_identity_payload(base_ref="develop")]
    )
    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_files", [None, "many", -1, 1.5, True])
async def test_pr_validation_fails_closed_without_a_usable_changed_file_count(
    monkeypatch, changed_files
):
    # ``None`` omits the field entirely; the others are not a non-negative int.
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(monkeypatch, git, views=[_pr_identity_payload(changed_files=changed_files)])
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.asyncio
async def test_pr_identity_pins_the_changed_file_count_in_the_same_snapshot(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    commands: list[list[str]] = []

    async def fake_arun_subprocess(cmd, cwd, timeout):
        commands.append(cmd)
        return MagicMock(returncode=0, stdout=_pr_identity_payload(changed_files=7), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    identity = await gm.aget_pr_identity("/some/checkout", _PR_URL)

    assert identity.changed_files == 7
    # One REST ``pulls/{n}`` read carries the OIDs and the count together, so
    # the count belongs to the same base/head pair and a count that differs
    # between the two snapshots means the head that was diffed is not the
    # head that would be merged.
    assert commands == [_PR_IDENTITY_CMD]


@pytest.mark.asyncio
async def test_pr_identity_reads_only_the_rest_changed_files_spelling(monkeypatch):
    # The identity comes from the REST resource, whose field is
    # ``changed_files``.  The GraphQL spelling behind ``gh pr view --json``
    # (``changedFiles``) is not a count the REST snapshot produces, so a
    # payload carrying only that is an incomplete identity.
    import json

    from src.git.manager import GitManager

    gm = GitManager()
    payload = json.loads(_pr_identity_payload(changed_files=None))
    payload["changedFiles"] = 5

    async def fake_arun_subprocess(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.aget_pr_identity("/some/checkout", _PR_URL)


@pytest.mark.asyncio
async def test_pr_validation_detects_a_changed_file_count_between_snapshots(monkeypatch):
    git = _FakeGit({_HEAD_A: _diff("work.py")})
    gm = _gm_with_git(
        monkeypatch,
        git,
        views=[_pr_identity_payload(changed_files=1), _pr_identity_payload(changed_files=2)],
    )
    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pr_validation_derives_the_diff_against_a_real_repository(tmp_path):
    """The faked git cannot prove GitHub serves a fetch by OID with gh's credentials.

    Needs a ``gh`` on PATH that is logged in and can reach github.com; skips
    otherwise.  cli/cli#1 is a merged, immutable public PR whose one changed
    file is ``command/pr.go``.
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

    gm = GitManager()
    identity = await gm.avalidate_pr_for_merge(str(tmp_path), "https://github.com/cli/cli/pull/1")
    assert identity.head_oid == "e9a3253762e768badaa1d4a5b3d267416d1e42f4"
    cache = tmp_path / "pr-diff-cache" / "github.com" / "cli" / "cli.git"
    assert (cache / "HEAD").is_file()
    paths = await gm._arun(
        [
            "diff-tree", "-r", "--no-renames", "--name-only", "-z",
            identity.base_oid, identity.head_oid,
        ],
        cwd=str(cache),
    )  # fmt: skip
    assert paths.split("\0") == ["command/pr.go", ""]


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
    monkeypatch.setattr(gm, "_arun", _FakeGit({_HEAD_B: _diff("work.py")}))
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid="b" * 40,
        expected_base_ref="main",
    )

    assert result["success"] is False
    assert "identity changed" in result["error"]
    assert "head" in result["error"]
    assert merge_called is False


@pytest.mark.asyncio
async def test_pr_merge_proceeds_when_only_the_base_moved_after_ci_validation(monkeypatch):
    """Exit gate stark-impact-60.6 M4: base-branch movement is not a PR change.

    With several agents delivering concurrently the default branch advances
    between the CI check and the merge on most attempts.  The PR-files diff
    inspected during validation is a merge-base diff and ``gh pr merge``
    merges into the *current* base tip anyway, so refusing here only ever
    produced a spurious "identity changed" the final-reviewer could not act
    on.  The head OID stays pinned all the way into ``--match-head-commit``.
    """
    from src.git.manager import GitManager

    gm = GitManager()
    merge_cmd: list[str] | None = None

    async def fake_arun_subprocess(cmd, cwd, timeout):
        nonlocal merge_cmd
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(base_oid="e" * 40), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            merge_cmd = cmd
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    monkeypatch.setattr(gm, "_arun", _FakeGit({_HEAD_A: _diff("work.py")}))
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid="b" * 40,
        expected_base_ref="main",
    )

    assert result["success"] is True, result
    assert merge_cmd is not None
    assert merge_cmd[-3:] == ["--match-head-commit", "b" * 40, "--delete-branch"]


@pytest.mark.asyncio
async def test_pr_merge_refuses_when_pr_is_retargeted_after_ci_validation(monkeypatch):
    """Dropping the base OID from the pin must not drop retarget detection.

    The base OID used to catch this only by accident (another branch has
    another tip).  The base *branch name* is what the review and the
    landed-on-default-branch check actually depend on, so it is pinned
    explicitly.
    """
    from src.git.manager import GitManager

    gm = GitManager()
    merge_called = False

    async def fake_arun_subprocess(cmd, cwd, timeout):
        nonlocal merge_called
        if _is_identity_call(cmd):
            return MagicMock(
                returncode=0, stdout=_pr_identity_payload(base_ref="develop"), stderr=""
            )
        if cmd[:3] == ["gh", "pr", "merge"]:
            merge_called = True
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    monkeypatch.setattr(gm, "_arun", _FakeGit({_HEAD_A: _diff("work.py")}))
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid="b" * 40,
        expected_base_ref="main",
    )

    assert result["success"] is False
    assert "identity changed" in result["error"]
    assert "main" in result["error"] and "develop" in result["error"]
    assert merge_called is False


@pytest.mark.asyncio
async def test_direct_manager_merge_validates_and_pins_identity(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)
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
        checkout_path, pr_url, method="squash", *, expected_head_oid=None, expected_base_ref=None
    ):
        calls["args"] = (checkout_path, pr_url, method)
        calls["head_oid"] = expected_head_oid
        calls["base_ref"] = expected_base_ref
        return {"success": True, "sha": "abc123", "error": None}

    handler.orchestrator.git.avalidate_pr_for_merge = AsyncMock(
        return_value=MagicMock(head_oid=head_oid, base_ref="main")
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
    # The base *branch* is pinned (a retargeted PR lands elsewhere); the base
    # OID is not, because it moves with every concurrent delivery.
    assert calls["base_ref"] == "main"


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
