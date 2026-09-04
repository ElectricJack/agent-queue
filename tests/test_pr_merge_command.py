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
async def test_pr_validation_uses_the_url_host_for_files_listing(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    commands: list[list[str]] = []

    async def fake_arun_subprocess(cmd, cwd, timeout):
        commands.append(cmd)
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")
        return MagicMock(
            returncode=0,
            stdout=_pr_files_stdout([{"filename": "work.py"}]),
            stderr="",
        )

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://ghe.example.com/org/repo/pull/42"
    )

    assert commands == [
        ["gh", "api", "--hostname", "ghe.example.com", "repos/org/repo/pulls/42"],
        [
            "gh",
            "api",
            "--hostname",
            "ghe.example.com",
            "--paginate",
            "repos/org/repo/pulls/42/files",
            "--jq",
            _PR_FILES_JQ,
        ],
        ["gh", "api", "--hostname", "ghe.example.com", "repos/org/repo/pulls/42"],
    ]


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


# The exact jq program handed to ``gh api``: one JSON object per PR-files
# entry carrying the destination ``filename`` and the rename or copy source
# ``previous_filename`` (``null`` for an ordinary change).
_PR_FILES_JQ = ".[] | {filename, previous_filename}"


def _pr_files_stdout(files: list[dict]) -> str:
    """Emulate what ``gh api --jq _PR_FILES_JQ`` prints for a PR-files payload.

    gh prints each non-scalar jq result as compact JSON on its own line.
    """
    import json

    return "".join(
        json.dumps({"filename": e["filename"], "previous_filename": e.get("previous_filename")})
        + "\n"
        for e in files
    )


def _clean_paths(count: int) -> str:
    """``count`` distinct non-reserved entries, as gh prints them."""
    return _pr_files_stdout([{"filename": f".a/{i:05d}"} for i in range(count)])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "entry", "reserved"),
    [
        ("added", {"filename": ".aq/claim.json", "status": "added"}, ".aq/claim.json"),
        ("modified", {"filename": ".aq-worktree.json", "status": "modified"}, ".aq-worktree.json"),
        ("deleted", {"filename": ".codex/config.json", "status": "deleted"}, ".codex/config.json"),
        (
            "renamed out of a reserved path",
            {"filename": "moved.json", "status": "renamed", "previous_filename": ".aq/claim.json"},
            ".aq/claim.json",
        ),
        (
            "renamed onto a reserved path",
            {"filename": ".codex/config.json", "status": "renamed", "previous_filename": "c.json"},
            ".codex/config.json",
        ),
        (
            "copied out of a reserved path",
            {"filename": "copy.json", "status": "copied", "previous_filename": ".aq/claim.json"},
            ".aq/claim.json",
        ),
    ],
)
async def test_pr_validation_rejects_added_modified_and_deleted_reserved_paths(
    monkeypatch, operation, entry, reserved
):
    from src.git.manager import GitManager

    gm = GitManager()
    files = [{"filename": "work.py", "status": "modified"}, entry]

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(changed_files=2), stderr="")
        assert cmd == [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--paginate",
            "repos/org/repo/pulls/42/files",
            "--jq",
            _PR_FILES_JQ,
        ]
        # GitHub reports a rename or copy under its new ``filename`` and keeps
        # the old name in ``previous_filename``; the guard must ask for both.
        assert cmd[cmd.index("--jq") + 1] == _PR_FILES_JQ
        return MagicMock(returncode=0, stdout=_pr_files_stdout(files), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    # Safety must not vary with whether the reserved path was added, modified,
    # deleted, or moved across the reserved boundary in either direction.
    with pytest.raises(GitError, match=f"reserved daemon bookkeeping.*{reserved}") as excinfo:
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")
    assert operation
    assert "work.py" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_pr_changed_files_include_rename_sources(monkeypatch):
    """``_apr_changed_files`` keeps one entry per file and every previous_filename."""
    from src.git.manager import GitManager, PullRequestFile, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 3)
    files = [
        {"filename": "work.py", "status": "modified"},
        {"filename": "moved.json", "status": "renamed", "previous_filename": ".aq/claim.json"},
        {"filename": "new.py", "status": "added"},
    ]

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[cmd.index("--jq") + 1] == _PR_FILES_JQ
        return MagicMock(returncode=0, stdout=_pr_files_stdout(files), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    entries = await gm._apr_changed_files("/some/checkout", identity)
    assert entries == [
        PullRequestFile("work.py"),
        PullRequestFile("moved.json", ".aq/claim.json"),
        PullRequestFile("new.py"),
    ]
    assert [path for e in entries for path in e.paths] == [
        "work.py",
        "moved.json",
        ".aq/claim.json",
        "new.py",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout",
    [
        "work.py\n",  # a bare path: the pre-entry listing format
        '{"filename": "work.py", "previous_filename": null}\n{"filename": ""}\n',
        '{"filename": "work.py", "previous_filename": ""}\n',
        '{"previous_filename": "old.py"}\n',
        '["work.py", null]\n',
        '{"filename": "work.py"',
    ],
)
async def test_pr_changed_files_fail_closed_on_an_unreadable_entry(monkeypatch, stdout):
    """An entry the guard cannot decode is an entry it cannot clear."""
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    with pytest.raises(GitError, match="could not inspect PR delivery diff"):
        await gm._apr_changed_files("/some/checkout", identity)


@pytest.mark.asyncio
async def test_pr_changed_files_accept_pretty_printed_entries(monkeypatch):
    """gh indents jq output on a TTY; the parser reads concatenated JSON either way."""
    from src.git.manager import GitManager, PullRequestFile, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 2)
    stdout = (
        '{\n  "filename": "work.py",\n  "previous_filename": null\n}\n'
        '{\n  "filename": "b.py",\n  "previous_filename": "a.py"\n}\n'
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    entries = await gm._apr_changed_files("/some/checkout", identity)
    assert entries == [PullRequestFile("work.py"), PullRequestFile("b.py", "a.py")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "reserved"),
    [
        ({"filename": "new.py", "status": "renamed", "previous_filename": "old.py"}, None),
        (
            {"filename": "moved.json", "status": "renamed", "previous_filename": ".aq/claim.json"},
            ".aq/claim.json",
        ),
    ],
)
async def test_pr_validation_counts_a_renamed_entry_as_one_changed_file(
    monkeypatch, entry, reserved
):
    """A rename is one entry of the PR-files listing carrying two names.

    GitHub's ``changed_files`` counts listing entries, while the reserved-path
    guard must inspect both the destination and the source of a rename. The
    completeness check therefore compares entries, not names: a PR whose only
    change is a rename is complete at one entry, and is then accepted or
    refused on its paths alone — never reported as an incomplete listing.
    """
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(changed_files=1), stderr="")
        assert cmd[cmd.index("--jq") + 1] == _PR_FILES_JQ
        return MagicMock(returncode=0, stdout=_pr_files_stdout([entry]), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    if reserved is None:
        identity = await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
        assert identity.changed_files == 1
    else:
        with pytest.raises(GitError, match=f"reserved daemon bookkeeping.*{reserved}"):
            await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


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
        return MagicMock(
            returncode=0, stdout=_pr_files_stdout([{"filename": "work.py"}]), stderr=""
        )

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    views = iter([_pr_identity_payload(), _pr_identity_payload(head_oid="c" * 40)])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    # The base OID moves on every push to the default branch, which says
    # nothing about this PR: the PR-files diff is a merge-base diff, so what
    # the PR introduces is unchanged.  Concurrent delivery must not turn
    # validation into a coin toss.
    views = iter([_pr_identity_payload(), _pr_identity_payload(base_oid="e" * 40)])

    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    # Retargeting the PR onto another branch is an identity change: the
    # commits would land somewhere the review never looked at.
    views = iter([_pr_identity_payload(), _pr_identity_payload(base_ref="develop")])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


def _gm_with_pr(monkeypatch, *, changed_files: object, listed: str, views: int = 2):
    """A GitManager whose gh reports ``changed_files`` and lists ``listed``."""
    from src.git.manager import GitManager

    gm = GitManager()
    snapshots = iter([_pr_identity_payload(changed_files=changed_files)] * views)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=next(snapshots), stderr="")
        assert cmd == [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--paginate",
            "repos/org/repo/pulls/42/files",
            "--jq",
            _PR_FILES_JQ,
        ]
        return MagicMock(returncode=0, stdout=listed, stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    return gm


_PR_FILES_API_CAP = 3000


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_when_the_files_listing_stops_at_the_api_cap(
    monkeypatch,
):
    # GitHub's "List pull request files" returns at most 3000 entries, so a PR
    # with 3000 clean paths sorting before ``.aq/`` hides ``.aq/claim.json`` in
    # the unlisted tail.  The listing is exactly the cap while the PR reports
    # one more file: the guard must refuse rather than trust the visible prefix.
    gm = _gm_with_pr(
        monkeypatch,
        changed_files=_PR_FILES_API_CAP + 1,
        listed=_clean_paths(_PR_FILES_API_CAP),
    )
    with pytest.raises(GitError, match="3000"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_exactly_at_the_api_cap(monkeypatch):
    # At the cap the listing and the count agree, yet nothing proves the
    # listing was not truncated at precisely that boundary: fail closed.
    gm = _gm_with_pr(
        monkeypatch,
        changed_files=_PR_FILES_API_CAP,
        listed=_clean_paths(_PR_FILES_API_CAP),
    )
    with pytest.raises(GitError, match="3000"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.asyncio
async def test_pr_validation_accepts_a_complete_listing_just_below_the_api_cap(monkeypatch):
    gm = _gm_with_pr(
        monkeypatch,
        changed_files=_PR_FILES_API_CAP - 1,
        listed=_clean_paths(_PR_FILES_API_CAP - 1),
    )
    identity = await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)
    assert identity.changed_files == _PR_FILES_API_CAP - 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("changed_files", "listed"), [(3, 2), (1, 2), (2, 0)])
async def test_pr_validation_fails_closed_when_listing_and_changed_file_count_disagree(
    monkeypatch, changed_files, listed
):
    # A listing that does not match the PR's own changed-file count cannot be
    # a complete merge-base diff, whichever side is larger.
    gm = _gm_with_pr(monkeypatch, changed_files=changed_files, listed=_clean_paths(listed))
    expected = rf"incomplete.*\b{listed}\b.*\b{changed_files}\b"
    with pytest.raises(GitError, match=expected):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_files", [None, "many", -1, 1.5, True])
async def test_pr_validation_fails_closed_without_a_usable_changed_file_count(
    monkeypatch, changed_files
):
    # ``None`` omits the field entirely; the others are not a non-negative int.
    gm = _gm_with_pr(monkeypatch, changed_files=changed_files, listed=_clean_paths(1))
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
    # the count belongs to the same base/head pair the listing is checked
    # against.
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
    from src.git.manager import GitManager

    gm = GitManager()
    views = iter([_pr_identity_payload(changed_files=1), _pr_identity_payload(changed_files=2)])

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=next(views), stderr="")
        return MagicMock(returncode=0, stdout=_clean_paths(1), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", _PR_URL)


@pytest.mark.asyncio
async def test_pr_merge_refuses_when_head_changes_after_ci_validation(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    merge_called = False

    async def fake_arun_subprocess(cmd, cwd, timeout):
        nonlocal merge_called
        if _is_identity_call(cmd):
            return MagicMock(returncode=0, stdout=_pr_identity_payload(head_oid="c" * 40), stderr="")
        if "--paginate" in cmd:
            return MagicMock(returncode=0, stdout=_clean_paths(1), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            merge_called = True
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
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
        return MagicMock(
            returncode=0, stdout=_pr_files_stdout([{"filename": "work.py"}]), stderr=""
        )

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
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
        return MagicMock(
            returncode=0, stdout=_pr_files_stdout([{"filename": "work.py"}]), stderr=""
        )

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
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
